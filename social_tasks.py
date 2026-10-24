import io
import os
import csv
import logging
import html
import random

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database import db
from keyboards import get_main_keyboard, get_cancel_keyboard, BTN_TASKS, safe_edit, MENU_BUTTONS, BTN_CANCEL
from config import ADMIN_IDS, ADMIN_GROUP_ID, CATEGORY_REWARDS
from tasks import task_manager
from verifier import inbox_verifier, API_ERROR, UNSUPPORTED

logger = logging.getLogger(__name__)
social_router = Router(name="social_tasks")
_verifying: set[int] = set()

os.makedirs('data', exist_ok=True)

# ফেসবুক নামের জন্য ডামি লিস্ট
FIRST_NAMES = ["John", "Michael", "Emma", "Olivia", "James", "William", "Sophia", "Ava", "David", "Richard", "Joseph", "Charles", "Thomas", "Daniel"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson"]

def append_to_csv(filename, row):
    file_path = f"data/{filename}"
    file_exists = os.path.exists(file_path)
    with open(file_path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            if filename == 'accounts_master.csv': writer.writerow(["Username", "Password", "2FA Key"])
            elif filename == 'gmail_master.csv': writer.writerow(["Email", "Password"])
            elif filename == 'facebook_master.csv': writer.writerow(["UID", "Password", "2FA Key"])
            elif filename == 'facebook_cookies_master.csv': writer.writerow(["UID", "Password", "Cookies"])
            elif filename == 'instagram_cookies_master.csv': writer.writerow(["Username", "Password", "Cookies"])
        writer.writerow(row)

def parse_credentials(stock_string):
    if "|" in stock_string:
        u, p = stock_string.split("|", 1)
        return u.strip(), p.strip()
    elif ":" in stock_string:
        u, p = stock_string.split(":", 1)
        return u.strip(), p.strip()
    return stock_string, "N/A"

class SocialTaskStates(StatesGroup):
    waiting_for_stock_data = State()
    working_on_gmail = State()
    waiting_for_fb_uid = State()
    waiting_for_fb_2fa = State()
    waiting_for_fb_cookies_uid = State()
    waiting_for_fb_cookies = State()
    waiting_for_insta_2fa = State()
    waiting_for_insta_cookies = State()
    waiting_for_reject_reason = State()

# --- ADMIN BULK STOCK UPLOAD (/addstock) ---
@social_router.message(Command("addstock"))
async def handle_addstock_start(message: Message, command: CommandObject, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    args = command.args.strip().lower() if command.args else ""
    if args not in ["facebook", "instagram", "gmail"]:
        await message.answer("⚠️ সঠিক নিয়মে ব্যবহার করুন:\n👉 <code>/addstock facebook</code>\n👉 <code>/addstock instagram</code>\n👉 <code>/addstock gmail</code>")
        return
    
    await state.set_state(SocialTaskStates.waiting_for_stock_data)
    await state.update_data(stock_category=args)
    
    if args == "gmail":
        format_msg = "📌 <b>ফরম্যাট:</b>\n<code>email|password|recovery_email</code>\n\n(প্রতি লাইনে একটি করে)"
    else:
        format_msg = "📌 <b>ফরম্যাট:</b>\n<code>ID|Password</code>\n\n(প্রতি লাইনে একটি করে)"

    await message.answer(
        f"📂 নির্বাচিত ক্যাটাগরি: <b>{args.upper()}</b>\n\n"
        f"📝 এখন আপনার একাউন্ট বা টাস্কগুলোর লিস্ট কপি করে পাঠিয়ে দিন:\n\n{format_msg}",
        parse_mode="HTML"
    )

@social_router.message(SocialTaskStates.waiting_for_stock_data, F.text)
async def process_stock_text_input(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    data = await state.get_data()
    category = data.get("stock_category")
    await state.clear()
    
    lines = message.text.strip().split('\n')
    added_count = 0
    
        if category == "gmail":
        task_list = []
        for line in lines:
            parts = line.strip().split('|')
            if len(parts) >= 2: # এখন অন্তত ২টি অংশ (ইমেইল ও পাসওয়ার্ড) পেলেই কাজ করবে
                email = parts[0].strip()
                password = parts[1].strip()
                
                # যদি রিকভারি মেইল দেওয়া থাকে তবে সেটি নেবে, না থাকলে অটোমেটিক রেন্ডম মেইল বানিয়ে নেবে
                if len(parts) >= 3 and parts[2].strip():
                    recovery = parts[2].strip()
                else:
                    recovery = f"user_{random.randint(100000, 999999)}@mail.tm" # অটো রিকভারি
                
                task_list.append({
                    "email": email,
                    "password": password,
                    "recovery": recovery,
                    "reward": CATEGORY_REWARDS.get("gmail", 10.0),
                    "status": "available"
                })
        if task_list:
            added_count = await db.add_bulk_tasks(task_list)

    else:
        for line in lines:
            line_data = line.strip()
            if line_data:
                await db.add_stock_account(category=category, account_data=line_data)
                added_count += 1
            
    await message.answer(
        f"✅ <b>সফলভাবে স্টক যুক্ত হয়েছে!</b>\n\n"
        f"📂 ক্যাটাগরি: <code>{category.upper()}</code>\n"
        f"🔢 মোট যুক্ত হয়েছে: <code>{added_count} টি</code>",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


# --- TASK MENUS ---
@social_router.message(F.text == BTN_TASKS)
async def handle_tasks_menu(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📘 Facebook Tasks", callback_data="show_fb_tasks")],
        [InlineKeyboardButton(text="📧 Gmail Tasks", callback_data="show_gmail_tasks")],
        [InlineKeyboardButton(text="📸 Instagram Tasks", callback_data="show_insta_tasks")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_task")]
    ])
    await message.answer("🎯 <b>কাজের ক্যাটাগরি নির্বাচন করুন:</b>", reply_markup=kb, parse_mode="HTML")

@social_router.callback_query(F.data == "cancel_task")
async def handle_cancel_task(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await db.release_user_task(callback.from_user.id)
    await callback.message.edit_text("❌ <b>টাস্কটি বাতিল করা হয়েছে।</b>", parse_mode="HTML")
    await callback.message.answer("প্রধান মেনু:", reply_markup=get_main_keyboard())
    await callback.answer()

@social_router.callback_query(F.data == "show_fb_tasks")
async def show_fb_tasks(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📘 Facebook 2FA", callback_data="cat_task_facebook")],
        [InlineKeyboardButton(text="🍪 Facebook Cookies", callback_data="cat_task_fb_cookies")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="back_to_tasks")]
    ])
    await callback.message.edit_text("📘 <b>Facebook Tasks:</b>\nআপনার পছন্দের কাজটি নির্বাচন করুন:", reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@social_router.callback_query(F.data == "show_insta_tasks")
async def show_insta_tasks(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Instagram 2FA", callback_data="cat_task_instagram")],
        [InlineKeyboardButton(text="🍪 Instagram Cookies", callback_data="cat_task_insta_cookies")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="back_to_tasks")]
    ])
    await callback.message.edit_text("📸 <b>Instagram Tasks:</b>\nআপনার পছন্দের কাজটি নির্বাচন করুন:", reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@social_router.callback_query(F.data == "back_to_tasks")
async def back_to_tasks(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📘 Facebook Tasks", callback_data="show_fb_tasks")],
        [InlineKeyboardButton(text="📧 Gmail Tasks", callback_data="show_gmail_tasks")],
        [InlineKeyboardButton(text="📸 Instagram Tasks", callback_data="show_insta_tasks")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_task")]
    ])
    await callback.message.edit_text("🎯 <b>কাজের ক্যাটাগরি নির্বাচন করুন:</b>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()

# --- GMAIL SMART VERIFY FEATURE ---
def _gmail_task_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Verify Task", callback_data="verify_task")],
        [InlineKeyboardButton(text="❌ Cancel Task", callback_data="cancel_task")],
    ])

def _gmail_task_text(task: dict) -> str:
    first, last = task_manager.display_names(task)
    reward_amount = CATEGORY_REWARDS.get("gmail", 10.0)
    return (
        "✏️ <b>নিচে দেওয়া তথ্য দিয়ে জিমেইল একাউন্ট তৈরি করুন:</b>\n\n"
        f"👤 <b>First Name:</b> <code>{html.escape(first)}</code>\n"
        f"👤 <b>Last Name:</b> <code>{html.escape(last)}</code>\n"
        f"📧 <b>Email:</b> <code>{html.escape(task['email'])}</code>\n"
        f"🔑 <b>Password:</b> <code>{html.escape(task['password'])}</code>\n\n"
        f"🛡️ <b>Recovery Email:</b>\n<code>{html.escape(task['recovery'])}</code>\n"
        f"💰 <b>রিওয়ার্ড:</b> <code>{reward_amount:.2f} BDT</code>"
    )

@social_router.callback_query(F.data == "show_gmail_tasks")
async def show_gmail_tasks_flow(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    task = await db.claim_task(user_id, fallback_task=await task_manager.generate_random_gmail())
    if not task:
        await callback.answer("⚠️ টাস্ক লোড করতে সমস্যা হয়েছে।", show_alert=True)
        return
    await state.set_state(SocialTaskStates.working_on_gmail)
    await safe_edit(callback.message, _gmail_task_text(task), _gmail_task_kb())
    await callback.message.answer("⚠️ সঠিকভাবে কাজটি সম্পাদন করুন!", reply_markup=get_cancel_keyboard())
    await callback.answer()

@social_router.callback_query(F.data == "verify_task")
async def handle_verify_task(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id in _verifying:
        await callback.answer("⏳ ভেরিফিকেশন চলছে, অপেক্ষা করুন...")
        return
    _verifying.add(user_id)
    try:
        task = await db.get_user_assigned_task(user_id)
        if not task:
            await state.clear()
            await callback.answer("⚠️ টাস্কটির মেয়াদ শেষ হয়ে গেছে।", show_alert=True)
            await safe_edit(callback.message, "⚠️ টাস্কটির মেয়াদ শেষ হয়ে গেছে।")
            return

        await callback.answer()
        await safe_edit(callback.message, "🔍 ইনবক্স চেক করা হচ্ছে... দয়া করে অপেক্ষা করুন ⏳")
        verified, code = await inbox_verifier.check_inbox(task["recovery"], task["email"])

        if not verified:
            if code == API_ERROR:
                sub_id = await db.add_submission(user_id, "gmail", f"Email: {task['email']} | Pass: {task['password']}")
                await state.clear()
                
                await safe_edit(
                    callback.message,
                    "⏳ <b>আপনার কাজটি ম্যানুয়াল রিভিউর জন্য জমা নেওয়া হয়েছে!</b>\n\n"
                    "অটো-ভেরিফিকেশন সার্ভার সাময়িক ডাউন থাকায় এটি অ্যাডমিন দ্বারা চেক করা হবে। রিভিউর পর ব্যালেন্স যুক্ত হবে।"
                )
                await callback.message.answer("প্রধান মেনু:", reply_markup=get_main_keyboard())
                
                admin_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Accept", callback_data=f"app_sub_{sub_id}_{user_id}_gmail"),
                     InlineKeyboardButton(text="❌ Reject", callback_data=f"rej_sub_{sub_id}_{user_id}_gmail")]
                ])
                try:
                    await callback.bot.send_message(
                        ADMIN_GROUP_ID,
                        f"📥 <b>নতুন টাস্ক (Gmail)</b>\n"
                        f"📧 Email: <code>{task['email']}</code>\n"
                        f"🔑 Pass: <code>{task['password']}</code>\n"
                        f"🆔 User ID: {user_id}",
                        reply_markup=admin_kb
                    )
                except Exception as e:
                    logger.error("Could not send to admin group: %s", e)
                return
            
            # ভেরিফাই ব্যর্থ হলে ম্যানুয়াল সাবমিট বাটন দেখানো
            reason = "গুগল থেকে রিকভারি মেইলে কনফার্মেশন মেসেজ আসেনি। আপনি চাইলে অ্যাডমিনকে ম্যানুয়ালি চেক করার জন্য পাঠাতে পারেন।"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 আবার চেক করুন", callback_data="verify_task")],
                [InlineKeyboardButton(text="📩 ম্যানুয়াল সাবমিট", callback_data="manual_submit_gmail")],
                [InlineKeyboardButton(text="❌ Cancel Task", callback_data="cancel_task")]
            ])
            await safe_edit(callback.message, _gmail_task_text(task) + f"\n\n❌ <b>ভেরিফিকেশন ব্যর্থ!</b>\n{reason}", kb)
            return

        result = await db.complete_task(task["id"], user_id)
        await state.clear()
        if not result:
            await safe_edit(callback.message, "⚠️ টাস্ক কমপ্লিট করতে সমস্যা হয়েছে।")
            return

        append_to_csv('gmail_master.csv', [task["email"], task["password"]])

        await safe_edit(
            callback.message,
            "🎉 <b>অভিনন্দন! আপনার টাস্ক সফলভাবে ভেরিফাই হয়েছে।</b>\n\n"
            f"💰 ব্যালেন্সে যোগ হয়েছে: <b>+{result['reward']:g} BDT</b>",
        )
        await callback.message.answer("প্রধান মেনু:", reply_markup=get_main_keyboard())

        if result["referrer_id"] and result["commission"] > 0:
            try:
                await callback.bot.send_message(result["referrer_id"], f"🎁 রেফারেল কমিশন: <b>+{result['commission']:.4f} BDT</b>")
            except: pass
    finally:
        _verifying.discard(user_id)

