import asyncio
import contextlib
import html
import io
import logging
import signal
import time
from urllib.parse import quote

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import web

from admin import admin_router
from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    LOCK_RENEW_SECONDS,
    LOCK_RETRY_SECONDS,
    LOCK_TTL,
    PORT,
    SUPPORT_USERNAME,
)
from database import db
from keyboards import (
    BTN_BALANCE,
    BTN_BONUS,
    BTN_CANCEL,
    BTN_NEW,
    BTN_REFERRALS,
    BTN_SUPPORT,
    BTN_TASKS,
    MENU_BUTTONS,
    get_cancel_keyboard,
    get_main_keyboard,
    safe_edit,
)
from social_tasks import social_router
from profile_dashboard import profile_router
from gift_codes import gift_router
from review_csv import review_csv_router
from withdrawal_settings import withdraw_settings_router
from withdraw import withdraw_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

router = Router(name="user")




def _fmt(x) -> str:
    return f"{float(x):.2f}"




# ----------------------------------------------------------------------------
# MIDDLEWARES
# ----------------------------------------------------------------------------
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and user.id not in ADMIN_IDS and await db.is_banned(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 আপনার অ্যাকাউন্টটি ব্যান করা হয়েছে!", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("🚫 <b>আপনার অ্যাকাউন্টটি ব্যান করা হয়েছে!</b>")
            return None
        return await handler(event, data)


class MenuResetMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        if event.text in MENU_BUTTONS:
            state: FSMContext | None = data.get("state")
            if state is not None:
                await state.clear()
        return await handler(event, data)


# ----------------------------------------------------------------------------
# START / MENU
# ----------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    referred_by = None
    if command.args and command.args.strip().isdigit():
        ref_id = int(command.args.strip())
        if ref_id != user_id:
            referred_by = ref_id

    await db.get_or_create_user(user_id=user_id, username=username, referred_by=referred_by)

    await message.answer(
        f"🌟 <b>স্বাগতম, {html.escape(message.from_user.first_name)}!</b>\n\n"
        "🚀 <b>মাইক্রো-টাস্ক ও আর্নিং প্ল্যাটফর্মে স্বাগতম।</b>\n"
        "অনুমোদিত কাজ সম্পন্ন করে public proof URL জমা দিন; admin review শেষে প্রযোজ্য reward যোগ হবে।\n\n"
        "📌 <b>মূল বৈশিষ্ট্যসমূহ:</b>\n"
        "▪️ Proof-based task submission ও review\n"
        "▪️ Profile dashboard, gift codes এবং referral income\n"
        "▪️ Admin-reviewed withdrawals\n\n"
        "👇 কাজ শুরু করতে নিচের মেনু থেকে অপশন নির্বাচন করুন:",
        reply_markup=get_main_keyboard(),
    )


@router.message(F.text == BTN_CANCEL)
async def handle_cancel_button(message: Message, state: FSMContext):
    await state.clear()
    await db.release_user_task(message.from_user.id)
    await message.answer("❌ কাজ বাতিল করা হয়েছে।", reply_markup=get_main_keyboard())


@router.message(F.text == BTN_BONUS)
async def handle_daily_bonus(message: Message):
    _, msg, _ = await db.claim_daily_bonus(message.from_user.id)
    await message.answer(msg, reply_markup=get_main_keyboard())




@router.message(F.text == BTN_REFERRALS)
async def handle_referral(message: Message):
    user_id = message.from_user.id
    user = await db.get_or_create_user(user_id, message.from_user.username or "")
    me = await message.bot.me()
    ref_link = f"https://t.me/{me.username}?start={user_id}"
    pct = await db.get_float_setting("referral_commission", 10.0)

    share_url = f"https://t.me/share/url?url={quote(ref_link, safe='')}&text={quote('Join this amazing earning bot!')}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌐 Share Now", url=share_url)]])
    await message.answer(
        "🎁 <b>My Referrals</b>\n"
        f"👥 Total Refer: <b>{int(user.get('referral_count') or 0)}</b>\n"
        f"💰 Total Refer Income: <b>{float(user.get('referral_income') or 0):.4f} BDT</b>\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{ref_link}</code>\n\n"
        f"ℹ️ You will receive <b>{pct:g}% commission</b> on earnings from each task completed by your referrals.",
        reply_markup=kb,
    )


