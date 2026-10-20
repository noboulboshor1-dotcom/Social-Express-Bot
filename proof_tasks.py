"""Safe proof submission flow: stores a public HTTPS proof URL, not credentials.

Use tasks such as website feedback, content review, QA testing or data labeling.
Do not use this module to collect passwords, session cookies, 2FA seeds or recovery
codes. Existing account-creation handlers must be removed/disabled separately.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from database import db
from keyboards import get_main_keyboard

logger = logging.getLogger(__name__)
proof_router = Router(name="safe_proof_tasks")

SAFE_CATEGORIES = frozenset({"website_feedback", "content_review", "data_labeling", "qa_testing"})
_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")


def _safe_https_url(raw: str) -> str | None:
    value = (raw or "").strip()
    if len(value) > 1500:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        if any(ch.isspace() for ch in value):
            return None
        # Keep URL intact for legitimate proof links, but reject URL credentials, query strings and fragments to avoid leaking tokens.
        return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


@proof_router.message(Command("submitproof"))
async def submit_proof(message: Message, command: CommandObject) -> None:
    """Usage: /submitproof website_feedback https://example.org/proof"""
    parts = (command.args or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "ব্যবহার: <code>/submitproof website_feedback https://example.org/proof</code>\n"
            "অনুমোদিত ক্যাটাগরি: <code>website_feedback</code>, <code>content_review</code>, "
            "<code>data_labeling</code>, <code>qa_testing</code>",
            reply_markup=get_main_keyboard(),
        )
        return

    category, raw_url = parts[0].lower(), parts[1]
    if not _CATEGORY_RE.fullmatch(category) or category not in SAFE_CATEGORIES:
        await message.answer("⚠️ এই টাস্ক ক্যাটাগরি অনুমোদিত নয়।")
        return

    proof_url = _safe_https_url(raw_url)
    if not proof_url:
        await message.answer("⚠️ একটি বৈধ HTTPS proof URL দিন। URL-এ username/password, query string বা fragment থাকতে পারবে না।")
        return

    # Deliberately store only a public proof URL and a category; no auth secrets.
    submission_id = await db.add_submission(
        user_id=message.from_user.id,
        category=category,
        data=f"proof_url={proof_url}",
        status="pending",
    )
    if submission_id is None:
        await message.answer("⚠️ সাবমিশন সংরক্ষণ করা যায়নি। আবার চেষ্টা করুন।")
        return

    await message.answer(
        f"✅ Proof জমা হয়েছে।\nSubmission ID: <code>{submission_id}</code>\nStatus: <b>Pending review</b>",
        reply_markup=get_main_keyboard(),
    )