# --- ম্যানুয়াল সাবমিট বাটন হ্যান্ডলার ---
@social_router.callback_query(F.data == "manual_submit_gmail")
async def handle_manual_submit_gmail(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    task = await db.get_user_assigned_task(user_id)
    if not task:
        await callback.answer("⚠️ টাস্ক পাওয়া যায়নি বা বাতিল হয়ে গেছে।", show_alert=True)
        return
        
    sub_id = await db.add_submission(user_id, "gmail", f"Email: {task['email']} | Pass: {task['password']}")
    
    # টাস্কটি স্টকে যেন ফেরত না যায় তাই 'submitted' স্ট্যাটাস দেওয়া হলো
    client = db._db()
    await client.table("tasks").update({"status": "submitted"}).eq("id", task["id"]).execute()
    
    await state.clear()
    await safe_edit(callback.message, "⏳ <b>আপনার কাজটি ম্যানুয়াল রিভিউর জন্য জমা নেওয়া হয়েছে!</b>\n\nঅ্যাডমিন এটি চেক করে একসেপ্ট করলে ব্যালেন্স যুক্ত হবে।")
    await callback.message.answer("প্রধান মেনু:", reply_markup=get_main_keyboard())
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Accept", callback_data=f"app_sub_{sub_id}_{user_id}_gmail"),
         InlineKeyboardButton(text="❌ Reject", callback_data=f"rej_sub_{sub_id}_{user_id}_gmail")]
    ])
    try:
        await callback.bot.send_message(ADMIN_GROUP_ID, f"📥 <b>নতুন টাস্ক (Gmail - Manual)</b>\n📧 Email: <code>{task['email']}</code>\n🔑 Pass: <code>{task['password']}</code>\n🆔 User ID: {user_id}", reply_markup=admin_kb)
    except: pass
    await callback.answer()


