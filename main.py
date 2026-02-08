# main.py
import os
import asyncio
import logging
import datetime
import random
import string
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
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://audayozaib:SaXaXket2GECpLvR@giveaway.x2eabrg.mongodb.net/giveaway?retryWrites=true&w=majority")
DB_NAME = os.getenv("DB_NAME", "giveaway")

# Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "2069413735:AAGpE9WlBwhMyb_P9vgF4Jqvii1ZtTxvEuQ")
OWNER_ID = int(os.getenv("OWNER_ID", "778375826"))

# Telethon API
API_ID = int(os.getenv("API_ID", "6825462"))
API_HASH = os.getenv("API_HASH", "3b3cb233c159b6f48798e10c4b5fdc83")

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
    """Encrypt sensitive data before storing."""
    if not data:
        return data
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    """Decrypt sensitive data from storage."""
    if not encrypted_data:
        return encrypted_data
    return cipher_suite.decrypt(encrypted_data.encode()).decode()

def log_event(event_type: str, details: str, user_id: int):
    """Log events to the database."""
    logs_collection.insert_one({
        "timestamp": datetime.datetime.now(),
        "event_type": event_type,
        "details": details,
        "user_id": user_id
    })

def get_user_status(user_id: int) -> str:
    """Get the access status of a user."""
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        return "not_registered"
    return user.get("access_status", "pending")

def is_approved(user_id: int) -> bool:
    """Check if a user is approved to use the bot."""
    return get_user_status(user_id) == "approved"

def is_owner(user_id: int) -> bool:
    """Check if the user is the bot owner."""
    return user_id == OWNER_ID

def get_user_accounts(user_id: int) -> List[Dict]:
    """Get all accounts linked to a user."""
    accounts = list(accounts_collection.find({"user_id": user_id}))
    for account in accounts:
        # Decrypt session data for use
        if "session_data" in account and account["session_data"]:
            account["session_data"] = decrypt_data(account["session_data"])
    return accounts

def get_paginated_accounts(user_id: int, page: int = 0, page_size: int = 10) -> Tuple[List[Dict], int]:
    """Get paginated accounts for a user."""
    accounts = get_user_accounts(user_id)
    total_pages = (len(accounts) + page_size - 1) // page_size
    start = page * page_size
    end = start + page_size
    paginated_accounts = accounts[start:end]
    return paginated_accounts, total_pages

