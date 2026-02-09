import asyncio
import logging
import os
import sys
import time
import json
import secrets
import hashlib
from datetime import datetime
from typing import Dict, List, Any, Optional, Set
from collections import defaultdict
from functools import wraps
from dataclasses import dataclass, asdict

# --- المكتبات الجديدة ---
from cryptography.fernet import Fernet
from cachetools import TTLCache
import aiofiles
from fastapi import FastAPI, WebSocket, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from contextlib import asynccontextmanager

# --- المكتبات الأساسية ---
from telethon import TelegramClient, events, Button, functions, types
from telethon.errors import *
from telethon.sessions import StringSession
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# --- الإعدادات والثوابت ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("UltimateManager")

load_dotenv()

# --- الثوابت ---
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
DEVELOPER_ID = int(os.getenv('DEVELOPER_ID', '0'))
MONITOR_INTERVAL = int(os.getenv('MONITOR_INTERVAL', '20'))
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))
BACKUP_CHANNEL = os.getenv('BACKUP_CHANNEL', '')
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
WEB_USERNAME = os.getenv('WEB_USERNAME', 'admin')
WEB_PASSWORD = os.getenv('WEB_PASSWORD', secrets.token_urlsafe(16))

# --- أنظمة الأمان ---

class SessionEncryption:
    """تشفير وفك تشفير ملفات الجلسات"""
    
    def __init__(self, key: str):
        # تأكد من أن المفتاح 32 بايت
        key_bytes = key.encode()[:32].ljust(32, b'0')[:32]
        # تحويل إلى base64 URL-safe
        import base64
        key_b64 = base64.urlsafe_b64encode(key_bytes)
        self.cipher = Fernet(key_b64)
        self.temp_sessions: Dict[str, str] = {}  # تتبع الملفات المؤقتة
    
    async def encrypt_session(self, phone: str):
        """تشفير ملف الجلسة"""
        session_file = f'sessions/{phone}.session'
        if not os.path.exists(session_file):
            return False
        
        try:
            async with aiofiles.open(session_file, 'rb') as f:
                data = await f.read()
            
            encrypted = self.cipher.encrypt(data)
            enc_path = f'sessions/{phone}.session.enc'
            
            async with aiofiles.open(enc_path, 'wb') as f:
                await f.write(encrypted)
            
            # حذف الأصلي بشكل آمن
            os.remove(session_file)
            logger.info(f"Session encrypted for {phone}")
            return True
        except Exception as e:
            logger.error(f"Encryption error for {phone}: {e}")
            return False
    
    async def decrypt_session(self, phone: str) -> Optional[str]:
        """فك تشفير ملف الجلسة وإرجاع المسار المؤقت"""
        enc_path = f'sessions/{phone}.session.enc'
        if not os.path.exists(enc_path):
            # ربما لم يُشفّر بعد
            session_file = f'sessions/{phone}.session'
            if os.path.exists(session_file):
                return session_file
            return None
        
        try:
            temp_path = f'sessions/.temp_{phone}_{int(time.time())}.session'
            
            async with aiofiles.open(enc_path, 'rb') as f:
                data = await f.read()
            
            decrypted = self.cipher.decrypt(data)
            
            async with aiofiles.open(temp_path, 'wb') as f:
                await f.write(decrypted)
            
            self.temp_sessions[phone] = temp_path
            return temp_path
        except Exception as e:
            logger.error(f"Decryption error for {phone}: {e}")
            return None
    
    async def cleanup_temp(self, phone: str):
        """تنظيف الملف المؤقت"""
        if phone in self.temp_sessions:
            try:
                if os.path.exists(self.temp_sessions[phone]):
                    os.remove(self.temp_sessions[phone])
                del self.temp_sessions[phone]
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
    
    async def secure_delete(self, phone: str):
        """حذف آمن لملفات الجلسة"""
        files_to_delete = [
            f'sessions/{phone}.session',
            f'sessions/{phone}.session.enc',
            f'sessions/{phone}.session-journal'
        ]
        
        for file_path in files_to_delete:
            if os.path.exists(file_path):
                # الكتابة فوق البيانات قبل الحذف
                with open(file_path, 'ba+') as f:
                    length = f.tell()
                    f.seek(0)
                    f.write(os.urandom(length))
                os.remove(file_path)
                logger.info(f"Securely deleted: {file_path}")

class RateLimiter:
    """حماية من محاولات متكررة"""
    
    def __init__(self, max_attempts: int = 5, window: int = 300):
        self.attempts = defaultdict(list)
        self.max_attempts = max_attempts
        self.window = window
        self.blocked: Dict[int, float] = {}
    
    def is_allowed(self, user_id: int) -> tuple[bool, Optional[int]]:
        """التحقق مما إذا كان المستخدم مسموحاً له"""
        now = time.time()
        
        # التحقق من الحظر
        if user_id in self.blocked:
            if now < self.blocked[user_id]:
                remaining = int(self.blocked[user_id] - now)
                return False, remaining
            else:
                del self.blocked[user_id]
                self.attempts[user_id] = []
        
        # تنظيف المحاولات القديمة
        self.attempts[user_id] = [
            t for t in self.attempts[user_id] 
            if now - t < self.window
        ]
        
        if len(self.attempts[user_id]) >= self.max_attempts:
            # حظر المستخدم
            block_duration = 900  # 15 دقيقة
            self.blocked[user_id] = now + block_duration
            return False, block_duration
        
        self.attempts[user_id].append(now)
        return True, None
    
    def reset(self, user_id: int):
        """إعادة تعيين المحاولات"""
        self.attempts[user_id] = []
        if user_id in self.blocked:
            del self.blocked[user_id]

class DeveloperAuth:
    """التحقق الثنائي للمطور"""
    
    def __init__(self):
        self.pending_auth: Dict[int, Dict] = {}
        self.verified_sessions: Set[int] = set()
        self.session_timeout = 3600  # ساعة واحدة
    
    async def request_auth(self, bot: TelegramClient, user_id: int) -> bool:
        """طلب كود تحقق"""
        if user_id != DEVELOPER_ID:
            return False
        
        # إنشاء كود عشوائي
        code = secrets.token_hex(3).upper()
        self.pending_auth[user_id] = {
            'code': code,
            'expires': time.time() + 300,  # 5 دقائق
            'attempts': 0
        }
        
        await bot.send_message(
            user_id,
            f"🔐 **كود التحقق:** `{code}`\n"
            f"⏰ صالح لمدة 5 دقائق\n"
            f"🚫 {3 - self.pending_auth[user_id]['attempts']} محاولات متبقية"
        )
        return True
    
    def verify_code(self, user_id: int, code: str) -> bool:
        """التحقق من الكود"""
        if user_id not in self.pending_auth:
            return False
        
        auth = self.pending_auth[user_id]
        
        if time.time() > auth['expires']:
            del self.pending_auth[user_id]
            return False
        
        if auth['code'] != code.upper():
            auth['attempts'] += 1
            if auth['attempts'] >= 3:
                del self.pending_auth[user_id]
            return False
        
        self.verified_sessions.add(user_id)
        del self.pending_auth[user_id]
        return True
    
    def is_verified(self, user_id: int) -> bool:
        """التحقق مما إذا كانت الجلسة نشطة"""
        return user_id in self.verified_sessions
    
    def logout(self, user_id: int):
        """تسجيل خروج المطور"""
        if user_id in self.verified_sessions:
            self.verified_sessions.remove(user_id)