# --- FACEBOOK AND INSTAGRAM TASK SELECTION ---
@social_router.callback_query(F.data.startswith("cat_task_"))
async def handle_category_selection(callback: CallbackQuery, state: FSMContext):
    cat_key = callback.data.removeprefix("cat_task_")
    user_id = callback.from_user.id

    if cat_key == "facebook":
        stock_item = await db.get_unused_stock("facebook")
        if not stock_item:
            await callback.answer("⚠️ বর্তমানে ফেসবুকের কোনো কাজ স্টকে নেই।", show_alert=True)
            return

        account_info, password = parse_credentials(stock_item['account_data'])
        await db.mark_stock_as_used(stock_item['id'], user_id=user_id)
        fb_reward = CATEGORY_REWARDS.get("facebook") or 4.0
        
        f_name, l_name = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
        
        await state.set_state(SocialTaskStates.waiting_for_fb_uid)
        await state.update_data(fb_user=account_info, fb_pass=password)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Send UID", callback_data="prompt_fb_uid")],
            [InlineKeyboardButton(text="❌ Cancel Task", callback_data="cancel_task")]
        ])
        text = (
            "📘 <b>Facebook Account Task</b>\n\n"
            f"👤 <b>First Name:</b> <code>{f_name}</code>\n"
            f"👤 <b>Last Name:</b> <code>{l_name}</code>\n"
            f"📱 <b>Account / ID:</b> <code>{html.escape(account_info)}</code>\n"
            f"🔑 <b>Password:</b> <code>{html.escape(password)}</code>\n\n"
            f"💰 <b>রিওয়ার্ড:</b> <code>{fb_reward:.2f} BDT</code>\n\n"
            "📌 ওপরের তথ্য দিয়ে ফেসবুক একাউন্টে লগইন করে 2FA সেট করুন। তারপর নিচে <b>Send UID</b> বাটনে ক্লিক করুন:"
        )
        await safe_edit(callback.message, text, kb)
        await callback.message.answer("⚠️ ইউআইডি টাইপ করে পাঠাতে পারেন:", reply_markup=get_cancel_keyboard())

    elif cat_key == "fb_cookies":
        stock_item = await db.get_unused_stock("facebook")
        if not stock_item:
            await callback.answer("⚠️ বর্তমানে ফেসবুকের কোনো কাজ স্টকে নেই।", show_alert=True)
            return

        account_info, password = parse_credentials(stock_item['account_data'])
        await db.mark_stock_as_used(stock_item['id'], user_id=user_id)
        fb_reward = CATEGORY_REWARDS.get("facebook_cookies") or 8.0
        
        f_name, l_name = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
        
        await state.set_state(SocialTaskStates.waiting_for_fb_cookies_uid)
        await state.update_data(fb_user=account_info, fb_pass=password)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Send UID", callback_data="prompt_fb_cookies_uid")],
            [InlineKeyboardButton(text="❌ Cancel Task", callback_data="cancel_task")]
        ])
        text = (
            "🍪 <b>Facebook Cookies Task</b>\n\n"
            f"👤 <b>First Name:</b> <code>{f_name}</code>\n"
            f"👤 <b>Last Name:</b> <code>{l_name}</code>\n"
            f"📱 <b>Account / ID:</b> <code>{html.escape(account_info)}</code>\n"
            f"🔑 <b>Password:</b> <code>{html.escape(password)}</code>\n\n"
            f"💰 <b>রিওয়ার্ড:</b> <code>{fb_reward:.2f} BDT</code>\n\n"
            "📌 ওপরের তথ্য দিয়ে লগইন করুন এবং কুকিজ নিয়ে নিচে <b>Send UID</b> বাটনে ক্লিক করুন:"
        )
        await safe_edit(callback.message, text, kb)
        await callback.message.answer("⚠️ ইউআইডি টাইপ করে পাঠাতে পারেন:", reply_markup=get_cancel_keyboard())

    elif cat_key == "instagram":
        stock_item = await db.get_unused_stock("instagram")
        if not stock_item:
            await callback.answer("⚠️ বর্তমানে ইনস্টাগ্রামের কোনো কাজ স্টকে নেই।", show_alert=True)
            return

        username, password = parse_credentials(stock_item['account_data'])
        await db.mark_stock_as_used(stock_item['id'], user_id=user_id)
        insta_reward = CATEGORY_REWARDS.get("instagram") or 5.0
        
        await state.set_state(SocialTaskStates.waiting_for_insta_2fa)
        await state.update_data(insta_user=username, insta_pass=password)

        text = (
            "📸 <b>Instagram Account Task</b>\n\n"
            f"👤 <b>Username:</b> <code>{html.escape(username)}</code>\n"
            f"🔐 <b>Password:</b> <code>{html.escape(password)}</code>\n"
            f"💰 <b>রিওয়ার্ড:</b> <code>{insta_reward:.2f} BDT</code>\n\n"
            "📌 লগইন করে 2FA চালু করুন এবং নিচের <b>2FA Set</b> বাটনে ক্লিক করুন।"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 2FA Set", callback_data="prompt_insta_2fa")],
            [InlineKeyboardButton(text="❌ Cancel Task", callback_data="cancel_task")]
        ])
        await safe_edit(callback.message, text, kb)
        await callback.message.answer("⚠️ আপনার 2FA Key পেস্ট করে সেন্ড করুন:", reply_markup=get_cancel_keyboard())

    elif cat_key == "insta_cookies":
        stock_item = await db.get_unused_stock("instagram")
        if not stock_item:
            await callback.answer("⚠️ বর্তমানে ইনস্টাগ্রামের কোনো কাজ স্টকে নেই।", show_alert=True)
            return

        username, password = parse_credentials(stock_item['account_data'])
        await db.mark_stock_as_used(stock_item['id'], user_id=user_id)
        insta_reward = CATEGORY_REWARDS.get("instagram_cookies") or 8.0
        
        await state.set_state(SocialTaskStates.waiting_for_insta_cookies)
        await state.update_data(insta_user=username, insta_pass=password)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍪 Submit Cookies", callback_data="prompt_insta_cookies")],
            [InlineKeyboardButton(text="❌ Cancel Task", callback_data="cancel_task")]
        ])
        text = (
            "🍪 <b>Instagram Cookies Task</b>\n\n"
            f"👤 <b>Username:</b> <code>{html.escape(username)}</code>\n"
            f"🔐 <b>Password:</b> <code>{html.escape(password)}</code>\n"
            f"💰 <b>রিওয়ার্ড:</b> <code>{insta_reward:.2f} BDT</code>\n\n"
            "📌 লগইন করে কুকিজ নিয়ে নিচের বাটনে চাপুন।"
        )
        await safe_edit(callback.message, text, kb)
        await callback.message.answer("⚠️ কুকিজ পেস্ট করে সেন্ড করুন:", reply_markup=get_cancel_keyboard())

    await callback.answer()

