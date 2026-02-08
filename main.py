# main.py
import os
import asyncio
import logging
import datetime
from typing import Dict, List
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
from bson.objectid import ObjectId
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl import functions, types
from cryptography.fernet import Fernet
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.getenv("DB_NAME", "telegram_bot_db")
ENCRYPTION_KEY_STR = os.getenv("ENCRYPTION_KEY")

# --- Security & Database Setup ---
if not all([BOT_TOKEN, API_ID, API_HASH, ENCRYPTION_KEY_STR]):
    logger.critical("One or more critical environment variables are missing!")
    exit(1)

cipher_suite = Fernet(ENCRYPTION_KEY_STR.encode())
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db["users"]
accounts_collection = db["accounts"]

# --- States for Conversations #
(ACCOUNT_PHONE, ACCOUNT_CODE, ACCOUNT_PASSWORD) = range(3)
(GROUP_NAME, GROUP_COUNT, GROUP_DELAY) = range(3, 6)

# --- Helper Functions ---
def encrypt_data(data: str) -> str: return cipher_suite.encrypt(data.encode()).decode()
def decrypt_data(encrypted_data: str) -> str: return cipher_suite.decrypt(encrypted_data.encode()).decode()

def get_user_status(user_id: int) -> str:
    user = users_collection.find_one({"user_id": user_id})
    return user.get("access_status", "not_registered") if user else "not_registered"

def is_approved(user_id: int) -> bool: return get_user_status(user_id) == "approved"
def is_owner(user_id: int) -> bool: return user_id == OWNER_ID

def get_user_accounts(user_id: int) -> List[Dict]:
    accounts = list(accounts_collection.find({"user_id": user_id}))
    for acc in accounts:
        if "session_data" in acc and acc["session_data"]:
            acc["session_data"] = decrypt_data(acc["session_data"])
    return accounts

async def send_message(update: Update, text: str, reply_markup=None):
    """إرسال رسالة بأمان سواء كانت استدعاء أو رسالة."""
    try:
        if update.message:
            return await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            return await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Could not send message: {e}")
        # Fallback for old messages that can't be edited
        if update.callback_query:
            try:
                await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            except Exception as e2:
                logger.error(f"Fallback send also failed: {e2}")


# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """القائمة الرئيسية التي تعتمد على حالة المستخدم."""
    user = update.effective_user
    user_id = user.id
    status = get_user_status(user_id)
    
    # Ensure user is in the database
    if status == "not_registered":
        users_collection.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id, "first_name": user.first_name, "username": user.username, "access_status": "not_registered"}},
            upsert=True
        )
    
    keyboard = []
    text = ""

    if status in ["not_registered", "rejected"]:
        keyboard = [
            [InlineKeyboardButton("📝 طلب استخدام البوت", callback_data="request_access")],
            [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
            [InlineKeyboardButton("📱 إدارة الحسابات", callback_data="manage_accounts")],
        ]
        text = f"👋 مرحباً {user.first_name}!\n\nأنت غير مسجل. اختر أحد الخيارات للبدء."
    elif status == "pending":
        keyboard = [
            [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
            [InlineKeyboardButton("📱 إدارة الحسابات", callback_data="manage_accounts")],
        ]
        text = f"👋 مرحباً {user.first_name}!\n\nطلبك قيد المراجعة. يمكنك إضافة حساباتك الآن."
    else: # approved
        keyboard = [
            [InlineKeyboardButton("📱 حساباتي", callback_data="accounts")],
            [InlineKeyboardButton("👥 المجموعات", callback_data="groups")],
            [InlineKeyboardButton("📊 حالتي", callback_data="status")],
        ]
        text = f"👋 مرحباً بعودتك {user.first_name}!\n\nاختر ما تريد القيام به:"

    await send_message(update, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض المساعدة."""
    text = "استخدم الأزرار للتنقل. \n\nللمالك: استخدم /approve [user_id] و /reject [user_id]."
    await send_message(update, text)

# --- Callback Query Handler (The Brain of the Bot) ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # --- Navigation ---
    if data == "main_menu": await start(update, context); return
    if data == "accounts": await _show_accounts_list(update, user_id, 0); return
    if data == "groups": await _show_group_creation_accounts(update, user_id); return
    if data in ["status", "stats"]:
        await send_message(update, "هذا الخيار قيد التطوير.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="main_menu")]]))
        return

    # --- Access Request Flow ---
    if data == "request_access":
        users_collection.update_one({"user_id": user_id}, {"$set": {"access_status": "pending"}})
        user = update.effective_user
        owner_keyboard = [
            [InlineKeyboardButton("✅ قبول", callback_data=f"owner_approve_{user_id}")],
            [InlineKeyboardButton("❌ رفض", callback_data=f"owner_reject_{user_id}")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="main_menu")]
        ]
        await context.bot.send_message(
            OWNER_ID,
            f"🔔 <b>طلب جديد</b>\n\n"
            f"المستخدم: {user.first_name} (@{user.username})\n"
            f"الآيدي: {user_id}",
            reply_markup=InlineKeyboardMarkup(owner_keyboard),
            parse_mode=ParseMode.HTML
        )
        await send_message(update, "✅ تم إرسال الطلب! سيتم إعلامك بالقرار.")
        return

    # --- Owner Actions ---
    if is_owner(user_id):
        if data.startswith("owner_approve_"):
            target_id = int(data.split("_")[2])
            users_collection.update_one({"user_id": target_id}, {"$set": {"access_status": "approved"}})
            await context.bot.send_message(target_id, "✅ <b>تم قبول طلبك!</b>\n\nيمكنك الآن استخدام البوت بالكامل.", parse_mode=ParseMode.HTML)
            await query.edit_message_text(f"✅ تم قبول المستخدم {target_id}.")
            return
        if data.startswith("owner_reject_"):
            target_id = int(data.split("_")[2])
            users_collection.update_one({"user_id": target_id}, {"$set": {"access_status": "rejected"}})
            await context.bot.send_message(target_id, "❌ <b>تم رفض طلبك.</b>", parse_mode=ParseMode.HTML)
            await query.edit_message_text(f"❌ تم رفض المستخدم {target_id}.")
            return

    # --- Account Management ---
    if data == "manage_accounts": await _show_accounts_list(update, user_id, 0); return
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

# --- Account Management Details ---
async def _show_accounts_list(update: Update, user_id: int, page: int):
    """عرض قائمة الحسابات مقسمة."""
    accounts = get_user_accounts(user_id)
    page_size = 10
    total_pages = (len(accounts) + page_size - 1) // page_size
    start_index = page * page_size
    end_index = start_index + page_size
    paginated_accounts = accounts[start_index:end_index]

    if not paginated_accounts:
        await send_message(update, "📱 لا توجد حسابات في هذه الصفحة.")
        return

    keyboard = []
    for acc in paginated_accounts:
        acc_id = str(acc["_id"])
        name = acc.get("first_name", "No Name")
        has_session = "session_data" in acc and acc["session_data"]
        monitoring = acc.get("monitoring_enabled", False)

        if has_session and monitoring: status_emoji = "🔵"
        elif has_session: status_emoji = "🟢"
        else: status_emoji = "🔴"
        
        keyboard.append([InlineKeyboardButton(f"{status_emoji} {name}", callback_data=f"view_account_{acc_id}")])

    # Navigation buttons
    nav_buttons = []
    if page > 0: nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"acc_page_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(f"صفحة {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1: nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"acc_page_{page+1}"))
    if nav_buttons: keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")])
    keyboard.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="main_menu")])
    
    text = f"📱 <b>حساباتك (صفحة {page+1})</b>\n\nاختر حساباً لإدارة تفاصيله:"
    await send_message(update, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def _get_account_full_details(client: TelegramClient, account: Dict) -> Dict:
    """جلب تفاصيل كاملة للحساب."""
    try:
        me = await client.get_me()
        account["first_name"] = me.first_name or "No Name"
        account["username"] = f"@{me.username}" if me.username else "No Username"
        account["bio"] = me.about or "No Bio"
        if me.photo:
            photo_bytes = await client.download_profile_photo(me.photo.file_id, file=BytesIO())
            account["profile_pic"] = photo_bytes.getvalue()
        else:
            account["profile_pic"] = None
    except Exception as e:
        logger.error(f"Could not fetch full details for account: {e}")
        account["first_name"] = account.get("first_name", "Unknown Account")
        account["username"] = "N/A"; account["bio"] = "N/A"; account["profile_pic"] = None
    return account

async def _show_account_details(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id_str: str):
    """عرض تفاصيل حساب معين مع الأزرار."""
    try: account_id = ObjectId(account_id_str)
    except Exception: await send_message(update, "❌ معرف الحساب غير صالح."); return

    account = accounts_collection.find_one({"_id": account_id, "user_id": update.effective_user.id})
    if not account or not account.get("session_data"): await send_message(update, "❌ لم يتم العثور على الحساب أو لا توجد جلسة."); return

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
        
        text = f"👤 <b>{name}</b>\n🔖 {username}\n📝 {bio}\n\n🔐 مراقبة الجلسات: {'مفعلة' if monitoring else 'معطلة'}"
        keyboard = [
            [InlineKeyboardButton("👥 إنشاء مجموعات", callback_data=f"create_groups_for_{account_id_str}")],
            [InlineKeyboardButton(f"{'🔴 إلغاء' if monitoring else '🔵 تفعيل'} مراقبة الجلسات", callback_data=f"toggle_monitoring_{account_id_str}")],
            [InlineKeyboardButton("🗑️ حذف الحساب", callback_data=f"delete_account_{account_id_str}")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="accounts")]
        ]
        
        if account.get("profile_pic"):
            await update.callback_query.message.reply_photo(photo=account["profile_pic"], caption=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        else:
            await send_message(update, text, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e: logger.error(f"Error showing account details: {e}"); await send_message(update, f"❌ حدث خطأ: {str(e)}")
    finally:
        if client.is_connected(): await client.disconnect()

async def _toggle_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id_str: str):
    """تبديل حالة المراقبة لحساب معين."""
    try: account_id = ObjectId(account_id_str)
    except Exception: await send_message(update, "❌ معرف الحساب غير صالح."); return

    account = accounts_collection.find_one({"_id": account_id, "user_id": update.effective_user.id})
    if not account: await send_message(update, "❌ لم يتم العثور على الحساب."); return

    new_status = not account.get("monitoring_enabled", False)
    accounts_collection.update_one({"_id": account_id}, {"$set": {"monitoring_enabled": new_status}})
    status_text = "تفعيل" if new_status else "إلغاء تفعيل"
    await send_message(update, f"🔐 <b>تم {status_text} مراقبة الجلسات</b>")
    await _show_account_details(update, context, account_id_str) # Refresh view

async def _delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id_str: str):
    """حذف حساب."""
    try: account_id = ObjectId(account_id_str)
    except Exception: await send_message(update, "❌ معرف الحساب غير صالح."); return

    account = accounts_collection.find_one({"_id": account_id, "user_id": update.effective_user.id})
    if not account: await send_message(update, "❌ لم يتم العثور على الحساب."); return

    name = account.get("first_name", "N/A")
    accounts_collection.delete_one({"_id": account_id})
    await send_message(update, f"🗑️ <b>تم حذف الحساب</b>\n\n{name}")
    await _show_accounts_list(update, update.effective_user.id, 0)

# --- Group Creation Flow ---
async def _show_group_creation_accounts(update: Update, user_id: int):
    """عرض الحسابات المناسبة لإنشاء المجموعات."""
    active_accounts = [acc for acc in get_user_accounts(user_id) if "session_data" in acc and acc["session_data"]]
    if not active_accounts:
        await send_message(update, "👥 <b>لا توجد حسابات نشطة</b>\n\nأضف حساباً بجلسة نشطة لإنشاء المجموعات.")
        return
    
    keyboard = []
    for acc in active_accounts:
        name = acc.get("first_name", "No Name")
        acc_id = str(acc["_id"])
        keyboard.append([InlineKeyboardButton(f"إنشاء باستخدام {name}", callback_data=f"create_groups_for_{acc_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="main_menu")])
    await send_message(update, "👥 <b>اختر حساباً لإنشاء المجموعات:</b>", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Conversation Handlers ---
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
        
        # Get account details after signing in
        me = await client.get_me()
        account_name = me.first_name or "No Name"
        
        session_final = client.session.save()
        await client.disconnect()
        
        accounts_collection.insert_one({
            "user_id": update.effective_user.id,
            "phone_number": context.user_data["phone"],
            "first_name": account_name, # Store name from the start
            "session_data": encrypt_data(session_final),
            "created_at": datetime.datetime.now(),
            "monitoring_enabled": False
        })
        
        await wait_msg.edit_text(f"✅ <b>تمت إضافة الحساب بنجاح!</b>\n\nالاسم: {account_name}")
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
        
        me = await client.get_me()
        account_name = me.first_name or "No Name"
        
        session_final = client.session.save()
        await client.disconnect()
        
        accounts_collection.insert_one({
            "user_id": update.effective_user.id,
            "phone_number": context.user_data["phone"],
            "first_name": account_name,
            "session_data": encrypt_data(session_final),
            "created_at": datetime.datetime.now(),
            "monitoring_enabled": False
        })
        
        await send_message(update, f"✅ <b>تمت إضافة الحساب بنجاح!</b>\n\nالاسم: {account_name}")
        return ConversationHandler.END
    except Exception as e:
        await send_message(update, f"❌ خطأ في كلمة المرور: {str(e)}")
        return ConversationHandler.END
    finally:
        if client.is_connected(): await client.disconnect()

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء أي محادثة."""
    await send_message(update, "❌ تم إلغاء العملية.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القائمة الرئيسية", "main_menu")]]))
    return ConversationHandler.END

