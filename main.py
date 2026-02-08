# main.py
import os
import asyncio
import logging
import datetime
import random
import string
import sys
import signal
import traceback
import uuid
from typing import Dict, List, Optional, Tuple, Union
from functools import wraps
from io import BytesIO
from bson.objectid import ObjectId

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.constants import ParseMode
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl import functions, types
from cryptography.fernet import Fernet
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
# MongoDB
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.getenv("DB_NAME", "telegram_bot_db")

# Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Telethon API
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

# Encryption
ENCRYPTION_KEY_STR = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY_STR:
    raise ValueError(
        "خطأ حرج: 'ENCRYPTION_KEY' غير موجود في متغيرات البيئة.\n"
        "يرجى إنشاء مفتاح (مثلاً باستخدام Fernet.generate_key()) وإضافته إلى ملف .env.\n"
        "مثال: ENCRYPTION_KEY=your_generated_key_here"
    )
cipher_suite = Fernet(ENCRYPTION_KEY_STR.encode())

# Conversation states
ACCOUNT_PHONE, ACCOUNT_CODE, ACCOUNT_PASSWORD = range(3)
GROUP_NAME, GROUP_COUNT, GROUP_DELAY = range(3, 6)

# Database connection
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Collections
users_collection = db["users"]
accounts_collection = db["accounts"]
sessions_collection = db["sessions"] # Not used directly but good to keep
settings_collection = db["settings"]
logs_collection = db["logs"]

# Initialize settings if not exists
if settings_collection.count_documents({}) == 0:
    settings_collection.insert_one({
        "monitoring_enabled": True,
        "owner_id": OWNER_ID
    })

# --- Helper Functions ---
def encrypt_data(data: str) -> str:
    """تشفير البيانات الحساسة قبل تخزينها."""
    if not data:
        return data
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    """فك تشفير البيانات الحساسة من التخزين."""
    if not encrypted_data:
        return encrypted_data
    return cipher_suite.decrypt(encrypted_data.encode()).decode()

def log_event(event_type: str, details: str, user_id: int):
    """تسجيل الأحداث في قاعدة البيانات."""
    logs_collection.insert_one({
        "timestamp": datetime.datetime.now(),
        "event_type": event_type,
        "details": details,
        "user_id": user_id
    })

def get_user_status(user_id: int) -> str:
    """الحصول على حالة وصول المستخدم."""
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        return "not_registered"
    return user.get("access_status", "pending")

def is_approved(user_id: int) -> bool:
    """التحقق مما إذا كان المستخدم معتمدًا لاستخدام البوت."""
    return get_user_status(user_id) == "approved"

def is_owner(user_id: int) -> bool:
    """التحقق مما إذا كان المستخدم هو مالك البوت."""
    return user_id == OWNER_ID

def get_user_accounts(user_id: int) -> List[Dict]:
    """الحصول على جميع الحسابات المرتبطة بالمستخدم."""
    accounts = list(accounts_collection.find({"user_id": user_id}))
    for account in accounts:
        if "session_data" in account and account["session_data"]:
            account["session_data"] = decrypt_data(account["session_data"])
    return accounts

def get_paginated_accounts(user_id: int, page: int = 0, page_size: int = 10) -> Tuple[List[Dict], int]:
    """الحصول على حسابات مقسمة لصفحات للمستخدم."""
    accounts = get_user_accounts(user_id)
    total_pages = (len(accounts) + page_size - 1) // page_size
    start = page * page_size
    end = start + page_size
    paginated_accounts = accounts[start:end]
    return paginated_accounts, total_pages

async def send_notification(context: ContextTypes.DEFAULT_TYPE, user_id: int, message: str):
    """إرسال إشعار إلى مستخدم."""
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to send notification to {user_id}: {e}")

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, message: str, reply_markup=None):
    """إرسال إشعار إلى مالك البوت."""
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to send notification to owner: {e}")