# --- PROMPT HANDLERS ---
@social_router.callback_query(F.data == "prompt_fb_uid")
async def handle_prompt_fb_uid(callback: CallbackQuery):
    await callback.message.answer("🏷️ <b>Enter your Facebook account UID:</b>", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    await callback.answer()

@social_router.callback_query(F.data == "prompt_fb_cookies_uid")
async def handle_prompt_fb_cookies_uid(callback: CallbackQuery):
    await callback.message.answer("🏷️ <b>Enter UID for Cookies:</b>", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    await callback.answer()

@social_router.callback_query(F.data == "prompt_insta_2fa")
async def handle_prompt_insta_2fa(callback: CallbackQuery):
    await callback.message.answer("🔑 <b>Enter 2FA Secret Key:</b>", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    await callback.answer()

@social_router.callback_query(F.data == "prompt_insta_cookies")
async def handle_prompt_insta_cookies(callback: CallbackQuery):
    await callback.message.answer("🍪 <b>Enter Instagram Cookies:</b>", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    await callback.answer()


# --- MESSAGE DATA HANDLERS (SENT TO ADMIN GROUP) ---
@social_router.message(SocialTaskStates.waiting_for_fb_uid, F.text)
async def process_fb_uid(message: Message, state: FSMContext):
    if message.text in [BTN_CANCEL, "❌ Cancel", "❌ Cancel Task"] or message.text in MENU_BUTTONS:
        await state.clear()
        await db.release_user_task(message.from_user.id)
        await message.answer("❌ কাজ বাতিল করা হয়েছে।", reply_markup=get_main_keyboard())
        return

    uid = message.text.strip()
    if uid.isdigit() and 10 <= len(uid) <= 18:
        await state.update_data(fb_uid=uid)
        await state.set_state(SocialTaskStates.waiting_for_fb_2fa)
        await message.answer("🔑 <b>Now enter your 2FA Key:</b>", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    else:
        await message.answer("⚠️ সঠিক 14-16 ডিজিটের Facebook UID দিন।", reply_markup=get_cancel_keyboard())

@social_router.message(SocialTaskStates.waiting_for_fb_2fa, F.text)
async def process_fb_2fa(message: Message, state: FSMContext):
    if message.text in [BTN_CANCEL, "❌ Cancel", "❌ Cancel Task"] or message.text in MENU_BUTTONS:
        await state.clear()
        await db.release_user_task(message.from_user.id)
        await message.answer("❌ কাজ বাতিল করা হয়েছে।", reply_markup=get_main_keyboard())
        return

    two_fa = message.text.strip()
    if len(two_fa.replace(" ", "")) >= 15 and two_fa.replace(" ", "").isalnum():
        # ৬-ডিজিটের কোড জেনারেট করা
        totp_result = task_manager.generate_totp(two_fa)
        if not totp_result:
            await message.answer("⚠️ আপনার দেওয়া 2FA Key টি সঠিক নয় বা ইনভ্যালিড। দয়া করে সঠিক Key দিন।", reply_markup=get_cancel_keyboard())
            return
            
        code, _ = totp_result

        data = await state.get_data()
        uid = data.get("fb_uid")
        password = data.get("fb_pass", "N/A")
        user_id = message.from_user.id
        
        append_to_csv('facebook_master.csv', [uid, password, two_fa])
        sub_id = await db.add_submission(user_id, "facebook_2fa", f"UID: {uid} | 2FA: {two_fa}")
        await state.clear()
        
        # ইউজারকে কোড দেওয়া এবং সাবমিট মেসেজ
        await message.answer(
            f"✅ <b>2FA Key গৃহীত হয়েছে!</b>\n\n"
            f"ফেসবুকে সেটআপ সম্পূর্ণ করতে নিচের ৬-ডিজিটের কোডটি বসান:\n"
            f"👉 <code>{code}</code> 👈\n\n"
            f"⏳ <b>আপনার কাজ অ্যাডমিনের কাছে রিভিউর জন্য জমা নেওয়া হয়েছে!</b>", 
            parse_mode="HTML", 
            reply_markup=get_main_keyboard()
        )
        
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Accept", callback_data=f"app_sub_{sub_id}_{user_id}_fb"),
             InlineKeyboardButton(text="❌ Reject", callback_data=f"rej_sub_{sub_id}_{user_id}_fb")]
        ])
        try:
            await message.bot.send_message(ADMIN_GROUP_ID, f"📥 <b>নতুন টাস্ক (FB 2FA)</b>\nUID: <code>{uid}</code>\n🔑 Pass: <code>{password}</code>\n2FA: <code>{two_fa}</code>\nUser ID: {user_id}", reply_markup=admin_kb)
        except: pass
    else:
        await message.answer("⚠️ সঠিক 2FA Key দিন। এটি সাধারণত ১৫-৩২ অক্ষরের কোড হয়ে থাকে।", reply_markup=get_cancel_keyboard())

@social_router.message(SocialTaskStates.waiting_for_fb_cookies_uid, F.text)
async def process_fb_cookies_uid(message: Message, state: FSMContext):
    if message.text in [BTN_CANCEL, "❌ Cancel", "❌ Cancel Task"] or message.text in MENU_BUTTONS:
        await state.clear()
        await db.release_user_task(message.from_user.id)
        await message.answer("❌ কাজ বাতিল করা হয়েছে।", reply_markup=get_main_keyboard())
        return

    uid = message.text.strip()
    if uid.isdigit() and 10 <= len(uid) <= 18:
        await state.update_data(fb_uid=uid)
        await state.set_state(SocialTaskStates.waiting_for_fb_cookies)
        await message.answer("🍪 <b>Now enter your Facebook account Cookies:</b>", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    else:
        await message.answer("⚠️ সঠিক UID দিন।", reply_markup=get_cancel_keyboard())

@social_router.message(SocialTaskStates.waiting_for_fb_cookies, F.text)
async def process_fb_cookies(message: Message, state: FSMContext):
    if message.text in [BTN_CANCEL, "❌ Cancel", "❌ Cancel Task"] or message.text in MENU_BUTTONS:
        await state.clear()
        await db.release_user_task(message.from_user.id)
        await message.answer("❌ কাজ বাতিল করা হয়েছে।", reply_markup=get_main_keyboard())
        return

    cookies = message.text.strip()
    if len(cookies) >= 30:
        data = await state.get_data()
        uid = data.get("fb_uid")
        password = data.get("fb_pass", "N/A")
        user_id = message.from_user.id
        
        append_to_csv('facebook_cookies_master.csv', [uid, password, cookies])
        sub_id = await db.add_submission(user_id, "facebook_cookies", f"UID: {uid} | Cookies: {cookies}")
        await state.clear()
        
        await message.answer("⏳ <b>আপনার ফেসবুক কুকিজ কাজ জমা নেওয়া হয়েছে!</b>", parse_mode="HTML", reply_markup=get_main_keyboard())
        
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Accept", callback_data=f"app_sub_{sub_id}_{user_id}_fb_cookies"),
             InlineKeyboardButton(text="❌ Reject", callback_data=f"rej_sub_{sub_id}_{user_id}_fb_cookies")]
        ])
        try:
            await message.bot.send_message(ADMIN_GROUP_ID, f"📥 <b>নতুন টাস্ক (FB Cookies)</b>\nUID: <code>{uid}</code>\n🔑 Pass: <code>{password}</code>\nCookies: <code>{cookies[:30]}...</code>\nUser ID: {user_id}", reply_markup=admin_kb)
        except: pass
    else:
        await message.answer("⚠️ Invalid Cookies! সঠিক এবং সম্পূর্ণ কুকিজ দিন।", reply_markup=get_cancel_keyboard())

