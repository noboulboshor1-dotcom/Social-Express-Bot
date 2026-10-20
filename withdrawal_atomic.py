"""Helpers for atomic Supabase RPC withdrawal operations.

After applying safe_upgrade.sql, replace calls to db.create_withdrawal(...) with
create_withdrawal_atomic(...) and db.update_withdrawal_status(...) with
review_withdrawal_atomic(...). These return compatible tuple shapes.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from database import db

logger = logging.getLogger(__name__)
_METHOD_ALIASES = {
    "bkash": "bkash", "bKash": "bkash",
    "nagad": "nagad", "Nagad": "nagad",
    "rocket": "rocket", "Rocket": "rocket",
    "binance": "binance", "Binance (USDT)": "binance",
}


def _payload(data):
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


async def create_withdrawal_atomic(user_id: int, amount: float, method: str, account_no: str):
    """Return (status, withdrawal) compatible with existing withdraw.py."""
    try:
        amt = Decimal(str(amount)).quantize(Decimal("0.01"))
        key = _METHOD_ALIASES.get(method)
        account = (account_no or "").strip()
        if not amt.is_finite() or amt <= 0 or not key or not account or len(account) > 255:
            return "error", None
        result = await db.client.rpc("create_withdrawal_atomic", {
            "p_user_id": int(user_id),
            "p_amount": str(amt),
            "p_method": key,
            "p_account_no": account,
        }).execute()
        payload = _payload(result.data)
        if not payload:
            return "error", None
        status = payload.get("status", "error")
        return status, payload.get("withdrawal") or payload
    except (InvalidOperation, ValueError, TypeError):
        return "error", None
    except Exception:
        logger.exception("Atomic withdrawal creation failed for user_id=%s", user_id)
        return "error", None


async def review_withdrawal_atomic(withdrawal_id: int, status: str, admin_id: int | None = None, reason: str = ""):
    """Return (success, withdrawal); rejection refund is in the same DB transaction."""
    if status not in {"approved", "rejected"} or int(withdrawal_id) <= 0:
        return False, None
    try:
        result = await db.client.rpc("review_withdrawal_atomic", {
            "p_withdrawal_id": int(withdrawal_id),
            "p_status": status,
            "p_admin_id": admin_id,
            "p_reason": (reason or "")[:500],
        }).execute()
        payload = _payload(result.data)
        if not payload or payload.get("status") != "ok":
            return False, None
        return True, payload.get("withdrawal")
    except Exception:
        logger.exception("Atomic withdrawal review failed for id=%s", withdrawal_id)
        return False, None
