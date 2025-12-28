# app.py - نظام مراقبة وإدارة الحجوزات مع تحسينات Cloudflare
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests, json, os, time, sqlite3, asyncio, random, hashlib, csv, io
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional, Tuple
import uvicorn, aiofiles, logging
from enum import Enum
from contextlib import asynccontextmanager
import threading
from pathlib import Path
import asyncio
from dataclasses import dataclass
from pydantic import BaseModel

# ==================== إعدادات آمنة ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('system.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ==================== النماذج ====================
class PlatformStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ERROR = "error"

class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"

# نماذج البيانات لـ Pydantic
class ReservationCreate(BaseModel):
    seller_name: str
    buyer_name: str
    plate_number: str
    priority: int = 1

class ReservationBatch(BaseModel):
    reservations: List[ReservationCreate]

class PlatformCheckRequest(BaseModel):
    url: Optional[str] = None
    check_cookies: bool = True

# ==================== إدارة قاعدة البيانات ====================
class DatabaseManager:
    def __init__(self):
        self.db_path = "monitor_data.db"
        self.init_database()
    
    def init_database(self):
        """تهيئة قاعدة البيانات"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS platform_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            check_type TEXT,
            check_name TEXT,
            status TEXT,
            details TEXT,
            response_code INTEGER
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reservation_id TEXT UNIQUE,
            seller_name TEXT,
            buyer_name TEXT,
            plate_number TEXT,
            priority INTEGER DEFAULT 1,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            submitted_at DATETIME,
            attempts INTEGER DEFAULT 0,
            last_attempt DATETIME,
            result TEXT,
            notes TEXT
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            level TEXT,
            source TEXT,
            message TEXT
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # الإعدادات الافتراضية
        default_settings = [
            ('check_interval', '60'),  # زيادة الفترة لتجنب الحظر
            ('max_attempts', '3'),
            ('concurrent_submissions', '1'),
            ('target_url', 'https://import-dep.mega-sy.com/registration'),
            ('auto_retry', 'true'),
            ('notification_enabled', 'true'),
            ('use_proxy', 'false'),
            ('proxy_list', ''),
            ('cf_bypass_enabled', 'true')
        ]
        
        for key, value in default_settings:
            cursor.execute('''
            INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)
            ''', (key, value))
        
        conn.commit()
        conn.close()
        self.log("Database initialized", "INFO", "database")
    
    def get_setting(self, key: str, default: str = None):
        """الحصول على إعداد"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else default
    
    def update_setting(self, key: str, value: str):
        """تحديث إعداد"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        INSERT OR REPLACE INTO system_settings (key, value, updated_at) 
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, value))
        conn.commit()
        conn.close()
    
    def log(self, message: str, level: str = "INFO", source: str = "system"):
        """تسجيل في قاعدة البيانات"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO system_logs (level, source, message) VALUES (?, ?, ?)",
            (level, source, message)
        )
        conn.commit()
        conn.close()
        # أيضًا تسجيل في كونسول للتصحيح
        getattr(logger, level.lower())(f"[{source}] {message}")
    
    def save_check_result(self, check_type: str, check_name: str, status: str, 
                         details: str = "", response_code: int = None):
        """حفظ نتيجة تحقق"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO platform_checks (check_type, check_name, status, details, response_code)
        VALUES (?, ?, ?, ?, ?)
        ''', (check_type, check_name, status, details, response_code))
        conn.commit()
        conn.close()
    
    def add_reservation(self, seller: str, buyer: str, plate: str, priority: int = 1) -> str:
        """إضافة حجز جديد"""
        reservation_id = f"RES_{int(time.time())}_{random.randint(1000, 9999)}"
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO reservations (reservation_id, seller_name, buyer_name, plate_number, priority)
        VALUES (?, ?, ?, ?, ?)
        ''', (reservation_id, seller, buyer, plate, priority))
        conn.commit()
        conn.close()
        
        self.log(f"Reservation added: {reservation_id}", "INFO", "reservation")
        return reservation_id
    
    def add_batch_reservations(self, reservations: List[Dict]) -> List[str]:
        """إضافة مجموعة حجوزات"""
        ids = []
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for res in reservations:
            reservation_id = f"RES_{int(time.time())}_{random.randint(1000, 9999)}"
            cursor.execute('''
            INSERT INTO reservations (reservation_id, seller_name, buyer_name, plate_number, priority)
            VALUES (?, ?, ?, ?, ?)
            ''', (reservation_id, res['seller_name'], res['buyer_name'], res['plate_number'], res.get('priority', 1)))
            ids.append(reservation_id)
        
        conn.commit()
        conn.close()
        
        self.log(f"Added {len(ids)} reservations", "INFO", "reservation")
        return ids
    
    def update_reservation_status(self, reservation_id: str, status: str, 
                                 result: str = None, notes: str = None, increment_attempts: bool = False):
        """تحديث حالة الحجز"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if increment_attempts:
            cursor.execute('''
            UPDATE reservations 
            SET status = ?, result = ?, notes = COALESCE(?, notes), 
                attempts = attempts + 1, last_attempt = CURRENT_TIMESTAMP
            WHERE reservation_id = ?
            ''', (status, result, notes, reservation_id))
        elif status == "submitted":
            cursor.execute('''
            UPDATE reservations 
            SET status = ?, result = ?, notes = COALESCE(?, notes), submitted_at = CURRENT_TIMESTAMP
            WHERE reservation_id = ?
            ''', (status, result, notes, reservation_id))
        else:
            cursor.execute('''
            UPDATE reservations SET status = ?, result = ?, notes = COALESCE(?, notes) 
            WHERE reservation_id = ?
            ''', (status, result, notes, reservation_id))
        
        conn.commit()
        conn.close()
    
    def get_reservation(self, reservation_id: str):
        """جلب حجوز محدد"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM reservations WHERE reservation_id = ?', (reservation_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                "id": row[0],
                "reservation_id": row[1],
                "seller_name": row[2],
                "buyer_name": row[3],
                "plate_number": row[4],
                "priority": row[5],
                "status": row[6],
                "created_at": row[7],
                "submitted_at": row[8],
                "attempts": row[9],
                "last_attempt": row[10],
                "result": row[11],
                "notes": row[12]
            }
        return None
    
    def get_pending_reservations(self, limit: int = 10):
        """جلب الحجوزات المعلقة"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        SELECT * FROM reservations 
        WHERE status = 'pending' 
        ORDER BY priority DESC, created_at ASC 
        LIMIT ?
        ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "id": r[0],
                "reservation_id": r[1],
                "seller_name": r[2],
                "buyer_name": r[3],
                "plate_number": r[4],
                "priority": r[5],
                "status": r[6],
                "created_at": r[7],
                "submitted_at": r[8],
                "attempts": r[9],
                "last_attempt": r[10],
                "result": r[11],
                "notes": r[12]
            }
            for r in rows
        ]
    
    def get_all_reservations(self, limit: int = 100, status: str = None):
        """جلب جميع الحجوزات"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if status:
            cursor.execute('''
            SELECT * FROM reservations 
            WHERE status = ?
            ORDER BY created_at DESC 
            LIMIT ?
            ''', (status, limit))
        else:
            cursor.execute('''
            SELECT * FROM residences 
            ORDER BY created_at DESC 
            LIMIT ?
            ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "id": r[0],
                "reservation_id": r[1],
                "seller_name": r[2],
                "buyer_name": r[3],
                "plate_number": r[4],
                "priority": r[5],
                "status": r[6],
                "created_at": r[7],
                "submitted_at": r[8],
                "attempts": r[9],
                "last_attempt": r[10],
                "result": r[11],
                "notes": r[12]
            }
            for r in rows
        ]
    
    def get_check_results(self, limit: int = 20, check_type: str = None):
        """جلب نتائج التحقق الأخيرة"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if check_type:
            cursor.execute('''
            SELECT * FROM platform_checks 
            WHERE check_type = ?
            ORDER BY timestamp DESC 
            LIMIT ?
            ''', (check_type, limit))
        else:
            cursor.execute('''
            SELECT * FROM platform_checks 
            ORDER BY timestamp DESC 
            LIMIT ?
            ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "check_type": r[2],
                "check_name": r[3],
                "status": r[4],
                "details": r[5],
                "response_code": r[6]
            }
            for r in rows
        ]
    
    def get_system_logs(self, limit: int = 50, level: str = None):
        """جلب سجلات النظام"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if level:
            cursor.execute('''
            SELECT * FROM system_logs 
            WHERE level = ?
            ORDER BY timestamp DESC 
            LIMIT ?
            ''', (level, limit))
        else:
            cursor.execute('''
            SELECT * FROM system_logs 
            ORDER BY timestamp DESC 
            LIMIT ?
            ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "level": r[2],
                "source": r[3],
                "message": r[4]
            }
            for r in rows
        ]
    
    def get_stats(self):
        """جلب إحصائيات النظام"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        stats = {}
        
        # إحصائيات الحجوزات
        cursor.execute("SELECT status, COUNT(*) FROM reservations GROUP BY status")
        stats["reservations"] = dict(cursor.fetchall())
        
        # إحصائيات التحقق
        cursor.execute("SELECT status, COUNT(*) FROM platform_checks GROUP BY status")
        stats["checks"] = dict(cursor.fetchall())
        
        # إحصائيات السجلات
        cursor.execute("SELECT level, COUNT(*) FROM system_logs GROUP BY level")
        stats["logs"] = dict(cursor.fetchall())
        
        # آخر تحقق ناجح
        cursor.execute('''
        SELECT timestamp FROM platform_checks 
        WHERE status = 'pass' 
        ORDER BY timestamp DESC LIMIT 1
        ''')
        last_success = cursor.fetchone()
        stats["last_success"] = last_success[0] if last_success else None
        
        # عدد المحاولات اليوم
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute('''
        SELECT COUNT(*) FROM reservations 
        WHERE DATE(created_at) = ? AND attempts > 0
        ''', (today,))
        stats["today_attempts"] = cursor.fetchone()[0]
        
        # معدل النجاح
        cursor.execute("SELECT COUNT(*) FROM reservations WHERE status = 'submitted'")
        successful = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM reservations WHERE attempts > 0")
        total_attempted = cursor.fetchone()[0]
        
        if total_attempted > 0:
            stats["success_rate"] = round((successful / total_attempted) * 100, 2)
        else:
            stats["success_rate"] = 0
        
        conn.close()
        return stats
    
    def delete_reservation(self, reservation_id: str) -> bool:
        """حذف حجز"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reservations WHERE reservation_id = ?", (reservation_id,))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        if affected > 0:
            self.log(f"Deleted reservation: {reservation_id}", "INFO", "reservation")
            return True
        return False
    
    def clear_old_data(self, days: int = 7):
        """مسح البيانات القديمة"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        
        # مسح السجلات القديمة
        cursor.execute("DELETE FROM platform_checks WHERE timestamp < ?", (cutoff_date,))
        checks_deleted = cursor.rowcount
        
        cursor.execute("DELETE FROM system_logs WHERE timestamp < ?", (cutoff_date,))
        logs_deleted = cursor.rowcount
        
        conn.commit()
        conn.close()
        
        self.log(f"Cleaned old data: {checks_deleted} checks, {logs_deleted} logs", "INFO", "maintenance")
        return {"checks": checks_deleted, "logs": logs_deleted}

# ==================== نواة النظام مع تحسينات Cloudflare ====================
class SmartPlatformMonitor:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.session = requests.Session()
        self.target_url = db.get_setting('target_url', 'https://import-dep.mega-sy.com/registration')
        self.setup_advanced_session()
        self.is_monitoring = False
        self.monitor_thread = None
        self.last_request_time = 0
        self.current_status = None
        self.status_lock = threading.Lock()
        self.proxies = self.load_proxies()
        self.request_counter = 0
        self.cf_retry_count = 0
    
    def load_proxies(self):
        """تحميل قائمة البروكسيات"""
        proxy_list = self.db.get_setting('proxy_list', '')
        if proxy_list:
            proxies = [p.strip() for p in proxy_list.split(',') if p.strip()]
            self.db.log(f"Loaded {len(proxies)} proxies", "INFO", "proxy")
            return proxies
        return []
    
    def get_proxy(self):
        """الحصول على بروكسي عشوائي"""
        if self.proxies and self.db.get_setting('use_proxy', 'false').lower() == 'true':
            return random.choice(self.proxies)
        return None
    
    def setup_advanced_session(self):
        """إعداد جلسة متقدمة مع headers مضادة لـ Cloudflare"""
        self.session.headers.update({
            "User-Agent": self.get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8,fr;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "DNT": "1",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        })
    
    def get_random_user_agent(self):
        """الحصول على User-Agent عشوائي حديث"""
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0"
        ]
        return random.choice(user_agents)
    
    def respect_rate_limit(self):
        """الاحترام rate limit مع تباين"""
        current_time = time.time()
        
        # زيادة الفترة بين الطلبات لتجنب Cloudflare
        min_wait = 5 if self.cf_retry_count > 0 else 3
        max_wait = 10 if self.cf_retry_count > 0 else 7
        
        if current_time - self.last_request_time < min_wait:
            wait_time = random.uniform(min_wait, max_wait)
            time.sleep(wait_time)
        
        self.last_request_time = current_time
    
    def load_cookies(self, cookies_file: str = "cookies.json"):
        """تحميل الكوكيز بذكاء"""
        try:
            if os.path.exists(cookies_file):
                with open(cookies_file, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                
                self.session.cookies.clear()
                for cookie in cookies:
                    if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value"):
                        self.session.cookies.set(
                            name=cookie["name"],
                            value=cookie["value"],
                            domain=cookie.get("domain", ""),
                            path=cookie.get("path", "/")
                        )
                
                self.db.save_check_result(
                    "cookies",
                    "تحميل الكوكيز",
                    CheckStatus.PASS,
                    f"تم تحميل {len(cookies)} كوكيز",
                    200
                )
                self.db.log(f"تم تحميل {len(cookies)} كوكيز", "INFO", "cookies")
                return True
            else:
                self.db.save_check_result(
                    "cookies",
                    "تحميل الكوكيز",
                    CheckStatus.WARNING,
                    "ملف الكوكيز غير موجود",
                    404
                )
                return False
                
        except Exception as e:
            self.db.save_check_result(
                "cookies",
                "تحميل الكوكيز",
                CheckStatus.FAIL,
                str(e),
                500
            )
            return False
    
    def prepare_request_headers(self):
        """تحضير headers للطلب مع تباين"""
        headers = {
            "User-Agent": self.get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "DNT": "1",
        }
        
        # إضافة مرجع عشوائي
        referers = [
            "https://www.google.com/",
            "https://www.bing.com/",
            "https://www.yahoo.com/",
            "https://duckduckgo.com/",
            "https://www.facebook.com/",
            f"https://{self.target_url.split('/')[2]}/",
            "https://import-dep.mega-sy.com/"
        ]
        
        headers["Referer"] = random.choice(referers)
        
        # إضافة headers إضافية بشكل عشوائي
        if random.random() > 0.5:
            headers["Sec-Ch-Ua"] = '"Google Chrome";v="121", "Not(A:Brand";v="8", "Chromium";v="121"'
            headers["Sec-Ch-Ua-Mobile"] = "?0"
            headers["Sec-Ch-Ua-Platform"] = '"Windows"'
        
        return headers
    
    def perform_comprehensive_check(self, url: str = None):
        """إجراء تحقق شامل مع تحسينات لـ Cloudflare"""
        self.respect_rate_limit()
        
        target_url = url or self.target_url
        
        try:
            # تغيير User-Agent بشكل دوري
            if random.random() < 0.5:
                self.session.headers["User-Agent"] = self.get_random_user_agent()
            
            # تحضير headers
            headers = self.prepare_request_headers()
            
            # إعداد البروكسي إذا مفعل
            proxies = None
            proxy = self.get_proxy()
            if proxy:
                proxies = {
                    'http': proxy,
                    'https': proxy
                }
                self.db.log(f"Using proxy: {proxy}", "INFO", "proxy")
            
            # محاولة مع timeout متغير
            timeout = random.randint(15, 25)
            
            # إضافة تأخير عشوائي قبل الطلب
            pre_delay = random.uniform(1, 3)
            time.sleep(pre_delay)
            
            response = self.session.get(
                target_url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
                proxies=proxies
            )
            
            # تسجيل نتيجة الاتصال
            if response.status_code == 200:
                self.db.save_check_result(
                    "connection",
                    "اتصال بالمنصة",
                    CheckStatus.PASS,
                    f"كود الحالة: {response.status_code}",
                    response.status_code
                )
                self.cf_retry_count = 0  # إعادة تعيين عداد إعادة المحاولة
                
                # تحليل الصفحة
                analysis = self.analyze_platform_page(response.text)
                if analysis:
                    with self.status_lock:
                        self.current_status = analysis
                
                return analysis
                
            elif response.status_code == 403 or response.status_code == 429:
                # زيادة عداد إعادة المحاولة
                self.cf_retry_count += 1
                
                # التحقق من محتوى الاستجابة
                if "cloudflare" in response.text.lower() or "cf-" in response.text.lower():
                    error_msg = f"Cloudflare WAF Blocked ({response.status_code}) - Retry #{self.cf_retry_count}"
                else:
                    error_msg = f"ممنوع الوصول ({response.status_code}) - Retry #{self.cf_retry_count}"
                
                self.db.save_check_result(
                    "connection",
                    "اتصال بالمنصة",
                    CheckStatus.FAIL,
                    error_msg,
                    response.status_code
                )
                
                # إذا كان هناك كتلة متكررة، حاول بطرق مختلفة
                if self.cf_retry_count >= 3:
                    self.db.log("Multiple Cloudflare blocks detected, trying alternative methods", "WARNING", "cloudflare")
                    return self.try_alternative_methods(target_url)
                
                self.db.log(f"Cloudflare block #{self.cf_retry_count}", "WARNING", "cloudflare")
                return None
                
            else:
                self.db.save_check_result(
                    "connection",
                    "اتصال بالمنصة",
                    CheckStatus.FAIL,
                    f"كود الحالة: {response.status_code}",
                    response.status_code
                )
                return None
                
        except requests.Timeout:
            self.db.save_check_result(
                "connection",
                "اتصال بالمنصة",
                CheckStatus.FAIL,
                f"انتهت مهلة الاتصال (Retry #{self.cf_retry_count})",
                408
            )
            return None
            
        except requests.RequestException as e:
            self.db.save_check_result(
                "connection",
                "اتصال بالمنصة",
                CheckStatus.FAIL,
                f"خطأ في الطلب: {str(e)[:100]}",
                500
            )
            return None
            
        except Exception as e:
            self.db.save_check_result(
                "connection",
                "اتصال بالمنصة",
                CheckStatus.FAIL,
                str(e)[:200],
                500
            )
            return None
    
    def try_alternative_methods(self, url: str):
        """محاولة طرق بديلة لتجاوز Cloudflare"""
        methods = [
            self.try_with_playwright,
            self.try_with_different_session,
            self.try_with_simple_requests
        ]
        
        for method in methods:
            try:
                result = method(url)
                if result:
                    self.db.log(f"Alternative method {method.__name__} succeeded", "INFO", "cloudflare")
                    return result
            except Exception as e:
                self.db.log(f"Alternative method {method.__name__} failed: {str(e)}", "WARNING", "cloudflare")
                continue
        
        return None
    
    def try_with_different_session(self, url: str):
        """محاولة بجلسة جديدة تماماً"""
        try:
            # إنشاء جلسة جديدة
            new_session = requests.Session()
            new_session.headers.update(self.prepare_request_headers())
            
            # تغيير User-Agent بشكل كامل
            new_session.headers["User-Agent"] = self.get_random_user_agent()
            
            # إضافة تأخير طويل
            time.sleep(random.uniform(5, 10))
            
            response = new_session.get(url, timeout=30, allow_redirects=True)
            
            if response.status_code == 200:
                self.db.save_check_result(
                    "connection",
                    "جلسة بديلة",
                    CheckStatus.PASS,
                    "تم الاتصال باستخدام جلسة جديدة",
                    response.status_code
                )
                return self.analyze_platform_page(response.text)
            
        except Exception as e:
            self.db.log(f"New session failed: {str(e)}", "WARNING", "cloudflare")
        
        return None
    
    def try_with_simple_requests(self, url: str):
        """محاولة مع طلب بسيط بدون جلسة"""
        try:
            # استخدام requests مباشرة بدون جلسة
            headers = {
                "User-Agent": self.get_random_user_agent(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ar",
                "Connection": "close",
            }
            
            time.sleep(random.uniform(8, 15))
            
            response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
            
            if response.status_code == 200:
                self.db.save_check_result(
                    "connection",
                    "طلب بسيط",
                    CheckStatus.PASS,
                    "تم الاتصال باستخدام طلب بسيط",
                    response.status_code
                )
                return self.analyze_platform_page(response.text)
            
        except Exception as e:
            self.db.log(f"Simple request failed: {str(e)}", "WARNING", "cloudflare")
        
        return None
    
    def try_with_playwright(self, url: str):
        """محاولة باستخدام Playwright (متصفح حقيقي)"""
        # هذه الطريقة تحتاج تثبيت playwright
        # سنضعها كطريقة احتياطية يمكن تفعيلها لاحقاً
        self.db.log("Playwright method not implemented, install playwright first", "INFO", "cloudflare")
        return None
    
    def analyze_platform_page(self, html_content: str):
        """تحليل صفحة المنصة"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            analysis = {
                "is_open": False,
                "form_enabled": False,
                "remaining_user": 0,
                "remaining_system": 0,
                "next_opening": None,
                "has_captcha": False,
                "timestamp": datetime.now().isoformat(),
                "cookies_valid": len(self.session.cookies) > 0,
                "page_title": soup.title.string if soup.title else "Unknown",
                "cf_protected": "cloudflare" in html_content.lower() or "cf-" in html_content.lower()
            }
            
            # التحقق من حالة النظام
            open_dot = soup.find("span", {"id": "openDot"})
            if open_dot:
                analysis["is_open"] = "dot-open" in open_dot.get("class", [])
                self.db.save_check_result(
                    "platform",
                    "حالة النظام",
                    CheckStatus.PASS if analysis["is_open"] else CheckStatus.WARNING,
                    "مفتوح" if analysis["is_open"] else "مغلق"
                )
            else:
                # محاولة البحث بطرق أخرى
                open_text = soup.find(string=lambda text: "مفتوح" in str(text).lower() if text else False)
                if open_text:
                    analysis["is_open"] = True
            
            # التحقق من النموذج
            fieldset = soup.find("fieldset", {"id": "formFields"})
            if fieldset:
                analysis["form_enabled"] = not fieldset.has_attr("disabled")
                self.db.save_check_result(
                    "form",
                    "تفعيل النموذج",
                    CheckStatus.PASS if analysis["form_enabled"] else CheckStatus.WARNING,
                    "مفعل" if analysis["form_enabled"] else "معطل"
                )
            
            # قراءة الأعداد المتبقية
            user_elem = soup.find("div", {"id": "remainingUser"})
            if user_elem:
                try:
                    analysis["remaining_user"] = int(''.join(filter(str.isdigit, user_elem.text.strip())))
                except:
                    pass
            
            system_elem = soup.find("div", {"id": "remainingSystem"})
            if system_elem:
                try:
                    analysis["remaining_system"] = int(''.join(filter(str.isdigit, system_elem.text.strip())))
                except:
                    pass
            
            # البحث عن الأعداد بطرق أخرى
            if analysis["remaining_system"] == 0:
                numbers = soup.find_all(string=lambda text: any(char.isdigit() for char in str(text)) if text else False)
                for num in numbers[:10]:
                    try:
                        if "متبقي" in str(num) or "عدد" in str(num):
                            digits = ''.join(filter(str.isdigit, str(num)))
                            if digits:
                                analysis["remaining_system"] = int(digits)
                                break
                    except:
                        continue
            
            # التحقق من السعة
            if analysis["remaining_system"] > 0:
                self.db.save_check_result(
                    "capacity",
                    "سعة النظام",
                    CheckStatus.PASS,
                    f"المستخدم: {analysis['remaining_user']} | النظام: {analysis['remaining_system']}"
                )
            else:
                self.db.save_check_result(
                    "capacity",
                    "سعة النظام",
                    CheckStatus.WARNING,
                    "السعة منتهية أو غير محددة"
                )
            
            # التحقق من CAPTCHA
            captcha_indicators = ["cf-turnstile", "recaptcha", "captcha", "تأكيد", "تحقق", "security check"]
            for indicator in captcha_indicators:
                if indicator in html_content.lower():
                    analysis["has_captcha"] = True
                    break
            
            if analysis["has_captcha"]:
                self.db.save_check_result(
                    "security",
                    "CAPTCHA Detection",
                    CheckStatus.WARNING,
                    "يحتاج حل تحقق أمني"
                )
            
            # البحث عن التوكنات الأمنية
            form = soup.find("form", {"id": "orderForm"})
            if not form:
                form = soup.find("form")
            
            if form:
                tokens_found = 0
                input_fields = form.find_all("input")
                for inp in input_fields:
                    name = inp.get("name", "").lower()
                    if any(token in name for token in ["_token", "hmac", "started_at", "csrf", "nonce"]):
                        tokens_found += 1
                
                self.db.save_check_result(
                    "security",
                    "التوكنات الأمنية",
                    CheckStatus.PASS if tokens_found >= 1 else CheckStatus.WARNING,
                    f"تم العثور على {tokens_found} توكن"
                )
            
            # البحث عن الموعد القادم
            next_msg = soup.find("span", {"id": "nextMsg"})
            if not next_msg:
                # البحث عن نص يشير إلى موعد قادم
                next_texts = soup.find_all(string=lambda text: "القادم" in str(text) or "الموعد" in str(text) if text else False)
                if next_texts:
                    analysis["next_opening"] = next_texts[0].strip()
            
            if next_msg:
                analysis["next_opening"] = next_msg.text.strip()
            
            # إضافة معلومات إضافية
            analysis["page_size"] = len(html_content)
            analysis["forms_found"] = len(soup.find_all("form"))
            
            return analysis
            
        except Exception as e:
            self.db.save_check_result(
                "analysis",
                "تحليل الصفحة",
                CheckStatus.FAIL,
                str(e)[:200]
            )
            return None
    
    def submit_reservation(self, seller: str, buyer: str, plate: str, reservation_id: str = None):
        """إرسال حجز (محاكاة للآن)"""
        try:
            # أولاً: التحقق من حالة النظام
            analysis = self.perform_comprehensive_check()
            
            if not analysis:
                result = {
                    "success": False,
                    "error": "فشل الاتصال بالمنصة",
                    "can_retry": True
                }
                if reservation_id:
                    self.db.update_reservation_status(
                        reservation_id, "failed", str(result), "فشل الاتصال", True
                    )
                return result
            
            if analysis.get("cf_protected", False) and not analysis.get("is_open", False):
                result = {
                    "success": False,
                    "error": "Cloudflare يحمي المنصة ويحتاج إلى تحايل إضافي",
                    "can_retry": True,
                    "cf_protected": True
                }
                if reservation_id:
                    self.db.update_reservation_status(
                        reservation_id, "failed", str(result), "Cloudflare يحمي المنصة", True
                    )
                return result
            
            if not analysis["is_open"]:
                result = {
                    "success": False,
                    "error": "النظام مغلق حالياً",
                    "can_retry": True,
                    "next_opening": analysis.get("next_opening")
                }
                if reservation_id:
                    self.db.update_reservation_status(
                        reservation_id, "failed", str(result), "النظام مغلق", True
                    )
                return result
            
            if not analysis["form_enabled"]:
                result = {
                    "success": False,
                    "error": "النموذج غير مفعل",
                    "can_retry": True
                }
                if reservation_id:
                    self.db.update_reservation_status(
                        reservation_id, "failed", str(result), "النموذج غير مفعل", True
                    )
                return result
            
            if analysis["remaining_system"] <= 0:
                result = {
                    "success": False,
                    "error": "تم استنفاذ السعة اليومية",
                    "can_retry": False
                }
                if reservation_id:
                    self.db.update_reservation_status(
                        reservation_id, "failed", str(result), "السعة منتهية", False
                    )
                return result
            
            # محاكاة الإرسال الناجح (في الإصدار الحقيقي هنا يتم الإرسال الفعلي)
            time.sleep(random.uniform(2, 4))  # محاكاة وقت الإرسال
            
            # نسبة نجاح 80% للمحاكاة
            if random.random() < 0.8:
                result = {
                    "success": True,
                    "message": f"تم إرسال الحجز بنجاح - {plate}",
                    "reference": f"REF_{int(time.time())}_{random.randint(1000, 9999)}",
                    "timestamp": datetime.now().isoformat(),
                    "capacity_remaining": analysis["remaining_system"] - 1,
                    "cf_bypassed": analysis.get("cf_protected", False)
                }
                if reservation_id:
                    self.db.update_reservation_status(
                        reservation_id, "submitted", str(result), "تم الإرسال بنجاح", False
                    )
            else:
                result = {
                    "success": False,
                    "error": "فشل في إرسال الحجز (محاكاة)",
                    "can_retry": True
                }
                if reservation_id:
                    self.db.update_reservation_status(
                        reservation_id, "failed", str(result), "فشل الإرسال (محاكاة)", True
                    )
            
            return result
            
        except Exception as e:
            error_result = {
                "success": False,
                "error": f"استثناء: {str(e)}",
                "can_retry": True
            }
            if reservation_id:
                self.db.update_reservation_status(
                    reservation_id, "failed", str(error_result), str(e), True
                )
            return error_result
    
    def start_monitoring(self, interval: int = 60):
        """بدء المراقبة التلقائية"""
        def monitor_loop():
            while self.is_monitoring:
                try:
                    self.perform_comprehensive_check()
                    # زيادة الفترة إذا كان هناك مشاكل مع Cloudflare
                    current_interval = interval
                    if self.cf_retry_count > 2:
                        current_interval = min(interval * 2, 300)  # مضاعفة الفترة بحد أقصى 5 دقائق
                    
                    time.sleep(current_interval)
                except Exception as e:
                    self.db.log(f"Monitoring error: {str(e)}", "ERROR", "monitor")
                    time.sleep(interval)
        
        if not self.is_monitoring:
            self.is_monitoring = True
            self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
            self.monitor_thread.start()
            self.db.log(f"Started monitoring with {interval}s interval", "INFO", "monitor")
    
    def stop_monitoring(self):
        """إيقاف المراقبة التلقائية"""
        self.is_monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        self.db.log("Stopped monitoring", "INFO", "monitor")
    
    def get_current_status(self):
        """الحصول على الحالة الحالية"""
        with self.status_lock:
            return self.current_status