@social_router.message(SocialTaskStates.waiting_for_insta_2fa, F.text)
async def process_insta_2fa(message: Message, state: FSMContext):
    if message.text in [BTN_CANCEL, "❌ Cancel", "❌ Cancel Task"] or message.text in MENU_BUTTONS:
        await state.clear()
        await db.release_user_task(message.from_user.id)
        await message.answer("❌ কাজ বাতিল করা হয়েছে।", reply_markup=get_main_keyboard())
        return

    two_fa = message.text.strip()
    if len(two_fa.replace(" ", "")) >= 15 and two_fa.replace(" ", "").isalnum():
        # ৬-ডিজিটের কোড জেনারেট করা
        totp_result = task_manager.generate_totp(two_fa)
        if not totp_result:
            await message.answer("⚠️ আপনার দেওয়া 2FA Key টি সঠিক নয় বা ইনভ্যালিড। দয়া করে সঠিক Key দিন।", reply_markup=get_cancel_keyboard())
            return
            
        code, _ = totp_result

        data = await state.get_data()
        username = data.get("insta_user")
        password = data.get("insta_pass")
        user_id = message.from_user.id
        
        append_to_csv('accounts_master.csv', [username, password, two_fa])
        sub_id = await db.add_submission(user_id, "instagram_2fa", f"Username: {username} | Pass: {password} | 2FA: {two_fa}")
        await state.clear()
        
        # ইউজারকে কোড দেওয়া এবং সাবমিট মেসেজ
        await message.answer(
            f"✅ <b>2FA Key গৃহীত হয়েছে!</b>\n\n"
            f"ইনস্টাগ্রামে সেটআপ সম্পূর্ণ করতে নিচের ৬-ডিজিটের কোডটি বসান:\n"
            f"👉 <code>{code}</code> 👈\n\n"
            f"⏳ <b>আপনার কাজ অ্যাডমিনের কাছে রিভিউর জন্য জমা নেওয়া হয়েছে!</b>", 
            parse_mode="HTML", 
            reply_markup=get_main_keyboard()
        )

        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Accept", callback_data=f"app_sub_{sub_id}_{user_id}_insta"),
             InlineKeyboardButton(text="❌ Reject", callback_data=f"rej_sub_{sub_id}_{user_id}_insta")]
        ])
        try:
            await message.bot.send_message(ADMIN_GROUP_ID, f"📥 <b>নতুন টাস্ক (Insta 2FA)</b>\nUser: <code>{username}</code>\n🔑 Pass: <code>{password}</code>\n2FA: <code>{two_fa}</code>\nUser ID: {user_id}", reply_markup=admin_kb)
        except: pass
    else:
        await message.answer("⚠️ সঠিক 2FA Key দিন। এটি সাধারণত ১৫-৩২ অক্ষরের কোড হয়ে থাকে।", reply_markup=get_cancel_keyboard())