# --- Decorators ---
def approved_only(func):
    """مصمم لتقييد الوصول للمستخدمين المعتمدين فقط."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_approved(user_id):
            await send_message(update, "⛔ <b>الوصول مرفوض</b>\n\nليس لديك إذن لاستخدام هذا البوت.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def owner_only(func):
    """مصمم لتقييد الوصول لمالك البوت فقط."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await send_message(update, "⛔ <b>الوصول مرفوض</b>\n\nهذا الأمر متاح فقط لمالك البوت.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Helper function for sending messages ---
async def send_message(update: Update, text: str, reply_markup=None):
    """دالة مساعدة لإرسال رسالة، معالجة كل من الرسائل والاستدعاءات."""
    if update.message:
        return await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.callback_query:
        try:
            return await update.callback_query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception as e: # If message is old, can't reply
            return await update.callback_query.edit_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    return None

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /start."""
    user = update.effective_user
    user_id = user.id
    
    existing_user = users_collection.find_one({"user_id": user_id})
    
    if not existing_user:
        users_collection.insert_one({
            "user_id": user_id,
            "username": user.username,
            "first_name": user.first_name,
            "access_status": "not_registered",
            "request_date": datetime.datetime.now()
        })

    status = get_user_status(user_id)
    keyboard = []

    if status == "not_registered":
        keyboard = [
            [InlineKeyboardButton("📝 طلب استخدام البوت", callback_data="request_access")],
            [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
            [InlineKeyboardButton("📱 إدارة الحسابات", callback_data="manage_accounts")],
        ]
        text = f"👋 مرحباً، {user.first_name}!\n\nأنت غير مسجل في البوت بعد. اختر أحد الخيارات أدناه للبدء."
    elif status == "pending":
        keyboard = [
            [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
            [InlineKeyboardButton("📱 إدارة الحسابات", callback_data="manage_accounts")],
        ]
        text = f"👋 مرحباً، {user.first_name}!\n\nطلبك لا يزال قيد المراجعة. يمكنك إضافة حساباتك في هذه الأثناء."
    elif status == "rejected":
        keyboard = [
            [InlineKeyboardButton("📝 طلب استخدام البوت", callback_data="request_access")],
            [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
            [InlineKeyboardButton("📱 إدارة الحسابات", callback_data="manage_accounts")],
        ]
        text = f"👋 مرحباً، {user.first_name}!\n\nتم رفض طلبك السابق. يمكنك إعادة الطلب أو إدارة حساباتك."
    else: # approved
        keyboard = [
            [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
            [InlineKeyboardButton("👥 المجموعات", callback_data="groups")],
            [InlineKeyboardButton("📊 حالتي", callback_data="status")],
            [InlineKeyboardButton("📊 إحصائياتي", callback_data="stats")],
        ]
        text = f"👋 مرحباً بعودتك، {user.first_name}!\n\nلديك وصول كامل للبوت. اختر ما تريد القيام به:"

    await send_message(update, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /help."""
    help_text = (
        "🤖 <b>بوت إدارة حسابات تيليجرام</b>\n\n"
        "<b>الأوامر المتاحة:</b>\n\n"
        "📱 /accounts - إدارة حسابات تيليجرام الخاصة بك\n"
        "👥 /groups - إنشاء مجموعات تيليجرام\n"
        "ℹ️ /status - تحقق من حالة حسابك\n"
        "📊 /stats - عرض إحصائياتك\n"
    )
    
    if is_owner(update.effective_user.id):
        help_text += (
            "\n🔧 <b>أوامر المالك:</b>\n\n"
            "✅ /approve [user_id] - الموافقة على طلب وصول المستخدم\n"
            "❌ /reject [user_id] - رفض طلب وصول المستخدم\n"
            "👥 /users - عرض جميع المستخدمين\n"
            "📊 /admin_stats - عرض إحصائيات النظام\n"
            "🔍 /logs - عرض سجلات النظام\n"
            "⚙️ /settings - تكوين إعدادات البوت\n"
        )
    
    await send_message(update, help_text)

