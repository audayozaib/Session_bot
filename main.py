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
from bson.objectid import ObjectId # <-- تمت إضافة هذا الاستيراد

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

async def send_notification(context: ContextTypes.DEFAULT_TYPE, user_id: int, message: str, reply_markup=None):
    """إرسال إشعار إلى مستخدم."""
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Failed to send notification to {user_id}: {e}")

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, message: str, reply_markup=None):
    """إرسال إشعار إلى مالك البوت."""
    await send_notification(context, OWNER_ID, message, reply_markup=reply_markup)

# Decorators
def approved_only(func):
    """مصمم لتقييد الوصول للمستخدمين المعتمدين فقط."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_approved(user_id):
            if update.message:
                await update.message.reply_text(
                    "⛔ <b>الوصول مرفوض</b>\n\n"
                    "ليس لديك إذن لاستخدام هذا البوت. "
                    "قد يكون طلب الوصول الخاص بك لا يزال معلقًا أو مرفوضًا.",
                    parse_mode=ParseMode.HTML
                )
            elif update.callback_query:
                await update.callback_query.message.reply_text(
                    "⛔ <b>الوصول مرفوض</b>\n\n"
                    "ليس لديك إذن لاستخدام هذا البوت. "
                    "قد يكون طلب الوصول الخاص بك لا يزال معلقًا أو مرفوضًا.",
                    parse_mode=ParseMode.HTML
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
            if update.message:
                await update.message.reply_text(
                    "⛔ <b>الوصول مرفوض</b>\n\n"
                    "هذا الأمر متاح فقط لمالك البوت.",
                    parse_mode=ParseMode.HTML
                )
            elif update.callback_query:
                await update.callback_query.message.reply_text(
                    "⛔ <b>الوصول مرفوض</b>\n\n"
                    "هذا الأمر متاح فقط لمالك البوت.",
                    parse_mode=ParseMode.HTML
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
        return await update.callback_query.message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
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
        
        # إنشاء أزرار للموافقة للمالك
        owner_keyboard = [
            [InlineKeyboardButton(f"✅ موافقة", callback_data=f"approve_user_{user_id}")],
            [InlineKeyboardButton(f"❌ رفض", callback_data=f"reject_user_{user_id}")]
        ]
        
        await notify_owner(
            context,
            f"🔔 <b>طلب وصول جديد</b>\n\n"
            f"<b>المستخدم:</b> {user.first_name} (@{user.username})\n"
            f"<b>المعرف:</b> {user_id}\n"
            f"<b>الحالة:</b> في انتظار الموافقة\n\n"
            f"استخدم الأزرار أدناه للموافقة أو الرفض.",
            reply_markup=InlineKeyboardMarkup(owner_keyboard)
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
    
    active_sessions = 0
    total_groups = 0
    
    for account in accounts:
        if "session_data" in account and account["session_data"]:
            active_sessions += 1
        total_groups += random.randint(0, 10)
    
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
    user_id = update.effective_user.id
    accounts, total_pages = get_paginated_accounts(user_id)
    
    keyboard = [
        [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    
    if not accounts:
        keyboard.insert(0, [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await send_message(
            update,
            "📱 <b>حساباتك</b>\n\n"
            "ليس لديك أي حسابات مرتبطة حتى الآن.\n\n"
            "استخدم الزر أدناه لإضافة حسابك الأول.",
            reply_markup=reply_markup
        )
        return
    
    accounts_text = "📱 <b>حساباتك</b>\n\n"
    
    for i, account in enumerate(accounts):
        account_id = str(account.get("_id", "N/A"))
        phone = account.get("phone_number", "N/A")
        created_at = account.get("created_at", datetime.datetime.now())
        created_at_str = created_at.strftime("%Y-%m-%d")
        
        has_session = "session_data" in account and account["session_data"]
        session_status = "🟢 نشط" if has_session else "🔴 لا توجد جلسة"
        
        accounts_text += (
            f"{i+1}. {phone}\n"
            f"   المعرف: {account_id}\n"
            f"   تاريخ الإضافة: {created_at_str}\n"
            f"   الحالة: {session_status}\n\n"
        )
        
        keyboard.insert(-1, [
            InlineKeyboardButton(f"إدارة {phone}", callback_data=f"manage_account_{account_id}"),
            InlineKeyboardButton(f"حذف {phone}", callback_data=f"delete_account_{account_id}") # هذا الزر سيتم حذفه لاحقاً
        ])
    
    if total_pages > 1:
        nav_buttons = []
        if 0 > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"accounts_page_{0-1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"صفحة 1/{total_pages}", callback_data="noop"))
        
        if total_pages > 1:
            nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"accounts_page_{1}"))
        
        keyboard.insert(-1, nav_buttons)
    
    keyboard.insert(-1, [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await send_message(
        update,
        accounts_text,
        reply_markup=reply_markup
    )

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
    """معالجة إدخال رقم الهاتف وإرسال رمز التحقق."""
    try:
        phone = update.message.text.strip().replace(' ', '')
        if not phone.startswith('+'):
            phone = '+' + phone

        context.user_data.clear()
        context.user_data["phone"] = phone

        wait_msg = await update.message.reply_text(
            "⏳ <b>جاري طلب الرمز من تيليجرام...</b>",
            parse_mode=ParseMode.HTML
        )

        session = StringSession()
        client = TelegramClient(session, API_ID, API_HASH)
        await client.connect()

        result = await client.send_code_request(phone)

        context.user_data["phone_code_hash"] = result.phone_code_hash
        context.user_data["session"] = session.save()

        await wait_msg.edit_text(
            f"✅ <b>تم إرسال الرمز</b>\n\n"
            f"الرقم: <code>{phone}</code>\n"
            "أرسل كود التحقق الآن.",
            parse_mode=ParseMode.HTML
        )

        await client.disconnect()
        return ACCOUNT_CODE

    except Exception as e:
        logger.error(f"Error in add_account_phone: {e}")
        await update.message.reply_text("❌ فشل إرسال الرمز، حاول لاحقاً.")
        return ConversationHandler.END

async def add_account_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة كود التحقق مع إعادة الإرسال الصحيحة."""
    code = update.message.text.strip().replace(' ', '')

    phone = context.user_data.get("phone")
    phone_code_hash = context.user_data.get("phone_code_hash")
    session_str = context.user_data.get("session")

    if not all([phone, phone_code_hash, session_str]):
        await update.message.reply_text("❌ انتهت الجلسة، استخدم /start من جديد.")
        return ConversationHandler.END

    wait_msg = await update.message.reply_text("🔄 جاري التحقق من الكود...")

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()

    try:
        await client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=phone_code_hash
        )

        session_final = client.session.save()

        accounts_collection.insert_one({
            "user_id": update.effective_user.id,
            "phone_number": phone,
            "session_data": encrypt_data(session_final),
            "created_at": datetime.datetime.now()
        })

        await wait_msg.edit_text(
            f"✅ تم ربط الحساب <code>{phone}</code> بنجاح!",
            parse_mode=ParseMode.HTML
        )

        return ConversationHandler.END

    except errors.SessionPasswordNeededError:
        context.user_data["session"] = client.session.save()
        await wait_msg.edit_text("🔐 الحساب محمي بالتحقق بخطوتين.\nأرسل كلمة المرور:")
        return ACCOUNT_PASSWORD

    except errors.PhoneCodeExpiredError:
        await wait_msg.edit_text("♻️ انتهت صلاحية الكود، جارٍ إرسال كود جديد...")

        result = await client.send_code_request(phone)

        context.user_data["phone_code_hash"] = result.phone_code_hash
        context.user_data["session"] = client.session.save()

        await update.message.reply_text(
            "📩 تم إرسال كود جديد.\n"
            "أرسل رمز التحقق الجديد:"
        )

        return ACCOUNT_CODE

    except errors.PhoneCodeInvalidError:
        await wait_msg.edit_text("❌ الكود غير صحيح، حاول مرة أخرى.")
        return ACCOUNT_CODE

    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ: {str(e)}")
        return ConversationHandler.END

    finally:
        if client.is_connected():
            await client.disconnect()

