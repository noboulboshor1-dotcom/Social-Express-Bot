"""Profile dashboard router for the existing Aiogram 3.x project.

Register profile_router before the main user router so it handles the existing
BTN_BALANCE message first. Requires database.db.get_user/get_user_stats.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import db
from keyboards import BTN_BALANCE, get_main_keyboard

logger = logging.getLogger(__name__)
profile_router = Router(name="profile_dashboard")


def _profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Refresh Profile", callback_data="profile_refresh")]
    ])


def _money(value: object) -> str:
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _profile_text(user_id: int, stats: dict) -> str:
    return (
        "👤 <b>My Profile</b>\n\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"💰 Current Balance: <b>{_money(stats.get('balance'))} BDT</b>\n"
        f"✅ Completed Tasks: <b>{int(stats.get('completed_tasks') or 0)}</b>\n"
        f"⏳ Pending Reviews: <b>{int(stats.get('pending_tasks') or 0)}</b>\n"
        f"❌ Rejected / Failed Tasks: <b>{int(stats.get('rejected_tasks') or 0)}</b>\n"
        f"👥 Total Referrals: <b>{int(stats.get('referral_count') or 0)}</b>\n"
        f"🎁 Referral Income: <b>{_money(stats.get('referral_income'))} BDT</b>"
    )


async def _send_profile(message: Message, user_id: int) -> None:
    try:
        user = await db.get_user(user_id)
        if not user:
            user = await db.get_or_create_user(user_id, message.from_user.username or "")
        stats = await db.get_user_stats(user_id)
        await message.answer(
            _profile_text(user_id, stats),
            reply_markup=_profile_keyboard(),
        )
    except Exception:
        logger.exception("Could not render profile for user_id=%s", user_id)
        await message.answer(
            "⚠️ প্রোফাইল লোড করা যায়নি। কিছুক্ষণ পরে আবার চেষ্টা করুন।",
            reply_markup=get_main_keyboard(),
        )


@profile_router.message(F.text.in_({BTN_BALANCE, "👤 My Profile", "💰 Balance"}))
async def show_profile(message: Message) -> None:
    await _send_profile(message, message.from_user.id)


@profile_router.callback_query(F.data == "profile_refresh")
async def refresh_profile(callback: CallbackQuery) -> None:
    try:
        stats = await db.get_user_stats(callback.from_user.id)
        text = _profile_text(callback.from_user.id, stats)
        if callback.message:
            await callback.message.edit_text(text, reply_markup=_profile_keyboard())
        await callback.answer("Profile refreshed")
    except TelegramBadRequest:
        # ব্যালেন্স বা ডেটা পরিবর্তন না হলে টেলিগ্রাম এই এরর দেয়, আমরা এটি ইগনোর করব
        await callback.answer() 
    except Exception:
        logger.exception("Could not refresh profile for user_id=%s", callback.from_user.id)
        await callback.answer("প্রোফাইল রিফ্রেশ করা যায়নি। পরে চেষ্টা করুন।", show_alert=True)