# --- Account Management ---
async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /accounts (للمستخدمين المعتمدين)."""
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    if not accounts:
        keyboard = [[InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")]]
        await send_message(update, "📱 <b>حساباتك</b>\n\nليس لديك أي حسابات بعد. أضف حسابك الأول!", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await _show_accounts_list(update, user_id, 0)

async def _show_accounts_list(update: Update, user_id: int, page: int):
    """دالة داخلية لعرض قائمة الحسابات."""
    accounts, total_pages = get_paginated_accounts(user_id, page)
    if not accounts:
        await send_message(update, "📱 <b>لا توجد حسابات في هذه الصفحة.</b>")
        return

    keyboard = []
    for acc in accounts:
        acc_id = str(acc["_id"])
        name = acc.get("first_name", "No Name")
        has_session = "session_data" in acc and acc["session_data"]
        monitoring = acc.get("monitoring_enabled", False)

        if has_session and monitoring:
            status_emoji = "🔵"
        elif has_session:
            status_emoji = "🟢"
        else:
            status_emoji = "🔴"
        
        keyboard.append([InlineKeyboardButton(f"{status_emoji} {name}", callback_data=f"view_account_{acc_id}")])

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"acc_page_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(f"صفحة {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"acc_page_{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")])
    keyboard.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="main_menu")])
    
    text = f"📱 <b>حساباتك (صفحة {page+1})</b>\n\nاختر حساباً لإدارة تفاصيله:"
    if update.callback_query:
        await update.callback_query.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await send_message(update, text, reply_markup=InlineKeyboardMarkup(keyboard))

# --- Group Creation ---
async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /groups."""
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    active_accounts = [acc for acc in accounts if "session_data" in acc and acc["session_data"]]

    if not active_accounts:
        await send_message(update, "👥 <b>إنشاء المجموعات</b>\n\nتحتاج إلى إضافة حساب واحد على الأقل به جلسة نشطة لإنشاء المجموعات.\n\nاستخدم /accounts لإضافة حساب.")
        return
    
    keyboard = []
    for acc in active_accounts:
        name = acc.get("first_name", "No Name")
        acc_id = str(acc["_id"])
        keyboard.append([InlineKeyboardButton(f"إنشاء باستخدام {name}", callback_data=f"create_groups_for_{acc_id}")])
    
    keyboard.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="main_menu")])
    await send_message(update, "👥 <b>اختر حساباً لإنشاء المجموعات:</b>", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Callback Query Handler ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة استدعاءات الأزرار."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # --- Main Navigation ---
    if data == "main_menu":
        await start(update, context)
        return
    if data == "accounts":
        await accounts_command(update, context)
        return
    if data == "groups":
        await groups_command(update, context)
        return
    if data in ["status", "stats"]:
        await send_message(update, "هذا الخيار قيد التطوير.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="main_menu")]]))
        return

    # --- Access Request ---
    if data == "request_access":
        if get_user_status(user_id) != "not_registered":
            await send_message(update, "لقد قدمت طلباً بالفعل أو أنت مسجل.")
            return
        
        users_collection.update_one({"user_id": user_id}, {"$set": {"access_status": "pending"}})
        user = update.effective_user
        
        # Notify owner with buttons
        owner_keyboard = [
            [InlineKeyboardButton("✅ قبول", callback_data=f"owner_approve_{user_id}")],
            [InlineKeyboardButton("❌ رفض", callback_data=f"owner_reject_{user_id}")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="main_menu")]
        ]
        await notify_owner(
            context,
            f"🔔 <b>طلب جديد</b>\n\n"
            f"المستخدم: {user.first_name} (@{user.username})\n"
            f"الآيدي: {user_id}",
            reply_markup=InlineKeyboardMarkup(owner_keyboard)
        )
        await send_message(update, "✅ <b>تم إرسال الطلب!</b>\n\nسيتم إعلامك فوراً عند اتخاذ قرار.")
        return
    
    # --- Owner Actions ---
    if data.startswith("owner_approve_"):
        target_id = int(data.split("_")[2])
        users_collection.update_one({"user_id": target_id}, {"$set": {"access_status": "approved"}})
        await send_notification(context, target_id, "✅ <b>تم قبول طلبك!</b>\n\nيمكنك الآن استخدام البوت بالكامل.")
        await query.edit_text(f"✅ تم قبول المستخدم {target_id}.")
        return
    if data.startswith("owner_reject_"):
        target_id = int(data.split("_")[2])
        users_collection.update_one({"user_id": target_id}, {"$set": {"access_status": "rejected"}})
        await send_notification(context, target_id, "❌ <b>تم رفض طلبك.</b>\n\nإذا كان هناك خطأ، تواصل مع المالك.")
        await query.edit_text(f"❌ تم رفض المستخدم {target_id}.")
        return

    # --- Account Management ---
    if data == "add_account":
        return await add_account_start(update, context)
    if data == "manage_accounts":
        await _show_accounts_list(update, user_id, 0)
        return
    if data.startswith("acc_page_"):
        page = int(data.split("_")[2])
        await _show_accounts_list(update, user_id, page)
        return
    if data.startswith("view_account_"):
        acc_id = data.split("_")[2]
        await _show_account_details(update, context, acc_id)
        return
    if data.startswith("toggle_monitoring_"):
        acc_id = data.split("_")[2]
        await _toggle_monitoring(update, context, acc_id)
        return
    if data.startswith("delete_account_"):
        acc_id = data.split("_")[2]
        await _delete_account(update, context, acc_id)
        return
    
    # --- Group Creation ---
    if data.startswith("create_groups_for_"):
        acc_id = data.split("_")[3]
        context.user_data["selected_account_id"] = acc_id
        return await create_groups_start(update, context)

    # --- Fallback ---
    await send_message(update, "❌ إجراء غير معروف.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="main_menu")]]))

# --- Detailed Account & Group Functions ---
async def _get_account_full_details(client: TelegramClient, account: Dict) -> Dict:
    """جلب تفاصيل كاملة للحساب باستخدام Telethon."""
    try:
        me = await client.get_me()
        account["first_name"] = me.first_name or "No Name"
        account["username"] = f"@{me.username}" if me.username else "No Username"
        account["bio"] = me.about or "No Bio"
        # Download profile picture
        if me.photo:
            path = await client.download_profile_photo(me.photo.file_id, file=BytesIO())
            account["profile_pic"] = path.getvalue()
        else:
            account["profile_pic"] = None
    except Exception as e:
        logger.error(f"Could not fetch full details for account: {e}")
        account["first_name"] = account.get("first_name", "Unknown Account")
        account["username"] = "N/A"
        account["bio"] = "N/A"
        account["profile_pic"] = None
    return account

async def _show_account_details(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id_str: str):
    """عرض تفاصيل حساب معين."""
    try:
        account_id = ObjectId(account_id_str)
    except Exception:
        await send_message(update, "❌ معرف الحساب غير صالح.")
        return

    account = accounts_collection.find_one({"_id": account_id, "user_id": update.effective_user.id})
    if not account:
        await send_message(update, "❌ لم يتم العثور على الحساب.")
        return

    client = TelegramClient(StringSession(account["session_data"]), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await send_message(update, "❌ جلسة هذا الحساب غير نشطة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", "accounts")]]))
            return
        
        account = await _get_account_full_details(client, account)
        await client.disconnect()

        name = account.get("first_name", "N/A")
        username = account.get("username", "N/A")
        bio = account.get("bio", "N/A")
        monitoring = account.get("monitoring_enabled", False)
        
        text = (
            f"👤 <b>{name}</b>\n"
            f"🔖 {username}\n"
            f"📝 {bio}\n\n"
            f"🔐 مراقبة الجلسات: {'مفعلة' if monitoring else 'معطلة'}"
        )

        keyboard = [
            [InlineKeyboardButton("👥 إنشاء مجموعات", callback_data=f"create_groups_for_{account_id_str}")],
            [InlineKeyboardButton(f"{'🔴 إلغاء' if monitoring else '🔵 تفعيل'} مراقبة الجلسات", callback_data=f"toggle_monitoring_{account_id_str}")],
            [InlineKeyboardButton("🗑️ حذف الحساب", callback_data=f"delete_account_{account_id_str}")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="accounts")]
        ]
        
        # Send with photo if exists
        if account.get("profile_pic"):
            await query.message.reply_photo(photo=account["profile_pic"], caption=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        else:
            await send_message(update, text, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.error(f"Error showing account details: {e}")
        await send_message(update, f"❌ حدث خطأ: {str(e)}")
    finally:
        if client.is_connected():
            await client.disconnect()

async def _toggle_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id_str: str):
    """تفعيل أو إلغاء تفعيل مراقبة الجلسات."""
    try:
        account_id = ObjectId(account_id_str)
    except Exception:
        await send_message(update, "❌ معرف الحساب غير صالح.")
        return

    account = accounts_collection.find_one({"_id": account_id, "user_id": update.effective_user.id})
    if not account:
        await send_message(update, "❌ لم يتم العثور على الحساب.")
        return

    new_status = not account.get("monitoring_enabled", False)
    accounts_collection.update_one({"_id": account_id}, {"$set": {"monitoring_enabled": new_status}})
    
    status_text = "تفعيل" if new_status else "إلغاء تفعيل"
    await send_notification(context, update.effective_user.id, f"🔐 <b>تم {status_text} مراقبة الجلسات</b>\n\nللحساب: {account.get('first_name', 'N/A')}")
    
    # Refresh the account details view
    await _show_account_details(update, context, account_id_str)

async def _delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id_str: str):
    """حذف حساب."""
    # Confirmation logic would go here if needed, but for simplicity, we delete directly.
    try:
        account_id = ObjectId(account_id_str)
    except Exception:
        await send_message(update, "❌ معرف الحساب غير صالح.")
        return

    account = accounts_collection.find_one({"_id": account_id, "user_id": update.effective_user.id})
    if not account:
        await send_message(update, "❌ لم يتم العثور على الحساب.")
        return

    name = account.get("first_name", "N/A")
    accounts_collection.delete_one({"_id": account_id})
    await send_notification(context, update.effective_user.id, f"🗑️ <b>تم حذف الحساب</b>\n\n{name}")
    
    await _show_accounts_list(update, update.effective_user.id, 0)