@dataclass
class SecurityRule:
    """قاعدة أمان"""
    rule_type: str
    enabled: bool = True
    params: Dict = None
    
    def __post_init__(self):
        if self.params is None:
            self.params = {}

class SecurityRulesEngine:
    """محرك القواعد الذكية"""
    
    def __init__(self):
        self.rules: List[SecurityRule] = []
        self.load_default_rules()
    
    def load_default_rules(self):
        """تحميل القواعد الافتراضية"""
        self.rules = [
            SecurityRule('country_whitelist', True, {'countries': []}),
            SecurityRule('device_blacklist', True, {'devices': ['Virtual', 'Emulator', 'BlueStacks']}),
            SecurityRule('time_based', True, {'allowed_hours': (0, 24)}),
            SecurityRule('max_sessions', True, {'max': 5}),
            SecurityRule('new_device_alert', True, {'notify': True})
        ]
    
    def should_terminate(self, auth: types.Authorization, existing_sessions: List[int]) -> tuple[bool, str]:
        """تقرير ما إذا كان يجب إنهاء الجلسة"""
        for rule in self.rules:
            if not rule.enabled:
                continue
            
            if rule.rule_type == 'country_whitelist':
                countries = rule.params.get('countries', [])
                if countries and auth.country not in countries:
                    return True, f"دولة غير مسموح بها: {auth.country}"
            
            elif rule.rule_type == 'device_blacklist':
                devices = rule.params.get('devices', [])
                if any(d.lower() in auth.device_model.lower() for d in devices):
                    return True, f"جهاز محظور: {auth.device_model}"
            
            elif rule.rule_type == 'time_based':
                start, end = rule.params.get('allowed_hours', (0, 24))
                current_hour = datetime.now().hour
                if not (start <= current_hour <= end):
                    return True, f"خارج وقت مسموح: {current_hour}:00"
            
            elif rule.rule_type == 'max_sessions':
                max_sess = rule.params.get('max', 5)
                if len(existing_sessions) >= max_sess:
                    return True, f"تجاوز الحد الأقصى للجلسات: {max_sess}"
        
        return False, ""
    
    def update_rule(self, rule_type: str, enabled: bool = None, params: Dict = None):
        """تحديث قاعدة"""
        for rule in self.rules:
            if rule.rule_type == rule_type:
                if enabled is not None:
                    rule.enabled = enabled
                if params:
                    rule.params.update(params)
                return True
        return False

# --- قاعدة البيانات ---

class Database:
    """فئة لإدارة قاعدة البيانات"""
    
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGO_URI, maxPoolSize=50)
        self.db = self.client['telegram_ultimate_manager']
        self.users = self.db['users']
        self.accounts = self.db['accounts']
        self.alerts = self.db['alerts']
        self.audit_log = self.db['audit_log']
        
    async def get_acc(self, acc_id: str) -> Optional[Dict]:
        try:
            return await self.accounts.find_one({'_id': acc_id})
        except Exception as e:
            logger.error(f"Error getting account {acc_id}: {e}")
            return None
    
    async def get_acc_by_phone(self, phone: str) -> Optional[Dict]:
        try:
            return await self.accounts.find_one({'phone': phone})
        except Exception as e:
            logger.error(f"Error getting account by phone: {e}")
            return None
    
    async def save_acc(self, data: Dict) -> bool:
        try:
            await self.accounts.insert_one(data)
            await self._log_action('account_created', data['owner_id'], data)
            return True
        except Exception as e:
            logger.error(f"Error saving account: {e}")
            return False
    
    async def update_acc(self, acc_id: str, data: Dict) -> bool:
        try:
            result = await self.accounts.update_one({'_id': acc_id}, {'$set': data})
            if result.modified_count > 0:
                await self._log_action('account_updated', None, {'acc_id': acc_id, 'data': data})
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating account: {e}")
            return False
    
    async def delete_acc(self, acc_id: str) -> bool:
        try:
            acc = await self.get_acc(acc_id)
            result = await self.accounts.delete_one({'_id': acc_id})
            if result.deleted_count > 0:
                await self._log_action('account_deleted', acc.get('owner_id') if acc else None, {'acc_id': acc_id})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error deleting account: {e}")
            return False
    
    async def get_user_accounts(self, user_id: int) -> List[Dict]:
        try:
            return await self.accounts.find({'owner_id': user_id}).to_list(100)
        except Exception as e:
            logger.error(f"Error getting user accounts: {e}")
            return []
    
    async def get_monitored_accounts(self) -> List[Dict]:
        try:
            return await self.accounts.find({'monitoring': True}).to_list(1000)
        except Exception as e:
            logger.error(f"Error getting monitored accounts: {e}")
            return []
    
    async def count_accounts(self, filter_dict: Dict = None) -> int:
        try:
            return await self.accounts.count_documents(filter_dict or {})
        except Exception as e:
            logger.error(f"Error counting accounts: {e}")
            return 0
    
    async def add_alert(self, level: str, message: str, account_id: str, details: Dict = None):
        """إضافة تنبيه"""
        try:
            await self.alerts.insert_one({
                'level': level,
                'message': message,
                'account_id': account_id,
                'details': details or {},
                'timestamp': datetime.now(),
                'read': False
            })
        except Exception as e:
            logger.error(f"Error adding alert: {e}")
    
    async def get_today_alerts_count(self) -> int:
        """عدد تنبيهات اليوم"""
        try:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            return await self.alerts.count_documents({'timestamp': {'$gte': today}})
        except Exception as e:
            logger.error(f"Error getting alerts count: {e}")
            return 0
    
    async def _log_action(self, action: str, user_id: Optional[int], details: Dict):
        """تسجيل عملية في سجل التدقيق"""
        try:
            await self.audit_log.insert_one({
                'action': action,
                'user_id': user_id,
                'details': details,
                'timestamp': datetime.now(),
                'ip': None  # يمكن إضافة IP لاحقاً
            })
        except Exception as e:
            logger.error(f"Error logging action: {e}")

