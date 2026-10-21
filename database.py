import asyncio
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from supabase import AsyncClient, acreate_client

from config import (
    SUPABASE_URL,
    SUPABASE_KEY,
    INSTANCE_ID,
    LOCK_TTL,
    TASK_STALE_MINUTES,
)

logger = logging.getLogger(__name__)

BD_TZ = timezone(timedelta(hours=6))
FLOAT_FIELDS = {"balance", "referral_income"}
LOCK_ROW_ID = 1

DEFAULT_SETTINGS = {
    "min_withdraw": "50.0",
    "referral_commission": "10.0",
    "daily_bonus": "1.0",
    "proof_channel": "",
}


def today_bd() -> str:
    """বর্তমান বাংলাদেশি তারিখ।"""
    return datetime.now(BD_TZ).date().isoformat()


class Database:
    def __init__(self):
        self.client: Optional[AsyncClient] = None
        self._ban_cache: dict[int, tuple[float, bool]] = {}
        self._lock_available = True

    # ================================================================
    # CONNECTION
    # ================================================================

    async def connect(self) -> None:
        try:
            self.client = await acreate_client(
                SUPABASE_URL,
                SUPABASE_KEY,
            )
            await self.client.table("settings").select("key").limit(1).execute()
            logger.info("Supabase async connection established.")
        except Exception:
            logger.exception("Supabase connection failed.")
            raise

    def _db(self) -> AsyncClient:
        if self.client is None:
            raise RuntimeError(
                "Database is not connected. Call await db.connect() first."
            )
        return self.client

    # ================================================================
    # SETTINGS
    # ================================================================

    async def get_setting(
        self,
        key: str,
        default_value: Optional[str] = None,
    ) -> str:
        fallback = (
            DEFAULT_SETTINGS.get(key, "")
            if default_value is None
            else default_value
        )
        try:
            res = await self._db().table("settings").select(
                "value"
            ).eq("key", key).limit(1).execute()

            if res.data and res.data[0].get("value") is not None:
                return str(res.data[0]["value"])

            return fallback
        except Exception:
            logger.exception("Error fetching setting %s", key)
            return fallback

    async def get_float_setting(self, key: str, default: float) -> float:
        raw = await self.get_setting(key, str(default))
        try:
            value = float(raw.strip())
            return value if math.isfinite(value) else default
        except (TypeError, ValueError, AttributeError):
            return default

    async def update_setting(self, key: str, value: str) -> bool:
        try:
            await self._db().table("settings").upsert(
                {"key": key, "value": str(value)},
                on_conflict="key",
            ).execute()
            return True
        except Exception:
            logger.exception("Error updating setting %s", key)
            return False

    # ================================================================
    # CATEGORIES
    # ================================================================

    async def get_categories(self) -> list[dict]:
        try:
            res = await self._db().table(
                "task_categories"
            ).select("*").order("category_key").execute()
            return res.data or []
        except Exception:
            logger.exception("Error fetching categories")
            return []

    async def is_category_enabled(self, cat_key: str) -> bool:
        try:
            res = await self._db().table(
                "task_categories"
            ).select("is_enabled").eq(
                "category_key", cat_key
            ).limit(1).execute()

            return bool(
                res.data and res.data[0].get("is_enabled", False)
            )
        except Exception:
            logger.exception("Error checking category %s", cat_key)
            return False

    async def toggle_category(self, cat_key: str) -> Optional[bool]:
        try:
            res = await self._db().table(
                "task_categories"
            ).select("is_enabled").eq(
                "category_key", cat_key
            ).limit(1).execute()

            if not res.data:
                return None

            new_value = not bool(res.data[0].get("is_enabled"))
            updated = await self._db().table(
                "task_categories"
            ).update(
                {"is_enabled": new_value}
            ).eq(
                "category_key", cat_key
            ).select("*").execute()

            return new_value if updated.data else None
        except Exception:
            logger.exception("Error toggling category %s", cat_key)
            return None

    # ================================================================
    # USERS
    # ================================================================

    async def get_user(self, user_id: int) -> Optional[dict]:
        res = await self._db().table("users").select(
            "*"
        ).eq("user_id", user_id).limit(1).execute()

        return res.data[0] if res.data else None

    async def get_or_create_user(
        self,
        user_id: int,
        username: str = "",
        referred_by: Optional[int] = None,
    ) -> dict:
        try:
            existing = await self.get_user(user_id)

            if existing:
                if username and existing.get("username") != username:
                    await self._db().table("users").update(
                        {"username": username}
                    ).eq("user_id", user_id).execute()
                    existing["username"] = username

                return existing

            valid_referrer = None
            if referred_by and referred_by != user_id:
                referrer = await self.get_user(referred_by)
                if referrer:
                    valid_referrer = referred_by

            user_data = {
                "user_id": user_id,
                "username": username or f"User_{user_id}",
                "balance": 0.0,
                "completed_tasks": 0,
                "referral_count": 0,
                "referral_income": 0.0,
                "referred_by": valid_referrer,
                "is_banned": False,
            }

            try:
                inserted = await self._db().table(
                    "users"
                ).insert(user_data).execute()
            except Exception:
                existing = await self.get_user(user_id)
                if existing:
                    return existing
                raise

            if valid_referrer:
                await self._apply_deltas(
                    valid_referrer,
                    {"referral_count": 1},
                )

            return inserted.data[0] if inserted.data else user_data

        except Exception:
            logger.exception("Error creating user %s", user_id)
            return {
                "user_id": user_id,
                "username": username or "",
                "balance": 0.0,
                "completed_tasks": 0,
                "referral_count": 0,
                "referral_income": 0.0,
                "is_banned": False,
            }

    async def increment_referral_count(self, referrer_id: int) -> bool:
        status, _ = await self._apply_deltas(
            referrer_id,
            {"referral_count": 1},
        )
        return status == "ok"

    async def get_user_stats(self, user_id: int) -> dict:
        defaults = {
            "balance": 0.0,
            "completed_tasks": 0,
            "pending_tasks": 0,
            "rejected_tasks": 0,
            "referral_count": 0,
            "referral_income": 0.0,
        }

        try:
            user = await self.get_user(user_id)
            if not user:
                return defaults

            pending = await self._db().table(
                "user_submissions"
            ).select("id", count="exact").eq(
                "user_id", user_id
            ).eq("status", "pending").execute()

            rejected = await self._db().table(
                "user_submissions"
            ).select("id", count="exact").eq(
                "user_id", user_id
            ).eq("status", "rejected").execute()

            return {
                "balance": float(user.get("balance") or 0),
                "completed_tasks": int(user.get("completed_tasks") or 0),
                "pending_tasks": int(pending.count or 0),
                "rejected_tasks": int(rejected.count or 0),
                "referral_count": int(user.get("referral_count") or 0),
                "referral_income": float(user.get("referral_income") or 0),
            }
        except Exception:
            logger.exception("Error fetching stats for %s", user_id)
            return defaults

    async def is_banned(self, user_id: int) -> bool:
        now = time.monotonic()
        cached = self._ban_cache.get(user_id)

        if cached and now - cached[0] < 20:
            return cached[1]

        try:
            res = await self._db().table("users").select(
                "is_banned"
            ).eq("user_id", user_id).limit(1).execute()

            banned = bool(
                res.data and res.data[0].get("is_banned", False)
            )
            self._ban_cache[user_id] = (now, banned)
            return banned
        except Exception:
            logger.exception("Error checking ban status for %s", user_id)
            return True

    async def set_user_ban_status(
        self,
        user_id: int,
        ban: bool,
    ) -> bool:
        try:
            res = await self._db().table("users").update(
                {"is_banned": ban}
            ).eq("user_id", user_id).select("*").execute()

            if not res.data:
                return False

            self._ban_cache[user_id] = (time.monotonic(), ban)
            return True
        except Exception:
            logger.exception("Error changing ban status")
            return False

    async def get_users_paginated(
        self,
        page: int = 1,
        limit: int = 6,
    ) -> tuple[list[dict], int]:
        page = max(1, page)
        limit = max(1, min(limit, 100))
        start = (page - 1) * limit

        try:
            res = await self._db().table("users").select(
                "user_id, username, balance, is_banned, completed_tasks",
                count="exact",
            ).order("user_id").range(
                start, start + limit - 1
            ).execute()

            return res.data or [], int(res.count or 0)
        except Exception:
            logger.exception("Error fetching users page")
            return [], 0

    async def get_all_users_count(self) -> int:
        try:
            res = await self._db().table("users").select(
                "user_id", count="exact"
            ).limit(1).execute()
            return int(res.count or 0)
        except Exception:
            logger.exception("Error counting users")
            return 0

    async def get_broadcast_user_ids(self) -> list[int]:
        ids = []
        start, size = 0, 1000

        try:
            while True:
                res = await self._db().table("users").select(
                    "user_id"
                ).or_(
                    "is_banned.is.null,is_banned.eq.false"
                ).order("user_id").range(
                    start, start + size - 1
                ).execute()

                batch = [
                    row["user_id"] for row in (res.data or [])
                ]
                ids.extend(batch)

                if len(batch) < size:
                    break
                start += size

        except Exception:
            logger.exception("Error fetching broadcast users")

        return ids

    # ================================================================
    # ATOMIC BALANCE AND COUNTER UPDATES
    # ================================================================

    async def _apply_deltas(
        self,
        user_id: int,
        deltas: dict,
        forbid_negative_balance: bool = False,
        retries: int = 8,
    ) -> tuple[str, Optional[dict]]:
        client = self._db()
        fields = list(deltas)

        for attempt in range(retries):
            res = await client.table("users").select(
                ",".join(fields)
            ).eq("user_id", user_id).limit(1).execute()

            if not res.data:
                return "missing", None

            row = res.data[0]
            new_values = {}

            for field, delta in deltas.items():
                old = row.get(field) or 0

                if field in FLOAT_FIELDS:
                    new_values[field] = round(
                        float(old) + float(delta), 4
                    )
                else:
                    new_values[field] = int(old) + int(delta)

            if (
                forbid_negative_balance
                and new_values.get("balance", 0) < 0
            ):
                return "insufficient", row

            query = client.table("users").update(new_values).eq(
                "user_id", user_id
            )

            for field in fields:
                if row.get(field) is None:
                    query = query.is_(field, "null")
                else:
                    query = query.eq(field, row[field])

            updated = await query.select("*").execute()

            if updated.data:
                return "ok", updated.data[0]

            await asyncio.sleep(
                0.05 * (attempt + 1) + random.random() * 0.05
            )

        return "conflict", None

    async def add_user_balance(
        self,
        user_id: int,
        amount: float,
    ) -> bool:
        if not math.isfinite(amount):
            return False

        status, _ = await self._apply_deltas(
            user_id,
            {"balance": amount},
        )
        return status == "ok"

    # ================================================================
    # DAILY BONUS (BANGLADESH TIME)
    # ================================================================

    async def claim_daily_bonus(
        self,
        user_id: int,
    ) -> tuple[bool, str, float]:
        try:
            user = await self.get_user(user_id)
            if not user:
                return False, "⚠️ ইউজার পাওয়া যায়নি।", 0.0

            today = today_bd()
            bonus = await self.get_float_setting("daily_bonus", 1.0)

            if bonus <= 0:
                return False, "⚠️ ডেইলি বোনাস বর্তমানে বন্ধ আছে।", 0.0

            old_checkin = user.get("last_checkin")

            marked = await self._db().table("users").update(
                {"last_checkin": today}
            ).eq(
                "user_id", user_id
            ).or_(
                f"last_checkin.is.null,last_checkin.neq.{today}"
            ).select("*").execute()

            if not marked.data:
                return (
                    False,
                    "⚠️ আপনি আজকের বোনাস ইতিমধ্যেই নিয়েছেন।",
                    0.0,
                )

            status, _ = await self._apply_deltas(
                user_id,
                {"balance": bonus},
            )

            if status != "ok":
                rollback = self._db().table("users").update(
                    {"last_checkin": old_checkin}
                ).eq(
                    "user_id", user_id
                ).eq("last_checkin", today)

                if old_checkin is None:
                    await rollback.execute()
                else:
                    await rollback.eq(
                        "last_checkin", today
                    ).execute()

                return False, "⚠️ বোনাস যোগ করা যায়নি।", 0.0

            return (
                True,
                f"🎉 আপনি +{bonus:g} BDT ডেইলি বোনাস পেয়েছেন!",
                bonus,
            )

        except Exception:
            logger.exception("Error claiming daily bonus for %s", user_id)
            return False, "⚠️ বোনাস ক্লেইম করতে সমস্যা হয়েছে।", 0.0

    # ================================================================
    # TASKS: tasks TABLE
    # ================================================================

    async def add_bulk_tasks(self, task_list: list[dict]) -> int:
        inserted = 0
        client = self._db()

        try:
            for start in range(0, len(task_list), 200):
                batch = task_list[start:start + 200]
                res = await client.table("tasks").insert(batch).execute()
                inserted += len(res.data or [])
        except Exception:
            logger.exception("Error inserting bulk tasks")

        return inserted

    async def get_stock_count(self) -> int:
        try:
            res = await self._db().table("tasks").select(
                "id", count="exact"
            ).eq("status", "available").limit(1).execute()
            return int(res.count or 0)
        except Exception:
            logger.exception("Error getting task stock count")
            return 0

    async def _release_stale_tasks(self) -> None:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=max(1, TASK_STALE_MINUTES))
        ).isoformat()

        try:
            await self._db().table("tasks").update({
                "status": "available",
                "assigned_to": None,
                "assigned_at": None,
            }).eq(
                "status", "assigned"
            ).lt("assigned_at", cutoff).execute()
        except Exception:
            logger.exception("Error releasing stale tasks")

    async def get_user_assigned_task(
        self,
        user_id: int,
    ) -> Optional[dict]:
        try:
            res = await self._db().table("tasks").select(
                "*"
            ).eq(
                "assigned_to", user_id
            ).eq(
                "status", "assigned"
            ).order(
                "id", desc=True
            ).limit(1).execute()

            return res.data[0] if res.data else None
        except Exception:
            logger.exception("Error fetching assigned task")
            return None

    async def claim_task(
        self,
        user_id: int,
        fallback_task: Optional[dict] = None,
    ) -> Optional[dict]:
        try:
            existing = await self.get_user_assigned_task(user_id)
            if existing:
                return existing

            await self._release_stale_tasks()
            client = self._db()

            for _ in range(5):
                available = await client.table("tasks").select(
                    "id"
                ).eq(
                    "status", "available"
                ).order("id").limit(1).execute()

                if not available.data:
                    break

                task_id = available.data[0]["id"]
                now = datetime.now(timezone.utc).isoformat()

                claimed = await client.table("tasks").update({
                    "status": "assigned",
                    "assigned_to": user_id,
                    "assigned_at": now,
                }).eq(
                    "id", task_id
                ).eq(
                    "status", "available"
                ).select("*").execute()

                if claimed.data:
                    return claimed.data[0]

                await asyncio.sleep(0.05)

            if fallback_task:
                row = dict(fallback_task)
                row.update({
                    "status": "assigned",
                    "assigned_to": user_id,
                    "assigned_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                })
                created = await client.table("tasks").insert(
                    row
                ).execute()
                return created.data[0] if created.data else None

            return None

        except Exception:
            logger.exception("Error claiming task for %s", user_id)
            return None

    async def release_user_task(self, user_id: int) -> None:
        try:
            await self._db().table("tasks").update({
                "status": "available",
                "assigned_to": None,
                "assigned_at": None,
            }).eq(
                "assigned_to", user_id
            ).eq(
                "status", "assigned"
            ).execute()
        except Exception:
            logger.exception("Error releasing task for %s", user_id)

    async def complete_task(
        self,
        task_id: int,
        user_id: int,
    ) -> Optional[dict]:
        try:
            client = self._db()

            completed = await client.table("tasks").update({
                "status": "completed",
            }).eq(
                "id", task_id
            ).eq(
                "assigned_to", user_id
            ).eq(
                "status", "assigned"
            ).select("*").execute()

            if not completed.data:
                return None

            task = completed.data[0]
            reward = float(task.get("reward") or 0)

            if not math.isfinite(reward) or reward < 0:
                await client.table("tasks").update({
                    "status": "assigned",
                }).eq(
                    "id", task_id
                ).eq("status", "completed").execute()
                return None

            status, user_row = await self._apply_deltas(
                user_id,
                {"balance": reward, "completed_tasks": 1},
            )

            if status != "ok":
                await client.table("tasks").update({
                    "status": "assigned",
                }).eq(
                    "id", task_id
                ).eq(
                    "assigned_to", user_id
                ).eq(
                    "status", "completed"
                ).execute()
                return None

            result = {
                "reward": reward,
                "referrer_id": None,
                "commission": 0.0,
            }

            referrer_id = (user_row or {}).get("referred_by")

            if referrer_id and referrer_id != user_id:
                pct = await self.get_float_setting(
                    "referral_commission", 10.0
                )
                commission = round(
                    reward * max(0.0, pct) / 100.0, 4
                )

                if commission > 0:
                    ref_status, _ = await self._apply_deltas(
                        referrer_id,
                        {
                            "balance": commission,
                            "referral_income": commission,
                        },
                    )

                    if ref_status == "ok":
                        result.update({
                            "referrer_id": referrer_id,
                            "commission": commission,
                        })

            return result

        except Exception:
            logger.exception("Error completing task %s", task_id)
            return None

    # ================================================================
    # STOCK ACCOUNTS
    # ================================================================

    async def add_stock_account(
        self,
        category: str,
        account_data: str,
    ) -> bool:
        try:
            await self._db().table("stock_accounts").insert({
                "category": category,
                "account_data": account_data,
                "is_used": False,
            }).execute()
            return True
        except Exception:
            logger.exception("Error adding stock account")
            return False

    async def get_unused_stock(
        self,
        category: str,
    ) -> Optional[dict]:
        try:
            res = await self._db().table("stock_accounts").select(
                "*"
            ).eq(
                "category", category
            ).eq(
                "is_used", False
            ).order("id").limit(1).execute()

            return res.data[0] if res.data else None
        except Exception:
            logger.exception("Error fetching stock")
            return None

    async def mark_stock_as_used(
        self,
        stock_id: int,
        user_id: int,
    ) -> bool:
        try:
            res = await self._db().table("stock_accounts").update({
                "is_used": True,
                "assigned_to": user_id,
            }).eq(
                "id", stock_id
            ).eq(
                "is_used", False
            ).select("*").execute()

            return bool(res.data)
        except Exception:
            logger.exception("Error marking stock as used")
            return False

    # ================================================================
    # SUBMISSIONS & ADMIN REVIEWS
    # ================================================================

    async def add_submission(
        self,
        user_id: int,
        category: str,
        data: str,
        status: str = "pending",
    ) -> Optional[int]:
        try:
            res = await self._db().table("user_submissions").insert({
                "user_id": user_id,
                "category": category,
                "details": data,
                "status": status,
            }).execute()

            return res.data[0]["id"] if res.data else None
        except Exception:
            logger.exception("Error adding submission")
            return None

    async def update_submission_status(
        self,
        sub_id: int | str,
        status: str,
    ) -> bool:
        if status not in {"pending", "accepted", "rejected"}:
            return False

        try:
            res = await self._db().table(
                "user_submissions"
            ).update(
                {"status": status}
            ).eq(
                "id", sub_id
            ).select("id").execute()

            return bool(res.data)
        except Exception:
            logger.exception("Error updating submission status")
            return False

    async def get_accepted_submissions(self) -> list[dict]:
        try:
            res = await self._db().table(
                "user_submissions"
            ).select("*").eq(
                "status", "accepted"
            ).execute()

            return res.data or []
        except Exception:
            logger.exception("Error fetching accepted submissions")
            return []

    async def approve_submission_and_reward(self, sub_id: int | str, user_id: int, reward: float) -> Optional[dict]:
        """
        ম্যানুয়াল টাস্ক অ্যাকসেপ্ট হলে ব্যালেন্স, টাস্ক কাউন্ট ও রেফারেল কমিশন আপডেট করে।
        """
        updated = await self.update_submission_status(sub_id, "accepted")
        if not updated:
            return None

        status, user_row = await self._apply_deltas(
            user_id,
            {"balance": reward, "completed_tasks": 1},
        )
        if status != "ok":
            return None

        result = {"reward": reward, "referrer_id": None, "commission": 0.0}
        referrer_id = (user_row or {}).get("referred_by")

        if referrer_id and referrer_id != user_id:
            pct = await self.get_float_setting("referral_commission", 10.0)
            commission = round(reward * max(0.0, pct) / 100.0, 4)

            if commission > 0:
                ref_status, _ = await self._apply_deltas(
                    referrer_id,
                    {"balance": commission, "referral_income": commission},
                )
                if ref_status == "ok":
                    result.update({"referrer_id": referrer_id, "commission": commission})

        return result

    # ================================================================
    # WITHDRAWALS
    # ================================================================

    async def create_withdrawal(
        self,
        user_id: int,
        amount: float,
        method: str,
        account_no: str,
    ) -> tuple[str, Optional[dict]]:
        if not math.isfinite(amount) or amount <= 0:
            return "error", None

        try:
            minimum = await self.get_float_setting(
                "min_withdraw", 50.0
            )

            if amount < minimum:
                return "below_min", None

            status, _ = await self._apply_deltas(
                user_id,
                {"balance": -amount},
                forbid_negative_balance=True,
            )

            if status == "insufficient":
                return "insufficient", None
            if status != "ok":
                return "error", None

            try:
                res = await self._db().table("withdrawals").insert({
                    "user_id": user_id,
                    "amount": amount,
                    "method": method,
                    "account_no": account_no,
                    "status": "pending",
                }).execute()

                if not res.data:
                    raise RuntimeError("Withdrawal insert returned no row")

                return "ok", res.data[0]

            except Exception:
                logger.exception("Withdrawal insert failed; refunding")
                refund_status, _ = await self._apply_deltas(
                    user_id, {"balance": amount}
                )

                if refund_status != "ok":
                    logger.critical(
                        "Withdrawal refund needs manual reconciliation "
                        "for user_id=%s, amount=%s",
                        user_id,
                        amount,
                    )

                return "error", None

        except Exception:
            logger.exception("Error creating withdrawal")
            return "error", None

    async def get_pending_withdrawals(
        self,
        limit: int = 20,
    ) -> tuple[list[dict], int]:
        try:
            limit = max(1, min(limit, 100))
            res = await self._db().table("withdrawals").select(
                "*", count="exact"
            ).eq(
                "status", "pending"
            ).order(
                "id"
            ).limit(limit).execute()

            return res.data or [], int(res.count or 0)
        except Exception:
            logger.exception("Error fetching withdrawals")
            return [], 0

    async def count_pending_withdrawals(self) -> int:
        try:
            res = await self._db().table("withdrawals").select(
                "id", count="exact"
            ).eq("status", "pending").limit(1).execute()

            return int(res.count or 0)
        except Exception:
            logger.exception("Error counting withdrawals")
            return 0

    async def update_withdrawal_status(
        self,
        withdrawal_id: int,
        status: str,
    ) -> tuple[bool, Optional[dict]]:
        if status not in {"approved", "rejected"}:
            return False, None

        try:
            updated = await self._db().table(
                "withdrawals"
            ).update(
                {"status": status}
            ).eq(
                "id", withdrawal_id
            ).eq(
                "status", "pending"
            ).select("*").execute()

            if not updated.data:
                return False, None

            withdrawal = updated.data[0]

            if status == "rejected":
                refund_status, _ = await self._apply_deltas(
                    withdrawal["user_id"],
                    {"balance": float(withdrawal["amount"])},
                )

                if refund_status != "ok":
                    await self._db().table(
                        "withdrawals"
                    ).update(
                        {"status": "pending"}
                    ).eq(
                        "id", withdrawal_id
                    ).eq(
                        "status", "rejected"
                    ).execute()

                    logger.critical(
                        "Withdrawal refund failed for id=%s",
                        withdrawal_id,
                    )
                    return False, None

            return True, withdrawal

        except Exception:
            logger.exception("Error updating withdrawal status")
            return False, None

    # ================================================================
    # SINGLE-INSTANCE BOT LEASE
    # bot_lock columns: id, owner, heartbeat
    # ================================================================

    async def acquire_leader(self) -> Optional[bool]:
        if not self._lock_available:
            return None

        now = int(time.time())
        client = self._db()

        try:
            updated = await client.table("bot_lock").update({
                "owner": INSTANCE_ID,
                "heartbeat": now,
            }).eq(
                "id", LOCK_ROW_ID
            ).or_(
                f"owner.eq.{INSTANCE_ID},heartbeat.lt.{now - LOCK_TTL}"
            ).select("*").execute()

            if updated.data:
                return True

            exists = await client.table("bot_lock").select(
                "id"
            ).eq(
                "id", LOCK_ROW_ID
            ).limit(1).execute()

            if exists.data:
                return False

            try:
                await client.table("bot_lock").insert({
                    "id": LOCK_ROW_ID,
                    "owner": INSTANCE_ID,
                    "heartbeat": now,
                }).execute()
                return True
            except Exception:
                return False

        except Exception:
            logger.exception(
                "bot_lock unavailable; refusing unsafe leader acquisition"
            )
            self._lock_available = False
            return None

    async def renew_leader(self) -> str:
        try:
            res = await self._db().table("bot_lock").update({
                "heartbeat": int(time.time()),
            }).eq(
                "id", LOCK_ROW_ID
            ).eq(
                "owner", INSTANCE_ID
            ).select("id").execute()

            return "ok" if res.data else "lost"
        except Exception:
            logger.exception("Leader lease renewal failed")
            return "error"

    async def release_leader(self) -> None:
        if not self._lock_available or self.client is None:
            return

        try:
            await self._db().table("bot_lock").update({
                "heartbeat": 0,
            }).eq(
                "id", LOCK_ROW_ID
            ).eq(
                "owner", INSTANCE_ID
            ).execute()
        except Exception:
            logger.exception("Leader lease release failed")


db = Database()