# --- Conversation Handlers for Adding Account ---
async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_message(update, "📱 <b>إضافة حساب جديد</b>\n\nأرسل رقم الهاتف مع رمز البلد (مثال: +966123456789).\n\nأرسل /cancel للإلغاء.")
    return ACCOUNT_PHONE

async def add_account_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip().replace(' ', '')
    if not phone.startswith('+'): phone = '+' + phone
    context.user_data["phone"] = phone
    wait_msg = await update.message.reply_text("⏳ جاري طلب الرمز...")
    
    try:
        session = StringSession()
        client = TelegramClient(session, API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        context.user_data["phone_code_hash"] = result.phone_code_hash
        context.user_data["session"] = session.save()
        await client.disconnect()
        await wait_msg.edit_text(f"✅ تم إرسال الرمز إلى <code>{phone}</code>.\n\nأرسل الكود الآن.")
        return ACCOUNT_CODE
    except Exception as e:
        await wait_msg.edit_text(f"❌ خطأ: {str(e)}")
        return ConversationHandler.END

async def add_account_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().replace(' ', '')
    wait_msg = await update.message.reply_text("🔄 جاري التحقق...")
    
    try:
        client = TelegramClient(StringSession(context.user_data["session"]), API_ID, API_HASH)
        await client.connect()
        await client.sign_in(phone=context.user_data["phone"], code=code, phone_code_hash=context.user_data["phone_code_hash"])
        session_final = client.session.save()
        await client.disconnect()
        
        account_id = accounts_collection.insert_one({
            "user_id": update.effective_user.id,
            "phone_number": context.user_data["phone"],
            "session_data": encrypt_data(session_final),
            "created_at": datetime.datetime.now(),
            "monitoring_enabled": False
        }).inserted_id
        
        await send_notification(context, update.effective_user.id, f"✅ <b>تم إضافة الحساب بنجاح!</b>\n\nالرقم: {context.user_data['phone']}")
        await wait_msg.edit_text("✅ تمت إضافة الحساب! يمكنك الآن إدارته من قائمة الحسابات.")
        return ConversationHandler.END

    except errors.SessionPasswordNeededError:
        context.user_data["session"] = client.session.save()
        await wait_msg.edit_text("🔐 الحساب محمي بكلمة مرور. أرسلها الآن.")
        return ACCOUNT_PASSWORD
    except Exception as e:
        await wait_msg.edit_text(f"❌ خطأ: {str(e)}")
        return ConversationHandler.END
    finally:
        if client.is_connected(): await client.disconnect()

async def add_account_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    try:
        client = TelegramClient(StringSession(context.user_data["session"]), API_ID, API_HASH)
        await client.connect()
        await client.sign_in(password=password)
        session_final = client.session.save()
        await client.disconnect()
        
        accounts_collection.insert_one({
            "user_id": update.effective_user.id,
            "phone_number": context.user_data["phone"],
            "session_data": encrypt_data(session_final),
            "created_at": datetime.datetime.now(),
            "monitoring_enabled": False
        })
        
        await send_notification(context, update.effective_user.id, f"✅ <b>تم إضافة الحساب بنجاح!</b>\n\nالرقم: {context.user_data['phone']}")
        await send_message(update, "✅ تمت إضافة الحساب بكلمة المرور!")
        return ConversationHandler.END
    except Exception as e:
        await send_message(update, f"❌ خطأ في كلمة المرور: {str(e)}")
        return ConversationHandler.END
    finally:
        if client.is_connected(): await client.disconnect()

async def cancel_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_message(update, "❌ تم إلغاء العملية.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القائمة الرئيسية", "main_menu")]]))
    return ConversationHandler.END