# --- إدارة النسخ الاحتياطي ---

class BackupManager:
    """مدير النسخ الاحتياطي"""
    
    def __init__(self, db: Database, encryption: SessionEncryption):
        self.db = db
        self.encryption = encryption
        self.backup_interval = 3600  # كل ساعة
        self.retention_count = 10
        self.running = False
    
    async def start(self):
        """بدء حلقة النسخ الاحتياطي"""
        self.running = True
        while self.running:
            await self._create_backup()
            await asyncio.sleep(self.backup_interval)
    
    def stop(self):
        """إيقاف النسخ الاحتياطي"""
        self.running = False
    
    async def _create_backup(self):
        """إنشاء نسخة احتياطية"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            accounts = await self.db.accounts.find().to_list(1000)
            
            backup_data = {
                'timestamp': timestamp,
                'accounts_count': len(accounts),
                'accounts': accounts,
                'version': '2.0'
            }
            
            # تشفير النسخة
            json_data = json.dumps(backup_data, default=str).encode()
            encrypted = self.encryption.cipher.encrypt(json_data)
            
            backup_path = f'backups/backup_{timestamp}.enc'
            os.makedirs('backups', exist_ok=True)
            
            async with aiofiles.open(backup_path, 'wb') as f:
                await f.write(encrypted)
            
            logger.info(f"Backup created: {backup_path}")
            await self._cleanup_old_backups()
            
            # إشعار المطور
            if BACKUP_CHANNEL:
                try:
                    # يمكن إرسال ملخص هنا
                    pass
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"Backup error: {e}")
    
    async def _cleanup_old_backups(self):
        """تنظيف النسخ القديمة"""
        try:
            backups = sorted([
                f for f in os.listdir('backups') 
                if f.startswith('backup_') and f.endswith('.enc')
            ])
            
            while len(backups) > self.retention_count:
                old_backup = backups.pop(0)
                os.remove(f'backups/{old_backup}')
                logger.info(f"Deleted old backup: {old_backup}")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
    
    async def restore_backup(self, backup_file: str) -> bool:
        """استعادة نسخة احتياطية"""
        try:
            async with aiofiles.open(f'backups/{backup_file}', 'rb') as f:
                encrypted = await f.read()
            
            decrypted = self.encryption.cipher.decrypt(encrypted)
            data = json.loads(decrypted)
            
            # استعادة الحسابات
            for acc in data.get('accounts', []):
                # التحقق من عدم وجود الحساب مسبقاً
                existing = await self.db.get_acc_by_phone(acc['phone'])
                if not existing:
                    await self.db.save_acc(acc)
            
            logger.info(f"Restored backup: {backup_file}")
            return True
        except Exception as e:
            logger.error(f"Restore error: {e}")
            return False

# --- تجمع العملاء (Client Pool) ---

class ClientPool:
    """تجمع إعادة استخدام العملاء"""
    
    def __init__(self, encryption: SessionEncryption, max_size: int = 20):
        self.pool: Dict[str, TelegramClient] = {}
        self.encryption = encryption
        self.max_size = max_size
        self._lock = asyncio.Lock()
        self.last_used: Dict[str, float] = {}
        self.cleanup_task = None
    
    async def start(self):
        """بدء مهمة التنظيف الدوري"""
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
    
    async def stop(self):
        """إيقاف التجمع"""
        if self.cleanup_task:
            self.cleanup_task.cancel()
        
        async with self._lock:
            for client in self.pool.values():
                try:
                    await client.disconnect()
                except:
                    pass
            self.pool.clear()
    
    async def get_client(self, phone: str) -> Optional[TelegramClient]:
        """الحصول على عميل من التجمع أو إنشاء جديد"""
        async with self._lock:
            # التحقق من وجوده في التجمع
            if phone in self.pool:
                client = self.pool[phone]
                if client.is_connected():
                    self.last_used[phone] = time.time()
                    return client
                else:
                    try:
                        await client.connect()
                        self.last_used[phone] = time.time()
                        return client
                    except:
                        del self.pool[phone]
            
            # إنشاء عميل جديد
            if len(self.pool) >= self.max_size:
                # إزالة الأقدم استخداماً
                oldest = min(self.last_used, key=self.last_used.get)
                await self._remove_client(oldest)
            
            # فك تشفير الجلسة
            session_path = await self.encryption.decrypt_session(phone)
            if not session_path:
                return None
            
            try:
                client = TelegramClient(session_path, API_ID, API_HASH)
                await client.connect()
                
                if not await client.is_user_authorized():
                    await client.disconnect()
                    await self.encryption.cleanup_temp(phone)
                    return None
                
                self.pool[phone] = client
                self.last_used[phone] = time.time()
                return client
                
            except Exception as e:
                logger.error(f"Error creating client for {phone}: {e}")
                await self.encryption.cleanup_temp(phone)
                return None
    
    async def release(self, phone: str, disconnect: bool = False):
        """إرجاع العميل إلى التجمع"""
        if disconnect and phone in self.pool:
            await self._remove_client(phone)
    
    async def _remove_client(self, phone: str):
        """إزالة عميل من التجمع"""
        if phone in self.pool:
            try:
                await self.pool[phone].disconnect()
            except:
                pass
            del self.pool[phone]
            if phone in self.last_used:
                del self.last_used[phone]
            await self.encryption.cleanup_temp(phone)
    
    async def _cleanup_loop(self):
        """حلقة تنظيف العملاء غير المستخدمة"""
        while True:
            await asyncio.sleep(300)  # كل 5 دقائق
            try:
                now = time.time()
                to_remove = [
                    phone for phone, last in self.last_used.items()
                    if now - last > 600  # 10 دقائق بدون استخدام
                ]
                for phone in to_remove:
                    await self._remove_client(phone)
                    logger.info(f"Cleaned up idle client: {phone}")
            except Exception as e:
                logger.error(f"Cleanup loop error: {e}")

# --- نظام الإشعارات المتعدد ---

class AlertManager:
    """مدير الإشعارات"""
    
    def __init__(self, bot: TelegramClient, db: Database):
        self.bot = bot
        self.db = db
        self.channels = {
            'owner': True,
            'developer': True,
            'backup_channel': bool(BACKUP_CHANNEL),
            'webhook': False
        }
    
    async def send_alert(self, level: str, message: str, account: Dict, 
                        details: Dict = None, buttons=None):
        """إرسال إشعار لجميع القنوات"""
        
        # حفظ في قاعدة البيانات
        await self.db.add_alert(level, message, str(account.get('_id')), details)
        
        formatted_message = (
            f"{'🚨' if level == 'critical' else '⚠️' if level == 'warning' else 'ℹ️'} "
            f**"تنبيه [{level.upper()}]**\n\n"
            f"📱 الحساب: `{account.get('phone', 'Unknown')}`\n"
            f"👤 المالك: `{account.get('owner_id', 'Unknown')}`\n"
            f"⏰ الوقت: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
            f"{message}"
        )
        
        # إرسال للمالك
        try:
            owner_id = account.get('owner_id')
            if owner_id:
                await self.bot.send_message(
                    owner_id, 
                    formatted_message,
                    buttons=buttons,
                    parse_mode='markdown'
                )
        except Exception as e:
            logger.error(f"Error sending to owner: {e}")
        
        # إرسال للمطور للحالات الحرجة
        if level in ['critical', 'security']:
            try:
                await self.bot.send_message(
                    DEVELOPER_ID,
                    f"**تنبيه للمطور:**\n{formatted_message}",
                    parse_mode='markdown'
                )
            except Exception as e:
                logger.error(f"Error sending to developer: {e}")
            
            # إرسال للقناة الاحتياطية
            if BACKUP_CHANNEL:
                try:
                    await self.bot.send_message(
                        int(BACKUP_CHANNEL),
                        formatted_message,
                        parse_mode='markdown'
                    )
                except Exception as e:
                    logger.error(f"Error sending to backup channel: {e}")

# --- محرك التحكم والمراقبة ---

class ControlEngine:
    """محرك المراقبة المحسّن"""
    
    def __init__(self, bot: TelegramClient, db: Database, 
                 encryption: SessionEncryption, pool: ClientPool,
                 alerts: AlertManager):
        self.bot = bot
        self.db = db
        self.encryption = encryption
        self.pool = pool
        self.alerts = alerts
        self.active_monitors: Dict[str, asyncio.Task] = {}
        self.retry_counts: Dict[str, int] = {}
        self.session_cache = TTLCache(maxsize=100, ttl=60)  # كاش الجلسات
        self.rules_engine = SecurityRulesEngine()
        self._lock = asyncio.Lock()
    
    async def start_monitor(self, acc: Dict) -> bool:
        """بدء مراقبة حساب"""
        acc_id = str(acc['_id'])
        
        async with self._lock:
            if acc_id in self.active_monitors:
                return False
            
            self.retry_counts[acc_id] = 0
            self.active_monitors[acc_id] = asyncio.create_task(
                self._monitor_loop(acc),
                name=f"monitor_{acc_id}"
            )
            logger.info(f"Started monitoring {acc['phone']}")
            return True
    
    async def stop_monitor(self, acc_id: str) -> bool:
        """إيقاف مراقبة حساب"""
        async with self._lock:
            if acc_id not in self.active_monitors:
                return False
            
            task = self.active_monitors[acc_id]
            task.cancel()
            
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            
            del self.active_monitors[acc_id]
            logger.info(f"Stopped monitoring {acc_id}")
            return True
    
    async def stop_all_monitors(self):
        """إيقاف جميع المراقبات"""
        async with self._lock:
            tasks = list(self.active_monitors.values())
            for task in tasks:
                task.cancel()
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            
            self.active_monitors.clear()
            logger.info("All monitors stopped")
    
    async def _monitor_loop(self, acc: Dict):
        """حلقة المراقبة الرئيسية"""
        acc_id = str(acc['_id'])
        phone = acc['phone']
        owner_id = acc['owner_id']
        
        while self.retry_counts[acc_id] < MAX_RETRIES:
            client = None
            try:
                # الحصول على عميل من التجمع
                client = await self.pool.get_client(phone)
                if not client:
                    raise Exception("Failed to get client from pool")
                
                self.retry_counts[acc_id] = 0
                
                while True:
                    # التحقق من حالة المراقبة
                    current = await self.db.get_acc(acc_id)
                    if not current or not current.get('monitoring'):
                        logger.info(f"Monitoring stopped for {phone}")
                        return
                    
                    # فحص صحة الاتصال
                    if not client.is_connected():
                        await client.connect()
                    
                    # الحصول على الجلسات (مع كاش)
                    auths = await self._get_sessions(client, phone)
                    safe_hashes = set(current.get('existing_sessions', []))
                    
                    # فحص كل جلسة
                    for auth in auths:
                        if auth.hash not in safe_hashes:
                            # تطبيق قواعد الأمان
                            should_term, reason = self.rules_engine.should_terminate(
                                auth, list(safe_hashes)
                            )
                            
                            if should_term:
                                await self._handle_unauthorized_session(
                                    client, auth, acc, reason
                                )
                            else:
                                # جلسة جديدة مسموح بها
                                await self._handle_new_session(client, auth, acc)
                            
                            safe_hashes.add(auth.hash)
                    
                    # تحديث قائمة الجلسات الآمنة
                    if len(safe_hashes) > len(current.get('existing_sessions', [])):
                        await self.db.update_acc(
                            acc_id, 
                            {'existing_sessions': list(safe_hashes)}
                        )
                    
                    await asyncio.sleep(MONITOR_INTERVAL)
                    
            except asyncio.CancelledError:
                logger.info(f"Monitor cancelled for {phone}")
                return
            except Exception as e:
                self.retry_counts[acc_id] += 1
                logger.error(f"Monitor error {phone} (attempt {self.retry_counts[acc_id]}): {e}")
                await asyncio.sleep(5 * self.retry_counts[acc_id])
            finally:
                if client:
                    await self.pool.release(phone)
        
        # تجاوز المحاولات
        await self.alerts.send_alert(
            'critical',
            f"توقف المراقبة بعد {MAX_RETRIES} محاولات فاشلة",
            acc,
            {'error': 'max_retries_exceeded'}
        )
        
        async with self._lock:
            if acc_id in self.active_monitors:
                del self.active_monitors[acc_id]
        
        # تعطيل المراقبة في قاعدة البيانات
        await self.db.update_acc(acc_id, {'monitoring': False})
    
    async def _get_sessions(self, client: TelegramClient, phone: str) -> List:
        """الحصول على الجلسات مع كاش"""
        cache_key = f"sessions_{phone}"
        
        if cache_key in self.session_cache:
            return self.session_cache[cache_key]
        
        auths = await client(functions.account.GetAuthorizationsRequest())
        self.session_cache[cache_key] = auths.authorizations
        return auths.authorizations
    
    async def _handle_unauthorized_session(self, client: TelegramClient, 
                                          auth: types.Authorization, 
                                          acc: Dict, reason: str):
        """معالجة جلسة غير مصرح بها"""
        phone = acc['phone']
        
        detail_msg = (
            f"🚨 **جلسة غير مصرح بها تم اكتشافها وإنهاؤها!**\n\n"
            f"📱 **الحساب:** `{phone}`\n"
            f"🚫 **السبب:** {reason}\n"
            f"━━━━━━━━━━━━━━\n"
            f"💻 **الجهاز:** `{auth.device_model}`\n"
            f"🌐 **النظام:** `{auth.platform} {auth.system_version}`\n"
            f"📍 **الدولة:** `{auth.country}`\n"
            f"🌐 **IP:** `{auth.ip}`\n"
            f"⏰ **الوقت:** `{auth.date_created}`\n"
            f"━━━━━━━━━━━━━━"
        )
        
        try:
            await client(functions.account.ResetAuthorizationRequest(hash=auth.hash))
            
            await self.alerts.send_alert(
                'security',
                detail_msg,
                acc,
                {
                    'device': auth.device_model,
                    'country': auth.country,
                    'ip': auth.ip,
                    'reason': reason
                }
            )
            logger.info(f"Terminated unauthorized session for {phone}")
            
        except Exception as e:
            error_msg = f"❌ فشل إنهاء الجلسة: {e}"
            await self.alerts.send_alert('critical', error_msg, acc)
            logger.error(f"Failed to terminate session: {e}")
    
    async def _handle_new_session(self, client: TelegramClient,
                                 auth: types.Authorization,
                                 acc: Dict):
        """معالجة جلسة جديدة مسموح بها"""
        msg = (
            f"📱 **جلسة جديدة مسجلة:**\n\n"
            f"💻 **الجهاز:** `{auth.device_model}`\n"
            f"🌐 **الموقع:** `{auth.country}`\n"
            f"⏰ **الوقت:** `{datetime.now().strftime('%Y-%m-%d %H:%M')}`"
        )
        
        await self.alerts.send_alert('info', msg, acc, {'new_session': True})

# --- خادم الويب (FastAPI) ---

class WebDashboard:
    """لوحة تحكم الويب"""
    
    def __init__(self, db: Database, engine: ControlEngine, 
                 backup: BackupManager, auth: DeveloperAuth):
        self.db = db
        self.engine = engine
        self.backup = backup
        self.dev_auth = auth
        self.app = FastAPI(title="Telegram Monitor Dashboard")
        self.security = HTTPBasic()
        self._setup_routes()
    
    def _setup_routes(self):
        """إعداد المسارات"""
        
        @self.app.get("/", response_class=HTMLResponse)
        async def dashboard(credentials: HTTPBasicCredentials = Depends(self.security)):
            if not self._verify_credentials(credentials):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            
            stats = await self._get_stats()
            
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Telegram Monitor Dashboard</title>
                <meta charset="UTF-8">
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 40px; background: #f0f2f5; }}
                    .card {{ background: white; padding: 20px; margin: 20px 0; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                    .stat {{ display: inline-block; margin: 10px 20px; }}
                    .stat-value {{ font-size: 2em; color: #1a73e8; }}
                    .stat-label {{ color: #666; }}
                    h1 {{ color: #202124; }}
                    .alert {{ padding: 10px; margin: 5px 0; border-radius: 5px; }}
                    .alert-critical {{ background: #fce8e8; color: #d93025; }}
                    .alert-warning {{ background: #fef3e8; color: #f9ab00; }}
                    button {{ padding: 10px 20px; margin: 5px; cursor: pointer; border: none; border-radius: 5px; background: #1a73e8; color: white; }}
                    button:hover {{ background: #1557b0; }}
                </style>
            </head>
            <body>
                <h1>📊 Telegram Monitor Dashboard</h1>
                
                <div class="card">
                    <h2>إحصائيات عامة</h2>
                    <div class="stat">
                        <div class="stat-value">{stats['total_accounts']}</div>
                        <div class="stat-label">إجمالي الحسابات</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value">{stats['monitored']}</div>
                        <div class="stat-label">المراقبة النشطة</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value">{stats['active_monitors']}</div>
                        <div class="stat-label">المحركات النشطة</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value">{stats['alerts_today']}</div>
                        <div class="stat-label">التنبيهات اليوم</div>
                    </div>
                </div>
                
                <div class="card">
                    <h2>التحكم</h2>
                    <button onclick="restartMonitors()">🔄 إعادة تشغيل المراقبة</button>
                    <button onclick="createBackup()">💾 نسخ احتياطي</button>
                    <button onclick="viewLogs()">📋 سجل العمليات</button>
                </div>
                
                <div class="card">
                    <h2>الحسابات النشطة</h2>
                    <div id="accounts">{self._render_accounts(stats['accounts'])}</div>
                </div>
                
                <script>
                    async function restartMonitors() {{
                        await fetch('/api/restart', {{method: 'POST'}});
                        alert('تم إعادة التشغيل');
                    }}
                    async function createBackup() {{
                        await fetch('/api/backup', {{method: 'POST'}});
                        alert('تم إنشاء النسخة الاحتياطية');
                    }}
                    setInterval(() => location.reload(), 30000);  // تحديث كل 30 ثانية
                </script>
            </body>
            </html>
            """
        
        @self.app.get("/api/stats")
        async def api_stats(credentials: HTTPBasicCredentials = Depends(self.security)):
            if not self._verify_credentials(credentials):
                raise HTTPException(status_code=401)
            return await self._get_stats()
        
        @self.app.post("/api/restart")
        async def restart_monitors(credentials: HTTPBasicCredentials = Depends(self.security)):
            if not self._verify_credentials(credentials):
                raise HTTPException(status_code=401)
            
            await self.engine.stop_all_monitors()
            accounts = await self.db.get_monitored_accounts()
            for acc in accounts:
                await self.engine.start_monitor(acc)
            return {"status": "restarted"}
        
        @self.app.post("/api/backup")
        async def trigger_backup(credentials: HTTPBasicCredentials = Depends(self.security)):
            if not self._verify_credentials(credentials):
                raise HTTPException(status_code=401)
            
            await self.backup._create_backup()
            return {"status": "backup_created"}
        
        @self.app.websocket("/ws")
        async def websocket(websocket: WebSocket):
            await websocket.accept()
            while True:
                stats = await self._get_stats()
                await websocket.send_json(stats)
                await asyncio.sleep(5)
    
    def _verify_credentials(self, credentials: HTTPBasicCredentials) -> bool:
        """التحقق من بيانات الدخول"""
        return (credentials.username == WEB_USERNAME and 
                credentials.password == WEB_PASSWORD)
    
    async def _get_stats(self) -> Dict:
        """جلب الإحصائيات"""
        accounts = await self.db.get_monitored_accounts()
        return {
            'total_accounts': await self.db.count_accounts(),
            'monitored': len(accounts),
            'active_monitors': len(self.engine.active_monitors),
            'alerts_today': await self.db.get_today_alerts_count(),
            'accounts': [
                {
                    'phone': a['phone'],
                    'monitoring': a.get('monitoring', False),
                    'owner': a['owner_id']
                }
                for a in accounts[:10]  # أول 10 فقط
            ]
        }
    
    def _render_accounts(self, accounts: List[Dict]) -> str:
        """عرض قائمة الحسابات"""
        if not accounts:
            return "<p>لا توجد حسابات نشطة</p>"
        
        html = "<table style='width:100%; border-collapse: collapse;'>"
        html += "<tr style='background:#f8f9fa'><th>الهاتف</th><th>الحالة</th><th>المالك</th></tr>"
        
        for acc in accounts:
            status = "🟢" if acc['monitoring'] else "🔴"
            html += f"""
            <tr style='border-bottom:1px solid #ddd'>
                <td>{acc['phone']}</td>
                <td>{status}</td>
                <td>{acc['owner']}</td>
            </tr>
            """
        html += "</table>"
        return html
    
    async def start(self):
        """بدء الخادم"""
        config = uvicorn.Config(self.app, host="0.0.0.0", port=8000, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()

# --- البوت الرئيسي ---

class UltimateBot:
    """البوت الرئيسي الشامل"""
    
    def __init__(self):
        self.bot = TelegramClient('ultimate_bot', API_ID, API_HASH)
        self.db = Database()
        self.encryption = SessionEncryption(ENCRYPTION_KEY)
        self.pool = ClientPool(self.encryption)
        self.backup = BackupManager(self.db, self.encryption)
        self.dev_auth = DeveloperAuth()
        self.rate_limiter = RateLimiter()
        
        # سيتم تهيئتهم لاحقاً
        self.alerts: Optional[AlertManager] = None
        self.engine: Optional[ControlEngine] = None
        self.web: Optional[WebDashboard] = None
        
        self.states: Dict[int, Dict] = {}
        self.running = False
    
    async def initialize(self):
        """تهيئة المكونات"""
        self.alerts = AlertManager(self.bot, self.db)
        self.engine = ControlEngine(
            self.bot, self.db, self.encryption, 
            self.pool, self.alerts
        )
        self.web = WebDashboard(self.db, self.engine, self.backup, self.dev_auth)
        
        await self.pool.start()
    
    async def run(self):
        """تشغيل البوت"""
        try:
            await self.initialize()
            
            await self.bot.start(bot_token=BOT_TOKEN)
            self._setup_handlers()
            self.running = True
            
            # بدء مراقبة الحسابات
            monitored = await self.db.get_monitored_accounts()
            for acc in monitored:
                await self.engine.start_monitor(acc)
            
            # بدء النسخ الاحتياطي
            backup_task = asyncio.create_task(self.backup.start())
            
            # بدء خادم الويب
            web_task = asyncio.create_task(self.web.start())
            
            logger.info("✅ Bot started successfully")
            logger.info(f"🌐 Web dashboard: http://localhost:8000")
            logger.info(f"👤 Username: {WEB_USERNAME}")
            
            await self.bot.run_until_disconnected()
            
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            raise
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """إيقاف آمن"""
        self.running = False
        logger.info("Shutting down...")
        
        await self.engine.stop_all_monitors()
        await self.pool.stop()
        self.backup.stop()
        await self.db.client.close()
        
        # تنظيف الملفات المؤقتة
        for phone in list(self.encryption.temp_sessions.keys()):
            await self.encryption.cleanup_temp(phone)
        
        logger.info("Shutdown complete")
    
    def _setup_handlers(self):
        """إعداد معالجات الأحداث"""
        
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start(event):
            uid = event.sender_id
            
            # التحقق من الحظر
            allowed, wait_time = self.rate_limiter.is_allowed(uid)
            if not allowed:
                await event.respond(f"⛔ تم حظرك. انتظر {wait_time // 60} دقيقة.")
                return
            
            user = await event.get_sender()
            
            # إشعار المطور
            if uid != DEVELOPER_ID:
                await self.bot.send_message(
                    DEVELOPER_ID,
                    f"👤 عضو جديد: {user.first_name}\n"
                    f"🆔 `{uid}`\n"
                    f"🔗 @{user.username or 'None'}"
                )
            
            # التحقق من المطور
            is_dev = uid == DEVELOPER_ID and self.dev_auth.is_verified(uid)
            
            btns = [
                [Button.inline("➕ إضافة حساب", b"add"), 
                 Button.inline("📂 حساباتي", b"list")],
                [Button.inline("⚙️ لوحة المطور", b"dev") if is_dev 
                 else Button.inline("🔐 تحقق", b"dev_auth") if uid == DEVELOPER_ID
                 else Button.inline("ℹ️ معلومات", b"info")]
            ]
            
            await event.respond(
                "🚀 **مركز السيطرة المتقدم**\n\n"
                "🛡 حماية ذكية للحسابات\n"
                "📊 مراقبة فورية للجلسات\n"
                "🔔 إشعارات فورية",
                buttons=btns
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"dev_auth"))
        async def dev_auth_handler(event):
            """طلب تحقق المطور"""
            uid = event.sender_id
            if uid != DEVELOPER_ID:
                await event.answer("❌ غير مصرح", alert=True)
                return
            
            await self.dev_auth.request_auth(self.bot, uid)
            await event.edit(
                "🔐 أرسل كود التحقق المكون من 6 أحرف:",
                buttons=[Button.inline("❌ إلغاء", b"home")]
            )
            self.states[uid] = {'step': 'dev_verify'}
        
        @self.bot.on(events.CallbackQuery())
        async def cb_handler(event):
            data = event.data
            uid = event.sender_id
            
            # التحقق من الحظر
            allowed, _ = self.rate_limiter.is_allowed(uid)
            if not allowed:
                await event.answer("⛔ أنت محظور مؤقتاً", alert=True)
                return
            
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
                    if not self.dev_auth.is_verified(uid):
                        await event.answer("🔐 يجب التحقق أولاً", alert=True)
                        return
                    await self._show_dev_panel(event)
                elif data == b"info":
                    await self._show_info(event)
            except Exception as e:
                logger.error(f"Callback error: {e}")
                await event.answer("❌ خطأ", alert=True)
        
        @self.bot.on(events.NewMessage())
        async def msg_handler(event):
            """معالج الرسائل"""
            uid = event.sender_id
            text = event.text.strip()
            
            if uid not in self.states:
                return
            
            state = self.states[uid]
            
            # التحقق من خطوة التحقق للمطور
            if state.get('step') == 'dev_verify':
                if self.dev_auth.verify_code(uid, text):
                    await event.respond("✅ تم التحقق بنجاح!")
                    del self.states[uid]
                    await start(event)
                else:
                    await event.respond("❌ كود خاطئ. حاول مرة أخرى.")
                return
            
            # خطوات إضافة الحساب
            try:
                if state['step'] == 'phone':
                    await self._process_phone(event, state, text)
                elif state['step'] == 'code':
                    await self._process_code(event, state, text)
                elif state['step'] == 'pass':
                    await self._process_password(event, state, text)
            except Exception as e:
                logger.error(f"Process error: {e}")
                await event.respond("❌ خطأ في العملية")
                del self.states[uid]
    
    # --- دوال المساعدة ---
    
    async def _show_accounts(self, event, uid):
        """عرض حسابات المستخدم"""
        accs = await self.db.get_user_accounts(uid)
        if not accs:
            await event.edit("❌ لا توجد حسابات", buttons=[Button.inline("🔙", b"home")])
            return
        
        btns = [[Button.inline(f"👤 {a['phone']}", f"view_{a['_id']}".encode())] 
                for a in accs]
        btns.append([Button.inline("🔙 رجوع", b"home")])
        await event.edit("📂 اختر حساباً:", buttons=btns)
    
    async def _show_account_details(self, event, data):
        """عرض تفاصيل الحساب"""
        acc_id = data.decode().split('_')[1]
        acc = await self.db.get_acc(acc_id)
        
        if not acc or acc['owner_id'] != event.sender_id:
            await event.answer("❌ غير موجود", alert=True)
            return
        
        status = "🟢 مراقب" if acc.get('monitoring') else "🔴 متوقف"
        sessions_count = len(acc.get('existing_sessions', []))
        
        btns = [
            [Button.inline(f"📱 الجلسات ({sessions_count})", f"sessions_{acc_id}".encode())],
            [Button.inline("▶️ تشغيل" if not acc.get('monitoring') else "⏹ إيقاف", 
                          f"toggle_{acc_id}".encode())],
            [Button.inline("🗑 حذف", f"del_{acc_id}".encode())],
            [Button.inline("🔙 رجوع", b"list")]
        ]
        
        await event.edit(
            f"👤 **{acc['phone']}**\n"
            f"📊 الحالة: {status}\n"
            f"📅 الإضافة: {acc.get('created_at', 'Unknown')}",
            buttons=btns
        )
    
    async def _show_sessions(self, event, data):
        """عرض جلسات الحساب"""
        acc_id = data.decode().split('_')[1]
        acc = await self.db.get_acc(acc_id)
        
        if not acc:
            await event.answer("❌ غير موجود", alert=True)
            return
        
        await event.answer("⏳ جاري التحميل...")
        
        client = await self.pool.get_client(acc['phone'])
        if not client:
            await event.respond("❌ لا يمكن الاتصال بالحساب")
            return
        
        try:
            auths = await client(functions.account.GetAuthorizationsRequest())
            text = f"📱 **جلسات {acc['phone']}:**\n\n"
            
            for i, a in enumerate(auths.authorizations, 1):
                current = " ✅" if a.current else ""
                text += (
                    f"{i}. `{a.device_model[:20]}`{current}\n"
                    f"   🌐 {a.platform} | {a.country}\n"
                    f"   📍 IP: {a.ip}\n\n"
                )
            
            await event.respond(text, buttons=[Button.inline("🔙", f"view_{acc_id}".encode())])
        finally:
            await self.pool.release(acc['phone'])
    
    async def _toggle_monitoring(self, event, data):
        """تشغيل/إيقاف المراقبة"""
        acc_id = data.decode().split('_')[1]
        acc = await self.db.get_acc(acc_id)
        
        if not acc or acc['owner_id'] != event.sender_id:
            await event.answer("❌ غير مصرح", alert=True)
            return
        
        new_val = not acc.get('monitoring', False)
        
        if await self.db.update_acc(acc_id, {'monitoring': new_val}):
            acc['monitoring'] = new_val  # تحديث المحلي
            
            if new_val:
                await self.engine.start_monitor(acc)
                await event.answer("✅ تم التشغيل")
            else:
                await self.engine.stop_monitor(acc_id)
                await event.answer("✅ تم الإيقاف")
            
            await self._show_account_details(event, data)
        else:
            await event.answer("❌ فشل التحديث", alert=True)
    
    async def _delete_account(self, event, data):
        """حذف حساب"""
        acc_id = data.decode().split('_')[1]
        acc = await self.db.get_acc(acc_id)
        
        if not acc or acc['owner_id'] != event.sender_id:
            await event.answer("❌ غير مصرح", alert=True)
            return
        
        # تأكيد الحذف
        if acc.get('monitoring'):
            await self.engine.stop_monitor(acc_id)
        
        if await self.db.delete_acc(acc_id):
            await self.encryption.secure_delete(acc['phone'])
            await event.answer("✅ تم الحذف")
            await self._show_accounts(event, event.sender_id)
        else:
            await event.answer("❌ فشل الحذف", alert=True)
    
    async def _start_add_process(self, event):
        """بدء إضافة حساب"""
        uid = event.sender_id
        
        # التحقق من الحد الأقصى
        user_accs = await self.db.get_user_accounts(uid)
        if len(user_accs) >= 5 and uid != DEVELOPER_ID:
            await event.answer("⛅ الحد الأقصى 5 حسابات", alert=True)
            return
        
        self.states[uid] = {'step': 'phone'}
        await event.edit(
            "📱 أرسل رقم الهاتف مع رمز الدولة:\n"
            "مثال: +9647700000000",
            buttons=[Button.inline("❌ إلغاء", b"home")]
        )
    
    async def _process_phone(self, event, state, text):
        """معالجة رقم الهاتف"""
        uid = event.sender_id
        
        if not text.startswith('+') or not text[1:].replace(' ', '').isdigit():
            await event.respond("❌ رقم غير صالح")
            return
        
        phone = text.replace(' ', '')
        
        # التحقق من عدم التكرار
        existing = await self.db.get_acc_by_phone(phone)
        if existing:
            await event.respond("❌ الحساب موجود مسبقاً")
            del self.states[uid]
            return
        
        # إنشاء جلسة مؤقتة
        temp_session = f'sessions/temp_{uid}_{int(time.time())}'
        client = TelegramClient(temp_session, API_ID, API_HASH)
        
        try:
            await client.connect()
            req = await client.send_code_request(phone)
            
            state.update({
                'step': 'code',
                'phone': phone,
                'client': client,
                'temp_session': temp_session,
                'hash': req.phone_code_hash
            })
            
            await event.respond("📩 أرسل كود التحقق:")
            
        except Exception as e:
            await client.disconnect()
            logger.error(f"Code request error: {e}")
            await event.respond(f"❌ خطأ: {e}")
            del self.states[uid]
    
    async def _process_code(self, event, state, text):
        """معالجة الكود"""
        try:
            await state['client'].sign_in(
                state['phone'], 
                text, 
                phone_code_hash=state['hash']
            )
            await self._finalize_account(event, state)
            
        except SessionPasswordNeededError:
            state['step'] = 'pass'
            await event.respond("🔐 الحساب محمي بكلمة سر. أرسلها:")
            
        except Exception as e:
            logger.error(f"Sign in error: {e}")
            await event.respond(f"❌ خطأ: {e}")
            await state['client'].disconnect()
            del self.states[uid]
    
    async def _process_password(self, event, state, text):
        """معالجة كلمة السر"""
        try:
            await state['client'].sign_in(password=text)
            await self._finalize_account(event, state)
            
        except Exception as e:
            logger.error(f"Password error: {e}")
            await event.respond(f"❌ خطأ: {e}")
            await state['client'].disconnect()
            del self.states[uid]
    
    async def _finalize_account(self, event, state):
        """إنهاء إضافة الحساب"""
        uid = event.sender_id
        phone = state['phone']
        client = state['client']
        
        try:
            # الحصول على الجلسات
            auths = await client(functions.account.GetAuthorizationsRequest())
            hashes = [a.hash for a in auths.authorizations]
            
            # نقل الجلسة للاسم النهائي
            final_session = f'sessions/{phone}'
            await client.disconnect()
            
            # إعادة تسمية الملف
            temp_file = f"{state['temp_session']}.session"
            final_file = f"{final_session}.session"
            
            if os.path.exists(temp_file):
                os.rename(temp_file, final_file)
            
            # تشفير الجلسة
            await self.encryption.encrypt_session(phone)
            
            # حفظ في قاعدة البيانات
            acc_id = str(int(time.time() * 1000))
            acc_data = {
                '_id': acc_id,
                'owner_id': uid,
                'phone': phone,
                'monitoring': True,
                'existing_sessions': hashes,
                'created_at': datetime.now(),
                'rules': {'country_whitelist': [], 'device_blacklist': True}
            }
            
            if await self.db.save_acc(acc_data):
                await self.engine.start_monitor(acc_data)
                await event.respond(
                    "✅ **تم إضافة الحساب بنجاح!**\n\n"
                    "🛡 المراقبة نشطة الآن\n"
                    "📱 سيتم إشعارك بأي جلسة جديدة",
                    buttons=[Button.inline("🔙 الرئيسية", b"home")]
                )
            else:
                raise Exception("Failed to save account")
                
        except Exception as e:
            logger.error(f"Finalize error: {e}")
            await event.respond(f"❌ فشل الحفظ: {e}")
        finally:
            if uid in self.states:
                del self.states[uid]
    
    async def _show_dev_panel(self, event):
        """لوحة المطور"""
        stats = await self.web._get_stats()
        
        text = (
            f"⚙️ **لوحة المطور**\n\n"
            f"📊 **الإحصائيات:**\n"
            f"• الحسابات: {stats['total_accounts']}\n"
            f• المراقبة: {stats['monitored']}\n"
            f"• النشطة: {stats['active_monitors']}\n"
            f"• التنبيهات اليوم: {stats['alerts_today']}\n\n"
            f"🔧 **الإعدادات:**\n"
            f"• الفترة: {MONITOR_INTERVAL}ث\n"
            f• المحاولات: {MAX_RETRIES}\n\n"
            f"🌐 **لوحة الويب:**\n"
            f"http://localhost:8000\n"
            f"User: `{WEB_USERNAME}`"
        )
        
        btns = [
            [Button.inline("🔄 إعادة التشغيل", b"restart_monitors")],
            [Button.inline("📊 التفاصيل", b"detailed_stats")],
            [Button.inline("🔒 تسجيل خروج", b"dev_logout")],
            [Button.inline("🔙 رجوع", b"home")]
        ]
        
        await event.edit(text, buttons=btns)
    
    async def _show_info(self, event):
        """عرض المعلومات"""
        await event.edit(
            "ℹ️ **مركز السيطرة المتقدم v2.0**\n\n"
            "🛡 **المميزات:**\n"
            "• تشفير كامل للجلسات\n"
            "• كشف ذكي للتهديدات\n"
            "• نسخ احتياطي تلقائي\n"
            "• لوحة تحكم ويب\n"
            "• إشعارات فورية\n\n"
            "⚡ **التقنيات:**\n"
            "• Python 3.11+\n"
            "• MongoDB + Encryption\n"
            "• FastAPI WebSocket\n"
            "• Asyncio Architecture\n\n"
            "👨‍💻 المطور: @YourUsername",
            buttons=[Button.inline("🔙 رجوع", b"home")]
        )

# --- التشغيل الرئيسي ---

if __name__ == '__main__':
    # إنشاء المجلدات
    for folder in ['sessions', 'backups', 'logs']:
        os.makedirs(folder, exist_ok=True)
    
    # إنشاء ملف .env إذا لم يكن موجوداً
    if not os.path.exists('.env'):
        with open('.env', 'w') as f:
            f.write(f"""# Telegram API
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token

# Database
MONGO_URI=mongodb://localhost:27017

# Security
DEVELOPER_ID=your_telegram_id
ENCRYPTION_KEY={Fernet.generate_key().decode()}

# Web Dashboard
WEB_USERNAME=admin
WEB_PASSWORD={secrets.token_urlsafe(16)}

# Optional
BACKUP_CHANNEL=
MONITOR_INTERVAL=20
MAX_RETRIES=3
""")
        print("✅ تم إنشاء ملف .env - قم بتعديله قبل التشغيل")
        sys.exit(0)
    
    # التشغيل
    bot = UltimateBot()
    
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n👋 تم الإيقاف من قبل المستخدم")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        sys.exit(1)
