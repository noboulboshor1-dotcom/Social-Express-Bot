"""Admin command to configure per-method withdrawal limits, charges and status.

Usage: /withdraw_config bkash 100 5 open
Methods: bkash, nagad, rocket, binance. Amount/charge are BDT and must be finite.
The atomic withdrawal RPC reads these settings directly from Supabase.
"""
from __future__ import annotations

import logging
import math

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import ADMIN_IDS
from database import db

logger = logging.getLogger(__name__)
withdraw_settings_router = Router(name="withdrawal_settings")
METHODS = {"bkash", "nagad", "rocket", "binance"}


@withdraw_settings_router.message(Command("withdraw_config"))
async def update_withdraw_config(message: Message, command: CommandObject) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").strip().lower().split()
    if len(parts) != 4:
        await message.answer("ব্যবহার: <code>/withdraw_config METHOD MINIMUM CHARGE open|closed</code>\nMethod: bkash, nagad, rocket, binance")
        return
    method, min_raw, charge_raw, status = parts
    if method not in METHODS or status not in {"open", "closed"}:
        await message.answer("⚠️ Method বা status সঠিক নয়।")
        return
    try:
        minimum = float(min_raw)
        charge = float(charge_raw)
        if not math.isfinite(minimum) or not math.isfinite(charge) or minimum <= 0 or charge < 0 or charge >= minimum:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Minimum positive হতে হবে এবং charge 0 থেকে minimum-এর কম হতে হবে।")
        return

    values = {
        f"withdraw_{method}_min": f"{minimum:.2f}",
        f"withdraw_{method}_charge": f"{charge:.2f}",
        f"withdraw_{method}_status": status,
    }
    try:
        rows = [{"key": key, "value": value} for key, value in values.items()]
        await db.client.table("settings").upsert(rows, on_conflict="key").execute()
        await message.answer(
            f"✅ {method.title()} updated\nMinimum: {minimum:.2f} BDT\n"
            f"Charge: {charge:.2f} BDT\nStatus: {status}"
        )
    except Exception:
        logger.exception("Withdrawal configuration update failed")
        await message.answer("⚠️ সেটিং সেভ করা যায়নি। ডাটাবেজে মানগুলো যাচাই করুন।")