# --- Conversation Handlers for Creating Groups ---
async def create_groups_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_message(update, "👥 <b>إنشاء مجموعات</b>\n\nماذا تريد أن تسمي المجموعات؟ (مثال: نادي الأصدقاء)")
    return GROUP_NAME

async def create_groups_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["group_name"] = update.message.text.strip()
    await send_message(update, f"✅ سيتم تسميتها: '{context.user_data['group_name']} 1', '{context.user_data['group_name']} 2', ...\n\nكم مجموعة تريد إنشاءها؟ (1-50)")
    return GROUP_COUNT

async def create_groups_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text.strip())
        if not 1 <= count <= 50: raise ValueError
        context.user_data["group_count"] = count
        await send_message(update, f"✅ ستقوم بإنشاء {count} مجموعة.\n\nكم ثانية تأخير بين كل مجموعة؟ (5-60)")
        return GROUP_DELAY
    except ValueError:
        await send_message(update, "❌ الرقم غير صالح. أرسل رقماً بين 1 و 50.")
        return GROUP_COUNT

async def create_groups_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delay = int(update.message.text.strip())
        if not 5 <= delay <= 60: raise ValueError
        context.user_data["group_delay"] = delay
        
        # Start the process
        acc_id = context.user_data.get("selected_account_id")
        if not acc_id:
            await send_message(update, "❌ لم يتم تحديد حساب. حاول مرة أخرى.")
            return ConversationHandler.END
        
        asyncio.create_task(_create_groups_task(update, context, acc_id))
        return ConversationHandler.END
        
    except ValueError:
        await send_message(update, "❌ الرقم غير صالح. أرسل رقماً بين 5 و 60.")
        return GROUP_DELAY