async def send_notification(context: ContextTypes.DEFAULT_TYPE, user_id: int, message: str):
    """Send a notification to a user."""
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to send notification to {user_id}: {e}")

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Send a notification to the bot owner."""
    await send_notification(context, OWNER_ID, message)

# Decorators
def approved_only(func):
    """Decorator to restrict access to approved users only."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_approved(user_id):
            await update.message.reply_text(
                "⛔ <b>Access Denied</b>\n\n"
                "You don't have permission to use this bot. "
                "Your access request might still be pending or rejected.",
                parse_mode=ParseMode.HTML
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def owner_only(func):
    """Decorator to restrict access to the bot owner only."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text(
                "⛔ <b>Access Denied</b>\n\n"
                "This command is only available to the bot owner.",
                parse_mode=ParseMode.HTML
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command."""
    user = update.effective_user
    user_id = user.id
    
    # Check if user exists in database
    existing_user = users_collection.find_one({"user_id": user_id})
    
    if not existing_user:
        # Create new user with pending status
        users_collection.insert_one({
            "user_id": user_id,
            "username": user.username,
            "first_name": user.first_name,
            "access_status": "pending",
            "request_date": datetime.datetime.now()
        })
        
        # Log the event
        log_event("access_request", f"User {user.first_name} (@{user.username}) requested access", user_id)
        
        # Notify owner
        await notify_owner(
            context,
            f"🔔 <b>New Access Request</b>\n\n"
            f"<b>User:</b> {user.first_name} (@{user.username})\n"
            f"<b>ID:</b> {user_id}\n"
            f"<b>Status:</b> Pending approval\n\n"
            f"Use /approve {user_id} to approve or /reject {user_id} to reject."
        )
        
        await update.message.reply_text(
            f"👋 Hello, {user.first_name}!\n\n"
            f"Welcome to the Telegram Account Manager Bot.\n\n"
            f"⏳ Your access request has been sent to the bot owner for approval.\n"
            f"You'll be notified once your request is reviewed.\n\n"
            f"Thank you for your patience!",
            parse_mode=ParseMode.HTML
        )
    else:
        status = existing_user.get("access_status", "pending")
        
        if status == "pending":
            await update.message.reply_text(
                f"👋 Hello, {user.first_name}!\n\n"
                f"Your access request is still pending approval.\n"
                f"You'll be notified once the bot owner reviews your request.\n\n"
                f"Thank you for your patience!",
                parse_mode=ParseMode.HTML
            )
        elif status == "approved":
            await update.message.reply_text(
                f"👋 Welcome back, {user.first_name}!\n\n"
                f"You have approved access to the bot.\n\n"
                f"Use /help to see available commands.",
                parse_mode=ParseMode.HTML
            )
        elif status == "rejected":
            await update.message.reply_text(
                f"👋 Hello, {user.first_name}!\n\n"
                f"Your access request has been rejected.\n\n"
                f"If you believe this is a mistake, please contact the bot owner.",
                parse_mode=ParseMode.HTML
            )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command."""
    user_id = update.effective_user.id
    status = get_user_status(user_id)
    
    if status != "approved":
        await update.message.reply_text(
            "⛔ <b>Access Denied</b>\n\n"
            "You don't have permission to use this bot. "
            "Your access request might still be pending or rejected.",
            parse_mode=ParseMode.HTML
        )
        return
    
    help_text = (
        "🤖 <b>Telegram Account Manager Bot</b>\n\n"
        "<b>Available Commands:</b>\n\n"
        "📱 /accounts - Manage your Telegram accounts\n"
        "👥 /groups - Create Telegram groups\n"
        "ℹ️ /status - Check your account status\n"
        "📊 /stats - View your statistics\n"
    )
    
    if is_owner(user_id):
        help_text += (
            "\n🔧 <b>Owner Commands:</b>\n\n"
            "✅ /approve [user_id] - Approve a user's access request\n"
            "❌ /reject [user_id] - Reject a user's access request\n"
            "👥 /users - View all users\n"
            "📊 /admin_stats - View system statistics\n"
            "🔍 /logs - View system logs\n"
            "⚙️ /settings - Configure bot settings\n"
        )
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

@approved_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /status command."""
    user_id = update.effective_user.id
    user = users_collection.find_one({"user_id": user_id})
    
    if not user:
        await update.message.reply_text(
            "❌ <b>Error</b>\n\n"
            "Your user account was not found in the database. "
            "Please try /start to register again.",
            parse_mode=ParseMode.HTML
        )
        return
    
    status = user.get("access_status", "unknown")
    request_date = user.get("request_date", datetime.datetime.now())
    request_date_str = request_date.strftime("%Y-%m-%d %H:%M:%S")
    
    accounts_count = accounts_collection.count_documents({"user_id": user_id})
    
    status_text = (
        f"📊 <b>Your Account Status</b>\n\n"
        f"🆔 <b>User ID:</b> {user_id}\n"
        f"👤 <b>Name:</b> {user.get('first_name', 'N/A')}\n"
        f"🔖 <b>Username:</b> @{user.get('username', 'N/A')}\n"
        f"📅 <b>Request Date:</b> {request_date_str}\n"
        f"✅ <b>Access Status:</b> {status.capitalize()}\n"
        f"📱 <b>Linked Accounts:</b> {accounts_count}\n"
    )
    
    await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)

@approved_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /stats command."""
    user_id = update.effective_user.id
    
    accounts = get_user_accounts(user_id)
    total_accounts = len(accounts)
    
    if total_accounts == 0:
        await update.message.reply_text(
            "📊 <b>Your Statistics</b>\n\n"
            "You don't have any linked accounts yet.\n\n"
            "Use /accounts to add your first account.",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Calculate statistics
    active_sessions = 0
    total_groups = 0
    
    for account in accounts:
        # Check if session is active (simplified check)
        if "session_data" in account and account["session_data"]:
            active_sessions += 1
        
        # Count groups created with this account (placeholder)
        total_groups += random.randint(0, 10)  # This would be real data in production
    
    stats_text = (
        f"📊 <b>Your Statistics</b>\n\n"
        f"📱 <b>Total Accounts:</b> {total_accounts}\n"
        f"🔐 <b>Active Sessions:</b> {active_sessions}\n"
        f"👥 <b>Groups Created:</b> {total_groups}\n"
    )
    
    await update.message.reply_text(stats_text, parse_mode=ParseMode.HTML)

# Owner commands
@owner_only
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /approve command."""
    if not context.args:
        await update.message.reply_text(
            "❌ <b>Error</b>\n\n"
            "Please provide a user ID to approve.\n\n"
            "Usage: /approve [user_id]",
            parse_mode=ParseMode.HTML
        )
        return
    
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ <b>Error</b>\n\n"
            "Invalid user ID. Please provide a numeric user ID.",
            parse_mode=ParseMode.HTML
        )
        return
    
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        await update.message.reply_text(
            "❌ <b>Error</b>\n\n"
            f"User with ID {user_id} not found in the database.",
            parse_mode=ParseMode.HTML
        )
        return
    
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"access_status": "approved"}}
    )
    
    # Log the event
    log_event("access_approved", f"User {user_id} was approved by owner", OWNER_ID)
    
    # Notify the user
    await send_notification(
        context,
        user_id,
        "✅ <b>Access Approved</b>\n\n"
        f"Your access request has been approved by the bot owner.\n\n"
        f"You can now use the bot. Use /help to see available commands.",
    )
    
    await update.message.reply_text(
        f"✅ <b>Success</b>\n\n"
        f"User {user_id} has been approved.\n\n"
        f"They have been notified of the approval.",
        parse_mode=ParseMode.HTML
    )

