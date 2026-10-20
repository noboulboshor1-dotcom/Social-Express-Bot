import asyncio
import html
import logging
import math
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from config import ADMIN_IDS
from database import db
from withdrawal_atomic import review_withdrawal_atomic
from keyboards import get_main_keyboard, safe_edit, withdrawal_review_kb

logger = logging.getLogger(__name__)

USERS_PER_PAGE = 6
_bg_tasks: set[asyncio.Task] = set()


class AdminStates(StatesGroup):
    waiting_for_ref_commission = State()
    waiting_for_daily_bonus = State()
    waiting_for_proof_channel = State()
    waiting_for_broadcast_msg = State()


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user and event.from_user.id in ADMIN_IDS)


# Every handler below is admin-only; non-admin updates fall through to other routers.
admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


def _fmt(x) -> str:
    return f"{float(x):.2f}"


def _mask_id(user_id: int) -> str:
    s = str(user_id)
    return f"{s[:3]}•••{s[-2:]}" if len(s) > 5 else s


# ----------------------------------------------------------------------------
# MAIN PANEL
# ----------------------------------------------------------------------------
def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Manage Task Categories", callback_data="admin_categories")],
        [InlineKeyboardButton(text="👥 User List & Ban Control", callback_data="admin_users_page_1")],
        [InlineKeyboardButton(text="📢 Broadcast Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⚙️ Bot Settings", callback_data="admin_settings")],
        [InlineKeyboardButton(text="💳 Pending Withdrawals", callback_data="admin_withdrawals")],
        [InlineKeyboardButton(text="🚪 Exit Admin Panel", callback_data="admin_exit")],
    ])


async def panel_text() -> str:
    total_users = await db.get_all_users_count()
    pending_withdrawals = await db.count_pending_withdrawals()
    pending_reviews = 0
    try:
        result = await db.client.table("user_submissions").select("id", count="exact").eq("status", "pending").limit(1).execute()
        pending_reviews = int(result.count or 0)
    except Exception:
        logger.exception("Could not count pending reviews for admin panel")
    return (
        "👑 <b>এডমিন কন্ট্রোল প্যানেল</b>\n\n"
        f"👥 মোট রেজিস্টার্ড ইউজার: <b>{total_users} জন</b>\n"
        f"⏳ Pending review: <b>{pending_reviews} টি</b>\n"
        f"💳 পেন্ডিং উইথড্রো: <b>{pending_withdrawals} টি</b>\n\n"
        "নিচের অপশনগুলো থেকে আপনার পছন্দ নির্বাচন করুন:"
    )


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()

    # Remove the user reply-keyboard silently: send an instant throw-away message
    # carrying ReplyKeyboardRemove and delete it, so nothing stays in the chat.
    try:
        tmp = await message.answer("…", reply_markup=ReplyKeyboardRemove(), disable_notification=True)
        await tmp.delete()
    except Exception as e:  # noqa: BLE001
        logger.debug("Silent keyboard removal skipped: %s", e)

    await message.answer(await panel_text(), reply_markup=admin_keyboard())


@admin_router.callback_query(F.data == "admin_back")
async def handle_admin_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, await panel_text(), admin_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "admin_exit")
async def handle_admin_exit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await callback.message.answer("প্রধান মেনু:", reply_markup=get_main_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "none")
async def handle_noop(callback: CallbackQuery):
    await callback.answer()


# ----------------------------------------------------------------------------
# CATEGORIES
# ----------------------------------------------------------------------------
async def categories_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    for cat in await db.get_categories():
        icon = "🟢 চালু" if cat.get("is_enabled") else "🔴 বন্ধ"
        rows.append([InlineKeyboardButton(
            text=f"{cat['category_name']} ({icon})",
            callback_data=f"toggle_cat_{cat['category_key']}",
        )])
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_back")])
    text = "📂 <b>টাস্ক ক্যাটাগরি ম্যানেজমেন্ট:</b>\nযে ক্যাটাগরি অন বা অফ করতে চান তাতে ক্লিক করুন:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data == "admin_categories")
async def handle_admin_categories(callback: CallbackQuery):
    text, kb = await categories_view()
    await safe_edit(callback.message, text, kb)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("toggle_cat_"))