# ==================== إنشاء القوالب ====================
def create_simple_templates():
    """إنشاء قوالب HTML بسيطة"""
    templates_dir = Path("templates")
    templates_dir.mkdir(exist_ok=True)
    
    # لوحة التحكم
    dashboard_html = """
    <!DOCTYPE html>
    <html dir="rtl" lang="ar">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>نظام مراقبة الحجوزات - لوحة التحكم</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.8.1/font/bootstrap-icons.css">
        <style>
            body { background-color: #f8f9fa; }
            .card { margin-bottom: 1rem; border-radius: 10px; }
            .status-open { color: #198754; }
            .status-closed { color: #dc3545; }
            .status-warning { color: #ffc107; }
            .stats-card { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
            .cf-badge { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        </style>
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark">
            <div class="container-fluid">
                <a class="navbar-brand" href="/">
                    <i class="bi bi-speedometer2"></i> نظام مراقبة الحجوزات
                </a>
                <div class="d-flex">
                    <a href="/dashboard" class="btn btn-outline-light me-2">
                        <i class="bi bi-speedometer2"></i> لوحة التحكم
                    </a>
                    <a href="/reservations" class="btn btn-outline-light me-2">
                        <i class="bi bi-calendar-check"></i> الحجوزات
                    </a>
                    <a href="/monitor" class="btn btn-outline-light me-2">
                        <i class="bi bi-graph-up"></i> المراقبة
                    </a>
                    <a href="/cf-bypass" class="btn btn-outline-warning me-2">
                        <i class="bi bi-shield-check"></i> تجاوز Cloudflare
                    </a>
                    <a href="/settings" class="btn btn-outline-light me-2">
                        <i class="bi bi-gear"></i> الإعدادات
                    </a>
                </div>
            </div>
        </nav>
        
        <div class="container mt-4">
            <div class="row">
                <div class="col-12">
                    <div class="card stats-card">
                        <div class="card-body">
                            <h5 class="card-title">
                                <i class="bi bi-bar-chart"></i> نظرة عامة
                            </h5>
                            <div class="row text-center">
                                <div class="col-md-3">
                                    <h3>{{ stats.reservations.get('pending', 0) }}</h3>
                                    <p>حجوزات معلقة</p>
                                </div>
                                <div class="col-md-3">
                                    <h3>{{ stats.reservations.get('submitted', 0) }}</h3>
                                    <p>حجوزات مكتملة</p>
                                </div>
                                <div class="col-md-3">
                                    <h3>{{ stats.today_attempts }}</h3>
                                    <p>محاولات اليوم</p>
                                </div>
                                <div class="col-md-3">
                                    <h3>{{ stats.success_rate }}%</h3>
                                    <p>معدل النجاح</p>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="row mt-3">
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-header bg-primary text-white">
                            <i class="bi bi-info-circle"></i> حالة النظام الحالية
                        </div>
                        <div class="card-body">
                            {% if current_status %}
                                <p>
                                    <strong>الحالة:</strong>
                                    {% if current_status.is_open %}
                                        <span class="status-open">مفتوح <i class="bi bi-check-circle"></i></span>
                                    {% else %}
                                        <span class="status-closed">مغلق <i class="bi bi-x-circle"></i></span>
                                    {% endif %}
                                </p>
                                <p><strong>سعة النظام:</strong> {{ current_status.remaining_system }}</p>
                                <p><strong>النموذج:</strong> 
                                    {% if current_status.form_enabled %}
                                        <span class="status-open">مفعل</span>
                                    {% else %}
                                        <span class="status-closed">معطل</span>
                                    {% endif %}
                                </p>
                                {% if current_status.cf_protected %}
                                <p><span class="badge cf-badge">محمي بـ Cloudflare</span></p>
                                {% endif %}
                                <p><strong>آخر تحديث:</strong> {{ current_status.timestamp }}</p>
                            {% else %}
                                <p class="text-muted">جارٍ تحميل حالة النظام...</p>
                            {% endif %}
                            <div class="btn-group">
                                <button class="btn btn-sm btn-outline-primary" onclick="refreshStatus()">
                                    <i class="bi bi-arrow-clockwise"></i> تحديث
                                </button>
                                <button class="btn btn-sm btn-outline-warning" onclick="cfBypassCheck()">
                                    <i class="bi bi-shield-check"></i> تحقق متقدم
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-header bg-info text-white">
                            <i class="bi bi-clock-history"></i> آخر التحققات
                        </div>
                        <div class="card-body">
                            {% if recent_checks %}
                                <div class="table-responsive">
                                    <table class="table table-sm">
                                        <thead>
                                            <tr>
                                                <th>النوع</th>
                                                <th>الحالة</th>
                                                <th>الوقت</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for check in recent_checks %}
                                            <tr>
                                                <td>{{ check.check_name }}</td>
                                                <td>
                                                    {% if check.status == 'pass' %}
                                                        <span class="badge bg-success">ناجح</span>
                                                    {% elif check.status == 'fail' %}
                                                        <span class="badge bg-danger">فشل</span>
                                                    {% else %}
                                                        <span class="badge bg-warning">تحذير</span>
                                                    {% endif %}
                                                </td>
                                                <td>{{ check.timestamp }}</td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                            {% else %}
                                <p class="text-muted">لا توجد تحققات حديثة</p>
                            {% endif %}
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="row mt-3">
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-header bg-warning text-dark">
                            <i class="bi bi-exclamation-triangle"></i> الحجوزات المعلقة
                        </div>
                        <div class="card-body">
                            {% if pending_reservations %}
                                <div class="table-responsive">
                                    <table class="table table-sm">
                                        <thead>
                                            <tr>
                                                <th>البائع</th>
                                                <th>المشتري</th>
                                                <th>اللوحة</th>
                                                <th>الإجراء</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for res in pending_reservations %}
                                            <tr>
                                                <td>{{ res.seller_name }}</td>
                                                <td>{{ res.buyer_name }}</td>
                                                <td>{{ res.plate_number }}</td>
                                                <td>
                                                    <button class="btn btn-sm btn-success" onclick="submitReservation('{{ res.reservation_id }}')">
                                                        <i class="bi bi-send"></i> إرسال
                                                    </button>
                                                </td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                                <button class="btn btn-warning w-100" onclick="submitAllPending()">
                                    <i class="bi bi-send-check"></i> إرسال جميع المعلقة
                                </button>
                            {% else %}
                                <p class="text-muted">لا توجد حجوزات معلقة</p>
                            {% endif %}
                        </div>
                    </div>
                </div>
                
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-header bg-secondary text-white">
                            <i class="bi bi-journal-text"></i> السجلات الحديثة
                        </div>
                        <div class="card-body">
                            {% if recent_logs %}
                                <div style="max-height: 200px; overflow-y: auto;">
                                    {% for log in recent_logs %}
                                        <div class="mb-1">
                                            <small class="text-muted">{{ log.timestamp }}</small>
                                            <span class="badge bg-{{ 'success' if log.level=='INFO' else 'danger' if log.level=='ERROR' else 'warning' }}">
                                                {{ log.level }}
                                            </span>
                                            <small>{{ log.message }}</small>
                                        </div>
                                    {% endfor %}
                                </div>
                            {% else %}
                                <p class="text-muted">لا توجد سجلات حديثة</p>
                            {% endif %}
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            async function refreshStatus() {
                const response = await fetch('/api/check', { method: 'POST' });
                location.reload();
            }
            
            async function cfBypassCheck() {
                const response = await fetch('/api/cf-bypass', { method: 'POST' });
                const result = await response.json();
                alert(result.message);
                if (result.success) {
                    setTimeout(() => location.reload(), 2000);
                }
            }
            
            async function submitReservation(reservationId) {
                const response = await fetch(`/api/reservations/${reservationId}/submit`, { method: 'POST' });
                const result = await response.json();
                alert(result.message);
                if (result.success) {
                    location.reload();
                }
            }
            
            async function submitAllPending() {
                if (confirm('هل تريد إرسال جميع الحجوزات المعلقة؟')) {
                    const response = await fetch('/api/reservations/submit-pending', { method: 'POST' });
                    const result = await response.json();
                    alert(result.message);
                    if (result.success) {
                        setTimeout(() => location.reload(), 2000);
                    }
                }
            }
        </script>
    </body>
    </html>
    """
    
    (templates_dir / "dashboard.html").write_text(dashboard_html, encoding="utf-8")
    
    # صفحة تجاوز Cloudflare
    cf_bypass_html = """
    <!DOCTYPE html>
    <html dir="rtl" lang="ar">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>تجاوز Cloudflare</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.8.1/font/bootstrap-icons.css">
        <style>
            .card { margin-bottom: 1rem; }
            .method-card { border-left: 4px solid #0d6efd; }
            .proxy-card { border-left: 4px solid #198754; }
            .warning-card { border-left: 4px solid #ffc107; }
        </style>
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark">
            <div class="container-fluid">
                <a class="navbar-brand" href="/">
                    <i class="bi bi-speedometer2"></i> نظام مراقبة الحجوزات
                </a>
                <div class="d-flex">
                    <a href="/dashboard" class="btn btn-outline-light me-2">لوحة التحكم</a>
                    <a href="/reservations" class="btn btn-outline-light me-2">الحجوزات</a>
                    <a href="/monitor" class="btn btn-outline-light me-2">المراقبة</a>
                    <a href="/cf-bypass" class="btn btn-warning me-2">تجاوز Cloudflare</a>
                    <a href="/settings" class="btn btn-outline-light">الإعدادات</a>
                </div>
            </div>
        </nav>
        
        <div class="container mt-4">
            <div class="row mb-4">
                <div class="col-12">
                    <div class="card warning-card">
                        <div class="card-body">
                            <h4 class="card-title">
                                <i class="bi bi-shield-exclamation"></i> تجاوز حماية Cloudflare
                            </h4>
                            <p class="card-text">
                                Cloudflare يحظر الطلبات الآلية. استخدم هذه الأدوات لتحسين فرص الوصول للمنصة.
                            </p>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="row">
                <div class="col-md-6">
                    <div class="card method-card">
                        <div class="card-header bg-primary text-white">
                            <i class="bi bi-gear"></i> طرق تجاوز Cloudflare
                        </div>
                        <div class="card-body">
                            <div class="list-group">
                                <div class="list-group-item">
                                    <h6><i class="bi bi-check-circle text-success"></i> تغيير User-Agent</h6>
                                    <small>استخدام متصفحات وعمليات مختلفة</small>
                                </div>
                                <div class="list-group-item">
                                    <h6><i class="bi bi-check-circle text-success"></i> إضافة Referers متنوعة</h6>
                                    <small>محاكاة حركة مرور طبيعية</small>
                                </div>
                                <div class="list-group-item">
                                    <h6><i class="bi bi-sliders"></i> استخدام البروكسيات</h6>
                                    <small>تغيير عنوان IP للطلبات</small>
                                </div>
                                <div class="list-group-item">
                                    <h6><i class="bi bi-clock"></i> التوقيت العشوائي</h6>
                                    <small>تجنب الأنماط القابلة للاكتشاف</small>
                                </div>
                            </div>
                            
                            <div class="mt-3">
                                <button class="btn btn-primary w-100" onclick="testAllMethods()">
                                    <i class="bi bi-play-circle"></i> اختبار جميع الطرق
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="col-md-6">
                    <div class="card proxy-card">
                        <div class="card-header bg-success text-white">
                            <i class="bi bi-shuffle"></i> إدارة البروكسيات
                        </div>
                        <div class="card-body">
                            <div class="mb-3">
                                <label class="form-label">قائمة البروكسيات (واحد لكل سطر)</label>
                                <textarea class="form-control" id="proxyList" rows="5" placeholder="http://proxy1:port
http://proxy2:port
https://proxy3:port"></textarea>
                                <div class="form-text">أدخل عناوين البروكسيات لتغيير عنوان IP</div>
                            </div>
                            
                            <div class="form-check mb-3">
                                <input class="form-check-input" type="checkbox" id="useProxy">
                                <label class="form-check-label" for="useProxy">
                                    تمكين استخدام البروكسيات
                                </label>
                            </div>
                            
                            <button class="btn btn-success" onclick="saveProxies()">
                                <i class="bi bi-save"></i> حفظ الإعدادات
                            </button>
                            <button class="btn btn-outline-secondary" onclick="loadProxySettings()">
                                <i class="bi bi-arrow-clockwise"></i> تحميل
                            </button>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="row mt-4">
                <div class="col-12">
                    <div class="card">
                        <div class="card-header bg-info text-white">
                            <i class="bi bi-speedometer"></i> اختبار الاتصال
                        </div>
                        <div class="card-body">
                            <div class="row">
                                <div class="col-md-4">
                                    <button class="btn btn-outline-primary w-100 mb-2" onclick="testMethod('simple')">
                                        <i class="bi bi-lightning"></i> اختبار بسيط
                                    </button>
                                </div>
                                <div class="col-md-4">
                                    <button class="btn btn-outline-success w-100 mb-2" onclick="testMethod('proxy')">
                                        <i class="bi bi-shuffle"></i> اختبار بالبروكسي
                                    </button>
                                </div>
                                <div class="col-md-4">
                                    <button class="btn btn-outline-warning w-100 mb-2" onclick="testMethod('advanced')">
                                        <i class="bi bi-magic"></i> اختبار متقدم
                                    </button>
                                </div>
                            </div>
                            
                            <div id="testResults" class="mt-3"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            async function loadProxySettings() {
                const response = await fetch('/api/settings');
                const result = await response.json();
                
                if (result.success) {
                    document.getElementById('proxyList').value = result.data.proxy_list || '';
                    document.getElementById('useProxy').checked = result.data.use_proxy === 'true';
                }
            }
            
            async function saveProxies() {
                const proxyList = document.getElementById('proxyList').value;
                const useProxy = document.getElementById('useProxy').checked;
                
                const settings = {
                    proxy_list: proxyList,
                    use_proxy: useProxy.toString()
                };
                
                const response = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(settings)
                });
                
                const result = await response.json();
                if (result.success) {
                    alert('تم حفظ إعدادات البروكسي بنجاح');
                } else {
                    alert('فشل حفظ الإعدادات');
                }
            }
            
            async function testMethod(method) {
                const resultsDiv = document.getElementById('testResults');
                resultsDiv.innerHTML = '<div class="alert alert-info">جار الاختبار...</div>';
                
                const response = await fetch(`/api/test-connection?method=${method}`, {
                    method: 'POST'
                });
                
                const result = await response.json();
                
                if (result.success) {
                    resultsDiv.innerHTML = `
                        <div class="alert alert-success">
                            <h5>✓ الاختبار ناجح</h5>
                            <p>${result.message}</p>
                            <p><strong>الوقت:</strong> ${result.timestamp}</p>
                            <p><strong>طريقة:</strong> ${result.method}</p>
                            <p><strong>حالة النظام:</strong> ${result.data.is_open ? 'مفتوح' : 'مغلق'}</p>
                            <p><strong>سعة متبقية:</strong> ${result.data.remaining_system}</p>
                        </div>
                    `;
                } else {
                    resultsDiv.innerHTML = `
                        <div class="alert alert-danger">
                            <h5>✗ فشل الاختبار</h5>
                            <p>${result.message}</p>
                            <p><strong>الخطأ:</strong> ${result.error || 'غير معروف'}</p>
                        </div>
                    `;
                }
            }
            
            async function testAllMethods() {
                const methods = ['simple', 'proxy', 'advanced'];
                const resultsDiv = document.getElementById('testResults');
                resultsDiv.innerHTML = '<div class="alert alert-info">جار اختبار جميع الطرق...</div>';
                
                let success = false;
                for (const method of methods) {
                    const response = await fetch(`/api/test-connection?method=${method}`, {
                        method: 'POST'
                    });
                    
                    const result = await response.json();
                    if (result.success) {
                        success = true;
                        resultsDiv.innerHTML = `
                            <div class="alert alert-success">
                                <h5>✓ نجحت طريقة ${result.method}</h5>
                                <p>${result.message}</p>
                                <p><strong>الحالة:</strong> ${result.data.is_open ? 'مفتوح' : 'مغلق'}</p>
                                <p><strong>السعة:</strong> ${result.data.remaining_system}</p>
                                <button class="btn btn-sm btn-success mt-2" onclick="useMethod('${result.method}')">
                                    استخدام هذه الطريقة
                                </button>
                            </div>
                        `;
                        break;
                    }
                }
                
                if (!success) {
                    resultsDiv.innerHTML = `
                        <div class="alert alert-danger">
                            <h5>✗ فشلت جميع الطرق</h5>
                            <p>يجب تحديث الكوكيز أو إعدادات البروكسي</p>
                            <button class="btn btn-sm btn-warning mt-2" onclick="uploadNewCookies()">
                                <i class="bi bi-upload"></i> رفع كوكيز جديدة
                            </button>
                        </div>
                    `;
                }
            }
            
            async function useMethod(method) {
                const response = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ default_method: method })
                });
                
                const result = await response.json();
                if (result.success) {
                    alert(`تم تعيين ${method} كطريقة افتراضية`);
                }
            }
            
            async function uploadNewCookies() {
                const input = document.createElement('input');
                input.type = 'file';
                input.accept = '.json';
                
                input.onchange = async (e) => {
                    const file = e.target.files[0];
                    if (!file) return;
                    
                    const formData = new FormData();
                    formData.append('file', file);
                    
                    const response = await fetch('/api/cookies/upload', {
                        method: 'POST',
                        body: formData
                    });
                    
                    const result = await response.json();
                    alert(result.message);
                    if (result.success) {
                        location.reload();
                    }
                };
                
                input.click();
            }
            
            // تحميل الإعدادات عند فتح الصفحة
            window.addEventListener('load', loadProxySettings);
        </script>
    </body>
    </html>
    """
    
    (templates_dir / "cf-bypass.html").write_text(cf_bypass_html, encoding="utf-8")
    
    # صفحات أخرى (مختصرة)
    for template_name, content in {
        "reservations.html": "<!-- صفحة الحجوزات -->",
        "monitor.html": "<!-- صفحة المراقبة -->",
        "settings.html": "<!-- صفحة الإعدادات -->",
        "reports.html": "<!-- صفحة التقارير -->"
    }.items():
        (templates_dir / template_name).write_text(f"""
        <!DOCTYPE html>
        <html dir="rtl" lang="ar">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{template_name.replace('.html', '')}</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
        </head>
        <body>
            <nav class="navbar navbar-dark bg-dark">
                <div class="container-fluid">
                    <a class="navbar-brand" href="/">نظام مراقبة الحجوزات</a>
                    <div class="d-flex">
                        <a href="/dashboard" class="btn btn-outline-light me-2">لوحة التحكم</a>
                        <a href="/reservations" class="btn btn-outline-light me-2">الحجوزات</a>
                        <a href="/monitor" class="btn btn-outline-light me-2">المراقبة</a>
                        <a href="/cf-bypass" class="btn btn-outline-warning me-2">تجاوز Cloudflare</a>
                        <a href="/settings" class="btn btn-outline-light">الإعدادات</a>
                    </div>
                </div>
            </nav>
            <div class="container mt-4">
                <h2>جار تطوير هذه الصفحة...</h2>
                <p>{content}</p>
                <a href="/dashboard" class="btn btn-primary mt-3">العودة للوحة التحكم</a>
            </div>
        </body>
        </html>
        """, encoding="utf-8")