@social_router.message(SocialTaskStates.waiting_for_insta_cookies, F.text)
async def process_insta_cookies(message: Message, state: FSMContext):
    if message.text in [BTN_CANCEL, "❌ Cancel", "❌ Cancel Task"] or message.text in MENU_BUTTONS:
        await state.clear()
        await db.release_user_task(message.from_user.id)
        await message.answer("❌ কাজ বাতিল করা হয়েছে।", reply_markup=get_main_keyboard())
        return

    cookies = message.text.strip()
    if len(cookies) >= 30:
        data = await state.get_data()
        username = data.get("insta_user")
        password = data.get("insta_pass")
        user_id = message.from_user.id
        
        append_to_csv('instagram_cookies_master.csv', [username, password, cookies])
        sub_id = await db.add_submission(user_id, "instagram_cookies", f"Username: {username} | Cookies: {cookies}")
        await state.clear()
        
        await message.answer("⏳ <b>আপনার ইনস্টাগ্রাম কুকিজ কাজ জমা নেওয়া হয়েছে!</b>", parse_mode="HTML", reply_markup=get_main_keyboard())

        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Accept", callback_data=f"app_sub_{sub_id}_{user_id}_insta_cookies"),
             InlineKeyboardButton(text="❌ Reject", callback_data=f"rej_sub_{sub_id}_{user_id}_insta_cookies")]
        ])
        try:
            await message.bot.send_message(ADMIN_GROUP_ID, f"📥 <b>নতুন টাস্ক (Insta Cookies)</b>\nUser: <code>{username}</code>\n🔑 Pass: <code>{password}</code>\nCookies: <code>{cookies[:30]}...</code>\nUser ID: {user_id}", reply_markup=admin_kb)
        except: pass
    else:
        await message.answer("⚠️ Invalid Cookies! সঠিক এবং সম্পূর্ণ কুকিজ দিন।", reply_markup=get_cancel_keyboard())