async def handle_toggle_category(callback: CallbackQuery):
    cat_key = callback.data.removeprefix("toggle_cat_")
    new_status = await db.toggle_category(cat_key)
    await callback.answer(f"ক্যাটাগরি {'চালু' if new_status else 'বন্ধ'} করা হয়েছে!")
    text, kb = await categories_view()
    await safe_edit(callback.message, text, kb)


# ----------------------------------------------------------------------------
# USERS: pagination + live Ban/Unban toggle
# ----------------------------------------------------------------------------
async def users_view(page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    users, total = await db.get_users_paginated(page, USERS_PER_PAGE)
    total_pages = max(1, math.ceil(total / USERS_PER_PAGE))
    if page > total_pages:                       # e.g. the last page emptied out
        page = total_pages
        users, total = await db.get_users_paginated(page, USERS_PER_PAGE)
    if not users:
        return None

    text = f"👥 <b>ইউজার ম্যানেজমেন্ট (পেজ {page}/{total_pages} • মোট {total}):</b>\n\n"
    rows = []
    for u in users:
        uid = u["user_id"]
        uname = html.escape(str(u.get("username") or f"User_{uid}"))
        banned = bool(u.get("is_banned"))
        badge = "❌ [Banned]" if banned else "✅ [Active]"
        text += (
            f"• <b>{uname}</b> {badge}\n"
            f"  ID: <code>{uid}</code> | Bal: <b>{_fmt(u.get('balance') or 0)} BDT</b>\n"
        )
        rows.append([
            InlineKeyboardButton(text=f"{'❌' if banned else '✅'} {str(u.get('username') or uid)[:18]}", callback_data="none"),
            InlineKeyboardButton(
                text="🟢 Unban" if banned else "🔴 Ban",
                callback_data=f"toggle_ban_{uid}_{page}",
            ),
        ])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"admin_users_page_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="none"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"admin_users_page_{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_back")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data.startswith("admin_users_page_"))
async def handle_admin_user_list(callback: CallbackQuery):
    try:
        page = max(1, int(callback.data.removeprefix("admin_users_page_")))
    except ValueError:
        await callback.answer()
        return
    view = await users_view(page)
    if not view:
        await callback.answer("⚠️ কোনো ইউজার পাওয়া যায়নি।", show_alert=True)
        return
    await safe_edit(callback.message, *view)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("toggle_ban_"))
async def handle_toggle_ban_inline(callback: CallbackQuery):
    try:
        _, _, uid_s, page_s = callback.data.split("_")
        uid, page = int(uid_s), int(page_s)
    except ValueError:
        await callback.answer("⚠️ অবৈধ রিকোয়েস্ট।", show_alert=True)
        return

    if uid in ADMIN_IDS:
        await callback.answer("⚠️ এডমিনকে ব্যান করা যাবে না।", show_alert=True)
        return

    user = await db.get_user(uid)
    if not user:
        await callback.answer("⚠️ ইউজার খুঁজে পাওয়া যায়নি।", show_alert=True)
        return

    new_ban = not bool(user.get("is_banned"))
    if not await db.set_user_ban_status(uid, new_ban):
        await callback.answer("⚠️ আপডেট ব্যর্থ হয়েছে।", show_alert=True)
        return

    await callback.answer(f"ইউজার {uid} {'ব্যান' if new_ban else 'আনব্যান'} করা হয়েছে ✔️")
    view = await users_view(page)             # re-render text + keyboard instantly
    if view:
        await safe_edit(callback.message, *view)


# ----------------------------------------------------------------------------
# SETTINGS (referral_commission / daily_bonus / proof_channel; withdrawal limits are per method)
# ----------------------------------------------------------------------------
async def settings_view() -> tuple[str, InlineKeyboardMarkup]:
    ref_comm = await db.get_float_setting("referral_commission", 10.0)
    bonus = await db.get_float_setting("daily_bonus", 1.0)
    proof = await db.get_setting("proof_channel", "")

    text = (
        "⚙️ <b>বোট কনফিগারেশন সেটিংস:</b>\n\n"
        f"🎁 রেফারেল কমিশন: <b>{ref_comm:g}%</b>\n"
        f"🎁 ডেইলি বোনাস: <b>{bonus:g} BDT</b>\n"
        f"📢 পেমেন্ট প্রুফ চ্যানেল: <code>{html.escape(proof) if proof else 'সেট করা নেই'}</code>\n\n"
        "💳 Withdrawal method configuration: <code>/withdraw_config METHOD MINIMUM CHARGE open|closed</code>\n\n"
        "<i>পরিবর্তন সাথে সাথে ইউজার প্যানেলে কার্যকর হয়।</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Edit Ref Commission", callback_data="edit_ref_comm")],
        [InlineKeyboardButton(text="✏️ Edit Daily Bonus", callback_data="edit_daily_bonus")],
        [InlineKeyboardButton(text="✏️ Edit Proof Channel", callback_data="edit_proof_channel")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_back")],
    ])
    return text, kb