async def _create_groups_task(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id_str: str):
    """المهمة الفعلية لإنشاء المجموعات في الخلفية."""
    query = update.callback_query
    try:
        account_id = ObjectId(account_id_str)
    except Exception:
        await send_message(update, "❌ معرف الحساب غير صالح.")
        return

    account = accounts_collection.find_one({"_id": account_id, "user_id": update.effective_user.id})
    if not account or not account.get("session_data"):
        await send_message(update, "❌ الحساب المحدد غير صالح أو لا توجد جلسة نشطة.")
        return

    client = TelegramClient(StringSession(account["session_data"]), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await send_message(update, "❌ جلسة الحساب غير نشطة.")
            return

        group_name = context.user_data["group_name"]
        group_count = context.user_data["group_count"]
        group_delay = context.user_data["group_delay"]
        
        processing_msg = await query.message.reply_text(f"⏳ <b>جاري إنشاء المجموعات...</b>\n\nالعدد: {group_count}")
        
        created_count = 0
        for i in range(group_count):
            try:
                title = f"{group_name} {i + 1}"
                result = await client(functions.channels.CreateChannelRequest(title=title, about="تم الإنشاء بواسطة البوت", megagroup=True))
                group = result.chats[0]
                
                # Send 10 nice messages
                welcome_messages = [
                    "مرحباً بكم في المجموعة! 🎉",
                    "هذه رسالة ترحيبية من البوت.",
                    "نأمل أن تستمتعوا بوقتكم هنا.",
                    "لا تترددوا في المشاركة والتفاعل.",
                    "القوانين بسيطة: احترام الجميع.",
                    "يمكنكم استخدام الأمر /help لمعرفة المزيد.",
                    "شكراً لانضمامكم! 🙏",
                    "هذه المجموعة تم إنشاؤها تلقائياً.",
                    "البوت الآن يرسل الرسائل الترحيبية.",
                    "انتهى الإعداد! استمتعوا بالمجموعة. ✅"
                ]
                for msg_text in welcome_messages:
                    await client.send_message(group.id, msg_text)
                    await asyncio.sleep(0.5) # Small delay between messages

                created_count += 1
                await processing_msg.edit_text(f"⏳ <b>جاري إنشاء المجموعات...</b>\n\nتم إنشاء {created_count} من {group_count}")
                
                if i < group_count - 1:
                    await asyncio.sleep(group_delay)

            except Exception as e:
                logger.error(f"Failed to create group {i+1}: {e}")
                # Continue to the next group
        
        await client.disconnect()
        await send_notification(context, update.effective_user.id, f"✅ <b>اكتمل إنشاء المجموعات!</b>\n\nتم إنشاء {created_count} مجموعة بنجاح.")
        await processing_msg.edit_text(
            f"✅ <b>اكتملت العملية!</b>\n\nتم إنشاء {created_count} مجموعة.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القائمة الرئيسية", "main_menu")]])
        )

    except Exception as e:
        logger.error(f"Error in group creation task: {e}")
        await processing_msg.edit_text(f"❌ فشلت العملية: {str(e)}")
    finally:
        if client.is_connected():
            await client.disconnect()