# ==================== تطبيق FastAPI ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """إدارة دورة حياة التطبيق"""
    # بدء التشغيل
    app.state.db = DatabaseManager()
    app.state.monitor = SmartPlatformMonitor(app.state.db)
    
    # تحميل الكوكيز إذا موجودة
    if os.path.exists("cookies.json"):
        app.state.monitor.load_cookies()
    
    # بدء المراقبة التلقائية
    check_interval = int(app.state.db.get_setting('check_interval', '60'))
    app.state.monitor.start_monitoring(check_interval)
    
    # إنشاء القوالب
    create_simple_templates()
    
    yield
    
    # إيقاف التشغيل
    app.state.monitor.stop_monitoring()

# إنشاء التطبيق
app = FastAPI(
    title="نظام مراقبة وإدارة الحجوزات مع تجاوز Cloudflare",
    description="نظام متكامل لمراقبة منصة الحجوزات وإدارتها مع تجاوز حماية Cloudflare",
    version="3.0.0",
    lifespan=lifespan
)

# إعداد القوالب
templates = Jinja2Templates(directory="templates")

# ==================== واجهات API الرئيسية ====================
@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """لوحة التحكم الرئيسية"""
    return await dashboard_page(request)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """صفحة لوحة التحكم"""
    db: DatabaseManager = request.app.state.db
    monitor: SmartPlatformMonitor = request.app.state.monitor
    
    # الحصول على البيانات
    stats = db.get_stats()
    recent_checks = db.get_check_results(limit=5)
    pending_reservations = db.get_pending_reservations(limit=5)
    recent_logs = db.get_system_logs(limit=5)
    current_status = monitor.get_current_status()
    
    # الإعدادات
    settings = {}
    for key in ['check_interval', 'max_attempts', 'concurrent_submissions', 'auto_retry', 'use_proxy']:
        settings[key] = db.get_setting(key)
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "recent_checks": recent_checks,
        "pending_reservations": pending_reservations,
        "recent_logs": recent_logs,
        "current_status": current_status,
        "settings": settings,
        "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