# --- ADMIN ACCEPT / REJECT (FROM GROUP) ---
@social_router.callback_query(F.data.startswith("app_sub_"))
async def handle_accept_submission(callback: CallbackQuery):
    parts = callback.data.split("_")
    sub_id = parts[2]
    user_id = int(parts[3])
    cat_type = parts[4]
    
    # Config file থেকে ক্যাটাগরি অনুযায়ী রেট ফেচ করা হচ্ছে
    if cat_type == "fb": reward = CATEGORY_REWARDS.get("facebook")
    elif cat_type == "fb_cookies": reward = CATEGORY_REWARDS.get("facebook_cookies")
    elif cat_type == "insta": reward = CATEGORY_REWARDS.get("instagram")
    elif cat_type == "insta_cookies": reward = CATEGORY_REWARDS.get("instagram_cookies")
    elif cat_type == "gmail": reward = CATEGORY_REWARDS.get("gmail")
    else: reward = 5.0

    # ডাটাবেজের ফাংশন কল করে ব্যালেন্স, রেফারেল এবং টাস্ক কাউন্ট আপডেট
    result = await db.approve_submission_and_reward(sub_id, user_id, reward)
    
    try:
        await callback.message.edit_text(callback.message.html_text + "\n\n✅ <b>ACCEPTED</b>", parse_mode="HTML")
    except: pass
    await callback.answer("Accepted!", show_alert=True)
    
    if result:
        try:
            await callback.bot.send_message(
                user_id,
                "🎉 <b>আপনার টাস্কটি এডমিন দ্বারা সফলভাবে গৃহীত হয়েছে!</b>\n\n"
                f"💰 আপনার ব্যালেন্সে <b>+{reward:.2f} BDT</b> যোগ করা হয়েছে।"
            )
        except Exception: pass
        
        if result.get("referrer_id") and result.get("commission") > 0:
            try:
                await callback.bot.send_message(
                    result["referrer_id"], 
                    f"🎁 রেফারেল কমিশন: <b>+{result['commission']:.4f} BDT</b>"
                )
            except Exception: pass

