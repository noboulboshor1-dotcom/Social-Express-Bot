"""CSV review workflow that exports IDs/metadata only, never credentials.

Export header: submission_id,user_id,category,status,created_at
Decision upload header: submission_id,decision,reason
Allowed decisions are accepted/rejected. Rewards are set by a server-side
category allowlist, not by the uploaded CSV.
"""
from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message

from config import ADMIN_IDS
from database import db

logger = logging.getLogger(__name__)
review_csv_router = Router(name="review_csv")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
SAFE_CATEGORY_REWARDS = {
    "website_feedback": Decimal("2.00"),
    "content_review": Decimal("3.00"),
    "data_labeling": Decimal("1.50"),
    "qa_testing": Decimal("4.00"),
}


class ReviewCSVStates(StatesGroup):
    waiting_for_decision_csv = State()


@review_csv_router.message(Command("export_pending_reviews"))
async def export_pending_reviews(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["submission_id", "user_id", "category", "status", "created_at"])
        start, batch_size = 0, 500
        exported = 0
        while True:
            res = await db.client.table("user_submissions").select(
                "id,user_id,category,status,created_at"
            ).eq("status", "pending").order("id").range(start, start + batch_size - 1).execute()
            rows = res.data or []
            for row in rows:
                # Formula injection mitigation for spreadsheet viewers.
                category = str(row.get("category") or "")
                if category.startswith(("=", "+", "-", "@")):
                    category = "'" + category
                writer.writerow([
                    row.get("id"), row.get("user_id"), category,
                    row.get("status"), row.get("created_at") or "",
                ])
                exported += 1
            if len(rows) < batch_size:
                break
            start += batch_size
        data = output.getvalue().encode("utf-8-sig")
        if exported == 0:
            await message.answer("এখন কোনো pending review নেই।")
            return
        await message.answer_document(
            BufferedInputFile(data, filename="pending_reviews.csv"),
            caption=f"Pending reviews: {exported}. সিদ্ধান্তের CSV-তে শুধু submission_id,decision,reason রাখুন।",
        )
    except Exception:
        logger.exception("Pending-review CSV export failed")
        await message.answer("⚠️ CSV export করা যায়নি।")


@review_csv_router.message(Command("import_reviews"))
async def begin_review_import(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(ReviewCSVStates.waiting_for_decision_csv)
    await message.answer(
        "সিদ্ধান্তের CSV ফাইল পাঠান। Header অবশ্যই হবে:\n"
        "<code>submission_id,decision,reason</code>\n"
        "decision: <code>accepted</code> অথবা <code>rejected</code>।"
    )


@review_csv_router.message(ReviewCSVStates.waiting_for_decision_csv, F.document)
async def process_review_csv(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    document = message.document
    if not document or not document.file_name or not document.file_name.lower().endswith(".csv"):
        await message.answer("শুধু .csv ফাইল গ্রহণ করা হয়।")
        return
    if (document.file_size or 0) > MAX_UPLOAD_BYTES:
        await message.answer("CSV ফাইল সর্বোচ্চ 2 MiB হতে পারবে।")
        return

    buffer = io.BytesIO()
    try:
        await message.bot.download(document, destination=buffer)
        buffer.seek(0)
        text = buffer.read(MAX_UPLOAD_BYTES + 1).decode("utf-8-sig")
        if len(text.encode("utf-8")) > MAX_UPLOAD_BYTES:
            await message.answer("CSV ফাইল সর্বোচ্চ 2 MiB হতে পারবে।")
            return
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames != ["submission_id", "decision", "reason"]:
            await message.answer("⚠️ CSV header ঠিক নয়। প্রত্যাশিত: submission_id,decision,reason")
            return
        rows = []
        for row in reader:
            if None in row or any(row.get(field) is None for field in ("submission_id", "decision", "reason")):
                await message.answer("⚠️ CSV row-তে অতিরিক্ত বা অনুপস্থিত column আছে।")
                return
            rows.append(row)
            if len(rows) > MAX_ROWS:
                await message.answer(f"CSV-তে সর্বোচ্চ {MAX_ROWS}টি data row থাকতে পারবে।")
                return
        if not rows:
            await message.answer(f"CSV-তে 1 থেকে {MAX_ROWS}টি row থাকতে হবে।")
            return

        await state.clear()
        done = skipped = 0
        seen: set[int] = set()
        for row in rows:
            raw_id = (row.get("submission_id") or "").strip()
            decision = (row.get("decision") or "").strip().lower()
            reason = (row.get("reason") or "").strip()[:500]
            if not raw_id.isdigit() or decision not in {"accepted", "rejected"}:
                skipped += 1
                continue
            submission_id = int(raw_id)
            if submission_id <= 0 or submission_id in seen:
                skipped += 1
                continue
            seen.add(submission_id)

            sub_res = await db.client.table("user_submissions").select(
                "id,user_id,category,status"
            ).eq("id", submission_id).limit(1).execute()
            if not sub_res.data:
                skipped += 1
                continue
            submission = sub_res.data[0]
            category = submission.get("category")
            reward = SAFE_CATEGORY_REWARDS.get(category, Decimal("0.00"))
            if submission.get("status") != "pending" or category not in SAFE_CATEGORY_REWARDS:
                skipped += 1
                continue

            try:
                result = await db.client.rpc("apply_submission_review", {
                    "p_submission_id": submission_id,
                    "p_decision": decision,
                    "p_reward": str(reward if decision == "accepted" else Decimal("0.00")),
                    "p_admin_id": message.from_user.id,
                    "p_reason": reason,
                }).execute()
                payload = result.data if isinstance(result.data, dict) else (result.data[0] if isinstance(result.data, list) and result.data else None)
                if not payload or payload.get("status") != "ok":
                    skipped += 1
                    continue
                done += 1
                notify_user = int(payload.get("user_id") or submission["user_id"])
                try:
                    if decision == "accepted":
                        amount = Decimal(str(payload.get("reward", reward)))
                        await message.bot.send_message(
                            notify_user,
                            f"✅ আপনার task review approved হয়েছে। Reward: <b>{amount:.2f} BDT</b>।",
                        )
                    else:
                        await message.bot.send_message(
                            notify_user,
                            "❌ আপনার task review rejected হয়েছে।"
                            + (f" কারণ: {reason}" if reason else ""),
                        )
                except Exception:
                    logger.info("Could not notify reviewed user_id=%s", notify_user)
            except Exception:
                logger.exception("Review RPC failed for submission_id=%s", submission_id)
                skipped += 1

        await message.answer(f"✅ Processed: {done}\n⚠️ Skipped: {skipped}")
    except UnicodeDecodeError:
        await message.answer("CSV UTF-8 encoding-এ দিতে হবে।")
    except Exception:
        logger.exception("Review CSV import failed")
        await message.answer("⚠️ CSV import করা যায়নি।")