# --- Session Monitoring Task ---
async def session_monitoring_task(app: Application):
    """مهمة خلفية لمراقبة الجلسات الجديدة."""
    logger.info("Session monitoring task started.")
    while True:
        try:
            # Find all accounts with monitoring enabled
            monitored_accounts = list(accounts_collection.find({"monitoring_enabled": True}))
            for acc in monitored_accounts:
                session_data = decrypt_data(acc["session_data"])
                client = TelegramClient(StringSession(session_data), API_ID, API_HASH)
                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        # Session is dead, disable monitoring
                        accounts_collection.update_one({"_id": acc["_id"]}, {"$set": {"monitoring_enabled": False}})
                        logger.warning(f"Monitoring disabled for dead session: {acc.get('phone_number')}")
                        continue
                    
                    authorizations = await client(functions.account.GetAuthorizationsRequest())
                    current_hashes = {auth.hash for auth in authorizations.authorizations}
                    known_hashes = set(acc.get("known_session_hashes", []))
                    
                    new_hashes = current_hashes - known_hashes
                    if new_hashes:
                        for new_hash in new_hashes:
                            # Find the authorization object to get details
                            auth_obj = next((a for a in authorizations.authorizations if a.hash == new_hash), None)
                            if auth_obj:
                                device_model = auth_obj.device_model or "Unknown Device"
                                platform = auth_obj.platform or "Unknown Platform"
                                await client(functions.account.ResetAuthorizationRequest(hash=new_hash))
                                logger.info(f"Killed new session on {acc.get('phone_number')} from {device_model} ({platform})")
                                
                                # Notify user
                                await app.bot.send_message(
                                    acc["user_id"],
                                    f"⚠️ <b>تم حظر جلسة جديدة!</b>\n\n"
                                    f"الحساب: {acc.get('first_name', 'N/A')}\n"
                                    f"الجهاز: {device_model} ({platform})\n"
                                    f"التوقيت: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                                    parse_mode=ParseMode.HTML
                                )
                    
                    # Update known hashes
                    accounts_collection.update_one({"_id": acc["_id"]}, {"$set": {"known_session_hashes": list(current_hashes)}})

                except Exception as e:
                    logger.error(f"Error monitoring account {acc.get('phone_number')}: {e}")
                finally:
                    if client.is_connected():
                        await client.disconnect()
            
            # Wait for 5 minutes before next check
            await asyncio.sleep(300)

        except Exception as e:
            logger.error(f"Critical error in session monitoring task: {e}")
            await asyncio.sleep(600) # Wait longer if there's a critical error

