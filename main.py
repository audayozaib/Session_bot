import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict, List, Any, Optional

from telethon import TelegramClient, events, Button, functions, types
from telethon.errors import *
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# --- إعدادات التسجيل الاحترافية ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("PremiumManager")

load_dotenv()

# --- الإعدادات ---
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
DEVELOPER_ID = int(os.getenv('DEVELOPER_ID', '778375826'))

# --- قاعدة البيانات المتطورة ---
class Database:
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGO_URI)
        self.db = self.client['telegram_premium_manager']
        self.users = self.db['users']
        self.accounts = self.db['accounts']
        self.logs = self.db['action_logs']

    async def log_action(self, user_id, action):
        await self.logs.insert_one({
            'user_id': user_id,
            'action': action,
            'time': datetime.now()
        })

db = Database()

# --- محرك المراقبة الذكي ---
class MonitoringEngine:
    def __init__(self, bot):
        self.bot = bot
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.clients: Dict[str, TelegramClient] = {}

    async def start_monitor(self, acc):
        acc_id = str(acc['_id'])
        if acc_id in self.active_tasks: return
        
        task = asyncio.create_task(self._monitor_worker(acc))
        self.active_tasks[acc_id] = task

    async def stop_monitor(self, acc_id):
        if acc_id in self.active_tasks:
            self.active_tasks[acc_id].cancel()
            del self.active_tasks[acc_id]
        if acc_id in self.clients:
            await self.clients[acc_id].disconnect()
            del self.clients[acc_id]

    async def _monitor_worker(self, acc):
        acc_id = str(acc['_id'])
        phone = acc['phone']
        owner_id = acc['owner_id']
        
        client = TelegramClient(f'sessions/{phone}', API_ID, API_HASH)
        self.clients[acc_id] = client
        
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await self.bot.send_message(owner_id, f"⚠️ **تنبيه:** الحساب `{phone}` سجل خروج، يرجى إعادة إضافته.")
                return

            while True:
                # جلب الجلسات
                authorizations = await client(functions.account.GetAuthorizationsRequest())
                current_acc = await db.accounts.find_one({'_id': acc['_id']})
                if not current_acc or not current_acc.get('monitoring'): break
                
                safe_hashes = current_acc.get('existing_sessions', [])
                
                for auth in authorizations.authorizations:
                    if auth.hash not in safe_hashes:
                        # جلسة غريبة!
                        await self._terminate_session(client, auth, owner_id, phone)
                
                await asyncio.sleep(15) # فحص سريع جداً كل 15 ثانية
        except Exception as e:
            logger.error(f"Monitor error for {phone}: {e}")
        finally:
            await client.disconnect()

    async def _terminate_session(self, client, auth, owner_id, phone):
        msg = (
            f"🛡 **[ نظام الحماية التلقائي ]**\n\n"
            f"⚠️ **محاولة دخول مشبوهة!**\n"
            f"📱 الحساب: `{phone}`\n"
            f"🖥 الجهاز: `{auth.device_model}`\n"
            f"🌍 الموقع: `{auth.country}`\n"
            f"🌐 IP: `{auth.ip}`\n\n"
            f"⚡️ **الحالة:** تم طرد الجلسة وتأمين الحساب فوراً."
        )
        try:
            await client(functions.account.ResetAuthorizationRequest(hash=auth.hash))
            await self.bot.send_message(owner_id, msg)
            await db.log_action(owner_id, f"تم طرد جلسة غريبة من حساب {phone}")
        except Exception as e:
            await self.bot.send_message(owner_id, f"❌ **فشل طرد جلسة:** {e}")