@router.message(F.text == BTN_SUPPORT)
async def handle_support(message: Message):
    await message.answer(
        "🎧 <b>সাপোর্ট ও হেল্প সেন্টার</b>\n\n"
        "যেকোনো প্রয়োজনে আমাদের সাপোর্ট টিমের সাথে যোগাযোগ করুন:\n"
        f"👉 এডমিন সাপোর্ট: {html.escape(SUPPORT_USERNAME)}",
        reply_markup=get_main_keyboard(),
    )


@router.message(F.text == BTN_NEW)
async def handle_iam_new(message: Message):
    await message.answer(
        "💡 <b>নতুনদের জন্য নির্দেশিকা:</b>\n\n"
        "১. অনুমোদিত micro-task সম্পন্ন করুন।\n"
        "২. <code>/submitproof website_feedback https://example.org/proof</code> দিয়ে proof URL জমা দিন।\n"
        "৩. Review-এর অবস্থা My Profile থেকে দেখুন।\n"
        "৪. প্রতিদিন <b>Daily Bonus</b> দাবি করুন!",
        reply_markup=get_main_keyboard(),
    )


# ----------------------------------------------------------------------------
# TASKS & CATEGORY SELECTION
# ----------------------------------------------------------------------------
SAFE_TASK_CATEGORIES = frozenset({"website_feedback", "content_review", "data_labeling", "qa_testing"})