async def create_groups_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_message(update, "👥 <b>إنشاء مجموعات</b>\n\nماذا تريد أن تسمي المجموعات؟ (مثال: نادي الأصدقاء)")
    return GROUP_NAME

async def create_groups_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["group_name"] = update.message.text.strip()
    await send_message(update, f"✅ سيتم تسميتها: '{context.user_data['group_name']} 1', ...\n\nكم مجموعة تريد إنشاءها؟ (1-50)")
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
        
        acc_id = context.user_data.get("selected_account_id")
        if not acc_id:
            await send_message(update, "❌ لم يتم تحديد حساب. حاول مرة أخرى.")
            return ConversationHandler.END
        
        # Start the background task
        asyncio.create_task(_create_groups_task(update, context, acc_id))
        await send_message(update, "⏳ <b>بدأت عملية إنشاء المجموعات!</b>\n\nسيتم إعلامك عند الانتهاء.")
        return ConversationHandler.END
        
    except ValueError:
        await send_message(update, "❌ الرقم غير صالح. أرسل رقماً بين 5 و 60.")
        return GROUP_DELAY

async def _create_groups_task(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id_str: str):
    """المهمة الفعلية لإنشاء المجموعات."""
    try: account_id = ObjectId(account_id_str)
    except Exception: return

    account = accounts_collection.find_one({"_id": account_id, "user_id": update.effective_user.id})
    if not account or not account.get("session_data"): return

    client = TelegramClient(StringSession(account["session_data"]), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized(): return

        group_name = context.user_data["group_name"]
        group_count = context.user_data["group_count"]
        group_delay = context.user_data["group_delay"]
        
        processing_msg = await update.callback_query.message.reply_text(f"⏳ <b>جاري إنشاء المجموعات...</b>\n\nالعدد: {group_count}")
        
        created_count = 0
        for i in range(group_count):
            try:
                title = f"{group_name} {i + 1}"
                result = await client(functions.channels.CreateChannelRequest(title=title, about="تم الإنشاء بواسطة البوت", megagroup=True))
                group = result.chats[0]
                
                welcome_messages = [
                    "مرحباً بكم في المجموعة! 🎉", "هذه رسالة ترحيبية من البوت.", "نأمل أن تستمتعوا بوقتكم هنا.",
                    "لا تترددوا في المشاركة والتفاعل.", "القوانين بسيطة: احترام الجميع.",
                    "يمكنكم استخدام الأمر /help لمعرفة المزيد.", "شكراً لانضمامكم! 🙏",
                    "هذه المجموعة تم إنشاؤها تلقائياً.", "البوت الآن يرسل الرسائل الترحيبية.",
                    "انتهى الإعداد! استمتعوا بالمجموعة. ✅"
                ]
                for msg_text in welcome_messages:
                    await client.send_message(group.id, msg_text)
                    await asyncio.sleep(0.5)

                created_count += 1
                await processing_msg.edit_text(f"⏳ <b>جاري إنشاء المجموعات...</b>\n\nتم إنشاء {created_count} من {group_count}")
                if i < group_count - 1: await asyncio.sleep(group_delay)

            except Exception as e: logger.error(f"Failed to create group {i+1}: {e}")
        
        await client.disconnect()
        await processing_msg.edit_text(
            f"✅ <b>اكتملت العملية!</b>\n\nتم إنشاء {created_count} مجموعة.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القائمة الرئيسية", "main_menu")]])
        )

    except Exception as e: logger.error(f"Error in group creation task: {e}")
    finally: if client.is_connected(): await client.disconnect()