# --- Main Function ---
def main():
    if not BOT_TOKEN or API_ID == 0 or not API_HASH:
        logger.error("BOT_TOKEN, API_ID, or API_HASH is not set!")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    # --- Conversation Handlers ---
    account_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_callback, pattern="^add_account$")],
        states={
            ACCOUNT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_phone), CommandHandler("cancel", cancel_account)],
            ACCOUNT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_code), CommandHandler("cancel", cancel_account)],
            ACCOUNT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_password), CommandHandler("cancel", cancel_account)],
        },
        fallbacks=[CommandHandler("cancel", cancel_account)],
        per_message=False,
    )

    group_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_callback, pattern="^create_groups_for_")],
        states={
            GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_name), CommandHandler("cancel", cancel_account)],
            GROUP_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_count), CommandHandler("cancel", cancel_account)],
            GROUP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_delay), CommandHandler("cancel", cancel_account)],
        },
        fallbacks=[CommandHandler("cancel", cancel_account)],
        per_message=False,
    )

    # --- Add Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("groups", groups_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(account_conv_handler)
    application.add_handler(group_conv_handler)
    
    # --- Owner Commands (keeping them for direct use) ---
    application.add_handler(CommandHandler("approve", owner_only(lambda u, c: None))) # Placeholder
    application.add_handler(CommandHandler("reject", owner_only(lambda u, c: None))) # Placeholder
    # ... other owner commands

    # --- Error Handler ---
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling an update:", exc_info=context.error)

    application.add_error_handler(error_handler)

    # --- Start Background Task ---
    loop = asyncio.get_event_loop()
    loop.create_task(session_monitoring_task(application))
    
    # --- Run Bot ---
    logger.info("Starting bot...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
