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
from telethon.tl import functions
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
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
cipher_suite = Fernet(ENCRYPTION_KEY.encode())

# Conversation states
ACCOUNT_PHONE, ACCOUNT_CODE, ACCOUNT_PASSWORD = range(3)
GROUP_NAME, GROUP_COUNT, GROUP_DELAY = range(3, 6)

# Database connection
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Collections
users_collection = db["users"]
accounts_collection = db["accounts"]
sessions_collection = db["sessions"]
settings_collection = db["settings"]
logs_collection = db["logs"]

# Initialize settings if not exists
if settings_collection.count_documents({}) == 0:
    settings_collection.insert_one({
        "monitoring_enabled": True,
        "owner_id": OWNER_ID
    })

# Helper functions
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
        # فك تشفير بيانات الجلسة للاستخدام
        if "session_data" in account and account["session_data"]:
            try:
                account["session_data"] = decrypt_data(account["session_data"])
            except Exception as e:
                logger.error(f"Failed to decrypt session for account {account.get('_id')}: {e}")
                account["session_data"] = None # Mark as invalid
    return accounts

def get_paginated_accounts(user_id: int, page: int = 0, page_size: int = 10) -> Tuple[List[Dict], int]:
    """الحصول على حسابات مقسمة لصفحات للمستخدم."""
    accounts = get_user_accounts(user_id)
    total_pages = (len(accounts) + page_size - 1) // page_size if accounts else 1
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

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, message: str):
    """إرسال إشعار إلى مالك البوت."""
    await send_notification(context, OWNER_ID, message)

# Decorators
def approved_only(func):
    """مصمم لتقييد الوصول للمستخدمين المعتمدين فقط."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_approved(user_id):
            await send_message(
                update,
                "⛔ <b>الوصول مرفوض</b>\n\n"
                "ليس لديك إذن لاستخدام هذا البوت. "
                "قد يكون طلب الوصول الخاص بك لا يزال معلقًا أو مرفوضًا.",
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def owner_only(func):
    """مصمم لتقييد الوصول لمالك البوت فقط."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await send_message(
                update,
                "⛔ <b>الوصول مرفوض</b>\n\n"
                "هذا الأمر متاح فقط لمالك البوت.",
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# Helper function for sending messages
async def send_message(update: Update, text: str, reply_markup=None):
    """دالة مساعدة لإرسال رسالة، معالجة كل من الرسائل والاستدعاءات."""
    if update.message:
        return await update.message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    elif update.callback_query:
        try:
            return await update.callback_query.message.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            # In case the original message is too old to be replied to
            await update.callback_query.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            return update.callback_query.message
    return None

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /start."""
    user = update.effective_user
    user_id = user.id
    
    existing_user = users_collection.find_one({"user_id": user_id})
    
    keyboard = [
        [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
        [InlineKeyboardButton("👥 المجموعات", callback_data="groups")],
        [InlineKeyboardButton("📊 حالتي", callback_data="status")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="stats")],
    ]
    
    if not existing_user:
        users_collection.insert_one({
            "user_id": user_id,
            "username": user.username,
            "first_name": user.first_name,
            "access_status": "pending",
            "request_date": datetime.datetime.now()
        })
        
        log_event("access_request", f"User {user.first_name} (@{user.username}) requested access", user_id)
        
        await notify_owner(
            context,
            f"🔔 <b>طلب وصول جديد</b>\n\n"
            f"<b>المستخدم:</b> {user.first_name} (@{user.username})\n"
            f"<b>المعرف:</b> {user_id}\n"
            f"<b>الحالة:</b> في انتظار الموافقة\n\n"
            f"استخدم /approve {user_id} للموافقة أو /reject {user_id} للرفض."
        )
        
        await send_message(
            update,
            f"👋 مرحباً، {user.first_name}!\n\n"
            f"مرحباً بك في بوت إدارة حسابات تيليجرام.\n\n"
            f"⏳ تم إرسال طلب الوصول الخاص بك إلى مالك البوت للموافقة.\n"
            f"سيتم إعلامك بمجرد مراجعة طلبك.\n\n"
            f"شكراً لصبرك!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        status = existing_user.get("access_status", "pending")
        
        if status == "pending":
            await send_message(
                update,
                f"👋 مرحباً، {user.first_name}!\n\n"
                f"طلب الوصول الخاص بك لا يزال معلقًا في انتظار الموافقة.\n"
                f"سيتم إعلامك بمجرد مراجعة مالك البوت لطلبك.\n\n"
                f"شكراً لصبرك!",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif status == "approved":
            await send_message(
                update,
                f"👋 مرحباً بعودتك، {user.first_name}!\n\n"
                f"لديك وصول معتمد للبوت.\n\n"
                f"استخدم الأزرار أدناه للتنقل.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif status == "rejected":
            await send_message(
                update,
                f"👋 مرحباً، {user.first_name}!\n\n"
                f"تم رفض طلب الوصول الخاص بك.\n\n"
                f"إذا كنت تعتقد أن هذا خطأ، يرجى الاتصال بمالك البوت."
            )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /help."""
    user_id = update.effective_user.id
    status = get_user_status(user_id)
    
    if status != "approved":
        await send_message(
            update,
            "⛔ <b>الوصول مرفوض</b>\n\n"
            "ليس لديك إذن لاستخدام هذا البوت. "
            "قد يكون طلب الوصول الخاص بك لا يزال معلقًا أو مرفوضًا."
        )
        return
    
    help_text = (
        "🤖 <b>بوت إدارة حسابات تيليجرام</b>\n\n"
        "<b>الأوامر المتاحة:</b>\n\n"
        "📱 /accounts - إدارة حسابات تيليجرام الخاصة بك\n"
        "👥 /groups - إنشاء مجموعات تيليجرام\n"
        "ℹ️ /status - تحقق من حالة حسابك\n"
        "📊 /stats - عرض إحصائياتك\n"
    )
    
    if is_owner(user_id):
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