# --- Session Monitoring Background Task ---
async def session_monitoring_task(app: Application):
    """مهمة خلفية لمراقبة وحظر الجلسات الجديدة."""
    logger.info("Session monitoring task started.")
    while True:
        try:
            monitored_accounts = list(accounts_collection.find({"monitoring_enabled": True}))
            for acc in monitored_accounts:
                session_data = decrypt_data(acc["session_data"])
                client = TelegramClient(StringSession(session_data), API_ID, API_HASH)
                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        accounts_collection.update_one({"_id": acc["_id"]}, {"$set": {"monitoring_enabled": False}})
                        continue
                    
                    authorizations = await client(functions.account.GetAuthorizationsRequest())
                    current_hashes = {auth.hash for auth in authorizations.authorizations}
                    known_hashes = set(acc.get("known_session_hashes", []))
                    
                    new_hashes = current_hashes - known_hashes
                    if new_hashes:
                        for new_hash in new_hashes:
                            auth_obj = next((a for a in authorizations.authorizations if a.hash == new_hash), None)
                            if auth_obj:
                                device_model = auth_obj.device_model or "Unknown Device"
                                platform = auth_obj.platform or "Unknown Platform"
                                await client(functions.account.ResetAuthorizationRequest(hash=new_hash))
                                logger.info(f"Killed new session on {acc.get('first_name')} from {device_model}")
                                
                                await app.bot.send_message(
                                    acc["user_id"],
                                    f"⚠️ <b>تم حظر جلسة جديدة!</b>\n\n"
                                    f"الحساب: {acc.get('first_name', 'N/A')}\n"
                                    f"الجهاز: {device_model} ({platform})\n"
                                    f"التوقيت: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                                    parse_mode=ParseMode.HTML
                                )
                    
                    accounts_collection.update_one({"_id": acc["_id"]}, {"$set": {"known_session_hashes": list(current_hashes)}})

                except Exception as e: logger.error(f"Error monitoring account {acc.get('first_name')}: {e}")
                finally: if client.is_connected(): await client.disconnect()
            
            await asyncio.sleep(300) # Check every 5 minutes

        except Exception as e: logger.error(f"Critical error in session monitoring task: {e}"); await asyncio.sleep(600)

# --- Main Function ---
def main():
    if not all([BOT_TOKEN, API_ID, API_HASH]): return

    application = Application.builder().token(BOT_TOKEN).build()
    
    # --- Conversation Handlers ---
    account_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_callback, pattern="^add_account$")],
        states={
            ACCOUNT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_phone), CommandHandler("cancel", cancel_action)],
            ACCOUNT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_code), CommandHandler("cancel", cancel_action)],
            ACCOUNT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_password), CommandHandler("cancel", cancel_action)],
        }, fallbacks=[CommandHandler("cancel", cancel_action)], per_message=False,
    )
    group_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_callback, pattern="^create_groups_for_")],
        states={
            GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_name), CommandHandler("cancel", cancel_action)],
            GROUP_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_count), CommandHandler("cancel", cancel_action)],
            GROUP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_groups_delay), CommandHandler("cancel", cancel_action)],
        }, fallbacks=[CommandHandler("cancel", cancel_action)], per_message=False,
    )

    # --- Add Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(account_conv)
    application.add_handler(group_conv)
    
    # --- Start Background Task ---
    loop = asyncio.get_event_loop()
    loop.create_task(session_monitoring_task(application))
    
    # --- Run Bot ---
    logger.info("Bot started successfully.")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