@social_router.callback_query(F.data.startswith("rej_sub_"))
async def handle_reject_prompt(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    sub_id = parts[2]
    user_id = int(parts[3])
    
    await state.set_state(SocialTaskStates.waiting_for_reject_reason)
    await state.update_data(target_sub_id=sub_id, target_user_id=user_id, target_msg_id=callback.message.message_id)

    await callback.message.reply("📝 <b>রিজেক্ট করার কারণ টাইপ করুন:</b>\n(ডিফল্ট মেসেজ পাঠাতে <code>default</code> লিখুন)")
    await callback.answer()

@social_router.message(SocialTaskStates.waiting_for_reject_reason, F.text)
async def process_reject_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    sub_id = data.get("target_sub_id")
    user_id = data.get("target_user_id")
    target_msg_id = data.get("target_msg_id")
    reason_text = message.text.strip()

    reason_msg = "আপনার জমা দেওয়া অ্যাকাউন্ট বা তথ্যগুলো সঠিক নিয়ম অনুযায়ী তৈরি করা হয়নি।" if reason_text.lower() == "default" else reason_text

    await db.update_submission_status(sub_id, "rejected")
    await state.clear()

    await message.answer("❌ টাস্ক বাতিল করা হয়েছে।")
    try:
        await message.bot.edit_message_text(chat_id=message.chat.id, message_id=target_msg_id, text="❌ <b>REJECTED</b>")
    except: pass

    try:
        await message.bot.send_message(
            user_id,
            "❌ <b>আপনার জমা দেওয়া কাজটি বাতিল (Rejected) করা হয়েছে।</b>\n\n"
            f"💬 <b>কারণ:</b> {reason_msg}\n\n"
            "দয়া করে নিয়ম মেনে আবার সঠিকভাবে চেষ্টা করুন।"
        )
    except Exception: pass

# --- EXPORT ACCOUNTS (/export_accounts) ---
@social_router.message(Command("export_accounts"))
async def export_accepted_accounts(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    accepted_tasks = await db.get_accepted_submissions()
    if not accepted_tasks:
        await message.answer("⚠️ কোনো একসেপ্ট করা একাউন্ট ডাটাবেজে নেই।")
        return
    file_content = "=== ACCEPTED ACCOUNTS LIST ===\n\n"
    for task in accepted_tasks:
        file_content += f"Category: {task['category']} | Details: {task['details']} | Date: {task['created_at']}\n"
    file_bytes = io.BytesIO(file_content.encode('utf-8'))
    document = BufferedInputFile(file_bytes.getvalue(), filename="accounts_list.txt")
    await message.answer_document(document=document, caption="📄 <b>একসেপ্ট হওয়া সকল অ্যাকাউন্টের লিস্ট।</b>")
    
# --- ADMIN CHECK STOCK (/stock) ---
@social_router.message(Command("stock"))
async def check_current_stock(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        client = db._db()
        # Facebook এর এভেইলেবল স্টক চেক
        fb_res = await client.table("stock_accounts").select("id", count="exact").eq("category", "facebook").eq("is_used", False).execute()
        fb_count = fb_res.count or 0
        
        # Instagram এর এভেইলেবল স্টক চেক
        insta_res = await client.table("stock_accounts").select("id", count="exact").eq("category", "instagram").eq("is_used", False).execute()
        insta_count = insta_res.count or 0
        
        # Gmail এর ম্যানুয়াল স্টক চেক
        gmail_res = await client.table("tasks").select("id", count="exact").eq("status", "available").execute()
        gmail_count = gmail_res.count or 0
        
        await message.answer(
            f"📊 <b>বর্তমান স্টক স্ট্যাটাস:</b>\n\n"
            f"📘 Facebook: <code>{fb_count}</code> টি কাজ বাকি আছে\n"
            f"📸 Instagram: <code>{insta_count}</code> টি কাজ বাকি আছে\n"
            f"📧 Gmail (Manual): <code>{gmail_count}</code> টি কাজ বাকি আছে\n\n"
            f"<i>(জিমেইল ম্যানুয়াল স্টক শেষ হলে বট অটোমেটিক টাস্ক দেওয়া শুরু করবে)</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer("⚠️ স্টক চেক করতে সমস্যা হয়েছে। ডাটাবেজ কানেকশন চেক করুন।")