@approved_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /status."""
    user_id = update.effective_user.id
    user = users_collection.find_one({"user_id": user_id})
    
    if not user:
        await send_message(
            update,
            "❌ <b>خطأ</b>\n\n"
            "لم يتم العثور على حساب المستخدم الخاص بك في قاعدة البيانات. "
            "يرجى محاولة /start للتسجيل مرة أخرى."
        )
        return
    
    status = user.get("access_status", "unknown")
    request_date = user.get("request_date", datetime.datetime.now())
    request_date_str = request_date.strftime("%Y-%m-%d %H:%M:%S")
    
    accounts_count = accounts_collection.count_documents({"user_id": user_id})
    
    keyboard = [
        [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    
    status_text = (
        f"📊 <b>حالة حسابك</b>\n\n"
        f"🆔 <b>معرف المستخدم:</b> {user_id}\n"
        f"👤 <b>الاسم:</b> {user.get('first_name', 'N/A')}\n"
        f"🔖 <b>اسم المستخدم:</b> @{user.get('username', 'N/A')}\n"
        f"📅 <b>تاريخ الطلب:</b> {request_date_str}\n"
        f"✅ <b>حالة الوصول:</b> {status.capitalize()}\n"
        f"📱 <b>الحسابات المرتبطة:</b> {accounts_count}\n"
    )
    
    await send_message(
        update,
        status_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@approved_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /stats."""
    user_id = update.effective_user.id
    
    accounts = get_user_accounts(user_id)
    total_accounts = len(accounts)
    
    keyboard = [
        [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    
    if total_accounts == 0:
        await send_message(
            update,
            "📊 <b>إحصائياتك</b>\n\n"
            "ليس لديك أي حسابات مرتبطة حتى الآن.\n\n"
            "استخدم /accounts لإضافة حسابك الأول.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    active_sessions = sum(1 for acc in accounts if acc.get("session_data"))
    total_groups = sum(random.randint(0, 10) for _ in accounts) # Placeholder
    
    stats_text = (
        f"📊 <b>إحصائياتك</b>\n\n"
        f"📱 <b>إجمالي الحسابات:</b> {total_accounts}\n"
        f"🔐 <b>الجلسات النشطة:</b> {active_sessions}\n"
        f"👥 <b>المجموعات المنشأة:</b> {total_groups}\n"
    )
    
    await send_message(
        update,
        stats_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Owner commands
@owner_only
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /approve."""
    if not context.args:
        await send_message(
            update,
            "❌ <b>خطأ</b>\n\n"
            "يرجى تقديم معرف مستخدم للموافقة.\n\n"
            "الاستخدام: /approve [user_id]"
        )
        return
    
    try:
        user_id = int(context.args[0])
    except ValueError:
        await send_message(
            update,
            "❌ <b>خطأ</b>\n\n"
            "معرف مستخدم غير صالح. يرجى تقديم معرف مستخدم رقمي."
        )
        return
    
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        await send_message(
            update,
            "❌ <b>خطأ</b>\n\n"
            f"لم يتم العثور على المستخدم بالمعرف {user_id} في قاعدة البيانات."
        )
        return
    
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"access_status": "approved"}}
    )
    
    log_event("access_approved", f"User {user_id} was approved by owner", OWNER_ID)
    
    await send_notification(
        context,
        user_id,
        "✅ <b>تمت الموافقة على الوصول</b>\n\n"
        f"تمت الموافقة على طلب الوصول الخاص بك من قبل مالك البوت.\n\n"
        f"يمكنك الآن استخدام البوت. استخدم /help لرؤية الأوامر المتاحة.",
    )
    
    await send_message(
        update,
        f"✅ <b>نجح</b>\n\n"
        f"تمت الموافقة على المستخدم {user_id}.\n\n"
        f"تم إعلامهم بالموافقة."
    )

@owner_only
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /reject."""
    if not context.args:
        await send_message(
            update,
            "❌ <b>خطأ</b>\n\n"
            "يرجى تقديم معرف مستخدم للرفض.\n\n"
            "الاستخدام: /reject [user_id]"
        )
        return
    
    try:
        user_id = int(context.args[0])
    except ValueError:
        await send_message(
            update,
            "❌ <b>خطأ</b>\n\n"
            "معرف مستخدم غير صالح. يرجى تقديم معرف مستخدم رقمي."
        )
        return
    
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        await send_message(
            update,
            "❌ <b>خطأ</b>\n\n"
            f"لم يتم العثور على المستخدم بالمعرف {user_id} في قاعدة البيانات."
        )
        return
    
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"access_status": "rejected"}}
    )
    
    log_event("access_rejected", f"User {user_id} was rejected by owner", OWNER_ID)
    
    await send_notification(
        context,
        user_id,
        "❌ <b>تم رفض الوصول</b>\n\n"
        f"تم رفض طلب الوصول الخاص بك من قبل مالك البوت.\n\n"
        f"إذا كنت تعتقد أن هذا خطأ، يرجى الاتصال بمالك البوت.",
    )
    
    await send_message(
        update,
        f"❌ <b>نجح</b>\n\n"
        f"تم رفض المستخدم {user_id}.\n\n"
        f"تم إعلامهم بالرفض."
    )

@owner_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /users."""
    users = list(users_collection.find({}))
    
    if not users:
        await send_message(
            update,
            "📊 <b>المستخدمون</b>\n\n"
            "لم يتم العثور على مستخدمين في قاعدة البيانات."
        )
        return
    
    users_text = "📊 <b>جميع المستخدمين</b>\n\n"
    
    for user in users:
        user_id = user.get("user_id", "N/A")
        username = user.get("username", "N/A")
        first_name = user.get("first_name", "N/A")
        status = user.get("access_status", "unknown")
        request_date = user.get("request_date", datetime.datetime.now())
        request_date_str = request_date.strftime("%Y-%m-%d")
        
        users_text += (
            f"🆔 {user_id} - {first_name} (@{username})\n"
            f"   الحالة: {status.capitalize()}\n"
            f"   التاريخ: {request_date_str}\n\n"
        )
    
    if len(users_text) > 4000:
        chunks = [users_text[i:i+4000] for i in range(0, len(users_text), 4000)]
        for chunk in chunks:
            await send_message(update, chunk)
    else:
        await send_message(update, users_text)

@owner_only
async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /admin_stats."""
    total_users = users_collection.count_documents({})
    approved_users = users_collection.count_documents({"access_status": "approved"})
    pending_users = users_collection.count_documents({"access_status": "pending"})
    rejected_users = users_collection.count_documents({"access_status": "rejected"})
    
    total_accounts = accounts_collection.count_documents({})
    total_sessions = sessions_collection.count_documents({})
    
    settings = settings_collection.find_one({})
    monitoring_enabled = settings.get("monitoring_enabled", True) if settings else True
    
    stats_text = (
        f"📊 <b>إحصائيات النظام</b>\n\n"
        f"👥 <b>المستخدمون:</b>\n"
        f"   الإجمالي: {total_users}\n"
        f"   المعتمدون: {approved_users}\n"
        f"   المعلقون: {pending_users}\n"
        f"   المرفوضون: {rejected_users}\n\n"
        f"📱 <b>الحسابات:</b> {total_accounts}\n"
        f"🔐 <b>الجلسات:</b> {total_sessions}\n\n"
        f"⚙️ <b>الإعدادات:</b>\n"
        f"   مراقبة الجلسات: {'مفعلة' if monitoring_enabled else 'معطلة'}\n"
    )
    
    await send_message(update, stats_text)

@owner_only
async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /logs."""
    logs = list(logs_collection.find({}).sort("timestamp", -1).limit(50))
    
    if not logs:
        await send_message(
            update,
            "📊 <b>سجلات النظام</b>\n\n"
            "لم يتم العثور على سجلات في قاعدة البيانات."
        )
        return
    
    logs_text = "📊 <b>سجلات النظام</b>\n\n"
    
    for log in logs:
        timestamp = log.get("timestamp", datetime.datetime.now())
        timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        event_type = log.get("event_type", "unknown")
        details = log.get("details", "N/A")
        user_id = log.get("user_id", "N/A")
        
        logs_text += (
            f"📅 {timestamp_str}\n"
            f"🔖 الحدث: {event_type}\n"
            f"👤 المستخدم: {user_id}\n"
            f"📝 التفاصيل: {details}\n\n"
        )
    
    if len(logs_text) > 4000:
        chunks = [logs_text[i:i+4000] for i in range(0, len(logs_text), 4000)]
        for chunk in chunks:
            await send_message(update, chunk)
    else:
        await send_message(update, logs_text)

@owner_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /settings."""
    settings = settings_collection.find_one({})
    monitoring_enabled = settings.get("monitoring_enabled", True) if settings else True
    
    keyboard = [
        [
            InlineKeyboardButton(
                "تبديل مراقبة الجلسات",
                callback_data=f"toggle_monitoring_{monitoring_enabled}"
            )
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    settings_text = (
        f"⚙️ <b>إعدادات البوت</b>\n\n"
        f"🔐 <b>مراقبة الجلسات:</b> {'مفعلة' if monitoring_enabled else 'معطلة'}\n\n"
        f"استخدم الزر أدناه لتبديل مراقبة الجلسات."
    )
    
    await send_message(
        update,
        settings_text,
        reply_markup=reply_markup
    )

# Account management
@approved_only
async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /accounts."""
    return await display_accounts_page(update, context, 0)

async def display_accounts_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """عرض صفحة محددة من حسابات المستخدم."""
    user_id = update.effective_user.id
    accounts, total_pages = get_paginated_accounts(user_id, page)
    
    keyboard = [
        [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
        [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    
    if not accounts:
        reply_markup = InlineKeyboardMarkup(keyboard)
        await send_message(
            update,
            "📱 <b>حساباتك</b>\n\n"
            "ليس لديك أي حسابات مرتبطة حتى الآن.\n\n"
            "استخدم الزر أدناه لإضافة حسابك الأول.",
            reply_markup=reply_markup
        )
        return

    accounts_text = f"📱 <b>حساباتك (صفحة {page + 1}/{total_pages})</b>\n\n"
    
    for i, account in enumerate(accounts):
        account_id = str(account.get("_id"))
        phone = account.get("phone_number", "N/A")
        created_at = account.get("created_at", datetime.datetime.now())
        created_at_str = created_at.strftime("%Y-%m-%d")
        
        has_session = account.get("session_data") is not None
        session_status = "🟢 نشط" if has_session else "🔴 لا توجد جلسة"
        
        accounts_text += (
            f"{i + 1 + page * 10}. {phone}\n"
            f"   الحالة: {session_status}\n\n"
        )
    
    # Add navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"accounts_page_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"صفحة {page+1}/{total_pages}", callback_data="noop"))
    
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"accounts_page_{page+1}"))
    
    if len(nav_buttons) > 1:
        keyboard.insert(len(keyboard) - 1, nav_buttons) # Insert before the last row (back button)

    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_message(update, accounts_text, reply_markup=reply_markup)


# Account conversation handlers
async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء عملية إضافة الحساب."""
    await send_message(
        update,
        "📱 <b>إضافة حساب جديد</b>\n\n"
        "يرجى إدخال رقم هاتف حساب تيليجرام الذي تريد إضافته.\n\n"
        "تضمين رمز البلد، على سبيل المثال، +1234567890\n\n"
        "أرسل /cancel لإلغاء هذه العملية."
    )
    return ACCOUNT_PHONE

async def add_account_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال رقم الهاتف."""
    phone = update.message.text.strip().replace(' ', '').replace('-', '')
    if not phone.startswith('+'):
        phone = '+' + phone

    if not phone.startswith('+') or not phone[1:].isdigit() or len(phone[1:]) < 10:
        await send_message(
            update,
            "❌ <b>رقم هاتف غير صالح</b>\n\n"
            "يرجى إدخال رقم هاتف صالح مع رمز البلد.\n\n"
            "أمثلة صحيحة:\n"
            "• +966501234567\n\n"
            "أرسل /cancel لإلغاء هذه العملية."
        )
        return ACCOUNT_PHONE

    context.user_data["phone"] = phone

    if API_ID == 0 or not API_HASH:
        await send_message(update, "❌ <b>خطأ في الإعدادات</b>\n\n لم يتم تكوين معرفات API بشكل صحيح.")
        return ConversationHandler.END

    wait_message = await update.message.reply_text("⏳ <b>جاري إرسال رمز التحقق...</b>")
    
    client = TelegramClient(None, API_ID, API_HASH, timeout=30, connection_retries=2)
    try:
        await client.connect()
        if not client.is_connected():
            raise Exception("Failed to connect")
        
        result = await client.send_code_request(phone)
        context.user_data["phone_code_hash"] = result.phone_code_hash
        
        await wait_message.edit_text(
            "✅ <b>تم إرسال رمز التحقق</b>\n\n"
            f"تم إرسال الرمز إلى {phone}.\n\n"
            "يرجى إدخال الرمز الآن.\n\n"
            "أرسل /cancel لإلغاء."
        )
        return ACCOUNT_CODE

    except errors.FloodWaitError as e:
        wait_time = e.seconds
        wait_str = f"{wait_time} ثانية" if wait_time < 60 else f"{wait_time // 60} دقيقة"
        await wait_message.edit_text(f"⏳ <b>انتظار مطلوب</b>\n\n يرجى الانتظار {wait_str} ثم المحاولة مرة أخرى.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error sending code to {phone}: {e}")
        await wait_message.edit_text(f"❌ <b>حدث خطأ</b>\n\n لم يتم إرسال الرمز. الخطأ: {str(e)}")
        return ConversationHandler.END
    finally:
        if client.is_connected():
            await client.disconnect()

async def add_account_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال رمز التحقق."""
    code = update.message.text.strip()
    if not code.isdigit():
        await send_message(update, "❌ <b>رمز غير صالح</b>\n\n يجب أن يحتوي على أرقام فقط.")
        return ACCOUNT_CODE

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        await client.sign_in(
            phone=context.user_data["phone"],
            code=code,
            phone_code_hash=context.user_data["phone_code_hash"]
        )
        
        session_string = client.session.save()
        await client.disconnect()

        user_id = update.effective_user.id
        phone = context.user_data["phone"]
        
        accounts_collection.insert_one({
            "user_id": user_id,
            "phone_number": phone,
            "session_data": encrypt_data(session_string),
            "created_at": datetime.datetime.now()
        })
        
        log_event("account_added", f"Account {phone} added for user {user_id}", user_id)
        await notify_owner(context, f"📱 <b>حساب جديد مضاف</b>\n\n المستخدم: {update.effective_user.first_name}\n الحساب: {phone}")

        keyboard = [[InlineKeyboardButton("📱 حساباتي", callback_data="accounts")]]
        await send_message(update, "✅ <b>تمت إضافة الحساب بنجاح</b>", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    except errors.SessionPasswordNeededError:
        await client.disconnect()
        await send_message(update, "🔐 <b>مطلوب مصادقة ثنائية العامل (2FA)</b>\n\n يرجى إدخال كلمة مرور 2FA.")
        return ACCOUNT_PASSWORD
    except errors.PhoneCodeInvalidError:
        await send_message(update, "❌ <b>رمز التحقق غير صحيح</b>\n\n يرجى التحقق والمحاولة مرة أخرى.")
        return ACCOUNT_CODE
    except Exception as e:
        logger.error(f"Error signing in with code: {e}")
        await send_message(update, f"❌ <b>خطأ</b>\n\n فشل تسجيل الدخول: {str(e)}")
        return ACCOUNT_CODE
    finally:
        if client.is_connected():
            await client.disconnect()

async def add_account_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال كلمة مرور 2FA."""
    password = update.message.text.strip()
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        await client.sign_in(
            password=password,
            phone=context.user_data["phone"],
            code=context.user_data["code"],
            phone_code_hash=context.user_data["phone_code_hash"]
        )
        
        session_string = client.session.save()
        await client.disconnect()

        user_id = update.effective_user.id
        phone = context.user_data["phone"]

        accounts_collection.insert_one({
            "user_id": user_id,
            "phone_number": phone,
            "session_data": encrypt_data(session_string),
            "created_at": datetime.datetime.now()
        })

        log_event("account_added", f"Account {phone} added for user {user_id}", user_id)
        await notify_owner(context, f"📱 <b>حساب جديد مضاف (مع 2FA)</b>\n\n المستخدم: {update.effective_user.first_name}\n الحساب: {phone}")
        
        keyboard = [[InlineKeyboardButton("📱 حساباتي", callback_data="accounts")]]
        await send_message(update, "✅ <b>تمت إضافة الحساب بنجاح</b>", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    except errors.PasswordHashInvalidError:
        await send_message(update, "❌ <b>كلمة مرور غير صحيحة</b>\n\n يرجى التحقق من كلمة مرور 2FA.")
        return ACCOUNT_PASSWORD
    except Exception as e:
        logger.error(f"Error signing in with password: {e}")
        await send_message(update, f"❌ <b>خطأ</b>\n\n فشل تسجيل الدخول: {str(e)}")
        return ACCOUNT_PASSWORD
    finally:
        if client.is_connected():
            await client.disconnect()

async def cancel_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء عملية إضافة الحساب."""
    context.user_data.clear()
    keyboard = [[InlineKeyboardButton("📱 حساباتي", callback_data="accounts")]]
    await send_message(update, "❌ <b>تم إلغاء العملية</b>", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

# Group creation
@approved_only
async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /groups."""
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    active_accounts = [acc for acc in accounts if acc.get("session_data")]

    keyboard = [[InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]]

    if not active_accounts:
        await send_message(
            update,
            "📱 <b>لا توجد حسابات نشطة</b>\n\n"
            "تحتاج إلى إضافة حساب بنجاح قبل إنشاء المجموعات.\n\n"
            "استخدم /accounts لإضافة حساب.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    keyboard.insert(0, [InlineKeyboardButton("➕ إنشاء مجموعات", callback_data="create_groups")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    accounts_text = "📱 <b>الحسابات المتاحة لإنشاء المجموعات</b>\n\n"
    for acc in active_accounts:
        accounts_text += f"• {acc['phone_number']}\n"
    
    await send_message(update, accounts_text, reply_markup=reply_markup)

# Group creation conversation handlers
async def create_groups_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء عملية إنشاء المجموعات."""
    await send_message(
        update,
        "👥 <b>إنشاء مجموعات</b>\n\n"
        "ماذا تريد أن تسمي مجموعاتك؟\n\n"
        "مثال: 'مجموعتي' سيتم إنشاء 'مجموعتي 1', 'مجموعتي 2', ...\n\n"
        "أرسل /cancel لإلغاء."
    )
    return GROUP_NAME

async def create_groups_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال اسم المجموعة."""
    name = update.message.text.strip()
    if not name:
        await send_message(update, "❌ <b>اسم غير صالح</b>\n\n يرجى إدخال اسم.")
        return GROUP_NAME
    
    context.user_data["group_name"] = name
    await send_message(update, f"✅ <b>تم تعيين الاسم</b>\n\n كم مجموعة تريد إنشاءها؟ (1-50)")
    return GROUP_COUNT

async def create_groups_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال عدد المجموعات."""
    try:
        count = int(update.message.text.strip())
        if not 1 <= count <= 50:
            raise ValueError
    except ValueError:
        await send_message(update, "❌ <b>عدد غير صالح</b>\n\n يرجى إدخال رقم بين 1 و 50.")
        return GROUP_COUNT
    
    context.user_data["group_count"] = count
    await send_message(update, f"✅ <b>تم تعيين العدد</b>\n\n كم تأخير بين كل مجموعة؟ بالثواني (5-60)")
    return GROUP_DELAY

async def create_groups_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال التأخير بين المجموعات."""
    try:
        delay = int(update.message.text.strip())
        if not 5 <= delay <= 60:
            raise ValueError
    except ValueError:
        await send_message(update, "❌ <b>تأخير غير صالح</b>\n\n يرجى إدخال رقم بين 5 و 60 ثانية.")
        return GROUP_DELAY

    context.user_data["group_delay"] = delay
    
    user_id = update.effective_user.id
    active_accounts = [acc for acc in get_user_accounts(user_id) if acc.get("session_data")]

    keyboard = []
    keyboard.append([InlineKeyboardButton(f"استخدام جميع {len(active_accounts)} حساب", callback_data="use_all_accounts")])
    for acc in active_accounts:
        keyboard.append([InlineKeyboardButton(f"استخدام {acc['phone_number']}", callback_data=f"use_account_{acc['_id']}")])
    keyboard.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel_groups")])
    
    await send_message(
        update,
        f"✅ <b>اكتملت الإعدادات</b>\n\n"
        f"الاسم: {context.user_data['group_name']}\n"
        f"العدد: {context.user_data['group_count']}\n"
        f"التأخير: {context.user_data['group_delay']}s\n\n"
        "اختر الحساب (الحسابات) للاستخدام:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

async def cancel_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء عملية إنشاء المجموعات."""
    keyboard = [[InlineKeyboardButton("👥 المجموعات", callback_data="groups")]]
    await send_message(update, "❌ <b>تم إلغاء العملية</b>", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def create_groups_process(context: ContextTypes.DEFAULT_TYPE, user_id: int, accounts: List[Dict]):
    """بدء عملية إنشاء المجموعات في الخلفية."""
    asyncio.create_task(
        create_groups_background(
            context,
            user_id,
            accounts,
            context.user_data.get("group_name", "مجموعة"),
            context.user_data.get("group_count", 1),
            context.user_data.get("group_delay", 10)
        )
    )

async def create_groups_background(context: ContextTypes.DEFAULT_TYPE, user_id: int, accounts: List[Dict], group_name: str, group_count: int, group_delay: int):
    """إنشاء المجموعات في الخلفية."""
    created, failed = 0, 0
    for i in range(group_count):
        account = random.choice(accounts)
        try:
            client = TelegramClient(StringSession(account["session_data"]), API_ID, API_HASH)
            await client.connect()
            result = await client(functions.channels.CreateChannelRequest(title=f"{group_name} {i+1}", about="Created by bot", megagroup=True))
            await client.disconnect()
            created += 1
            log_event("group_created", f"Group '{group_name} {i+1}' created", user_id)
        except Exception as e:
            logger.error(f"Failed to create group {i+1}: {e}")
            failed += 1
        finally:
            if client.is_connected(): await client.disconnect()
        if i < group_count - 1:
            await asyncio.sleep(group_delay)
    
    await send_notification(
        context, user_id,
        f"✅ <b>اكتمل إنشاء المجموعات</b>\n\n"
        f"النجاح: {created}\nالفشل: {failed}"
    )

# Callback query handlers
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة استدعاءات الأزرار."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = update.effective_user.id

    if data == "main_menu":
        await start(update, context)
    elif data == "status":
        await status_command(update, context)
    elif data == "stats":
        await stats_command(update, context)
    elif data == "accounts":
        await accounts_command(update, context)
    elif data == "groups":
        await groups_command(update, context)
    elif data == "add_account":
        return await add_account_start(update, context)
    elif data.startswith("accounts_page_"):
        page = int(data.split("_")[-1])
        await display_accounts_page(update, context, page)
    elif data.startswith("delete_account_"):
        account_id = data.split("_")[-1]
        account = accounts_collection.find_one({"_id": account_id})
        if account:
            accounts_collection.delete_one({"_id": account_id})
            log_event("account_deleted", f"Account {account['phone_number']} deleted", user_id)
            await send_message(update, f"✅ <b>تم حذف الحساب</b>\n\n {account['phone_number']}")
        else:
            await send_message(update, "❌ <b>لم يتم العثور على الحساب</b>")
    elif data.startswith("toggle_monitoring_"):
        current_state = data.split("_")[-1] == "True"
        new_state = not current_state
        settings_collection.update_one({}, {"$set": {"monitoring_enabled": new_state}})
        await send_message(update, f"✅ <b>تم التحديث</b>\n\n مراقبة الجلسات: {'مفعلة' if new_state else 'معطلة'}")
    elif data == "create_groups":
        return await create_groups_start(update, context)
    elif data == "cancel_groups":
        return await cancel_groups(update, context)
    elif data == "use_all_accounts":
        user_id = update.effective_user.id
        active_accounts = [acc for acc in get_user_accounts(user_id) if acc.get("session_data")]
        if active_accounts:
            await create_groups_process(context, user_id, active_accounts)
            await send_message(update, "✅ <b>تم بدء العملية</b>\n\n سيتم إعلامك عند الانتهاء.")
    elif data.startswith("use_account_"):
        account_id = data.split("_")[-1]
        account = accounts_collection.find_one({"_id": account_id})
        if account and account.get("session_data"):
            await create_groups_process(context, user_id, [account])
            await send_message(update, "✅ <b>تم بدء العملية</b>\n\n سيتم إعلامك عند الانتهاء.")
        else:
            await send_message(update, "❌ <b>الحساب غير نشط</b>")


def main():
    """بدء تشغيل البوت."""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("groups", groups_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("reject", reject_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("admin_stats", admin_stats_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("settings", settings_command))
    
    # Conversations
    account_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_account_start, pattern="^add_account$")],
        states={
            ACCOUNT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_phone)],
            ACCOUNT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_code)],
            ACCOUNT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_account)],
        per_message=False
    )
    application.add_handler(account_conv_handler)

    groups_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_groups_start, pattern="^create_groups$")],
        states={
            GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_name)],
            GROUP_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_count)],
            GROUP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_delay)],
        },
        fallbacks=[CallbackQueryHandler(cancel_groups, pattern="^cancel_groups$")],
        per_message=False
    )
    application.add_handler(groups_conv_handler)
    
    # Callbacks
    application.add_handler(CallbackQueryHandler(button_callback))
    
    application.run_polling()

if __name__ == "__main__":
    main()