@router.message(F.text == BTN_TASKS)
async def handle_tasks_menu(message: Message):
    categories = [
        cat for cat in await db.get_categories()
        if cat.get("is_enabled") and cat.get("category_key") in SAFE_TASK_CATEGORIES
    ]
    if not categories:
        await message.answer(
            "📋 নিরাপদ proof-based task জমা দিতে এই command ব্যবহার করুন:\n"
            "<code>/submitproof website_feedback https://example.org/proof</code>\n\n"
            "অনুমোদিত category: <code>website_feedback</code>, <code>content_review</code>, "
            "<code>data_labeling</code>, <code>qa_testing</code>",
            reply_markup=get_main_keyboard(),
        )
        return
    rows = [[InlineKeyboardButton(text=cat["category_name"], callback_data=f"cat_task_{cat['category_key']}")] for cat in categories]
    await message.answer("🎯 Category নির্বাচন করুন; এরপর /submitproof দিয়ে public HTTPS proof URL জমা দিন:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("cat_task_"))
async def handle_category_selection(callback: CallbackQuery):
    category = callback.data.removeprefix("cat_task_")
    if category not in SAFE_TASK_CATEGORIES:
        await callback.answer("এই task category চালু নেই।", show_alert=True)
        return
    await callback.message.answer(
        f"Category: <code>{category}</code>\n"
        f"ব্যবহার করুন: <code>/submitproof {category} https://example.org/proof</code>"
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_task")
async def handle_cancel_task(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await db.release_user_task(callback.from_user.id)
    await safe_edit(callback.message, "❌ টাস্কটি বাতিল করা হয়েছে।")
    await callback.message.answer("প্রধান মেনু:", reply_markup=get_main_keyboard())
    await callback.answer()


# --- Gmail verification ------------------------------------------------------


# --- Facebook Submissions & Admin Review Flow ---------------------------------






# --- Instagram Submissions & Admin Review Flow --------------------------------




# ----------------------------------------------------------------------------
# ADMIN ACCEPT / REJECT & FILE EXPORT (/export_accounts)
# ----------------------------------------------------------------------------








# ----------------------------------------------------------------------------
# UNKNOWN COMMAND FALLBACK HANDLER
# ----------------------------------------------------------------------------
@router.message()
async def unknown_command_handler(message: Message):
    await message.answer(
        "┏ ⚠️ <b>অপরিচিত কমান্ড বা বার্তা!</b>\n"
        "┃\n"
        "┣ ❌ দুঃখিত, আপনার পাঠানো লেখা বা কমান্ডটি আমি বুঝতে পারিনি।\n"
        "┗ 💡 দয়া করে নিচের মেনু ব্যবহার করুন অথবা বট রিফ্রেশ করতে /start লিখুন।",
        parse_mode="HTML"
    )


# ----------------------------------------------------------------------------
# GLOBAL ERROR HANDLER
# ----------------------------------------------------------------------------
async def on_error(event: ErrorEvent) -> bool:
    logger.error("Unhandled error: %s", event.exception, exc_info=event.exception)
    update = event.update
    with contextlib.suppress(Exception):
        if update.callback_query:
            await update.callback_query.answer("⚠️ একটি সমস্যা হয়েছে, আবার চেষ্টা করুন।", show_alert=True)
        elif update.message:
            await update.message.answer("⚠️ একটি সমস্যা হয়েছে, আবার চেষ্টা করুন।")
    return True


# ----------------------------------------------------------------------------
# WEB SERVER (Render health check)
# ----------------------------------------------------------------------------
async def handle_web(_request: web.Request) -> web.Response:
    return web.Response(text="Micro-task bot healthy")


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_web)
    app.router.add_get("/healthz", handle_web)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("🌐 Server running on port %s", PORT)
    return runner


# ----------------------------------------------------------------------------
# SINGLE-INSTANCE POLLING
# ----------------------------------------------------------------------------
def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())
    dp.message.outer_middleware(MenuResetMiddleware())
    dp.errors.register(on_error)
    dp.include_router(admin_router)
    dp.include_router(withdraw_router)
    dp.include_router(profile_router)
    dp.include_router(gift_router)
    dp.include_router(review_csv_router)
    dp.include_router(withdrawal_settings_router)
    dp.include_router(social_router)  # safe proof-only shim; no credential collection
    dp.include_router(router)
    return dp


async def leader_keeper(dp: Dispatcher, lost: asyncio.Event) -> None:
    last_ok = time.monotonic()
    while True:
        await asyncio.sleep(LOCK_RENEW_SECONDS)
        state = await db.renew_leader()
        now = time.monotonic()
        if state == "ok":
            last_ok = now
        elif state == "lost" or now - last_ok > LOCK_TTL - LOCK_RENEW_SECONDS:
            logger.error("Lost the polling lease (%s) - stopping polling.", state)
            lost.set()
            with contextlib.suppress(Exception):
                await dp.stop_polling()
            return


async def run_polling(bot: Bot, dp: Dispatcher, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        acquired = await db.acquire_leader()
        if acquired is False:
            logger.info("Another instance is polling - waiting for the lease...")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=LOCK_RETRY_SECONDS)
            continue

        lost = asyncio.Event()
        keeper = asyncio.create_task(leader_keeper(dp, lost)) if acquired else None
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            logger.info("🚀 Starting bot polling...")
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                close_bot_session=False,
            )
        except TelegramConflictError:
            logger.error("TelegramConflictError: another poller/webhook is active. Retrying in 10s.")
            await asyncio.sleep(10)
            continue
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            continue
        except TelegramNetworkError as e:
            logger.error("Network error: %s. Retrying in 5s.", e)
            await asyncio.sleep(5)
            continue
        except Exception:
            logger.exception("Polling crashed. Restarting in 5s.")
            await asyncio.sleep(5)
            continue
        finally:
            if keeper:
                keeper.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await keeper

        if not lost.is_set():
            break
        logger.warning("Lease lost - going back to the queue.")


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)

    runner = await start_web_server()
    try:
        await db.connect()
        logger.info("Safe proof-based micro-task bot starting.")
        await run_polling(bot, dp, stop_event)
    finally:
        await db.release_leader()
        await bot.session.close()
        await runner.cleanup()
        logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped!")