async def add_account_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة كلمة مرور التحقق الثنائي."""
    password = update.message.text.strip()
    session_str = context.user_data.get("session")
    phone = context.user_data.get("phone")

    if not session_str or not phone:
        await send_message(update, "❌ انتهت الجلسة، استخدم /start.")
        return ConversationHandler.END

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()

    try:
        await client.sign_in(password=password)

        session_final = client.session.save()

        account_id = accounts_collection.insert_one({
            "user_id": update.effective_user.id,
            "phone_number": phone,
            "session_data": encrypt_data(session_final),
            "created_at": datetime.datetime.now()
        }).inserted_id

        log_event("account_added", f"Account {phone} added", update.effective_user.id)

        await notify_owner(
            context,
            f"📱 <b>حساب جديد</b>\n\n"
            f"المستخدم: {update.effective_user.id}\n"
            f"الحساب: {phone}\n"
            f"ID: {account_id}"
        )

        keyboard = [
            [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
            [InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="main_menu")]
        ]

        await send_message(
            update,
            f"✅ <b>تمت إضافة الحساب {phone} بنجاح</b>",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        return ConversationHandler.END

    except errors.PasswordHashInvalidError:
        await send_message(update, "❌ كلمة المرور غير صحيحة، حاول مرة أخرى.")
        return ACCOUNT_PASSWORD

    except Exception as e:
        logger.error(f"2FA error: {e}")
        await send_message(update, "❌ فشل تسجيل الدخول.")
        return ConversationHandler.END

    finally:
        await client.disconnect()

async def cancel_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء عملية إضافة الحساب."""
    keys_to_remove = ["phone", "phone_code_hash", "code", "password"]
    for key in keys_to_remove:
        if key in context.user_data:
            del context.user_data[key]
    
    keyboard = [
        [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
        [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    
    await send_message(
        update,
        "❌ <b>تم إلغاء العملية</b>\n\n"
        "تم إلغاء عملية إضافة الحساب بنجاح.\n\n"
        "يمكنك البدء مرة أخرى عند الضرورة.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

# Group creation
@approved_only
async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أمر /groups."""
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    keyboard = [
        [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    
    if not accounts:
        await send_message(
            update,
            "📱 <b>لا توجد حسابات متاحة</b>\n\n"
            "تحتاج إلى إضافة حساب واحد على الأقل قبل إنشاء المجموعات.\n\n"
            "استخدم /accounts لإضافة حساب.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    keyboard.insert(0, [InlineKeyboardButton("➕ إنشاء مجموعات", callback_data="create_groups")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    accounts_text = "📱 <b>الحسابات المتاحة لإنشاء المجموعات</b>\n\n"
    
    for i, account in enumerate(accounts):
        phone = account.get("phone_number", "N/A")
        has_session = "session_data" in account and account["session_data"]
        session_status = "🟢 نشط" if has_session else "🔴 لا توجد جلسة"
        
        accounts_text += f"{i+1}. {phone} - {session_status}\n"
    
    accounts_text += "\nاستخدم الزر أدناه لإنشاء المجموعات."
    
    await send_message(
        update,
        accounts_text,
        reply_markup=reply_markup
    )

# Group creation conversation handlers
async def create_groups_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء عملية إنشاء المجموعات."""
    await send_message(
        update,
        "👥 <b>إنشاء مجموعات</b>\n\n"
        "دعنا نكوين إعدادات إنشاء المجموعات الخاصة بك.\n\n"
        "أولاً، ماذا تريد أن تسمي مجموعاتك؟\n\n"
        "يمكنك استخدام نمط مثل 'مجموعتي' وسينشئ البوت 'مجموعتي 1'، 'مجموعتي 2'، إلخ.\n\n"
        "أرسل /cancel لإلغاء هذه العملية."
    )
    return GROUP_NAME

async def create_groups_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال اسم المجموعة."""
    name = update.message.text.strip()
    
    if not name:
        await send_message(
            update,
            "❌ <b>اسم غير صالح</b>\n\n"
            "يرجى إدخال اسم مجموعة صالح.\n\n"
            "أرسل /cancel لإلغاء هذه العملية."
        )
        return GROUP_NAME
    
    context.user_data["group_name"] = name
    
    await send_message(
        update,
        f"✅ <b>تم تعيين اسم المجموعة</b>\n\n"
        f"سيتم تسمية المجموعات: '{name} 1'، '{name} 2'، إلخ.\n\n"
        "كم مجموعة تريد إنشاءها؟\n\n"
        "يرجى إدخال رقم بين 1 و 50.\n\n"
        "أرسل /cancel لإلغاء هذه العملية."
    )
    return GROUP_COUNT

async def create_groups_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال عدد المجموعات."""
    count_text = update.message.text.strip()
    
    try:
        count = int(count_text)
        if count < 1 or count > 50:
            raise ValueError("Count out of range")
    except ValueError:
        await send_message(
            update,
            "❌ <b>عدد غير صالح</b>\n\n"
            "يرجى إدخال رقم بين 1 و 50.\n\n"
            "أرسل /cancel لإلغاء هذه العملية."
        )
        return GROUP_COUNT
    
    context.user_data["group_count"] = count
    
    await send_message(
        update,
        f"✅ <b>تم تعيين عدد المجموعات</b>\n\n"
        f"ستقوم بإنشاء {count} مجموعة.\n\n"
        "كم من التأخير تريده بين إنشاء كل مجموعة؟\n\n"
        "يرجى إدخال التأخير بالثواني (بين 5 و 60).\n\n"
        "أرسل /cancel لإلغاء هذه العملية."
    )
    return GROUP_DELAY

async def create_groups_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال التأخير بين المجموعات."""
    delay_text = update.message.text.strip()
    
    try:
        delay = int(delay_text)
        if delay < 5 or delay > 60:
            raise ValueError("Delay out of range")
    except ValueError:
        await send_message(
            update,
            "❌ <b>تأخير غير صالح</b>\n\n"
            "يرجى إدخال رقم بين 5 و 60.\n\n"
            "أرسل /cancel لإلغاء هذه العملية."
        )
        return GROUP_DELAY
    
    context.user_data["group_delay"] = delay
    
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    active_accounts = []
    for account in accounts:
        if "session_data" in account and account["session_data"]:
            active_accounts.append(account)
    
    if not active_accounts:
        await send_message(
            update,
            "❌ <b>لا توجد جلسات نشطة</b>\n\n"
            "ليس لديك أي حسابات بها جلسات نشطة.\n\n"
            "يرجى إضافة حساب به جلسة نشطة أولاً.\n\n"
            "أرسل /cancel لإلغاء هذه العملية."
        )
        return ConversationHandler.END
    
    keyboard = []
    
    keyboard.append([
        InlineKeyboardButton(
            f"استخدام جميع {len(active_accounts)} حساب",
            callback_data="use_all_accounts"
        )
    ])
    
    for account in active_accounts:
        phone = account.get("phone_number", "N/A")
        account_id = str(account.get("_id", "N/A"))
        keyboard.append([
            InlineKeyboardButton(
                f"استخدام {phone}",
                callback_data=f"use_account_{account_id}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel_groups")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await send_message(
        update,
        "✅ <b>تم تكوين الإعدادات</b>\n\n"
        f"اسم المجموعة: {context.user_data['group_name']}\n"
        f"عدد المجموعات: {context.user_data['group_count']}\n"
        f"التأخير بين المجموعات: {context.user_data['group_delay']} ثانية\n\n"
        "يرجى تحديد الحساب (الحسابات) التي تريد استخدامها لإنشاء المجموعات:",
        reply_markup=reply_markup
    )
    return ConversationHandler.END

async def cancel_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء عملية إنشاء المجموعات."""
    keyboard = [
        [InlineKeyboardButton("👥 المجموعات", callback_data="groups")],
        [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    
    await send_message(
        update,
        "❌ <b>تم إلغاء العملية</b>\n\n"
        "تم إلغاء عملية إنشاء المجموعات.\n\n"
        "استخدم /groups للمحاولة مرة أخرى.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

# Callback query handlers
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة استدعاءات الأزرار."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = update.effective_user.id
    
    if data == "main_menu":
        keyboard = [
            [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
            [InlineKeyboardButton("👥 المجموعات", callback_data="groups")],
            [InlineKeyboardButton("📊 حالتي", callback_data="status")],
            [InlineKeyboardButton("📊 إحصائياتي", callback_data="stats")],
        ]
        
        await query.message.reply_text(
            "🏠 <b>القائمة الرئيسية</b>\n\n"
            "يرجى اختيار الخيار الذي تريده:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        return
    
    elif data == "status":
        await status_command(update, context)
        return
    
    elif data == "stats":
        await stats_command(update, context)
        return
    
    elif data == "accounts":
        await accounts_command(update, context)
        return
    
    elif data == "groups":
        await groups_command(update, context)
        return
    
    elif data == "add_account":
        return await add_account_start(update, context)
    
    elif data.startswith("manage_account_"):
        account_id_str = data.split("_", 2)[2]
        try:
            account_id = ObjectId(account_id_str)
        except Exception:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nمعرف الحساب غير صالح.", parse_mode=ParseMode.HTML)
            return
        
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nلم يتم العثور على الحساب.", parse_mode=ParseMode.HTML)
            return
        
        phone = account.get("phone_number", "N/A")
        created_at = account.get("created_at", datetime.datetime.now())
        created_at_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
        
        has_session = "session_data" in account and account["session_data"]
        session_status = "🟢 نشط" if has_session else "🔴 لا توجد جلسة"
        is_monitoring = account.get("monitor_sessions", False)
        
        keyboard = []
        
        if has_session:
            keyboard.append([InlineKeyboardButton("🔄 تحديث الجلسة", callback_data=f"refresh_session_{account_id_str}")])
            keyboard.append([InlineKeyboardButton("🗑️ حذف الجلسة", callback_data=f"delete_session_{account_id_str}")])
        else:
            keyboard.append([InlineKeyboardButton("➕ إنشاء جلسة", callback_data=f"create_session_{account_id_str}")])
        
        keyboard.append([InlineKeyboardButton("🔔 تفعيل المراقبة" if not is_monitoring else "🔕 إيقاف المراقبة", callback_data=f"toggle_monitoring_account_{account_id_str}")])
        keyboard.append([InlineKeyboardButton("🗑️ حذف جميع الجلسات", callback_data=f"terminate_all_sessions_{account_id_str}")])
        keyboard.append([InlineKeyboardButton("⬅️ العودة إلى الحسابات", callback_data="accounts")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        account_text = (
            f"📱 <b>تفاصيل الحساب</b>\n\n"
            f"📞 <b>الهاتف:</b> {phone}\n"
            f"🆔 <b>المعرف:</b> {account_id_str}\n"
            f"📅 <b>تاريخ الإضافة:</b> {created_at_str}\n"
            f"🔐 <b>حالة الجلسة:</b> {session_status}\n"
            f"🔔 <b>حالة المراقبة:</b> {'مفعلة' if is_monitoring else 'معطلة'}\n\n"
            f"استخدم الأزرار أدناه لإدارة هذا الحساب."
        )
        
        await query.message.reply_text(account_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    elif data.startswith("approve_user_"):
        target_user_id = int(data.split("_", 2)[2])
        user = users_collection.find_one({"user_id": target_user_id})
        if not user:
            await query.message.reply_text("❌ لم يتم العثور على المستخدم.", parse_mode=ParseMode.HTML)
            return

        users_collection.update_one({"user_id": target_user_id}, {"$set": {"access_status": "approved"}})
        log_event("access_approved", f"User {target_user_id} was approved by owner", OWNER_ID)
        
        await send_notification(context, target_user_id, "✅ <b>تمت الموافقة على الوصول</b>\n\nتمت الموافقة على طلبك. يمكنك الآن استخدام البوت.")
        await query.message.reply_text(f"✅ تمت الموافقة على المستخدم {target_user_id}.", parse_mode=ParseMode.HTML)
        await query.edit_message_reply_markup(reply_markup=None) # إزالة الأزرار

    elif data.startswith("reject_user_"):
        target_user_id = int(data.split("_", 2)[2])
        user = users_collection.find_one({"user_id": target_user_id})
        if not user:
            await query.message.reply_text("❌ لم يتم العثور على المستخدم.", parse_mode=ParseMode.HTML)
            return

        users_collection.update_one({"user_id": target_user_id}, {"$set": {"access_status": "rejected"}})
        log_event("access_rejected", f"User {target_user_id} was rejected by owner", OWNER_ID)
        
        await send_notification(context, target_user_id, "❌ <b>تم رفض الوصول</b>\n\nتم رفض طلبك. إذا كان هناك خطأ، تواصل مع المالك.")
        await query.message.reply_text(f"❌ تم رفض المستخدم {target_user_id}.", parse_mode=ParseMode.HTML)
        await query.edit_message_reply_markup(reply_markup=None) # إزالة الأزرار

    elif data.startswith("toggle_monitoring_account_"):
        account_id_str = data.split("_", 3)[3]
        try:
            account_id = ObjectId(account_id_str)
        except Exception:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nمعرف الحساب غير صالح.", parse_mode=ParseMode.HTML)
            return

        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nلم يتم العثور على الحساب.", parse_mode=ParseMode.HTML)
            return
        
        new_status = not account.get("monitor_sessions", False)
        accounts_collection.update_one({"_id": account_id}, {"$set": {"monitor_sessions": new_status}})
        
        status_text = "تفعيل" if new_status else "إيقاف"
        await query.message.reply_text(f"✅ <b>تم {status_text} المراقبة</b>\n\nتم {status_text.lower()} مراقبة جلسات الحساب بنجاح.", parse_mode=ParseMode.HTML)

    elif data.startswith("terminate_all_sessions_"):
        account_id_str = data.split("_", 3)[3]
        try:
            account_id = ObjectId(account_id_str)
        except Exception:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nمعرف الحساب غير صالح.", parse_mode=ParseMode.HTML)
            return

        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nلم يتم العثور على الحساب.", parse_mode=ParseMode.HTML)
            return
        
        phone = account.get("phone_number", "N/A")
        session_data = account.get("session_data")
        
        if not session_data:
            await query.message.reply_text("❌ <b>لا توجد جلسة</b>\n\nهذا الحساب ليس لديه جلسة نشطة لإنهائها.", parse_mode=ParseMode.HTML)
            return

        processing_msg = await query.message.reply_text("⏳ <b>جاري إنهاء جميع الجلسات...</b>", parse_mode=ParseMode.HTML)
        
        try:
            decrypted_session = decrypt_data(session_data)
            client = TelegramClient(StringSession(decrypted_session), API_ID, API_HASH)
            await client.connect()
            await client(functions.auth.ResetAuthorizationsRequest())
            await client.disconnect()
            
            accounts_collection.update_one({"_id": account_id}, {"$unset": {"session_data": ""}})
            log_event("all_sessions_terminated", f"All sessions terminated for account {phone}", user_id)
            
            await processing_msg.edit_text(
                f"✅ <b>تم إنهاء جميع الجلسات</b>\n\n"
                f"تم قطع اتصال جميع الأجهزة من الحساب {phone}.\n\n"
                f"ستحتاج إلى إنشاء جلسة جديدة لاستخدام هذا الحساب.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Error terminating sessions for {phone}: {e}")
            await processing_msg.edit_text("❌ <b>فشل إنهاء الجلسات</b>\n\nلم يتمكن البوت من إنهاء الجلسات. تأكد من أن الجلسة الحالية صالحة.", parse_mode=ParseMode.HTML)
    
    elif data.startswith("refresh_session_"):
        account_id_str = data.split("_", 2)[2]
        try:
            account_id = ObjectId(account_id_str)
        except Exception:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nمعرف الحساب غير صالح.", parse_mode=ParseMode.HTML)
            return
        
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nلم يتم العثور على الحساب.", parse_mode=ParseMode.HTML)
            return
        
        phone = account.get("phone_number", "N/A")
        processing_message = await query.message.reply_text("⏳ <b>جاري تحديث الجلسة</b>\n\nيرجى الانتظار...", parse_mode=ParseMode.HTML)
        
        try:
            session_data = decrypt_data(account["session_data"])
            client = TelegramClient(StringSession(session_data), API_ID, API_HASH)
            await client.connect()
            
            if await client.is_user_authorized():
                session_string = client.session.save()
                await client.disconnect()
                accounts_collection.update_one({"_id": account_id}, {"$set": {"session_data": encrypt_data(session_string)}})
                log_event("session_refreshed", f"Session refreshed for account {phone}", user_id)
                await processing_message.edit_text(f"✅ <b>تم تحديث الجلسة بنجاح</b>\n\nالجلسة الآن صالحة للاستخدام.", parse_mode=ParseMode.HTML)
            else:
                await client.disconnect()
                await processing_message.edit_text(f"❌ <b>فشل تحديث الجلسة</b>\n\nجلسة الحساب {phone} غير صالحة.\n\nيرجى إنشاء جلسة جديدة.", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error refreshing session: {e}")
            await processing_message.edit_text(f"❌ <b>خطأ في تحديث الجلسة</b>\n\n{str(e)}", parse_mode=ParseMode.HTML)
    
    elif data.startswith("delete_session_"):
        account_id_str = data.split("_", 2)[2]
        try:
            account_id = ObjectId(account_id_str)
        except Exception:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nمعرف الحساب غير صالح.", parse_mode=ParseMode.HTML)
            return
        
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nلم يتم العثور على الحساب.", parse_mode=ParseMode.HTML)
            return
        
        phone = account.get("phone_number", "N/A")
        keyboard = [
            [InlineKeyboardButton("✅ نعم، احذف", callback_data=f"confirm_delete_session_{account_id_str}")],
            [InlineKeyboardButton("❌ لا، إلغاء", callback_data=f"manage_account_{account_id_str}")]
        ]
        await query.message.reply_text(
            f"⚠️ <b>تأكيد حذف الجلسة</b>\n\n"
            f"هل أنت متأكد من أنك تريد حذف جلسة الحساب {phone}؟",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data.startswith("confirm_delete_session_"):
        account_id_str = data.split("_", 3)[3]
        try:
            account_id = ObjectId(account_id_str)
        except Exception:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nمعرف الحساب غير صالح.", parse_mode=ParseMode.HTML)
            return
        
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nلم يتم العثور على الحساب.", parse_mode=ParseMode.HTML)
            return
        
        phone = account.get("phone_number", "N/A")
        accounts_collection.update_one({"_id": account_id}, {"$unset": {"session_data": ""}})
        log_event("session_deleted", f"Session deleted for account {phone}", user_id)
        await query.message.reply_text(f"✅ <b>تم حذف الجلسة</b>\n\nتم حذف جلسة الحساب {phone} بنجاح.", parse_mode=ParseMode.HTML)
    
    elif data.startswith("create_session_"):
        account_id_str = data.split("_", 2)[2]
        try:
            account_id = ObjectId(account_id_str)
        except Exception:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nمعرف الحساب غير صالح.", parse_mode=ParseMode.HTML)
            return
        
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text("❌ <b>خطأ</b>\n\nلم يتم العثور على الحساب.", parse_mode=ParseMode.HTML)
            return
        
        phone = account.get("phone_number", "N/A")
        context.user_data["session_account_id"] = account_id_str
        context.user_data["session_phone"] = phone
        
        await query.message.reply_text("📱 <b>إنشاء جلسة جديدة</b>\n\nيرجى الانتظار...", parse_mode=ParseMode.HTML)
        
        try:
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            result = await client.send_code_request(phone)
            await client.disconnect()
            
            context.user_data["session_phone_code_hash"] = result.phone_code_hash
            
            await query.message.reply_text(
                "✅ <b>تم إرسال رمز التحقق</b>\n\n"
                "يرجى إدخال الرمز الذي تلقيته.\n\n"
                "أرسل /cancel لإلغاء هذه العملية.",
                parse_mode=ParseMode.HTML
            )
            return ACCOUNT_CODE
            
        except Exception as e:
            logger.error(f"Error sending code request: {e}")
            await query.message.reply_text(f"❌ <b>خطأ</b>\n\nفشل في إرسال رمز التحقق: {str(e)}", parse_mode=ParseMode.HTML)
    
    elif data.startswith("toggle_monitoring_"):
        current_state = data.split("_", 2)[2] == "True"
        new_state = not current_state
        settings_collection.update_one({}, {"$set": {"monitoring_enabled": new_state}})
        log_event("settings_changed", f"Session monitoring toggled from {current_state} to {new_state}", update.effective_user.id)
        
        keyboard = [[InlineKeyboardButton("تبديل مراقبة الجلسات", callback_data=f"toggle_monitoring_{new_state}")]]
        settings_text = f"⚙️ <b>إعدادات البوت</b>\n\n🔐 <b>مراقبة الجلسات:</b> {'مفعلة' if new_state else 'معطلة'}\n\nاستخدم الزر أدناه لتبديل مراقبة الجلسات."
        await query.edit_message_text(settings_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    
    elif data == "noop":
        pass
    
    else:
        await query.message.reply_text("❌ <b>خطأ</b>\n\nإجراء غير معروف. يرجى المحاولة مرة أخرى.", parse_mode=ParseMode.HTML)

# Helper function for creating groups with accounts
async def create_groups_with_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE, accounts: List[Dict]):
    """إنشاء مجموعات باستخدام الحسابات المحددة."""
    query = update.callback_query
    user_id = update.effective_user.id
    
    group_name = context.user_data.get("group_name", "Group")
    group_count = context.user_data.get("group_count", 1)
    group_delay = context.user_data.get("group_delay", 10)
    
    processing_message = await query.message.reply_text(
        "⏳ <b>جاري إنشاء المجموعات</b>\n\n"
        f"يتم الآن إنشاء {group_count} مجموعة باستخدام {len(accounts)} حساب.\n\n"
        f"اسم المجموعة: {group_name}\n"
        f"التأخير بين المجموعات: {group_delay} ثانية\n\n"
        "يرجى الانتظار...",
        parse_mode=ParseMode.HTML
    )
    
    created_groups = 0
    failed_groups = 0
    
    try:
        for i in range(group_count):
            account = accounts[i % len(accounts)]
            phone = account.get("phone_number", "N/A")
            session_data = account.get("session_data", "")
            
            try:
                client = TelegramClient(StringSession(session_data), API_ID, API_HASH)
                await client.connect()
                
                if await client.is_user_authorized():
                    group_title = f"{group_name} {i+1}"
                    # **التعديل الرئيسي هنا: تغيير megagroup=False إلى megagroup=True**
                    result = await client(functions.channels.CreateChannelRequest(
                        title=group_title,
                        about=f"مجموعة تم إنشاؤها بواسطة بوت إدارة الحسابات",
                        megagroup=True  # <-- تم تغيير هذا إلى True لإنشاء مجموعة
                    ))
                    
                    created_groups += 1
                    log_event("group_created", f"Group {group_title} created with account {phone}", user_id)
                    
                    if i < group_count - 1:
                        await processing_message.edit_text(
                            "⏳ <b>جاري إنشاء المجموعات</b>\n\n"
                            f"تم إنشاء {created_groups} من {group_count} مجموعة.\n\n"
                            f"يرجى الانتظار...",
                            parse_mode=ParseMode.HTML
                        )
                        await asyncio.sleep(group_delay)
                else:
                    failed_groups += 1
                    logger.error(f"Session not authorized for account {phone}")
                
                await client.disconnect()
                
            except Exception as e:
                failed_groups += 1
                logger.error(f"Error creating group with account {phone}: {e}")
        
        keyboard = [
            [InlineKeyboardButton("👥 المجموعات", callback_data="groups")],
            [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
        ]
        
        await processing_message.edit_text(
            f"✅ <b>اكتمل إنشاء المجموعات</b>\n\n"
            f"تم إنشاء {created_groups} مجموعة بنجاح.\n"
            f"فشل إنشاء {failed_groups} مجموعة.\n\n"
            f"استخدم الأزرار أدناه للمتابعة.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"Error in group creation process: {e}")
        await processing_message.edit_text(
            f"❌ <b>خطأ في إنشاء المجموعات</b>\n\n"
            f"حدث خطأ أثناء عملية إنشاء المجموعات:\n\n"
            f"{str(e)}\n\n"
            f"يرجى المحاولة مرة أخرى لاحقًا.",
            parse_mode=ParseMode.HTML
        )

# Error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        error_message = str(context.error).replace("<", "&lt;").replace(">", "&gt;")
        update_str = str(update).replace("<", "&lt;").replace(">", "&gt;")
        
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"⚠️ <b>خطأ</b>\n\n"
                 f"حدث خطأ أثناء معالجة تحديث:\n\n"
                 f"الخطأ: {error_message}\n\n"
                 f"التحديث: {update_str}",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to send error notification to owner: {e}")

# Main function
def main():
    """بدء البوت."""
    try:
        if not BOT_TOKEN:
            logger.error("BOT_TOKEN is not set!")
            print("ERROR: BOT_TOKEN is not set in environment variables!")
            return
        
        if API_ID == 0 or not API_HASH:
            logger.error("API_ID or API_HASH is not set!")
            print("ERROR: API_ID or API_HASH is not set in environment variables!")
            return
        
        print("=" * 50)
        print("🤖 Telegram Account Manager Bot")
        print("=" * 50)
        print(f"🔑 Bot Token: {BOT_TOKEN[:10]}...")
        print(f"👤 Owner ID: {OWNER_ID}")
        print(f"🆔 API ID: {API_ID}")
        print(f"🔐 API Hash: {API_HASH[:10]}...")
        print(f"🗄️ MongoDB URI: {MONGO_URI[:20]}...")
        print("=" * 50)
        
        try:
            client.admin.command('ping')
            logger.info("✅ Connected to MongoDB successfully")
            print("✅ Connected to MongoDB successfully")
        except Exception as e:
            logger.error(f"❌ Failed to connect to MongoDB: {e}")
            print(f"❌ Failed to connect to MongoDB: {e}")
            return
        
        application = Application.builder().token(BOT_TOKEN).build()
        
        async def cancel_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
            keys_to_remove = ["phone", "phone_code_hash", "code", "password", "group_name", "group_count", "group_delay", "session_account_id", "session_phone", "session_phone_code_hash"]
            for key in keys_to_remove:
                if key in context.user_data:
                    del context.user_data[key]
            
            keyboard = [
                [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
                [InlineKeyboardButton("👥 المجموعات", callback_data="groups")],
                [InlineKeyboardButton("📊 حالتي", callback_data="status")],
                [InlineKeyboardButton("📊 إحصائياتي", callback_data="stats")],
                [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
            ]
            
            await send_message(update, "❌ <b>تم إلغاء العملية</b>\n\nيمكنك اختيار خيار آخر:", reply_markup=InlineKeyboardMarkup(keyboard))
            return ConversationHandler.END
        
        account_conv_handler = ConversationHandler(
            entry_points=[CommandHandler("add_account", add_account_start), CallbackQueryHandler(button_callback, pattern="^add_account$")],
            states={
                ACCOUNT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_phone), CommandHandler("cancel", cancel_any)],
                ACCOUNT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_code), CommandHandler("cancel", cancel_any)],
                ACCOUNT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_password), CommandHandler("cancel", cancel_any)],
            },
            fallbacks=[CommandHandler("cancel", cancel_any)],
            per_message=False,
            allow_reentry=True,
            name="account_conversation"
        )
        
        group_conv_handler = ConversationHandler(
            entry_points=[CommandHandler("create_groups", create_groups_start), CallbackQueryHandler(button_callback, pattern="^create_groups$")],
            states={
                GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_name), CommandHandler("cancel", cancel_any)],
                GROUP_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_count), CommandHandler("cancel", cancel_any)],
                GROUP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_delay), CommandHandler("cancel", cancel_any)],
            },
            fallbacks=[CommandHandler("cancel", cancel_any)],
            per_message=False,
            allow_reentry=True,
            name="group_conversation"
        )
        
        logger.info("Adding command handlers...")
        print("📝 Adding command handlers...")
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(CommandHandler("users", users_command))
        application.add_handler(CommandHandler("admin_stats", admin_stats_command))
        application.add_handler(CommandHandler("logs", logs_command))
        application.add_handler(CommandHandler("settings", settings_command))
        application.add_handler(CommandHandler("accounts", accounts_command))
        application.add_handler(CommandHandler("groups", groups_command))
        
        application.add_handler(account_conv_handler)
        application.add_handler(group_conv_handler)
        application.add_handler(CallbackQueryHandler(button_callback))
        application.add_error_handler(error_handler)
        
        logger.info("Starting Telegram Account Manager Bot...")
        print("🚀 Starting bot...")
        print("⏳ Bot is running... Press Ctrl+C to stop")
        print("=" * 50)
        
        application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES, timeout=30, read_timeout=30, write_timeout=30, connect_timeout=30, pool_timeout=30)
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"Error running bot: {e}", exc_info=True)
        print(f"❌ Error running bot: {e}")
        traceback.print_exc()
    finally:
        logger.info("Bot shutdown complete")
        print("🔚 Bot shutdown complete")

if __name__ == "__main__":
    def signal_handler(sig, frame):
        print('\n🛑 Received interrupt signal, shutting down...')
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        main()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