@owner_only
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /reject command."""
    if not context.args:
        await update.message.reply_text(
            "❌ <b>Error</b>\n\n"
            "Please provide a user ID to reject.\n\n"
            "Usage: /reject [user_id]",
            parse_mode=ParseMode.HTML
        )
        return
    
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ <b>Error</b>\n\n"
            "Invalid user ID. Please provide a numeric user ID.",
            parse_mode=ParseMode.HTML
        )
        return
    
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        await update.message.reply_text(
            "❌ <b>Error</b>\n\n"
            f"User with ID {user_id} not found in the database.",
            parse_mode=ParseMode.HTML
        )
        return
    
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"access_status": "rejected"}}
    )
    
    # Log the event
    log_event("access_rejected", f"User {user_id} was rejected by owner", OWNER_ID)
    
    # Notify the user
    await send_notification(
        context,
        user_id,
        "❌ <b>Access Rejected</b>\n\n"
        f"Your access request has been rejected by the bot owner.\n\n"
        f"If you believe this is a mistake, please contact the bot owner.",
    )
    
    await update.message.reply_text(
        f"❌ <b>Success</b>\n\n"
        f"User {user_id} has been rejected.\n\n"
        f"They have been notified of the rejection.",
        parse_mode=ParseMode.HTML
    )

@owner_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /users command."""
    users = list(users_collection.find({}))
    
    if not users:
        await update.message.reply_text(
            "📊 <b>Users</b>\n\n"
            "No users found in the database.",
            parse_mode=ParseMode.HTML
        )
        return
    
    users_text = "📊 <b>All Users</b>\n\n"
    
    for user in users:
        user_id = user.get("user_id", "N/A")
        username = user.get("username", "N/A")
        first_name = user.get("first_name", "N/A")
        status = user.get("access_status", "unknown")
        request_date = user.get("request_date", datetime.datetime.now())
        request_date_str = request_date.strftime("%Y-%m-%d")
        
        users_text += (
            f"🆔 {user_id} - {first_name} (@{username})\n"
            f"   Status: {status.capitalize()}\n"
            f"   Requested: {request_date_str}\n\n"
        )
    
    # Split into chunks if too long
    if len(users_text) > 4000:
        chunks = [users_text[i:i+4000] for i in range(0, len(users_text), 4000)]
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(users_text, parse_mode=ParseMode.HTML)

@owner_only
async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /admin_stats command."""
    total_users = users_collection.count_documents({})
    approved_users = users_collection.count_documents({"access_status": "approved"})
    pending_users = users_collection.count_documents({"access_status": "pending"})
    rejected_users = users_collection.count_documents({"access_status": "rejected"})
    
    total_accounts = accounts_collection.count_documents({})
    total_sessions = sessions_collection.count_documents({})
    
    settings = settings_collection.find_one({})
    monitoring_enabled = settings.get("monitoring_enabled", True) if settings else True
    
    stats_text = (
        f"📊 <b>System Statistics</b>\n\n"
        f"👥 <b>Users:</b>\n"
        f"   Total: {total_users}\n"
        f"   Approved: {approved_users}\n"
        f"   Pending: {pending_users}\n"
        f"   Rejected: {rejected_users}\n\n"
        f"📱 <b>Accounts:</b> {total_accounts}\n"
        f"🔐 <b>Sessions:</b> {total_sessions}\n\n"
        f"⚙️ <b>Settings:</b>\n"
        f"   Session Monitoring: {'Enabled' if monitoring_enabled else 'Disabled'}\n"
    )
    
    await update.message.reply_text(stats_text, parse_mode=ParseMode.HTML)

@owner_only
async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /logs command."""
    logs = list(logs_collection.find({}).sort("timestamp", -1).limit(50))
    
    if not logs:
        await update.message.reply_text(
            "📊 <b>System Logs</b>\n\n"
            "No logs found in the database.",
            parse_mode=ParseMode.HTML
        )
        return
    
    logs_text = "📊 <b>System Logs</b>\n\n"
    
    for log in logs:
        timestamp = log.get("timestamp", datetime.datetime.now())
        timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        event_type = log.get("event_type", "unknown")
        details = log.get("details", "N/A")
        user_id = log.get("user_id", "N/A")
        
        logs_text += (
            f"📅 {timestamp_str}\n"
            f"🔖 Event: {event_type}\n"
            f"👤 User: {user_id}\n"
            f"📝 Details: {details}\n\n"
        )
    
    # Split into chunks if too long
    if len(logs_text) > 4000:
        chunks = [logs_text[i:i+4000] for i in range(0, len(logs_text), 4000)]
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(logs_text, parse_mode=ParseMode.HTML)