# --- البوت الرئيسي بواجهة Premium ---
class PremiumBot:
    def __init__(self):
        self.bot = TelegramClient('premium_bot', API_ID, API_HASH)
        self.engine = MonitoringEngine(self.bot)
        self.states = {}

    async def start(self):
        await self.bot.start(bot_token=BOT_TOKEN)
        self._handlers()
        # إعادة تشغيل المراقبة
        async for acc in db.accounts.find({'monitoring': True}):
            await self.engine.start_monitor(acc)
        logger.info("Premium Bot Started!")
        await self.bot.run_until_disconnected()

    def _handlers(self):
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_cmd(event):
            uid = event.sender_id
            user = await event.get_sender()
            
            # إشعار المطور
            if uid != DEVELOPER_ID:
                await self.bot.send_message(DEVELOPER_ID, f"🆕 **مستخدم جديد انضم:**\n👤 الاسم: {user.first_name}\n🆔 الآيدي: `{uid}`")

            text = (
                f"💎 **أهلاً بك في نظام الإدارة المتقدم**\n\n"
                f"أنا بوت احترافي مصمم لحماية حساباتك ومراقبة الجلسات بدقة عالية.\n\n"
                f"📊 **إحصائياتك:**\n"
                f"👤 المستخدم: {user.first_name}\n"
                f"🆔 الآيدي: `{uid}`\n"
                f"📱 الحسابات المضافة: {await db.accounts.count_documents({'owner_id': uid})}"
            )
            btns = [
                [Button.inline("➕ إضافة حساب جديد", b"add_acc"), Button.inline("📂 حساباتي", b"my_accs")],
                [Button.inline("📜 سجل العمليات", b"logs"), Button.inline("ℹ️ حول النظام", b"about")]
            ]
            if uid == DEVELOPER_ID:
                btns.append([Button.inline("⚙️ لوحة المطور", b"dev_panel")])
            
            await event.respond(text, buttons=btns)

        @self.bot.on(events.CallbackQuery())
        async def manager(event):
            data = event.data
            uid = event.sender_id

            if data == b"my_accs":
                accs = await db.accounts.find({'owner_id': uid}).to_list(100)
                if not accs:
                    return await event.edit("❌ **لا توجد حسابات مضافة حالياً.**", buttons=[Button.inline("➕ إضافة الآن", b"add_acc"), Button.inline("🔙 رجوع", b"home")])
                
                btns = []
                for a in accs:
                    status = "🟢" if a.get('monitoring') else "🔴"
                    btns.append([Button.inline(f"{status} {a['phone']}", f"manage_{a['_id']}".encode())])
                btns.append([Button.inline("🔙 رجوع للقائمة الرئيسية", b"home")])
                await event.edit("📂 **قائمة حساباتك المضافة:**", buttons=btns)

            elif data.startswith(b"manage_"):
                acc_id = data.decode().split('_')[1]
                acc = await db.accounts.find_one({'_id': acc_id})
                m_status = "✅ مفعلة" if acc.get('monitoring') else "❌ معطلة"
                m_btn = "🔴 تعطيل المراقبة" if acc.get('monitoring') else "🟢 تفعيل المراقبة"
                
                text = (
                    f"⚙️ **إدارة الحساب:** `{acc['phone']}`\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🛡 **حالة المراقبة:** {m_status}\n"
                    f"📅 تاريخ الإضافة: {acc['created_at'].strftime('%Y-%m-%d')}\n"
                    f"━━━━━━━━━━━━━━"
                )
                btns = [
                    [Button.inline(m_btn, f"toggle_{acc_id}".encode())],
                    [Button.inline("🗑 حذف الحساب نهائياً", f"confirm_del_{acc_id}".encode())],
                    [Button.inline("🔙 رجوع", b"my_accs")]
                ]
                await event.edit(text, buttons=btns)

            elif data.startswith(b"toggle_"):
                acc_id = data.decode().split('_')[1]
                acc = await db.accounts.find_one({'_id': acc_id})
                new_val = not acc.get('monitoring')
                await db.accounts.update_one({'_id': acc_id}, {'$set': {'monitoring': new_val}})
                
                if new_val: await self.engine.start_monitor(acc)
                else: await self.engine.stop_monitor(acc_id)
                
                await event.answer(f"تم {'تفعيل' if new_val else 'تعطيل'} المراقبة بنجاح!")
                await manager(event)

            elif data == b"add_acc":
                self.states[uid] = {'step': 'phone'}
                await event.edit("📱 **يرجى إرسال رقم الهاتف الآن:**\nمثال: `+966500000000`", buttons=[Button.inline("❌ إلغاء", b"home")])

            elif data == b"home":
                self.states.pop(uid, None)
                await start_cmd(event)

        @self.bot.on(events.NewMessage())
        async def add_flow(event):
            uid = event.sender_id
            if uid not in self.states: return
            
            state = self.states[uid]
            text = event.text.strip()

            if state['step'] == 'phone':
                client = TelegramClient(f'sessions/{text}', API_ID, API_HASH)
                await client.connect()
                try:
                    req = await client.send_code_request(text)
                    state.update({'step': 'code', 'phone': text, 'client': client, 'hash': req.phone_code_hash})
                    await event.respond(f"📩 **تم إرسال الكود إلى {text}**\nأرسل الكود هنا:")
                except Exception as e:
                    await event.respond(f"❌ **خطأ:** {e}")

            elif state['step'] == 'code':
                try:
                    await state['client'].sign_in(state['phone'], text, phone_code_hash=state['hash'])
                    await self._save_account(uid, state['phone'], state['client'])
                    await event.respond("✅ **تم ربط الحساب وتفعيل الحماية التلقائية!**")
                    self.states.pop(uid)
                except SessionPasswordNeededError:
                    state['step'] = 'pass'
                    await event.respond("🔐 **الحساب محمي بكلمة سر (2FA):**\nأرسل كلمة السر الآن:")
                except Exception as e:
                    await event.respond(f"❌ **خطأ في الكود:** {e}")

            elif state['step'] == 'pass':
                try:
                    await state['client'].sign_in(password=text)
                    await self._save_account(uid, state['phone'], state['client'])
                    await event.respond("✅ **تم تسجيل الدخول بنجاح!**")
                    self.states.pop(uid)
                except Exception as e:
                    await event.respond(f"❌ **كلمة سر خاطئة:** {e}")

    async def _save_account(self, owner_id, phone, client):
        auths = await client(functions.account.GetAuthorizationsRequest())
        hashes = [a.hash for a in auths.authorizations]
        acc_id = str(time.time()).replace('.', '')
        
        acc = {
            '_id': acc_id,
            'owner_id': owner_id,
            'phone': phone,
            'monitoring': True,
            'existing_sessions': hashes,
            'created_at': datetime.now()
        }
        await db.accounts.insert_one(acc)
        await self.engine.start_monitor(acc)
        await client.disconnect()
        await db.log_action(owner_id, f"إضافة حساب جديد {phone}")

if __name__ == '__main__':
    if not os.path.exists('sessions'): os.makedirs('sessions')
    asyncio.run(PremiumBot().start())