def _cancel_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Cancel", callback_data="admin_settings")]
    ])


@admin_router.callback_query(F.data == "admin_settings")
async def handle_admin_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await settings_view()
    await safe_edit(callback.message, text, kb)
    await callback.answer()


async def _prompt_edit(callback: CallbackQuery, state: FSMContext, new_state: State, prompt: str):
    await state.set_state(new_state)
    await safe_edit(callback.message, prompt, _cancel_edit_kb())
    await callback.answer()




@admin_router.callback_query(F.data == "edit_ref_comm")
async def handle_edit_ref_comm(callback: CallbackQuery, state: FSMContext):
    await _prompt_edit(callback, state, AdminStates.waiting_for_ref_commission,
                       "✏️ নতুন <b>রেফারেল কমিশন (%)</b> লিখে পাঠান (০ - ১০০):")


@admin_router.callback_query(F.data == "edit_daily_bonus")
async def handle_edit_daily_bonus(callback: CallbackQuery, state: FSMContext):
    await _prompt_edit(callback, state, AdminStates.waiting_for_daily_bonus,
                       "✏️ নতুন <b>ডেইলি বোনাস পরিমাণ (BDT)</b> লিখে পাঠান:")


@admin_router.callback_query(F.data == "edit_proof_channel")
async def handle_edit_proof_channel(callback: CallbackQuery, state: FSMContext):
    await _prompt_edit(
        callback, state, AdminStates.waiting_for_proof_channel,
        "✏️ পেমেন্ট প্রুফ চ্যানেলের ইউজারনেম বা আইডি লিখে পাঠান "
        "(যেমন: <code>@Technova_Proof</code> বা <code>-1001234567890</code>)।\n"
        "বন্ধ করতে <code>-</code> লিখুন। বটকে চ্যানেলে এডমিন করা থাকতে হবে।",
    )


async def _save_number_setting(message: Message, state: FSMContext, key: str, label: str,
                               unit: str, lo: float, hi: float | None):
    try:
        val = float(message.text.strip().replace(",", ""))
        if not math.isfinite(val) or val < lo or (hi is not None and val > hi):
            raise ValueError
    except ValueError:
        rng = f"{lo:g} - {hi:g}" if hi is not None else f"{lo:g} বা তার বেশি"
        await message.answer(f"⚠️ একটি সঠিক সংখ্যা লিখুন ({rng})।", reply_markup=_cancel_edit_kb())
        return

    if not await db.update_setting(key, f"{val:g}"):
        await message.answer("⚠️ সেটিং সেভ করা যায়নি, আবার চেষ্টা করুন।", reply_markup=_cancel_edit_kb())
        return

    await state.clear()
    text, kb = await settings_view()
    await message.answer(f"✅ {label} সফলভাবে <b>{val:g} {unit}</b> সেট করা হয়েছে!\n\n{text}", reply_markup=kb)




@admin_router.message(AdminStates.waiting_for_ref_commission, F.text)
async def process_new_ref_commission(message: Message, state: FSMContext):
    await _save_number_setting(message, state, "referral_commission", "রেফারেল কমিশন", "%", 0, 100)


@admin_router.message(AdminStates.waiting_for_daily_bonus, F.text)
async def process_new_daily_bonus(message: Message, state: FSMContext):
    await _save_number_setting(message, state, "daily_bonus", "ডেইলি বোনাস", "BDT", 0, None)


def _normalize_channel(raw: str) -> str | None:
    value = raw.strip()
    if value == "-":
        return ""
    value = re.sub(r"^https?://t\.me/", "", value, flags=re.I).strip("/")
    if re.fullmatch(r"-100\d{5,}", value):
        return value
    value = value.lstrip("@")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
        return "@" + value
    return None