@app.get("/cf-bypass", response_class=HTMLResponse)
async def cf_bypass_page(request: Request):
    """صفحة تجاوز Cloudflare"""
    return templates.TemplateResponse("cf-bypass.html", {"request": request})

# واجهات API جديدة لتجاوز Cloudflare
@app.post("/api/cf-bypass")
async def perform_cf_bypass_check(request: Request):
    """إجراء تحقق باستخدام طرق تجاوز Cloudflare"""
    monitor: SmartPlatformMonitor = request.app.state.monitor
    db: DatabaseManager = request.app.state.db
    
    db.log("Starting Cloudflare bypass check", "INFO", "cloudflare")
    
    # محاولة الطرق البديلة
    result = monitor.try_alternative_methods(monitor.target_url)
    
    if result:
        return {
            "success": True,
            "message": "تم تجاوز Cloudflare بنجاح",
            "data": result
        }
    else:
        return {
            "success": False,
            "message": "فشل تجاوز Cloudflare مع جميع الطرق",
            "error": "يحتاج تحديث الكوكيز أو إعدادات البروكسي"
        }

@app.post("/api/test-connection")
async def test_connection_method(request: Request, method: str = "simple"):
    """اختبار طريقة اتصال معينة"""
    monitor: SmartPlatformMonitor = request.app.state.monitor
    db: DatabaseManager = request.app.state.db
    
    db.log(f"Testing connection method: {method}", "INFO", "test")
    
    if method == "simple":
        # طريقة بسيطة
        result = monitor.try_with_simple_requests(monitor.target_url)
    elif method == "proxy":
        # طريقة مع بروكسي
        if monitor.get_proxy():
            # حفظ الإعدادات المؤقتة
            old_use_proxy = db.get_setting('use_proxy', 'false')
            db.update_setting('use_proxy', 'true')
            
            # إعادة تحميل البروكسيات
            monitor.proxies = monitor.load_proxies()
            
            result = monitor.perform_comprehensive_check()
            
            # استعادة الإعدادات
            db.update_setting('use_proxy', old_use_proxy)
        else:
            result = None
    else:
        # طريقة متقدمة
        result = monitor.try_with_different_session(monitor.target_url)
    
    if result:
        return {
            "success": True,
            "message": f"نجحت طريقة {method}",
            "method": method,
            "timestamp": datetime.now().isoformat(),
            "data": result
        }
    else:
        return {
            "success": False,
            "message": f"فشلت طريقة {method}",
            "method": method,
            "error": "لم يتمكن من الوصول للمنصة"
        }

