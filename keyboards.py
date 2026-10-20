"""Shared UI helpers. Kept separate so no module has to import main.py
(which caused circular imports and a double module load)."""
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

logger = logging.getLogger(__name__)

BTN_TASKS = "📋 Tasks ▸"
BTN_BALANCE = "👤 My Profile"
BTN_WITHDRAW = "💵 Withdraw"
BTN_REFERRALS = "🎁 My Referrals"
BTN_BONUS = "🎁 Daily Bonus"
BTN_SUPPORT = "🎧 Support"
BTN_NEW = "💡 I am New"
BTN_CANCEL = "❌ Cancel"

# Pressing any of these always leaves whatever flow the user was in.
MENU_BUTTONS = frozenset(
    {BTN_TASKS, BTN_BALANCE, BTN_WITHDRAW, BTN_REFERRALS, BTN_BONUS, BTN_SUPPORT, BTN_NEW, BTN_CANCEL}
)


def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TASKS), KeyboardButton(text=BTN_BALANCE)],
            [KeyboardButton(text=BTN_WITHDRAW), KeyboardButton(text=BTN_REFERRALS)],
            [KeyboardButton(text=BTN_BONUS), KeyboardButton(text=BTN_SUPPORT)],
            [KeyboardButton(text=BTN_NEW)],
        ],
        resize_keyboard=True,
    )


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


def withdrawal_review_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_wd_{req_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_wd_{req_id}"),
            ]
        ]
    )


async def safe_edit(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    """edit_text that never raises: ignores 'message is not modified' and falls
    back to sending a fresh message if the old one can no longer be edited."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return False
        logger.warning("edit_text failed (%s) - sending a new message instead", e)
        try:
            await message.answer(text, reply_markup=reply_markup)
            return True
        except Exception as inner:  # noqa: BLE001
            logger.error("fallback send failed: %s", inner)
            return False