@admin_router.message(AdminStates.waiting_for_proof_channel, F.text)
async def process_new_proof_channel(message: Message, state: FSMContext):
    channel = _normalize_channel(message.text)
    if channel is None:
        await message.answer("⚠️ সঠিক ইউজারনেম (@name) বা আইডি (-100...) দিন।", reply_markup=_cancel_edit_kb())
        return
    if not await db.update_setting("proof_channel", channel):
        await message.answer("⚠️ সেটিং সেভ করা যায়নি, আবার চেষ্টা করুন।", reply_markup=_cancel_edit_kb())
        return

    await state.clear()
    warning = ""
    if channel:
        try:
            chat_id = int(channel) if channel.startswith("-") else channel
            member = await message.bot.get_chat_member(chat_id, (await message.bot.me()).id)
            if member.status not in ("administrator", "creator"):
                warning = "\n\n⚠️ বট এই চ্যানেলের এডমিন নয় - পোস্ট করতে পারবে না।"
        except Exception:  # noqa: BLE001
            warning = "\n\n⚠️ চ্যানেলটি যাচাই করা যায়নি - বটকে এডমিন করা আছে কিনা দেখুন।"

    text, kb = await settings_view()
    shown = html.escape(channel) if channel else "বন্ধ"
    await message.answer(f"✅ পেমেন্ট প্রুফ চ্যানেল: <b>{shown}</b>{warning}\n\n{text}", reply_markup=kb)


# ----------------------------------------------------------------------------
# BROADCAST (runs in the background so the bot keeps serving users)
# ----------------------------------------------------------------------------
@admin_router.callback_query(F.data == "admin_broadcast")
async def handle_broadcast_click(callback: CallbackQuery, state: FSMContext):
    await _prompt_edit(
        callback, state, AdminStates.waiting_for_broadcast_msg,
        "📢 সমস্ত ইউজারের কাছে পাঠানোর জন্য আপনার ব্রডকাস্ট মেসেজটি পাঠান (টেক্সট/ছবি/ভিডিও):",
    )
    # the cancel button of _prompt_edit goes to settings; point it back to the panel instead
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Cancel", callback_data="admin_back")]
    ]))


async def _run_broadcast(bot, admin_chat: int, from_chat: int, message_id: int, user_ids: list[int]):
    sent = failed = 0
    for uid in user_ids:
        for attempt in range(2):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=message_id)
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                failed += 1
                break
            except Exception:  # noqa: BLE001
                failed += 1
                break
        await asyncio.sleep(0.05)            # ~20 msg/s, under Telegram's global limit
    try:
        await bot.send_message(
            admin_chat,
            f"✅ ব্রডকাস্ট সম্পন্ন!\n📤 সফল: <b>{sent}</b>\n🚫 ব্যর্থ/ব্লক: <b>{failed}</b>",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not send broadcast report: %s", e)


@admin_router.message(AdminStates.waiting_for_broadcast_msg)
async def process_broadcast_send(message: Message, state: FSMContext):
    await state.clear()
    user_ids = await db.get_broadcast_user_ids()
    await message.answer(f"⏳ মোট <b>{len(user_ids)}</b> জন ইউজারের কাছে মেসেজ পাঠানো শুরু হয়েছে...")
    task = asyncio.create_task(
        _run_broadcast(message.bot, message.chat.id, message.chat.id, message.message_id, user_ids)
    )
    _bg_tasks.add(task)                       # keep a reference so it is not garbage-collected
    task.add_done_callback(_bg_tasks.discard)


# ----------------------------------------------------------------------------
# WITHDRAWAL REVIEW
# ----------------------------------------------------------------------------
def _withdrawal_text(req: dict) -> str:
    return (
        f"🧾 <b>Request #{req['id']}</b>\n"
        f"👤 ইউজার আইডি: <code>{req['user_id']}</code>\n"
        f"💰 পরিমাণ: <b>{_fmt(req['amount'])} BDT</b>\n"
        f"💳 মেথড: <b>{html.escape(str(req['method']))}</b>\n"
        f"📱 অ্যাকাউন্ট: <code>{html.escape(str(req['account_no']))}</code>"
    )


@admin_router.callback_query(F.data == "admin_withdrawals")
async def handle_admin_withdrawals(callback: CallbackQuery):
    withdrawals, total = await db.get_pending_withdrawals(limit=20)
    if not withdrawals:
        await callback.answer("⚠️ বর্তমানে কোনো পেন্ডিং উইথড্রো রিকোয়েস্ট নেই।", show_alert=True)
        return

    note = f" (প্রথম {len(withdrawals)} টি, মোট {total})" if total > len(withdrawals) else f" ({total})"
    await safe_edit(
        callback.message,
        f"💳 <b>পেন্ডিং উইথড্রো রিকোয়েস্টসমূহ{note}:</b>",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="admin_back")]]),
    )
    for req in withdrawals:
        await callback.message.answer(_withdrawal_text(req), reply_markup=withdrawal_review_kb(req["id"]))
    await callback.answer()