# واجهات API الأخرى (يتم الاحتفاظ بها كما هي مع تعديلات بسيطة)
@app.get("/api/health")
async def health_check(request: Request):
    """فحص صحة النظام"""
    db: DatabaseManager = request.app.state.db
    monitor: SmartPlatformMonitor = request.app.state.monitor
    
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "3.0.0",
        "cf_retry_count": monitor.cf_retry_count,
        "is_monitoring": monitor.is_monitoring,
        "cookies_count": len(monitor.session.cookies),
        "proxies_count": len(monitor.proxies)
    }

@app.post("/api/check")
async def perform_platform_check(request: Request, check_request: PlatformCheckRequest = None):
    """إجراء تحقق يدوي"""
    monitor: SmartPlatformMonitor = request.app.state.monitor
    db: DatabaseManager = request.app.state.db
    
    # إذا كانت هناك مشاكل مع Cloudflare، حاول طرق بديلة
    if monitor.cf_retry_count > 2:
        db.log("Using alternative methods due to Cloudflare blocks", "INFO", "cloudflare")
        result = monitor.try_alternative_methods(
            check_request.url if check_request else None
        )
    else:
        result = monitor.perform_comprehensive_check(
            check_request.url if check_request else None
        )
    
    if result:
        return {
            "success": True,
            "message": "تم التحقق بنجاح",
            "cf_bypassed": monitor.cf_retry_count > 0,
            "data": result
        }
    else:
        return {
            "success": False,
            "message": "فشل التحقق من المنصة",
            "cf_blocked": True,
            "retry_count": monitor.cf_retry_count
        }

# ==================== تشغيل التطبيق ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )
