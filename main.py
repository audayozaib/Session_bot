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
            # استخدام دالة مساعدة للرد على الرسالة أو الاستدعاء
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
            # استخدام دالة مساعدة للرد على الرسالة أو الاستدعاء
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
    
    # التحقق مما إذا كان المستخدم موجودًا في قاعدة البيانات
    existing_user = users_collection.find_one({"user_id": user_id})
    
    # إنشاء لوحة مفاتيح للقائمة الرئيسية
    keyboard = [
        [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
        [InlineKeyboardButton("👥 المجموعات", callback_data="groups")],
        [InlineKeyboardButton("📊 حالتي", callback_data="status")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="stats")],
    ]
    
    if not existing_user:
        # إنشاء مستخدم جديد بحالة معلقة
        users_collection.insert_one({
            "user_id": user_id,
            "username": user.username,
            "first_name": user.first_name,
            "access_status": "pending",
            "request_date": datetime.datetime.now()
        })
        
        # تسجيل الحدث
        log_event("access_request", f"User {user.first_name} (@{user.username}) requested access", user_id)
        
        # إعلام المالك
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
    
    # إنشاء زر العودة
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
    
    # إنشاء زر العودة
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
    
    # حساب الإحصائيات
    active_sessions = 0
    total_groups = 0
    
    for account in accounts:
        # التحقق مما إذا كانت الجلسة نشطة (تحقق مبسط)
        if "session_data" in account and account["session_data"]:
            active_sessions += 1
        
        # حساب المجموعات المنشأة بهذا الحساب (عنصر نائب)
        total_groups += random.randint(0, 10)  # This would be real data in production
    
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
    
    # تسجيل الحدث
    log_event("access_approved", f"User {user_id} was approved by owner", OWNER_ID)
    
    # إعلام المستخدم
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
    
    # تسجيل الحدث
    log_event("access_rejected", f"User {user_id} was rejected by owner", OWNER_ID)
    
    # إعلام المستخدم
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
    
    # تقسيم إلى أجزاء إذا كان طويلاً جداً
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
    
    # تقسيم إلى أجزاء إذا كان طويلاً جداً
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
    
    # إنشاء زر العودة
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
        
        # التحقق مما إذا كانت الجلسة موجودة
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
            InlineKeyboardButton(f"حذف {phone}", callback_data=f"delete_account_{account_id}")
        ])
    
    # إضافة أزرار التنقل إذا كان هناك أكثر من صفحة واحدة
    if total_pages > 1:
        nav_buttons = []
        if 0 > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"accounts_page_{0-1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"صفحة 1/{total_pages}", callback_data="noop"))
        
        if total_pages > 1:
            nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"accounts_page_{1}"))
        
        keyboard.insert(-1, nav_buttons)
    
    # إضافة زر إضافة حساب
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
    """معالجة إدخال كود التحقق."""
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
        await wait_msg.edit_text("🔐 الحساب محمي بـ 2FA\nأرسل كلمة المرور:")
        return ACCOUNT_PASSWORD

    except errors.PhoneCodeExpiredError:
        await wait_msg.edit_text("❌ انتهت صلاحية الكود.")
        return ConversationHandler.END

    except Exception as e:
        await wait_msg.edit_text(f"❌ خطأ: {str(e)}")
        return ConversationHandler.END

    finally:
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
    # تنظيف بيانات المستخدم المؤقتة
    if "phone" in context.user_data:
        del context.user_data["phone"]
    if "phone_code_hash" in context.user_data:
        del context.user_data["phone_code_hash"]
    if "code" in context.user_data:
        del context.user_data["code"]
    if "password" in context.user_data:
        del context.user_data["password"]
    
    # إنشاء لوحة مفاتيح مع الخيارات
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
    
    # إنشاء زر العودة
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
    
    # تخزين الاسم في السياق
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
    
    # تخزين العدد في السياق
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
    
    # تخزين التأخير في السياق
    context.user_data["group_delay"] = delay
    
    # الحصول على حسابات المستخدم
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    # تصفية الحسابات ذات الجلسات النشطة
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
    
    # إنشاء لوحة مفاتيح مع خيارات الحسابات
    keyboard = []
    
    # خيار استخدام جميع الحسابات
    keyboard.append([
        InlineKeyboardButton(
            f"استخدام جميع {len(active_accounts)} حساب",
            callback_data="use_all_accounts"
        )
    ])
    
    # خيار تحديد حسابات محددة
    for account in active_accounts:
        phone = account.get("phone_number", "N/A")
        account_id = str(account.get("_id", "N/A"))
        keyboard.append([
            InlineKeyboardButton(
                f"استخدام {phone}",
                callback_data=f"use_account_{account_id}"
            )
        ])
    
    # زر الإلغاء
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
    # إنشاء لوحة مفاتيح مع الخيارات
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
    
    # التنقل في القائمة الرئيسية
    if data == "main_menu":
        # إنشاء لوحة مفاتيح للقائمة الرئيسية
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
    
    # زر الحالة
    elif data == "status":
        await status_command(update, context)
        return
    
    # زر الإحصائيات
    elif data == "stats":
        await stats_command(update, context)
        return
    
    # زر الحسابات
    elif data == "accounts":
        await accounts_command(update, context)
        return
    
    # زر المجموعات
    elif data == "groups":
        await groups_command(update, context)
        return
    
    # زر إضافة حساب
    elif data == "add_account":
        # بدء محادثة إضافة الحساب
        return await add_account_start(update, context)
    
    # زر إدارة الحساب
    elif data.startswith("manage_account_"):
        # استخراج معرف الحساب
        account_id = data.split("_", 2)[2]
        
        # الحصول على تفاصيل الحساب
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>خطأ</b>\n\n"
                "لم يتم العثور على الحساب.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        created_at = account.get("created_at", datetime.datetime.now())
        created_at_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
        
        # التحقق مما إذا كانت الجلسة موجودة
        has_session = "session_data" in account and account["session_data"]
        session_status = "🟢 نشط" if has_session else "🔴 لا توجد جلسة"
        
        # إنشاء لوحة مفاتيح مع خيارات الإدارة
        keyboard = []
        
        if has_session:
            keyboard.append([
                InlineKeyboardButton("🔄 تحديث الجلسة", callback_data=f"refresh_session_{account_id}")
            ])
            keyboard.append([
                InlineKeyboardButton("🗑️ حذف الجلسة", callback_data=f"delete_session_{account_id}")
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("➕ إنشاء جلسة", callback_data=f"create_session_{account_id}")
            ])
        
        keyboard.append([
            InlineKeyboardButton("🗑️ حذف الحساب", callback_data=f"delete_account_{account_id}")
        ])
        keyboard.append([
            InlineKeyboardButton("⬅️ العودة إلى الحسابات", callback_data="accounts")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        account_text = (
            f"📱 <b>تفاصيل الحساب</b>\n\n"
            f"📞 <b>الهاتف:</b> {phone}\n"
            f"🆔 <b>المعرف:</b> {account_id}\n"
            f"📅 <b>تاريخ الإضافة:</b> {created_at_str}\n"
            f"🔐 <b>حالة الجلسة:</b> {session_status}\n\n"
            f"استخدم الأزرار أدناه لإدارة هذا الحساب."
        )
        
        await query.message.reply_text(
            account_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    # زر حذف الحساب
    elif data.startswith("delete_account_"):
        # استخراج معرف الحساب
        account_id = data.split("_", 2)[2]
        
        # الحصول على تفاصيل الحساب
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>خطأ</b>\n\n"
                "لم يتم العثور على الحساب.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        
        # إنشاء لوحة مفاتيح للتأكيد
        keyboard = [
            [
                InlineKeyboardButton("✅ نعم، احذف", callback_data=f"confirm_delete_account_{account_id}"),
                InlineKeyboardButton("❌ لا، إلغاء", callback_data="accounts")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(
            f"⚠️ <b>تأكيد حذف الحساب</b>\n\n"
            f"هل أنت متأكد من أنك تريد حذف الحساب {phone}؟\n\n"
            f"لا يمكن التراجع عن هذا الإجراء.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    # زر تأكيد حذف الحساب
    elif data.startswith("confirm_delete_account_"):
        # استخراج معرف الحساب
        account_id = data.split("_", 3)[3]
        
        # الحصول على تفاصيل الحساب
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>خطأ</b>\n\n"
                "لم يتم العثور على الحساب.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        user_id = account.get("user_id", update.effective_user.id)
        
        # حذف الحساب
        accounts_collection.delete_one({"_id": account_id})
        
        # تسجيل الحدث
        log_event("account_deleted", f"Account {phone} deleted by user {user_id}", user_id)
        
        # إعلام المالك
        await notify_owner(
            context,
            f"📱 <b>تم حذف الحساب</b>\n\n"
            f"المستخدم: {update.effective_user.first_name} (@{update.effective_user.username})\n"
            f"الحساب: {phone}\n"
            f"معرف الحساب: {account_id}"
        )
        
        # إنشاء لوحة مفاتيح مع الخيارات
        keyboard = [
            [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
            [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
        ]
        
        await query.message.reply_text(
            f"✅ <b>تم حذف الحساب</b>\n\n"
            f"تم حذف الحساب {phone} بنجاح.\n\n"
            f"استخدم /accounts لإدارة حساباتك المتبقية.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    # التنقل بين صفحات الحسابات
    elif data.startswith("accounts_page_"):
        # استخراج رقم الصفحة
        try:
            page = int(data.split("_", 2)[2])
        except (IndexError, ValueError):
            page = 0
        
        user_id = update.effective_user.id
        accounts, total_pages = get_paginated_accounts(user_id, page)
        
        if not accounts:
            await query.message.reply_text(
                "📱 <b>حساباتك</b>\n\n"
                "لم يتم العثور على حسابات في هذه الصفحة.",
                parse_mode=ParseMode.HTML
            )
            return
        
        accounts_text = "📱 <b>حساباتك</b>\n\n"
        keyboard = []
        
        for i, account in enumerate(accounts):
            account_id = str(account.get("_id", "N/A"))
            phone = account.get("phone_number", "N/A")
            created_at = account.get("created_at", datetime.datetime.now())
            created_at_str = created_at.strftime("%Y-%m-%d")
            
            # التحقق مما إذا كانت الجلسة موجودة
            has_session = "session_data" in account and account["session_data"]
            session_status = "🟢 نشط" if has_session else "🔴 لا توجد جلسة"
            
            accounts_text += (
                f"{i+1}. {phone}\n"
                f"   المعرف: {account_id}\n"
                f"   تاريخ الإضافة: {created_at_str}\n"
                f"   الحالة: {session_status}\n\n"
            )
            
            keyboard.append([
                InlineKeyboardButton(f"إدارة {phone}", callback_data=f"manage_account_{account_id}"),
                InlineKeyboardButton(f"حذف {phone}", callback_data=f"delete_account_{account_id}")
            ])
        
        # إضافة أزرار التنقل
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"accounts_page_{page-1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"صفحة {page+1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"accounts_page_{page+1}"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        # إضافة زر إضافة حساب
        keyboard.append([InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")])
        keyboard.append([InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(
            accounts_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    # زر إنشاء مجموعات
    elif data == "create_groups":
        # بدء محادثة إنشاء المجموعات
        return await create_groups_start(update, context)
    
    # استخدام جميع الحسابات لإنشاء المجموعات
    elif data == "use_all_accounts":
        # الحصول على حسابات المستخدم
        accounts = get_user_accounts(user_id)
        
        # تصفية الحسابات ذات الجلسات النشطة
        active_accounts = []
        for account in accounts:
            if "session_data" in account and account["session_data"]:
                active_accounts.append(account)
        
        if not active_accounts:
            await query.message.reply_text(
                "❌ <b>لا توجد جلسات نشطة</b>\n\n"
                "ليس لديك أي حسابات بها جلسات نشطة.\n\n"
                "يرجى إضافة حساب به جلسة نشطة أولاً.",
                parse_mode=ParseMode.HTML
            )
            return
        
        # بدء عملية إنشاء المجموعات بجميع الحسابات
        await create_groups_with_accounts(update, context, active_accounts)
    
    # استخدام حساب محدد لإنشاء المجموعات
    elif data.startswith("use_account_"):
        # استخراج معرف الحساب
        account_id = data.split("_", 2)[2]
        
        # الحصول على تفاصيل الحساب
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>خطأ</b>\n\n"
                "لم يتم العثور على الحساب.",
                parse_mode=ParseMode.HTML
            )
            return
        
        # التحقق مما إذا كانت الجلسة موجودة
        if "session_data" not in account or not account["session_data"]:
            await query.message.reply_text(
                "❌ <b>لا توجد جلسة نشطة</b>\n\n"
                "هذا الحساب ليس لديه جلسة نشطة.\n\n"
                "يرجى إنشاء جلسة لهذا الحساب أولاً.",
                parse_mode=ParseMode.HTML
            )
            return
        
        # بدء عملية إنشاء المجموعات بهذا الحساب
        await create_groups_with_accounts(update, context, [account])
    
    # إلغاء إنشاء المجموعات
    elif data == "cancel_groups":
        await cancel_groups(update, context)
    
    # تحديث الجلسة
    elif data.startswith("refresh_session_"):
        # استخراج معرف الحساب
        account_id = data.split("_", 2)[2]
        
        # الحصول على تفاصيل الحساب
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>خطأ</b>\n\n"
                "لم يتم العثور على الحساب.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        
        # إرسال رسالة معالجة
        processing_message = await query.message.reply_text(
            "⏳ <b>جاري تحديث الجلسة</b>\n\n"
            f"يتم الآن تحديث جلسة الحساب {phone}...\n\n"
            "يرجى الانتظار.",
            parse_mode=ParseMode.HTML
        )
        
        try:
            # الحصول على بيانات الجلسة
            session_data = decrypt_data(account["session_data"])
            
            # إنشاء عميل Telethon مع الجلسة
            client = TelegramClient(
                StringSession(session_data),
                API_ID,
                API_HASH
            )
            
            await client.connect()
            
            # التحقق مما إذا كانت الجلسة صالحة
            if await client.is_user_authorized():
                # الجلسة صالحة، فقط احفظها مرة أخرى لتحديثها
                session_string = client.session.save()
                
                await client.disconnect()
                
                # تحديث الحساب ببيانات الجلسة الجديدة
                accounts_collection.update_one(
                    {"_id": account_id},
                    {"$set": {"session_data": encrypt_data(session_string)}}
                )
                
                # تسجيل الحدث
                log_event("session_refreshed", f"Session refreshed for account {phone}", user_id)
                
                await processing_message.edit_text(
                    f"✅ <b>تم تحديث الجلسة بنجاح</b>\n\n"
                    f"تم تحديث جلسة الحساب {phone} بنجاح.\n\n"
                    f"الجلسة الآن صالحة للاستخدام.",
                    parse_mode=ParseMode.HTML
                )
            else:
                await client.disconnect()
                
                await processing_message.edit_text(
                    f"❌ <b>فشل تحديث الجلسة</b>\n\n"
                    f"جلسة الحساب {phone} غير صالحة.\n\n"
                    f"يرجى إنشاء جلسة جديدة لهذا الحساب.",
                    parse_mode=ParseMode.HTML
                )
                
        except Exception as e:
            logger.error(f"Error refreshing session: {e}")
            await processing_message.edit_text(
                f"❌ <b>خطأ في تحديث الجلسة</b>\n\n"
                f"حدث خطأ أثناء تحديث جلسة الحساب {phone}:\n\n"
                f"{str(e)}\n\n"
                f"يرجى المحاولة مرة أخرى لاحقًا.",
                parse_mode=ParseMode.HTML
            )
    
    # حذف الجلسة
    elif data.startswith("delete_session_"):
        # استخراج معرف الحساب
        account_id = data.split("_", 2)[2]
        
        # الحصول على تفاصيل الحساب
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>خطأ</b>\n\n"
                "لم يتم العثور على الحساب.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        
        # إنشاء لوحة مفاتيح للتأكيد
        keyboard = [
            [
                InlineKeyboardButton("✅ نعم، احذف", callback_data=f"confirm_delete_session_{account_id}"),
                InlineKeyboardButton("❌ لا، إلغاء", callback_data=f"manage_account_{account_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(
            f"⚠️ <b>تأكيد حذف الجلسة</b>\n\n"
            f"هل أنت متأكد من أنك تريد حذف جلسة الحساب {phone}؟\n\n"
            f"سيحتاج الحساب إلى مصادقة جديدة إذا أردت استخدامه مرة أخرى.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    # تأكيد حذف الجلسة
    elif data.startswith("confirm_delete_session_"):
        # استخراج معرف الحساب
        account_id = data.split("_", 3)[3]
        
        # الحصول على تفاصيل الحساب
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>خطأ</b>\n\n"
                "لم يتم العثور على الحساب.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        
        # حذف بيانات الجلسة
        accounts_collection.update_one(
            {"_id": account_id},
            {"$unset": {"session_data": ""}}
        )
        
        # تسجيل الحدث
        log_event("session_deleted", f"Session deleted for account {phone}", user_id)
        
        await query.message.reply_text(
            f"✅ <b>تم حذف الجلسة</b>\n\n"
            f"تم حذف جلسة الحساب {phone} بنجاح.\n\n"
            f"ستحتاج إلى إنشاء جلسة جديدة إذا أردت استخدام هذا الحساب.",
            parse_mode=ParseMode.HTML
        )
    
    # إنشاء جلسة
    elif data.startswith("create_session_"):
        # استخراج معرف الحساب
        account_id = data.split("_", 2)[2]
        
        # الحصول على تفاصيل الحساب
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>خطأ</b>\n\n"
                "لم يتم العثور على الحساب.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        
        # تخزين معرف الحساب في السياق لإنشاء الجلسة
        context.user_data["session_account_id"] = account_id
        context.user_data["session_phone"] = phone
        
        # بدء عملية إنشاء الجلسة
        await query.message.reply_text(
            "📱 <b>إنشاء جلسة جديدة</b>\n\n"
            f"سيتم الآن إنشاء جلسة جديدة للحساب {phone}.\n\n"
            "قد تحتاج إلى إدخال رمز التحقق وكلمة مرور 2FA إذا كانت مطلوبة.\n\n"
            "يرجى الانتظار...",
            parse_mode=ParseMode.HTML
        )
        
        try:
            # إنشاء عميل Telethon مؤقت لطلب الرمز
            client = TelegramClient(
                StringSession(),
                API_ID,
                API_HASH
            )
            
            await client.connect()
            
            # طلب الرمز
            result = await client.send_code_request(phone)
            
            await client.disconnect()
            
            # تخزين تجزئة رمز الهاتف للتحقق
            context.user_data["session_phone_code_hash"] = result.phone_code_hash
            
            await query.message.reply_text(
                "✅ <b>تم إرسال رمز التحقق</b>\n\n"
                "تم إرسال رمز التحقق إلى حساب تيليجرام الخاص بك.\n\n"
                "يرجى إدخال الرمز الذي تلقيته.\n\n"
                "أرسل /cancel لإلغاء هذه العملية.",
                parse_mode=ParseMode.HTML
            )
            return ACCOUNT_CODE
            
        except Exception as e:
            logger.error(f"Error sending code request: {e}")
            await query.message.reply_text(
                f"❌ <b>خطأ</b>\n\n"
                f"فشل في إرسال رمز التحقق: {str(e)}\n\n"
                "يرجى المحاولة مرة أخرى لاحقًا أو الاتصال بالدعم.",
                parse_mode=ParseMode.HTML
            )
    
    # تبديل المراقبة
    elif data.startswith("toggle_monitoring_"):
        # استخراج الحالة الحالية
        current_state = data.split("_", 2)[2] == "True"
        
        # تبديل الحالة
        new_state = not current_state
        
        # تحديث الإعدادات
        settings_collection.update_one(
            {},
            {"$set": {"monitoring_enabled": new_state}}
        )
        
        # تسجيل الحدث
        log_event(
            "settings_changed",
            f"Session monitoring toggled from {current_state} to {new_state}",
            update.effective_user.id
        )
        
        # تحديث الرسالة
        keyboard = [
            [
                InlineKeyboardButton(
                    "تبديل مراقبة الجلسات",
                    callback_data=f"toggle_monitoring_{new_state}"
                )
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        settings_text = (
            f"⚙️ <b>إعدادات البوت</b>\n\n"
            f"🔐 <b>مراقبة الجلسات:</b> {'مفعلة' if new_state else 'معطلة'}\n\n"
            f"استخدم الزر أدناه لتبديل مراقبة الجلسات."
        )
        
        await query.message.edit_text(
            settings_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    # لا يوجد عملية
    elif data == "noop":
        # لا يوجد عملية، فقط الاعتراف بضغط الزر
        pass
    
    else:
        # استدعاء غير معروف
        await query.message.reply_text(
            "❌ <b>خطأ</b>\n\n"
            "إجراء غير معروف. يرجى المحاولة مرة أخرى.",
            parse_mode=ParseMode.HTML
        )

# Helper function for creating groups with accounts
async def create_groups_with_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE, accounts: List[Dict]):
    """إنشاء مجموعات باستخدام الحسابات المحددة."""
    query = update.callback_query
    user_id = update.effective_user.id
    
    group_name = context.user_data.get("group_name", "Group")
    group_count = context.user_data.get("group_count", 1)
    group_delay = context.user_data.get("group_delay", 10)
    
    # إرسال رسالة معالجة
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
        # إنشاء المجموعات واحدة تلو الأخرى
        for i in range(group_count):
            # تحديد الحساب بطريقة round-robin
            account = accounts[i % len(accounts)]
            phone = account.get("phone_number", "N/A")
            session_data = account.get("session_data", "")
            
            try:
                # إنشاء عميل Telethon مع الجلسة
                client = TelegramClient(
                    StringSession(session_data),
                    API_ID,
                    API_HASH
                )
                
                await client.connect()
                
                # التحقق مما إذا كانت الجلسة صالحة
                if await client.is_user_authorized():
                    # إنشاء المجموعة
                    group_title = f"{group_name} {i+1}"
                    result = await client(functions.channels.CreateChannelRequest(
                        title=group_title,
                        about=f"Created by Telegram Account Manager Bot",
                        megagroup=False
                    ))
                    
                    created_groups += 1
                    
                    # تسجيل الحدث
                    log_event("group_created", f"Group {group_title} created with account {phone}", user_id)
                    
                    # تحديث رسالة التقدم
                    if i < group_count - 1:  # لا تحدث بعد المجموعة الأخيرة
                        await processing_message.edit_text(
                            "⏳ <b>جاري إنشاء المجموعات</b>\n\n"
                            f"تم إنشاء {created_groups} من {group_count} مجموعة.\n\n"
                            f"اسم المجموعة: {group_name}\n"
                            f"التأخير بين المجموعات: {group_delay} ثانية\n\n"
                            "يرجى الانتظار...",
                            parse_mode=ParseMode.HTML
                        )
                    
                    # التأخير بين إنشاء المجموعات
                    if i < group_count - 1:  # لا تؤخر بعد المجموعة الأخيرة
                        await asyncio.sleep(group_delay)
                else:
                    failed_groups += 1
                    logger.error(f"Session not authorized for account {phone}")
                
                await client.disconnect()
                
            except Exception as e:
                failed_groups += 1
                logger.error(f"Error creating group with account {phone}: {e}")
        
        # رسالة الحالة النهائية
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
    """تسجيل الخطأ وإرسال رسالة تيليجرام لإعلام المالك."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    # إعلام المالك بالخطأ
    try:
        # تنسيق الخطأ لتجنب مشاكل HTML
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
        # التحقق من المتغيرات الأساسية
        if not BOT_TOKEN:
            logger.error("BOT_TOKEN is not set!")
            print("ERROR: BOT_TOKEN is not set in environment variables!")
            return
        
        if API_ID == 0 or not API_HASH:
            logger.error("API_ID or API_HASH is not set!")
            print("ERROR: API_ID or API_HASH is not set in environment variables!")
            return
        
        # طباعة معلومات البداية
        print("=" * 50)
        print("🤖 Telegram Account Manager Bot")
        print("=" * 50)
        print(f"🔑 Bot Token: {BOT_TOKEN[:10]}...")
        print(f"👤 Owner ID: {OWNER_ID}")
        print(f"🆔 API ID: {API_ID}")
        print(f"🔐 API Hash: {API_HASH[:10]}...")
        print(f"🗄️ MongoDB URI: {MONGO_URI[:20]}...")
        print("=" * 50)
        
        # التحقق من الاتصال بقاعدة البيانات
        try:
            client.admin.command('ping')
            logger.info("✅ Connected to MongoDB successfully")
            print("✅ Connected to MongoDB successfully")
        except Exception as e:
            logger.error(f"❌ Failed to connect to MongoDB: {e}")
            print(f"❌ Failed to connect to MongoDB: {e}")
            return
        
        # بناء التطبيق (الطريقة القديمة المتوافقة مع جميع الإصدارات)
        application = Application.builder().token(BOT_TOKEN).build()
        
        # دالة الإلغاء العامة
        async def cancel_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """إلغاء أي محادثة قيد التشغيل."""
            # تنظيف بيانات المستخدم المؤقتة
            keys_to_remove = ["phone", "phone_code_hash", "code", "password", 
                              "group_name", "group_count", "group_delay",
                              "session_account_id", "session_phone", "session_phone_code_hash",
                              "adding_account", "creating_groups"]
            
            for key in keys_to_remove:
                if key in context.user_data:
                    del context.user_data[key]
            
            # إنشاء لوحة مفاتيح مع الخيارات
            keyboard = [
                [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
                [InlineKeyboardButton("👥 المجموعات", callback_data="groups")],
                [InlineKeyboardButton("📊 حالتي", callback_data="status")],
                [InlineKeyboardButton("📊 إحصائياتي", callback_data="stats")],
                [InlineKeyboardButton("⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")]
            ]
            
            await send_message(
                update,
                "❌ <b>تم إلغاء العملية</b>\n\n"
                "تم إلغاء العملية الحالية بنجاح.\n\n"
                "يمكنك اختيار خيار آخر من الأزرار أدناه:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END
        
        # معالج محادثة الحساب
        account_conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("add_account", add_account_start),
                CallbackQueryHandler(button_callback, pattern="^add_account$"),
            ],
            states={
                ACCOUNT_PHONE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_phone),
                    CommandHandler("cancel", cancel_any)
                ],
                ACCOUNT_CODE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_code),
                    CommandHandler("cancel", cancel_any)
                ],
                ACCOUNT_PASSWORD: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_password),
                    CommandHandler("cancel", cancel_any)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_any)],
            per_message=False,
            allow_reentry=True,
            name="account_conversation"
        )
        
        # معالج محادثة إنشاء المجموعات
        group_conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("create_groups", create_groups_start),
                CallbackQueryHandler(button_callback, pattern="^create_groups$"),
            ],
            states={
                GROUP_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_name),
                    CommandHandler("cancel", cancel_any)
                ],
                GROUP_COUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_count),
                    CommandHandler("cancel", cancel_any)
                ],
                GROUP_DELAY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_delay),
                    CommandHandler("cancel", cancel_any)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_any)],
            per_message=False,
            allow_reentry=True,
            name="group_conversation"
        )
        
        # إضافة معالجات الأوامر
        logger.info("Adding command handlers...")
        print("📝 Adding command handlers...")
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CommandHandler("stats", stats_command))
        
        # أوامر المالك
        application.add_handler(CommandHandler("approve", approve_command))
        application.add_handler(CommandHandler("reject", reject_command))
        application.add_handler(CommandHandler("users", users_command))
        application.add_handler(CommandHandler("admin_stats", admin_stats_command))
        application.add_handler(CommandHandler("logs", logs_command))
        application.add_handler(CommandHandler("settings", settings_command))
        
        # إدارة الحسابات
        application.add_handler(CommandHandler("accounts", accounts_command))
        
        # إنشاء المجموعات
        application.add_handler(CommandHandler("groups", groups_command))
        
        # إضافة معالجات المحادثة
        application.add_handler(account_conv_handler)
        application.add_handler(group_conv_handler)
        
        # معالج استدعاء الأزرار
        application.add_handler(CallbackQueryHandler(button_callback))
        
        # معالج الأخطاء
        application.add_error_handler(error_handler)
        
        # رسالة بدء التشغيل
        logger.info("Starting Telegram Account Manager Bot...")
        print("🚀 Starting bot...")
        
        # تشغيل البوت مع المهلات القديمة
        print("⏳ Bot is running... Press Ctrl+C to stop")
        print("=" * 50)
        
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            timeout=30,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=30,
            pool_timeout=30
        )
        
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

# إضافة نقطة دخول رئيسية
if __name__ == "__main__":
    # تشغيل البوت في حلقة لمنع الإغلاق
    def signal_handler(sig, frame):
        print('\n🛑 Received interrupt signal, shutting down...')
        sys.exit(0)
    
    # تسجيل معالج الإشارات
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # تشغيل البوت
    try:
        main()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
