"""Gift-code creation/redemption handlers backed by atomic Supabase RPCs.

SQL migration safe_upgrade.sql must be applied before enabling this router.
Commands:
  /redeem CODE
  /gift_create CODE AMOUNT CLAIM_LIMIT
  /gift_disable CODE
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import ADMIN_IDS
from database import db
from keyboards import get_main_keyboard

logger = logging.getLogger(__name__)
gift_router = Router(name="gift_codes")
_CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")


def _rpc_payload(data):
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


def _money(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw)
        if not value.is_finite() or value <= 0 or value > Decimal("1000000"):
            return None
        return value.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


@gift_router.message(Command("redeem"))
async def redeem_gift_code(message: Message, command: CommandObject) -> None:
    code = (command.args or "").strip().upper()
    if not _CODE_RE.fullmatch(code):
        await message.answer("ব্যবহার: <code>/redeem YOURCODE</code>")
        return

    try:
        result = await db.client.rpc("redeem_gift_code", {
            "p_user_id": message.from_user.id,
            "p_code": code,
        }).execute()
        payload = _rpc_payload(result.data)
        if not payload or payload.get("status") != "ok":
            reason = payload.get("reason") if payload else None
            messages = {
                "not_found": "এই গিফট কোড পাওয়া যায়নি।",
                "inactive": "এই গিফট কোড বর্তমানে বন্ধ।",
                "expired": "এই গিফট কোডের মেয়াদ শেষ।",
                "limit_reached": "এই গিফট কোডের দাবি-সীমা পূর্ণ হয়েছে।",
                "already_claimed": "আপনি এই কোডটি আগেই ব্যবহার করেছেন।",
                "user_missing": "আপনার প্রোফাইল পাওয়া যায়নি। /start দিন।",
            }
            await message.answer("⚠️ " + messages.get(reason, "কোড রিডিম করা যায়নি।"))
            return

        amount = payload.get("amount", "0.00")
        balance = payload.get("balance")
        tail = f"\nবর্তমান ব্যালেন্স: <b>{Decimal(str(balance)):.2f} BDT</b>" if balance is not None else ""
        await message.answer(f"🎉 গিফট কোড সফলভাবে রিডিম হয়েছে!\nযোগ হয়েছে: <b>{Decimal(str(amount)):.2f} BDT</b>{tail}", reply_markup=get_main_keyboard())
    except Exception:
        logger.exception("Gift code redemption failed for user_id=%s", message.from_user.id)
        await message.answer("⚠️ এখন গিফট কোড রিডিম করা যাচ্ছে না। পরে চেষ্টা করুন।")


@gift_router.message(Command("gift_create"))
async def create_gift_code(message: Message, command: CommandObject) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").strip().split()
    if len(parts) != 3:
        await message.answer("ব্যবহার: <code>/gift_create CODE AMOUNT CLAIM_LIMIT</code>")
        return

    code = parts[0].upper()
    amount = _money(parts[1])
    if not _CODE_RE.fullmatch(code) or amount is None:
        await message.answer("⚠️ কোড বা পরিমাণ সঠিক নয়। পরিমাণ 0.01–1,000,000 BDT হতে হবে।")
        return
    try:
        claim_limit = int(parts[2])
    except ValueError:
        claim_limit = 0
    if not 1 <= claim_limit <= 1_000_000:
        await message.answer("⚠️ Claim limit 1 থেকে 1,000,000-এর মধ্যে দিন।")
        return

    try:
        res = await db.client.table("gift_codes").insert({
            "code": code,
            "amount": str(amount),
            "max_claims": claim_limit,
            "claim_count": 0,
            "active": True,
            "created_by": message.from_user.id,
        }).execute()
        if not res.data:
            raise RuntimeError("Gift-code insert did not return a row")
        await message.answer(
            f"✅ গিফট কোড তৈরি হয়েছে।\nকোড: <code>{code}</code>\n"
            f"পরিমাণ: <b>{amount:.2f} BDT</b>\nসর্বোচ্চ দাবি: <b>{claim_limit}</b>"
        )
    except Exception:
        logger.exception("Gift-code create failed; code=%s", code)
        await message.answer("⚠️ কোড তৈরি হয়নি। কোডটি আগে থেকে থাকলে অন্য কোড ব্যবহার করুন।")


@gift_router.message(Command("gift_disable"))
async def disable_gift_code(message: Message, command: CommandObject) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    code = (command.args or "").strip().upper()
    if not _CODE_RE.fullmatch(code):
        await message.answer("ব্যবহার: <code>/gift_disable CODE</code>")
        return
    try:
        res = await db.client.table("gift_codes").update({"active": False}).eq("code", code).eq("active", True).select("code").execute()
        if res.data:
            await message.answer(f"✅ <code>{code}</code> নিষ্ক্রিয় করা হয়েছে।")
        else:
            await message.answer("⚠️ সক্রিয় কোডটি পাওয়া যায়নি।")
    except Exception:
        logger.exception("Gift-code disable failed; code=%s", code)
        await message.answer("⚠️ কোড নিষ্ক্রিয় করা যায়নি।")
