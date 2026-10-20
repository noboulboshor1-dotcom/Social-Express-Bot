"""Drop-in dynamic withdrawal router for the uploaded Aiogram 3.x bot.

Requires `safe_upgrade.sql` and `withdrawal_atomic.py`. This implementation
supports bKash, Nagad, Rocket and Binance; checks live per-method configuration;
uses Decimal for money; and reserves funds with one atomic database RPC.
"""
from __future__ import annotations

import html
import logging
import re
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS
from database import db
from keyboards import BTN_WITHDRAW, get_cancel_keyboard, get_main_keyboard, safe_edit, withdrawal_review_kb
from withdrawal_atomic import create_withdrawal_atomic

logger = logging.getLogger(__name__)
withdraw_router = Router(name="withdraw")

METHODS = {
    "bkash": "bKash",
    "nagad": "Nagad",
    "rocket": "Rocket",
    "binance": "Binance (USDT)",
}
DEFAULT_MINIMUMS = {"bkash": Decimal("100"), "nagad": Decimal("100"), "rocket": Decimal("100"), "binance": Decimal("100")}
DEFAULT_CHARGES = {"bkash": Decimal("5"), "nagad": Decimal("5"), "rocket": Decimal("5"), "binance": Decimal("0")}
_BD_MOBILE = re.compile(r"^(?:\+?88)?(01[3-9]\d{8})$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TRC20 = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
_BEP20 = re.compile(r"^0x[a-fA-F0-9]{40}$")
_BINANCE_ID = re.compile(r"^\d{6,20}$")


class WithdrawStates(StatesGroup):
    waiting_for_method = State()
    waiting_for_account = State()
    waiting_for_amount = State()


def _decimal(raw: object) -> Decimal | None:
    try:
        value = Decimal(str(raw))
        if not value.is_finite():
            return None
        return value.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fmt(value: object) -> str:
    amount = _decimal(value)
    return f"{amount:,.2f}" if amount is not None else "0.00"


async def _method_config(key: str) -> tuple[bool, Decimal, Decimal]:
    """Read status, minimum and fee live; fail closed when status is missing."""
    if key not in METHODS:
        return False, Decimal("0"), Decimal("0")
    status = (await db.get_setting(f"withdraw_{key}_status", "closed")).strip().lower()
    minimum = _decimal(await db.get_setting(f"withdraw_{key}_min", str(DEFAULT_MINIMUMS[key])))
    charge = _decimal(await db.get_setting(f"withdraw_{key}_charge", str(DEFAULT_CHARGES[key])))
    if minimum is None or charge is None or minimum <= 0 or charge < 0 or charge >= minimum:
        return False, Decimal("0"), Decimal("0")
    return status == "open", minimum, charge


def validate_account(method_key: str, raw: str) -> str | None:
    value = re.sub(r"\s+", "", (raw or "").strip())
    if not value or len(value) > 255:
        return None
    if method_key in {"bkash", "nagad", "rocket"}:
        match = _BD_MOBILE.fullmatch(value)
        return match.group(1) if match else None
    if method_key == "binance" and (_BINANCE_ID.fullmatch(value) or _EMAIL.fullmatch(value) or _TRC20.fullmatch(value) or _BEP20.fullmatch(value)):
        return value
    return None


def _account_hint(method: str) -> str:
    if method in {"bkash", "nagad", "rocket"}:
        return "বাংলাদেশি ১১ ডিজিটের মোবাইল নম্বর (যেমন: <code>01XXXXXXXXX</code>)"
    return "Binance Pay ID, ইমেইল, TRC20 বা BEP20 wallet address"


async def _live_balance(user_id: int) -> Decimal:
    user = await db.get_or_create_user(user_id, "")
    return _decimal(user.get("balance")) or Decimal("0.00")


async def _available_methods() -> list[tuple[str, str, Decimal, Decimal]]:
    available = []
    for key, label in METHODS.items():
        is_open, minimum, charge = await _method_config(key)
        if is_open:
            available.append((key, label, minimum, charge))
    return available


async def _notify_admins(bot, req: dict) -> None:
    key = str(req.get("method") or "")
    label = METHODS.get(key, key)
    text = (
        "🔔 <b>New Withdrawal Request</b>\n\n"
        f"🧾 Request: <code>#{req.get('id')}</code>\n"
        f"👤 User ID: <code>{req.get('user_id')}</code>\n"
        f"💰 Requested: <b>{_fmt(req.get('amount'))} BDT</b>\n"
        f"➖ Charge: <b>{_fmt(req.get('charge'))} BDT</b>\n"
        f"💸 Payout: <b>{_fmt(req.get('payout_amount'))} BDT</b>\n"
        f"💳 Method: <b>{html.escape(label)}</b>\n"
        f"📱 Account: <code>{html.escape(str(req.get('account_no') or ''))}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=withdrawal_review_kb(int(req["id"])))
        except Exception:
            logger.exception("Could not notify admin_id=%s of withdrawal", admin_id)


@withdraw_router.message(F.text == BTN_WITHDRAW)
async def handle_withdraw_btn(message: Message, state: FSMContext) -> None:
    await state.clear()
    balance = await _live_balance(message.from_user.id)
    choices = await _available_methods()
    if not choices:
        await message.answer("⚠️ বর্তমানে সব withdrawal method বন্ধ আছে।", reply_markup=get_main_keyboard())
        return

    rows = []
    for key, label, minimum, charge in choices:
        rows.append([InlineKeyboardButton(
            text=f"{label} · Min {_fmt(minimum)} · Fee {_fmt(charge)} BDT",
            callback_data=f"wd_method_{key}",
        )])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="wd_cancel")])
    await state.set_state(WithdrawStates.waiting_for_method)
    await message.answer(
        f"💰 Available balance: <b>{_fmt(balance)} BDT</b>\n\nপেমেন্ট মাধ্যম নির্বাচন করুন:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@withdraw_router.callback_query(F.data.startswith("wd_method_"))
async def handle_wd_method(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.removeprefix("wd_method_")
    if key not in METHODS:
        await callback.answer("Invalid method", show_alert=True)
        return
    is_open, minimum, charge = await _method_config(key)
    if not is_open:
        await callback.answer("এই withdrawal method বর্তমানে বন্ধ।", show_alert=True)
        return
    balance = await _live_balance(callback.from_user.id)
    if balance < minimum:
        await state.clear()
        await safe_edit(callback.message, f"⚠️ আপনার ব্যালেন্স {_fmt(balance)} BDT; এই মাধ্যমে সর্বনিম্ন {_fmt(minimum)} BDT।")
        await callback.answer()
        return

    await state.update_data(withdraw_method=key)
    await state.set_state(WithdrawStates.waiting_for_account)
    await safe_edit(callback.message, f"✅ Method: <b>{html.escape(METHODS[key])}</b>\nMinimum: <b>{_fmt(minimum)} BDT</b> · Fee: <b>{_fmt(charge)} BDT</b>")
    await callback.message.answer(f"আপনার {html.escape(METHODS[key])} destination দিন।\n{_account_hint(key)}", reply_markup=get_cancel_keyboard())
    await callback.answer()


@withdraw_router.message(WithdrawStates.waiting_for_account, F.text)
async def handle_wd_account(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("withdraw_method")
    if key not in METHODS:
        await state.clear()
        await message.answer("⚠️ Session expired. আবার Withdraw নির্বাচন করুন।", reply_markup=get_main_keyboard())
        return
    account = validate_account(key, message.text)
    if not account:
        await message.answer(f"⚠️ সঠিক destination দিন।\n{_account_hint(key)}", reply_markup=get_cancel_keyboard())
        return
    await state.update_data(withdraw_account=account)
    await state.set_state(WithdrawStates.waiting_for_amount)
    is_open, minimum, charge = await _method_config(key)
    balance = await _live_balance(message.from_user.id)
    if not is_open:
        await state.clear()
        await message.answer("⚠️ এই withdrawal method বন্ধ করা হয়েছে।", reply_markup=get_main_keyboard())
        return
    await message.answer(
        f"💰 Balance: <b>{_fmt(balance)} BDT</b>\nMinimum: <b>{_fmt(minimum)} BDT</b>\n"
        f"Fee: <b>{_fmt(charge)} BDT</b>\n\nকত টাকা withdraw করবেন? (gross amount লিখুন)",
        reply_markup=get_cancel_keyboard(),
    )


@withdraw_router.message(WithdrawStates.waiting_for_amount, F.text)
async def handle_wd_amount(message: Message, state: FSMContext) -> None:
    amount = _decimal(message.text.strip().replace(",", ""))
    if amount is None or amount <= 0:
        await message.answer("⚠️ বৈধ positive amount দিন, যেমন <code>150.00</code>।", reply_markup=get_cancel_keyboard())
        return
    data = await state.get_data()
    key, account = data.get("withdraw_method"), data.get("withdraw_account")
    if key not in METHODS or not account:
        await state.clear()
        await message.answer("⚠️ Session expired. আবার Withdraw নির্বাচন করুন।", reply_markup=get_main_keyboard())
        return

    # Clear the FSM before the RPC to reduce duplicate submissions. The DB RPC
    # is authoritative and debits balance + inserts the request atomically.
    await state.clear()
    status, req = await create_withdrawal_atomic(message.from_user.id, amount, key, account)
    if status == "ok" and req:
        await message.answer(
            "✅ Withdrawal request জমা হয়েছে।\n\n"
            f"Requested: <b>{_fmt(req.get('amount'))} BDT</b>\n"
            f"Charge: <b>{_fmt(req.get('charge'))} BDT</b>\n"
            f"Expected payout: <b>{_fmt(req.get('payout_amount'))} BDT</b>\n"
            f"Method: <b>{html.escape(METHODS.get(key, key))}</b>\nStatus: <b>Pending review</b>",
            reply_markup=get_main_keyboard(),
        )
        await _notify_admins(message.bot, req)
    elif status == "below_min":
        minimum = (req or {}).get("minimum") if isinstance(req, dict) else None
        await message.answer(f"⚠️ Minimum withdrawal is {_fmt(minimum)} BDT for this method.", reply_markup=get_main_keyboard())
    elif status == "closed":
        await message.answer("⚠️ এই withdrawal method এখন বন্ধ।", reply_markup=get_main_keyboard())
    elif status == "insufficient":
        await message.answer("⚠️ আপনার পর্যাপ্ত ব্যালেন্স নেই।", reply_markup=get_main_keyboard())
    else:
        await message.answer("⚠️ Withdrawal request জমা হয়নি। ব্যালেন্স পরিবর্তনের আগে database status যাচাই করুন।", reply_markup=get_main_keyboard())


@withdraw_router.callback_query(F.data == "wd_cancel")
async def handle_wd_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        await safe_edit(callback.message, "❌ Withdrawal process cancelled.")
        await callback.message.answer("প্রধান মেনু:", reply_markup=get_main_keyboard())
    await callback.answer()