@owner_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /settings command."""
    settings = settings_collection.find_one({})
    monitoring_enabled = settings.get("monitoring_enabled", True) if settings else True
    
    keyboard = [
        [
            InlineKeyboardButton(
                "Toggle Session Monitoring",
                callback_data=f"toggle_monitoring_{monitoring_enabled}"
            )
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    settings_text = (
        f"⚙️ <b>Bot Settings</b>\n\n"
        f"🔐 <b>Session Monitoring:</b> {'Enabled' if monitoring_enabled else 'Disabled'}\n\n"
        f"Use the button below to toggle session monitoring."
    )
    
    await update.message.reply_text(
        settings_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

# Account management
@approved_only
async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /accounts command."""
    user_id = update.effective_user.id
    accounts, total_pages = get_paginated_accounts(user_id)
    
    if not accounts:
        keyboard = [
            [InlineKeyboardButton("➕ Add Account", callback_data="add_account")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📱 <b>Your Accounts</b>\n\n"
            "You don't have any linked accounts yet.\n\n"
            "Use the button below to add your first account.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
        return
    
    accounts_text = "📱 <b>Your Accounts</b>\n\n"
    keyboard = []
    
    for i, account in enumerate(accounts):
        account_id = account.get("_id", "N/A")
        phone = account.get("phone_number", "N/A")
        created_at = account.get("created_at", datetime.datetime.now())
        created_at_str = created_at.strftime("%Y-%m-%d")
        
        # Check if session exists
        has_session = "session_data" in account and account["session_data"]
        session_status = "🟢 Active" if has_session else "🔴 No Session"
        
        accounts_text += (
            f"{i+1}. {phone}\n"
            f"   ID: {account_id}\n"
            f"   Added: {created_at_str}\n"
            f"   Status: {session_status}\n\n"
        )
        
        keyboard.append([
            InlineKeyboardButton(f"Manage {phone}", callback_data=f"manage_account_{account_id}"),
            InlineKeyboardButton(f"Delete {phone}", callback_data=f"delete_account_{account_id}")
        ])
    
    # Add navigation buttons if more than one page
    if total_pages > 1:
        nav_buttons = []
        if 0 > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"accounts_page_{0-1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"Page 1/{total_pages}", callback_data="noop"))
        
        if total_pages > 1:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"accounts_page_{1}"))
        
        keyboard.append(nav_buttons)
    
    # Add add account button
    keyboard.append([InlineKeyboardButton("➕ Add Account", callback_data="add_account")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        accounts_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

# Account conversation handlers
async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the account addition process."""
    await update.message.reply_text(
        "📱 <b>Add New Account</b>\n\n"
        "Please enter the phone number of the Telegram account you want to add.\n\n"
        "Include the country code, e.g., +1234567890\n\n"
        "Send /cancel to abort this process.",
        parse_mode=ParseMode.HTML
    )
    return ACCOUNT_PHONE

async def add_account_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the phone number input."""
    phone = update.message.text.strip()
    
    # Basic validation
    if not phone.startswith('+') or not phone[1:].isdigit():
        await update.message.reply_text(
            "❌ <b>Invalid Phone Number</b>\n\n"
            "Please enter a valid phone number with country code.\n\n"
            "Example: +1234567890\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return ACCOUNT_PHONE
    
    # Store phone number in context
    context.user_data["phone"] = phone
    
    # Create a temporary Telethon client to request the code
    try:
        client = TelegramClient(
            StringSession(),
            API_ID,
            API_HASH
        )
        
        await client.connect()
        
        # Request code
        result = await client.send_code_request(phone)
        
        await client.disconnect()
        
        # Store phone code hash for verification
        context.user_data["phone_code_hash"] = result.phone_code_hash
        
        await update.message.reply_text(
            "✅ <b>Verification Code Sent</b>\n\n"
            "A verification code has been sent to your Telegram account.\n\n"
            "Please enter the code you received.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return ACCOUNT_CODE
        
    except Exception as e:
        logger.error(f"Error sending code request: {e}")
        await update.message.reply_text(
            f"❌ <b>Error</b>\n\n"
            f"Failed to send verification code: {str(e)}\n\n"
            "Please try again later or contact support.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

async def add_account_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the verification code input."""
    code = update.message.text.strip()
    
    # Basic validation
    if not code.isdigit():
        await update.message.reply_text(
            "❌ <b>Invalid Code</b>\n\n"
            "The verification code should only contain numbers.\n\n"
            "Please try again.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return ACCOUNT_CODE
    
    # Store code in context
    context.user_data["code"] = code
    
    # Check if 2FA is needed
    try:
        client = TelegramClient(
            StringSession(),
            API_ID,
            API_HASH
        )
        
        await client.connect()
        
        # Try to sign in with the code
        try:
            await client.sign_in(
                context.user_data["phone"],
                context.user_data["phone_code_hash"],
                code
            )
            
            # If we get here, no 2FA is needed
            session_string = client.session.save()
            
            await client.disconnect()
            
            # Save account to database
            user_id = update.effective_user.id
            phone = context.user_data["phone"]
            
            account_data = {
                "user_id": user_id,
                "phone_number": phone,
                "session_data": encrypt_data(session_string),
                "created_at": datetime.datetime.now()
            }
            
            account_id = accounts_collection.insert_one(account_data).inserted_id
            
            # Log the event
            log_event("account_added", f"Account {phone} added for user {user_id}", user_id)
            
            # Notify owner
            await notify_owner(
                context,
                f"📱 <b>New Account Added</b>\n\n"
                f"User: {update.effective_user.first_name} (@{update.effective_user.username})\n"
                f"Account: {phone}\n"
                f"Account ID: {account_id}"
            )
            
            await update.message.reply_text(
                "✅ <b>Account Added Successfully</b>\n\n"
                f"Your account {phone} has been added to the bot.\n\n"
                "You can now use this account for group creation and other features.\n\n"
                "Use /accounts to manage your accounts.",
                parse_mode=ParseMode.HTML
            )
            
            return ConversationHandler.END
            
        except errors.SessionPasswordNeededError:
            # 2FA is needed
            await client.disconnect()
            
            await update.message.reply_text(
                "🔐 <b>Two-Factor Authentication Required</b>\n\n"
                "This account has 2FA enabled.\n\n"
                "Please enter your 2FA password.\n\n"
                "Send /cancel to abort this process.",
                parse_mode=ParseMode.HTML
            )
            return ACCOUNT_PASSWORD
            
    except Exception as e:
        logger.error(f"Error during sign in: {e}")
        await update.message.reply_text(
            f"❌ <b>Error</b>\n\n"
            f"Failed to sign in: {str(e)}\n\n"
            "Please check the verification code and try again.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return ACCOUNT_CODE

async def add_account_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the 2FA password input."""
    password = update.message.text.strip()
    
    # Store password in context
    context.user_data["password"] = password
    
    try:
        client = TelegramClient(
            StringSession(),
            API_ID,
            API_HASH
        )
        
        await client.connect()
        
        # Try to sign in with the code and password
        await client.sign_in(
            context.user_data["phone"],
            context.user_data["phone_code_hash"],
            context.user_data["code"],
            password=password
        )
        
        # Get session string
        session_string = client.session.save()
        
        await client.disconnect()
        
        # Save account to database
        user_id = update.effective_user.id
        phone = context.user_data["phone"]
        
        account_data = {
            "user_id": user_id,
            "phone_number": phone,
            "session_data": encrypt_data(session_string),
            "created_at": datetime.datetime.now()
        }
        
        account_id = accounts_collection.insert_one(account_data).inserted_id
        
        # Log the event
        log_event("account_added", f"Account {phone} added for user {user_id}", user_id)
        
        # Notify owner
        await notify_owner(
            context,
            f"📱 <b>New Account Added</b>\n\n"
            f"User: {update.effective_user.first_name} (@{update.effective_user.username})\n"
            f"Account: {phone}\n"
            f"Account ID: {account_id}"
        )
        
        await update.message.reply_text(
            "✅ <b>Account Added Successfully</b>\n\n"
            f"Your account {phone} has been added to the bot.\n\n"
            "You can now use this account for group creation and other features.\n\n"
            "Use /accounts to manage your accounts.",
            parse_mode=ParseMode.HTML
        )
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Error during sign in with password: {e}")
        await update.message.reply_text(
            f"❌ <b>Error</b>\n\n"
            f"Failed to sign in: {str(e)}\n\n"
            "Please check your 2FA password and try again.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return ACCOUNT_PASSWORD

async def cancel_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the account addition process."""
    await update.message.reply_text(
        "❌ <b>Process Cancelled</b>\n\n"
        "The account addition process has been cancelled.\n\n"
        "Use /accounts to manage your accounts.",
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

# Group creation
@approved_only
async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /groups command."""
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    if not accounts:
        await update.message.reply_text(
            "📱 <b>No Accounts Available</b>\n\n"
            "You need to add at least one account before creating groups.\n\n"
            "Use /accounts to add an account.",
            parse_mode=ParseMode.HTML
        )
        return
    
    keyboard = [
        [InlineKeyboardButton("➕ Create Groups", callback_data="create_groups")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    accounts_text = "📱 <b>Available Accounts for Group Creation</b>\n\n"
    
    for i, account in enumerate(accounts):
        phone = account.get("phone_number", "N/A")
        has_session = "session_data" in account and account["session_data"]
        session_status = "🟢 Active" if has_session else "🔴 No Session"
        
        accounts_text += f"{i+1}. {phone} - {session_status}\n"
    
    accounts_text += "\nUse the button below to create groups."
    
    await update.message.reply_text(
        accounts_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

# Group creation conversation handlers
async def create_groups_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the group creation process."""
    await update.message.reply_text(
        "👥 <b>Create Groups</b>\n\n"
        "Let's configure your group creation settings.\n\n"
        "First, what would you like to name your groups?\n\n"
        "You can use a pattern like 'My Group' and the bot will create 'My Group 1', 'My Group 2', etc.\n\n"
        "Send /cancel to abort this process.",
        parse_mode=ParseMode.HTML
    )
    return GROUP_NAME

async def create_groups_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the group name input."""
    name = update.message.text.strip()
    
    if not name:
        await update.message.reply_text(
            "❌ <b>Invalid Name</b>\n\n"
            "Please enter a valid group name.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return GROUP_NAME
    
    # Store name in context
    context.user_data["group_name"] = name
    
    await update.message.reply_text(
        f"✅ <b>Group Name Set</b>\n\n"
        f"Groups will be named: '{name} 1', '{name} 2', etc.\n\n"
        "How many groups would you like to create?\n\n"
        "Please enter a number between 1 and 50.\n\n"
        "Send /cancel to abort this process.",
        parse_mode=ParseMode.HTML
    )
    return GROUP_COUNT

async def create_groups_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the group count input."""
    count_text = update.message.text.strip()
    
    try:
        count = int(count_text)
        if count < 1 or count > 50:
            raise ValueError("Count out of range")
    except ValueError:
        await update.message.reply_text(
            "❌ <b>Invalid Count</b>\n\n"
            "Please enter a number between 1 and 50.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return GROUP_COUNT
    
    # Store count in context
    context.user_data["group_count"] = count
    
    await update.message.reply_text(
        f"✅ <b>Group Count Set</b>\n\n"
        f"You will create {count} groups.\n\n"
        "How much delay would you like between creating each group?\n\n"
        "Please enter the delay in seconds (between 5 and 60).\n\n"
        "Send /cancel to abort this process.",
        parse_mode=ParseMode.HTML
    )
    return GROUP_DELAY

async def create_groups_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the group delay input."""
    delay_text = update.message.text.strip()
    
    try:
        delay = int(delay_text)
        if delay < 5 or delay > 60:
            raise ValueError("Delay out of range")
    except ValueError:
        await update.message.reply_text(
            "❌ <b>Invalid Delay</b>\n\n"
            "Please enter a number between 5 and 60.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return GROUP_DELAY
    
    # Store delay in context
    context.user_data["group_delay"] = delay
    
    # Get user accounts
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    # Filter accounts with active sessions
    active_accounts = []
    for account in accounts:
        if "session_data" in account and account["session_data"]:
            active_accounts.append(account)
    
    if not active_accounts:
        await update.message.reply_text(
            "❌ <b>No Active Sessions</b>\n\n"
            "You don't have any accounts with active sessions.\n\n"
            "Please add an account with an active session first.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END
    
    # Create keyboard with account options
    keyboard = []
    
    # Option to use all accounts
    keyboard.append([
        InlineKeyboardButton(
            f"Use All {len(active_accounts)} Accounts",
            callback_data="use_all_accounts"
        )
    ])
    
    # Option to select specific accounts
    for account in active_accounts:
        phone = account.get("phone_number", "N/A")
        keyboard.append([
            InlineKeyboardButton(
                f"Use {phone}",
                callback_data=f"use_account_{account.get('_id')}"
            )
        ])
    
    # Cancel button
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_groups")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "✅ <b>Settings Configured</b>\n\n"
        f"Group Name: {context.user_data['group_name']}\n"
        f"Number of Groups: {context.user_data['group_count']}\n"
        f"Delay Between Groups: {context.user_data['group_delay']} seconds\n\n"
        "Please select which account(s) to use for creating groups:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

async def cancel_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the group creation process."""
    await update.message.reply_text(
        "❌ <b>Process Cancelled</b>\n\n"
        "The group creation process has been cancelled.\n\n"
        "Use /groups to try again.",
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

# Callback query handlers
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "add_account":
        # Start account addition conversation
        await query.message.reply_text(
            "📱 <b>Add New Account</b>\n\n"
            "Please enter the phone number of the Telegram account you want to add.\n\n"
            "Include the country code, e.g., +1234567890\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        context.user_data["adding_account"] = True
        return ACCOUNT_PHONE
    
    elif data.startswith("manage_account_"):
        # Extract account ID
        account_id = data.split("_", 2)[2]
        
        # Get account details
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>Error</b>\n\n"
                "Account not found.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        created_at = account.get("created_at", datetime.datetime.now())
        created_at_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
        
        # Check if session exists
        has_session = "session_data" in account and account["session_data"]
        session_status = "🟢 Active" if has_session else "🔴 No Session"
        
        # Create keyboard with management options
        keyboard = []
        
        if has_session:
            keyboard.append([
                InlineKeyboardButton("🔄 Refresh Session", callback_data=f"refresh_session_{account_id}")
            ])
            keyboard.append([
                InlineKeyboardButton("🗑️ Delete Session", callback_data=f"delete_session_{account_id}")
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("➕ Create Session", callback_data=f"create_session_{account_id}")
            ])
        
        keyboard.append([
            InlineKeyboardButton("🗑️ Delete Account", callback_data=f"delete_account_{account_id}")
        ])
        keyboard.append([
            InlineKeyboardButton("⬅️ Back to Accounts", callback_data="back_to_accounts")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        account_text = (
            f"📱 <b>Account Details</b>\n\n"
            f"📞 <b>Phone:</b> {phone}\n"
            f"🆔 <b>ID:</b> {account_id}\n"
            f"📅 <b>Added:</b> {created_at_str}\n"
            f"🔐 <b>Session Status:</b> {session_status}\n\n"
            f"Use the buttons below to manage this account."
        )
        
        await query.message.reply_text(
            account_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    elif data.startswith("delete_account_"):
        # Extract account ID
        account_id = data.split("_", 2)[2]
        
        # Get account details
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>Error</b>\n\n"
                "Account not found.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        
        # Create confirmation keyboard
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_account_{account_id}"),
                InlineKeyboardButton("❌ No, Cancel", callback_data="back_to_accounts")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(
            f"⚠️ <b>Delete Account Confirmation</b>\n\n"
            f"Are you sure you want to delete the account {phone}?\n\n"
            f"This action cannot be undone.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    elif data.startswith("confirm_delete_account_"):
        # Extract account ID
        account_id = data.split("_", 3)[3]
        
        # Get account details
        account = accounts_collection.find_one({"_id": account_id})
        if not account:
            await query.message.reply_text(
                "❌ <b>Error</b>\n\n"
                "Account not found.",
                parse_mode=ParseMode.HTML
            )
            return
        
        phone = account.get("phone_number", "N/A")
        user_id = account.get("user_id", update.effective_user.id)
        
        # Delete account
        accounts_collection.delete_one({"_id": account_id})
        
        # Log the event
        log_event("account_deleted", f"Account {phone} deleted by user {user_id}", user_id)
        
        # Notify owner
        await notify_owner(
            context,
            f"📱 <b>Account Deleted</b>\n\n"
            f"User: {update.effective_user.first_name} (@{update.effective_user.username})\n"
            f"Account: {phone}\n"
            f"Account ID: {account_id}"
        )
        
        await query.message.reply_text(
            f"✅ <b>Account Deleted</b>\n\n"
            f"The account {phone} has been deleted successfully.\n\n"
            f"Use /accounts to manage your remaining accounts.",
            parse_mode=ParseMode.HTML
        )
    
    elif data == "back_to_accounts":
        # Go back to accounts list
        await accounts_command(update, context)
    
    elif data.startswith("accounts_page_"):
        # Extract page number
        try:
            page = int(data.split("_", 2)[2])
        except (IndexError, ValueError):
            page = 0
        
        user_id = update.effective_user.id
        accounts, total_pages = get_paginated_accounts(user_id, page)
        
        if not accounts:
            await query.message.reply_text(
                "📱 <b>Your Accounts</b>\n\n"
                "No accounts found on this page.",
                parse_mode=ParseMode.HTML
            )
            return
        
        accounts_text = "📱 <b>Your Accounts</b>\n\n"
        keyboard = []
        
        for i, account in enumerate(accounts):
            account_id = account.get("_id", "N/A")
            phone = account.get("phone_number", "N/A")
            created_at = account.get("created_at", datetime.datetime.now())
            created_at_str = created_at.strftime("%Y-%m-%d")
            
            # Check if session exists
            has_session = "session_data" in account and account["session_data"]
            session_status = "🟢 Active" if has_session else "🔴 No Session"
            
            accounts_text += (
                f"{i+1}. {phone}\n"
                f"   ID: {account_id}\n"
                f"   Added: {created_at_str}\n"
                f"   Status: {session_status}\n\n"
            )
            
            keyboard.append([
                InlineKeyboardButton(f"Manage {phone}", callback_data=f"manage_account_{account_id}"),
                InlineKeyboardButton(f"Delete {phone}", callback_data=f"delete_account_{account_id}")
            ])
        
        # Add navigation buttons
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"accounts_page_{page-1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"accounts_page_{page+1}"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        # Add add account button
        keyboard.append([InlineKeyboardButton("➕ Add Account", callback_data="add_account")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(
            accounts_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    elif data == "create_groups":
        # Start group creation conversation
        await query.message.reply_text(
            "👥 <b>Create Groups</b>\n\n"
            "Let's configure your group creation settings.\n\n"
            "First, what would you like to name your groups?\n\n"
            "You can use a pattern like 'My Group' and the bot will create 'My Group 1', 'My Group 2', etc.\n\n"
            "Send /cancel to abort this process.",
            parse_mode=ParseMode.HTML
        )
        context.user_data["creating_groups"] = True
        return GROUP_NAME
    
    elif data.startswith("toggle_monitoring_"):
        # Extract current state
        current_state = data.split("_", 2)[2] == "True"
        
        # Toggle the state
        new_state = not current_state
        
        # Update settings
        settings_collection.update_one(
            {},
            {"$set": {"monitoring_enabled": new_state}}
        )
        
        # Log the event
        log_event(
            "settings_changed",
            f"Session monitoring toggled from {current_state} to {new_state}",
            update.effective_user.id
        )
        
        # Update the message
        keyboard = [
            [
                InlineKeyboardButton(
                    "Toggle Session Monitoring",
                    callback_data=f"toggle_monitoring_{new_state}"
                )
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        settings_text = (
            f"⚙️ <b>Bot Settings</b>\n\n"
            f"🔐 <b>Session Monitoring:</b> {'Enabled' if new_state else 'Disabled'}\n\n"
            f"Use the button below to toggle session monitoring."
        )
        
        await query.message.edit_text(
            settings_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    elif data == "noop":
        # No operation, just acknowledge the button press
        pass
    
    else:
        # Unknown callback
        await query.message.reply_text(
            "❌ <b>Error</b>\n\n"
            "Unknown action. Please try again.",
            parse_mode=ParseMode.HTML
        )

# Error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the owner."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    # Notify the owner about the error
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"⚠️ <b>Error</b>\n\n"
                 f"An error occurred while processing an update:\n\n"
                 f"Error: {context.error}\n\n"
                 f"Update: {update}",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to send error notification to owner: {e}")

# Main function
def main():
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stats", stats_command))
    
    # Owner commands
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("reject", reject_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("admin_stats", admin_stats_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("settings", settings_command))
    
    # Account management
    application.add_handler(CommandHandler("accounts", accounts_command))
    
    # Group creation
    application.add_handler(CommandHandler("groups", groups_command))
    
    # Account conversation handler
    account_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("add_account", add_account_start),
            CallbackQueryHandler(button_callback, pattern="^add_account$"),
        ],
        states={
            ACCOUNT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_phone)],
            ACCOUNT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_code)],
            ACCOUNT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_account)],
        per_message=False,
    )
    application.add_handler(account_conv_handler)
    
    # Group creation conversation handler
    group_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("create_groups", create_groups_start),
            CallbackQueryHandler(button_callback, pattern="^create_groups$"),
        ],
        states={
            GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_name)],
            GROUP_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_count)],
            GROUP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_delay)],
        },
        fallbacks=[CommandHandler("cancel", cancel_groups)],
        per_message=False,
    )
    application.add_handler(group_conv_handler)
    
    # Callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    # Run the bot
    application.run_polling()

if __name__ == "__main__":
    main()