async def _post_proof(bot, req: dict) -> None:
    channel = (await db.get_setting("proof_channel", "")).strip()
    if not channel:
        return
    chat_id = int(channel) if channel.startswith("-") else channel
    text = (
        "🎉 <b>New Successful Withdrawal!</b>\n\n"
        f"👤 User ID: <code>{_mask_id(req['user_id'])}</code>\n"
        f"💰 Amount: <b>{_fmt(req['amount'])} BDT</b>\n"
        f"💳 Method: <b>{html.escape(str(req['method']))}</b>\n"
        "✅ Status: <b>Paid / Approved</b>\n\n"
        "🤖 Join Bot & Start Earning Now!"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:  # noqa: BLE001
        logger.error("Error posting proof to %s: %s", channel, e)


@admin_router.callback_query(F.data.regexp(r"^(approve|reject)_wd_\d+$"))
async def handle_withdrawal_action(callback: CallbackQuery):
    action, _, req_id_s = callback.data.split("_")
    status = "approved" if action == "approve" else "rejected"

    ok, req = await review_withdrawal_atomic(int(req_id_s), status, admin_id=callback.from_user.id)
    if not ok or not req:
        # already handled by another admin (or a refund failure) - drop the stale buttons
        await callback.answer("⚠️ রিকোয়েস্টটি ইতিমধ্যে প্রসেস হয়েছে বা আপডেট করা যায়নি।", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        return

    user_id, amount, method = req["user_id"], float(req["amount"]), req["method"]
    bot = callback.bot

    if status == "approved":
        verdict = "✅ উইথড্রো এপ্রুভ হয়েছে।"
        try:
            await bot.send_message(
                user_id,
                "🎉 <b>আপনার উইথড্রো সফল হয়েছে!</b>\n\n"
                f"💰 পরিমাণ: <b>{_fmt(amount)} BDT</b>\n"
                f"💳 মাধ্যম: <b>{html.escape(str(method))}</b>\n\n"
                "✅ আপনার পেমেন্ট সফলভাবে পাঠিয়ে দেওয়া হয়েছে। পেমেন্ট প্রুফ চ্যানেল চেক করুন!",
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Error sending approval DM to %s: %s", user_id, e)
        await _post_proof(bot, req)
    else:
        verdict = "❌ উইথড্রো রিজেক্ট এবং টাকা রিফান্ড করা হয়েছে।"
        user = await db.get_user(user_id) or {}
        try:
            await bot.send_message(
                user_id,
                "❌ <b>আপনার উইথড্রো রিকোয়েস্ট রিজেক্ট করা হয়েছে!</b>\n\n"
                f"💰 <b>{_fmt(amount)} BDT</b> আপনার ব্যালেন্সে ফিরিয়ে দেওয়া হয়েছে।\n"
                f"💵 বর্তমান ব্যালেন্স: <b>{_fmt(user.get('balance') or 0)} BDT</b>\n\n"
                "⚠️ ভুল অ্যাকাউন্ট নম্বর বা অন্য কোনো সমস্যার কারণে এটি হতে পারে। "
                "দয়া করে সঠিক তথ্য দিয়ে আবার উইথড্রো করুন।",
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Error sending rejection DM to %s: %s", user_id, e)

    await safe_edit(callback.message, f"{callback.message.html_text}\n\n{verdict}")
    await callback.answer(verdict)


# ----------------------------------------------------------------------------
# BULK TASKS
# ----------------------------------------------------------------------------


