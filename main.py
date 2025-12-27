# app.py - نظام مراقبة وإدارة الحجوزات (إصدار Web API)
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
            ('check_interval', '30'),
            ('max_attempts', '3'),
            ('concurrent_submissions', '1'),
            ('target_url', 'https://import-dep.mega-sy.com/registration'),
            ('auto_retry', 'true'),
            ('notification_enabled', 'true')
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
            SELECT * FROM reservations 
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

# ==================== نواة النظام ====================
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
    
    def setup_advanced_session(self):
        """إعداد جلسة متقدمة"""
        self.session.headers.update({
            "User-Agent": self.get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
            "DNT": "1",
        })
    
    def get_random_user_agent(self):
        """الحصول على User-Agent عشوائي"""
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebkit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ]
        return random.choice(user_agents)
    
    def respect_rate_limit(self):
        """الاحترام rate limit"""
        current_time = time.time()
        
        if current_time - self.last_request_time < 2:
            wait_time = random.uniform(1, 3)
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
                    CheckStatus.FAIL,
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
    
    def perform_comprehensive_check(self, url: str = None):
        """إجراء تحقق شامل"""
        self.respect_rate_limit()
        
        target_url = url or self.target_url
        
        try:
            # تغيير User-Agent بشكل دوري
            if random.random() < 0.3:
                self.session.headers["User-Agent"] = self.get_random_user_agent()
            
            # إضافة headers إضافية
            headers = {
                "Referer": "https://import-dep.mega-sy.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            
            response = self.session.get(
                target_url,
                headers=headers,
                timeout=10,
                allow_redirects=True
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
                
                # تحليل الصفحة
                analysis = self.analyze_platform_page(response.text)
                if analysis:
                    with self.status_lock:
                        self.current_status = analysis
                
                return analysis
                
            elif response.status_code == 403:
                self.db.save_check_result(
                    "connection",
                    "اتصال بالمنصة",
                    CheckStatus.FAIL,
                    f"ممنوع الوصول (403) - Cloudflare/WAF",
                    response.status_code
                )
                self.db.log("حظر الوصول 403 - تحتاج تحديث الكوكيز", "WARNING", "connection")
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
                "انتهت مهلة الاتصال",
                408
            )
            return None
            
        except Exception as e:
            self.db.save_check_result(
                "connection",
                "اتصال بالمنصة",
                CheckStatus.FAIL,
                str(e),
                500
            )
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
                "page_title": soup.title.string if soup.title else "Unknown"
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
                    analysis["remaining_user"] = int(user_elem.text.strip())
                except:
                    pass
            
            system_elem = soup.find("div", {"id": "remainingSystem"})
            if system_elem:
                try:
                    analysis["remaining_system"] = int(system_elem.text.strip())
                except:
                    pass
            
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
                    "السعة منتهية"
                )
            
            # التحقق من CAPTCHA
            if soup.find("div", {"class": "cf-turnstile"}):
                analysis["has_captcha"] = True
                self.db.save_check_result(
                    "security",
                    "Cloudflare Turnstile",
                    CheckStatus.WARNING,
                    "يحتاج حل CAPTCHA يدوي"
                )
            
            # البحث عن التوكنات الأمنية
            form = soup.find("form", {"id": "orderForm"})
            if form:
                tokens_found = 0
                for token_name in ["_token", "hmac", "started_at"]:
                    if form.find("input", {"name": token_name}):
                        tokens_found += 1
                
                self.db.save_check_result(
                    "security",
                    "التوكنات الأمنية",
                    CheckStatus.PASS if tokens_found >= 2 else CheckStatus.WARNING,
                    f"تم العثور على {tokens_found}/3 توكن"
                )
            
            # البحث عن الموعد القادم
            next_msg = soup.find("span", {"id": "nextMsg"})
            if next_msg:
                analysis["next_opening"] = next_msg.text.strip()
            
            return analysis
            
        except Exception as e:
            self.db.save_check_result(
                "analysis",
                "تحليل الصفحة",
                CheckStatus.FAIL,
                str(e)
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
            time.sleep(random.uniform(1, 2))  # محاكاة وقت الإرسال
            
            # نسبة نجاح 80% للمحاكاة
            if random.random() < 0.8:
                result = {
                    "success": True,
                    "message": f"تم إرسال الحجز بنجاح - {plate}",
                    "reference": f"REF_{int(time.time())}_{random.randint(1000, 9999)}",
                    "timestamp": datetime.now().isoformat(),
                    "capacity_remaining": analysis["remaining_system"] - 1
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
    
    def start_monitoring(self, interval: int = 30):
        """بدء المراقبة التلقائية"""
        def monitor_loop():
            while self.is_monitoring:
                try:
                    self.perform_comprehensive_check()
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
    check_interval = int(app.state.db.get_setting('check_interval', '30'))
    app.state.monitor.start_monitoring(check_interval)
    
    yield
    
    # إيقاف التشغيل
    app.state.monitor.stop_monitoring()

# إنشاء التطبيق
app = FastAPI(
    title="نظام مراقبة وإدارة الحجوزات",
    description="نظام متكامل لمراقبة منصة الحجوزات وإدارتها",
    version="2.0.0",
    lifespan=lifespan
)

# إعداد القوالب
templates = Jinja2Templates(directory="templates")

# ==================== واجهات API الرئيسية ====================
@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """لوحة التحكم الرئيسية"""
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
    for key in ['check_interval', 'max_attempts', 'concurrent_submissions', 'auto_retry']:
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

# واجهات API للصحة
@app.get("/api/health")
async def health_check():
    """فحص صحة النظام"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "2.0.0"
    }

# واجهات API للمراقبة
@app.get("/api/status")
async def get_platform_status(request: Request):
    """الحصول على حالة المنصة"""
    monitor: SmartPlatformMonitor = request.app.state.monitor
    status = monitor.get_current_status()
    
    return {
        "success": True,
        "data": status,
        "timestamp": datetime.now().isoformat()
    }

@app.post("/api/check")
async def perform_platform_check(request: Request, check_request: PlatformCheckRequest = None):
    """إجراء تحقق يدوي"""
    monitor: SmartPlatformMonitor = request.app.state.monitor
    db: DatabaseManager = request.app.state.db
    
    if check_request and check_request.check_cookies:
        cookies_loaded = monitor.load_cookies()
    
    result = monitor.perform_comprehensive_check(
        check_request.url if check_request else None
    )
    
    if result:
        return {
            "success": True,
            "message": "تم التحقق بنجاح",
            "data": result
        }
    else:
        return {
            "success": False,
            "message": "فشل التحقق من المنصة"
        }

# واجهات API للحجوزات
@app.get("/api/reservations")
async def get_reservations(
    request: Request,
    status: Optional[str] = None,
    limit: int = 100,
    page: int = 1
):
    """جلب الحجوزات"""
    db: DatabaseManager = request.app.state.db
    
    offset = (page - 1) * limit
    reservations = db.get_all_reservations(limit=limit, status=status)
    
    # التصحيح البسيط للترقيم
    if offset > 0:
        reservations = reservations[offset:offset+limit]
    else:
        reservations = reservations[:limit]
    
    return {
        "success": True,
        "data": reservations,
        "count": len(reservations),
        "page": page,
        "total_pages": (len(db.get_all_reservations(limit=1000)) + limit - 1) // limit
    }

@app.post("/api/reservations")
async def create_reservation(request: Request, reservation: ReservationCreate):
    """إنشاء حجز جديد"""
    db: DatabaseManager = request.app.state.db
    
    reservation_id = db.add_reservation(
        reservation.seller_name,
        reservation.buyer_name,
        reservation.plate_number,
        reservation.priority
    )
    
    return {
        "success": True,
        "message": "تم إضافة الحجز",
        "reservation_id": reservation_id,
        "data": db.get_reservation(reservation_id)
    }

@app.post("/api/reservations/batch")
async def create_batch_reservations(request: Request, batch: ReservationBatch):
    """إنشاء مجموعة حجوزات"""
    db: DatabaseManager = request.app.state.db
    
    reservations_data = [
        {
            "seller_name": res.seller_name,
            "buyer_name": res.buyer_name,
            "plate_number": res.plate_number,
            "priority": res.priority
        }
        for res in batch.reservations
    ]
    
    reservation_ids = db.add_batch_reservations(reservations_data)
    
    return {
        "success": True,
        "message": f"تم إضافة {len(reservation_ids)} حجز",
        "reservation_ids": reservation_ids,
        "count": len(reservation_ids)
    }

@app.post("/api/reservations/upload-csv")
async def upload_reservations_csv(
    request: Request,
    file: UploadFile = File(...),
    has_header: bool = Form(True)
):
    """رفع حجوزات من ملف CSV"""
    db: DatabaseManager = request.app.state.db
    
    content = await file.read()
    content_str = content.decode('utf-8')
    
    # تحليل CSV
    csv_reader = csv.reader(io.StringIO(content_str))
    rows = list(csv_reader)
    
    if has_header and rows:
        rows = rows[1:]  # تخطي العنوان
    
    reservations = []
    for i, row in enumerate(rows, 1):
        if len(row) >= 3:
            reservations.append({
                "seller_name": row[0].strip(),
                "buyer_name": row[1].strip(),
                "plate_number": row[2].strip(),
                "priority": int(row[3].strip()) if len(row) > 3 and row[3].strip().isdigit() else 1
            })
    
    if not reservations:
        raise HTTPException(status_code=400, detail="لم يتم العثور على بيانات صحيحة في الملف")
    
    reservation_ids = db.add_batch_reservations(reservations)
    
    return {
        "success": True,
        "message": f"تم معالجة {len(reservation_ids)} سجل من {len(rows)} سطر",
        "reservation_ids": reservation_ids[:10],  # إرجاع أول 10 فقط
        "total_count": len(reservation_ids)
    }

@app.get("/api/reservations/{reservation_id}")
async def get_reservation_by_id(request: Request, reservation_id: str):
    """جلب حجز محدد"""
    db: DatabaseManager = request.app.state.db
    
    reservation = db.get_reservation(reservation_id)
    
    if not reservation:
        raise HTTPException(status_code=404, detail="الحجز غير موجود")
    
    return {
        "success": True,
        "data": reservation
    }

@app.delete("/api/reservations/{reservation_id}")
async def delete_reservation(request: Request, reservation_id: str):
    """حذف حجز"""
    db: DatabaseManager = request.app.state.db
    
    success = db.delete_reservation(reservation_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="الحجز غير موجود")
    
    return {
        "success": True,
        "message": "تم حذف الحجز"
    }

@app.post("/api/reservations/{reservation_id}/submit")
async def submit_reservation(request: Request, reservation_id: str):
    """إرسال حجوز محدد"""
    db: DatabaseManager = request.app.state.db
    monitor: SmartPlatformMonitor = request.app.state.monitor
    
    reservation = db.get_reservation(reservation_id)
    
    if not reservation:
        raise HTTPException(status_code=404, detail="الحجز غير موجود")
    
    if reservation["status"] == "submitted":
        return {
            "success": False,
            "message": "تم إرسال هذا الحجز مسبقاً"
        }
    
    # إرسال الحجز
    result = monitor.submit_reservation(
        reservation["seller_name"],
        reservation["buyer_name"],
        reservation["plate_number"],
        reservation_id
    )
    
    return {
        "success": result.get("success", False),
        "message": result.get("message", result.get("error", "حدث خطأ")),
        "data": result
    }

@app.post("/api/reservations/submit-pending")
async def submit_pending_reservations(request: Request, background_tasks: BackgroundTasks):
    """إرسال الحجوزات المعلقة"""
    db: DatabaseManager = request.app.state.db
    monitor: SmartPlatformMonitor = request.app.state.monitor
    
    pending = db.get_pending_reservations(limit=10)
    
    if not pending:
        return {
            "success": False,
            "message": "لا توجد حجوزات معلقة"
        }
    
    # دالة الخلفية للإرسال
    async def submit_batch(reservations):
        for reservation in reservations:
            try:
                monitor.submit_reservation(
                    reservation["seller_name"],
                    reservation["buyer_name"],
                    reservation["plate_number"],
                    reservation["reservation_id"]
                )
                # انتظار بين الإرسالات
                await asyncio.sleep(random.uniform(2, 5))
            except Exception as e:
                db.log(f"Failed to submit {reservation['reservation_id']}: {str(e)}", "ERROR", "submission")
    
    # إضافة المهمة للخلفية
    background_tasks.add_task(submit_batch, pending)
    
    return {
        "success": True,
        "message": f"بدأ إرسال {len(pending)} حجوز في الخلفية",
        "count": len(pending)
    }

# واجهات API للإعدادات
@app.get("/api/settings")
async def get_settings(request: Request):
    """جلب إعدادات النظام"""
    db: DatabaseManager = request.app.state.db
    
    settings_keys = [
        'check_interval', 'max_attempts', 'concurrent_submissions',
        'target_url', 'auto_retry', 'notification_enabled'
    ]
    
    settings = {}
    for key in settings_keys:
        settings[key] = db.get_setting(key)
    
    return {
        "success": True,
        "data": settings
    }

@app.post("/api/settings")
async def update_settings(request: Request, settings: Dict[str, Any]):
    """تحديث إعدادات النظام"""
    db: DatabaseManager = request.app.state.db
    monitor: SmartPlatformMonitor = request.app.state.monitor
    
    updated = []
    for key, value in settings.items():
        if key in ['check_interval', 'max_attempts', 'concurrent_submissions']:
            if not str(value).isdigit():
                raise HTTPException(status_code=400, detail=f"قيمة {key} يجب أن تكون رقمية")
        
        db.update_setting(key, str(value))
        updated.append(key)
        
        # تطبيق التغييرات الفورية
        if key == 'check_interval':
            monitor.stop_monitoring()
            monitor.start_monitoring(int(value))
        elif key == 'target_url':
            monitor.target_url = value
    
    return {
        "success": True,
        "message": f"تم تحديث {len(updated)} إعداد",
        "updated": updated
    }

# واجهات API للكوكيز
@app.post("/api/cookies/upload")
async def upload_cookies(request: Request, file: UploadFile = File(...)):
    """رفع ملف الكوكيز"""
    monitor: SmartPlatformMonitor = request.app.state.monitor
    db: DatabaseManager = request.app.state.db
    
    content = await file.read()
    
    try:
        # حفظ الملف مؤقتاً
        temp_path = "temp_cookies.json"
        with open(temp_path, "wb") as f:
            f.write(content)
        
        # تحميل الكوكيز
        success = monitor.load_cookies(temp_path)
        
        # حذف الملف المؤقت
        if os.path.exists(temp_path):
            os.remove(temp_path)
        
        if success:
            # حفظ نسخة دائمة
            with open("cookies.json", "wb") as f:
                f.write(content)
            
            return {
                "success": True,
                "message": "تم تحميل الكوكيز بنجاح"
            }
        else:
            raise HTTPException(status_code=400, detail="فشل تحميل الكوكيز")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"خطأ في معالجة الملف: {str(e)}")

@app.get("/api/cookies/status")
async def get_cookies_status(request: Request):
    """الحصول على حالة الكوكيز"""
    monitor: SmartPlatformMonitor = request.app.state.monitor
    
    return {
        "success": True,
        "has_cookies": len(monitor.session.cookies) > 0,
        "count": len(monitor.session.cookies),
        "cookies": [{"name": c.name, "value": c.value[:20] + "..." if len(c.value) > 20 else c.value} 
                   for c in monitor.session.cookies]
    }

# واجهات API للتقارير
@app.get("/api/reports/checks")
async def get_checks_report(
    request: Request,
    check_type: Optional[str] = None,
    limit: int = 100,
    format: str = "json"
):
    """تقرير نتائج التحقق"""
    db: DatabaseManager = request.app.state.db
    
    checks = db.get_check_results(limit=limit, check_type=check_type)
    
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        
        writer.writerow(["Timestamp", "Check Type", "Check Name", "Status", "Details", "Response Code"])
        for check in checks:
            writer.writerow([
                check["timestamp"],
                check["check_type"],
                check["check_name"],
                check["status"],
                check["details"],
                check["response_code"] or ""
            ])
        
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=checks_report.csv"}
        )
    
    return {
        "success": True,
        "count": len(checks),
        "data": checks
    }

@app.get("/api/reports/reservations")
async def get_reservations_report(
    request: Request,
    status: Optional[str] = None,
    limit: int = 100,
    format: str = "json"
):
    """تقرير الحجوزات"""
    db: DatabaseManager = request.app.state.db
    
    reservations = db.get_all_reservations(limit=limit, status=status)
    
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        
        writer.writerow(["Reservation ID", "Seller", "Buyer", "Plate", "Status", "Created", "Submitted", "Attempts", "Result"])
        for res in reservations:
            writer.writerow([
                res["reservation_id"],
                res["seller_name"],
                res["buyer_name"],
                res["plate_number"],
                res["status"],
                res["created_at"],
                res["submitted_at"] or "",
                res["attempts"],
                res["result"] or ""
            ])
        
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=reservations_report.csv"}
        )
    
    return {
        "success": True,
        "count": len(reservations),
        "data": reservations
    }

# واجهات API للصيانة
@app.post("/api/maintenance/cleanup")
async def cleanup_old_data(request: Request, days: int = 7):
    """تنظيف البيانات القديمة"""
    db: DatabaseManager = request.app.state.db
    
    if days < 1:
        raise HTTPException(status_code=400, detail="عدد الأيام يجب أن يكون 1 أو أكثر")
    
    result = db.clear_old_data(days)
    
    return {
        "success": True,
        "message": f"تم تنظيف البيانات الأقدم من {days} أيام",
        "data": result
    }

@app.post("/api/maintenance/reset")
async def reset_system(request: Request, confirm: bool = False):
    """إعادة تعيين النظام (يجب التأكيد)"""
    if not confirm:
        raise HTTPException(status_code=400, detail="يجب تأكيد إعادة التعيين")
    
    # هنا يمكن إضافة منطق إعادة التعيين
    # لكن كن حذراً لأن هذا قد يحذف البيانات
    
    return {
        "success": True,
        "message": "تم إعادة تعيين النظام (وهمي في هذا الإصدار)"
    }

# ==================== واجهات الويب ====================
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """صفحة لوحة التحكم"""
    return await get_dashboard(request)

@app.get("/reservations", response_class=HTMLResponse)
async def reservations_page(request: Request):
    """صفحة إدارة الحجوزات"""
    db: DatabaseManager = request.app.state.db
    
    status_filter = request.query_params.get("status", "all")
    page = int(request.query_params.get("page", 1))
    limit = 20
    
    if status_filter != "all":
        reservations = db.get_all_reservations(limit=1000, status=status_filter)
    else:
        reservations = db.get_all_reservations(limit=1000)
    
    # الترقيم البسيط
    total = len(reservations)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    paginated_reservations = reservations[start_idx:end_idx]
    
    return templates.TemplateResponse("reservations.html", {
        "request": request,
        "reservations": paginated_reservations,
        "current_status": status_filter,
        "current_page": page,
        "total_pages": (total + limit - 1) // limit,
        "total_reservations": total
    })

@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    """صفحة المراقبة"""
    db: DatabaseManager = request.app.state.db
    monitor: SmartPlatformMonitor = request.app.state.monitor
    
    checks = db.get_check_results(limit=50)
    current_status = monitor.get_current_status()
    logs = db.get_system_logs(limit=20)
    
    return templates.TemplateResponse("monitor.html", {
        "request": request,
        "checks": checks,
        "current_status": current_status,
        "logs": logs,
        "is_monitoring": monitor.is_monitoring
    })

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """صفحة الإعدادات"""
    db: DatabaseManager = request.app.state.db
    
    settings_keys = [
        'check_interval', 'max_attempts', 'concurrent_submissions',
        'target_url', 'auto_retry', 'notification_enabled'
    ]
    
    settings = {}
    for key in settings_keys:
        settings[key] = db.get_setting(key)
    
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "settings": settings
    })

@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    """صفحة التقارير"""
    db: DatabaseManager = request.app.state.db
    
    stats = db.get_stats()
    recent_checks = db.get_check_results(limit=10)
    
    return templates.TemplateResponse("reports.html", {
        "request": request,
        "stats": stats,
        "recent_checks": recent_checks
    })

# ==================== تشغيل التطبيق ====================
if __name__ == "__main__":
    # إنشاء مجلد القوالب إذا لم يكن موجوداً
    os.makedirs("templates", exist_ok=True)
    
    # إنشاء قوالب HTML بسيطة
    create_simple_templates()
    
    # تشغيل الخادم
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info"
    )

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
                                <p><strong>آخر تحديث:</strong> {{ current_status.timestamp }}</p>
                            {% else %}
                                <p class="text-muted">جارٍ تحميل حالة النظام...</p>
                            {% endif %}
                            <button class="btn btn-sm btn-outline-primary" onclick="refreshStatus()">
                                <i class="bi bi-arrow-clockwise"></i> تحديث
                            </button>
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
    
    # صفحة الحجوزات
    reservations_html = """
    <!DOCTYPE html>
    <html dir="rtl" lang="ar">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>إدارة الحجوزات</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.8.1/font/bootstrap-icons.css">
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark">
            <div class="container-fluid">
                <a class="navbar-brand" href="/">
                    <i class="bi bi-speedometer2"></i> نظام مراقبة الحجوزات
                </a>
                <div class="d-flex">
                    <a href="/dashboard" class="btn btn-outline-light me-2">لوحة التحكم</a>
                    <a href="/reservations" class="btn btn-light me-2">الحجوزات</a>
                    <a href="/monitor" class="btn btn-outline-light me-2">المراقبة</a>
                    <a href="/settings" class="btn btn-outline-light">الإعدادات</a>
                </div>
            </div>
        </nav>
        
        <div class="container mt-4">
            <div class="row mb-3">
                <div class="col-12">
                    <div class="card">
                        <div class="card-body">
                            <h5 class="card-title"><i class="bi bi-plus-circle"></i> إضافة حجز جديد</h5>
                            <form id="addReservationForm">
                                <div class="row">
                                    <div class="col-md-3">
                                        <input type="text" class="form-control" placeholder="اسم البائع" id="seller" required>
                                    </div>
                                    <div class="col-md-3">
                                        <input type="text" class="form-control" placeholder="اسم المشتري" id="buyer" required>
                                    </div>
                                    <div class="col-md-3">
                                        <input type="text" class="form-control" placeholder="رقم اللوحة" id="plate" required>
                                    </div>
                                    <div class="col-md-2">
                                        <select class="form-control" id="priority">
                                            <option value="1">عادي</option>
                                            <option value="2">متوسط</option>
                                            <option value="3">عالي</option>
                                        </select>
                                    </div>
                                    <div class="col-md-1">
                                        <button type="submit" class="btn btn-primary w-100">
                                            <i class="bi bi-plus"></i> إضافة
                                        </button>
                                    </div>
                                </div>
                            </form>
                            
                            <div class="mt-3">
                                <button class="btn btn-outline-primary me-2" data-bs-toggle="modal" data-bs-target="#uploadModal">
                                    <i class="bi bi-upload"></i> رفع ملف CSV
                                </button>
                                <a href="/api/reports/reservations?format=csv" class="btn btn-outline-secondary">
                                    <i class="bi bi-download"></i> تصدير CSV
                                </a>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="row">
                <div class="col-12">
                    <div class="card">
                        <div class="card-header">
                            <h5 class="mb-0">
                                <i class="bi bi-list-ul"></i> قائمة الحجوزات
                                <span class="badge bg-secondary">{{ total_reservations }}</span>
                            </h5>
                            <div class="btn-group mt-2">
                                <a href="?status=all&page=1" class="btn btn-sm btn-outline-secondary {% if current_status == 'all' %}active{% endif %}">الكل</a>
                                <a href="?status=pending&page=1" class="btn btn-sm btn-outline-warning {% if current_status == 'pending' %}active{% endif %}">معلق</a>
                                <a href="?status=submitted&page=1" class="btn btn-sm btn-outline-success {% if current_status == 'submitted' %}active{% endif %}">مكتمل</a>
                                <a href="?status=failed&page=1" class="btn btn-sm btn-outline-danger {% if current_status == 'failed' %}active{% endif %}">فشل</a>
                            </div>
                        </div>
                        <div class="card-body">
                            {% if reservations %}
                                <div class="table-responsive">
                                    <table class="table table-hover">
                                        <thead>
                                            <tr>
                                                <th>رقم الحجز</th>
                                                <th>البائع</th>
                                                <th>المشتري</th>
                                                <th>اللوحة</th>
                                                <th>الأولوية</th>
                                                <th>الحالة</th>
                                                <th>المحاولات</th>
                                                <th>الإنشاء</th>
                                                <th>الإجراءات</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for res in reservations %}
                                            <tr>
                                                <td><small>{{ res.reservation_id }}</small></td>
                                                <td>{{ res.seller_name }}</td>
                                                <td>{{ res.buyer_name }}</td>
                                                <td><strong>{{ res.plate_number }}</strong></td>
                                                <td>
                                                    {% if res.priority == 1 %}
                                                        <span class="badge bg-secondary">عادي</span>
                                                    {% elif res.priority == 2 %}
                                                        <span class="badge bg-info">متوسط</span>
                                                    {% else %}
                                                        <span class="badge bg-danger">عالي</span>
                                                    {% endif %}
                                                </td>
                                                <td>
                                                    {% if res.status == 'pending' %}
                                                        <span class="badge bg-warning">معلق</span>
                                                    {% elif res.status == 'submitted' %}
                                                        <span class="badge bg-success">مكتمل</span>
                                                    {% elif res.status == 'failed' %}
                                                        <span class="badge bg-danger">فشل</span>
                                                    {% else %}
                                                        <span class="badge bg-secondary">{{ res.status }}</span>
                                                    {% endif %}
                                                </td>
                                                <td>{{ res.attempts }}</td>
                                                <td><small>{{ res.created_at }}</small></td>
                                                <td>
                                                    {% if res.status == 'pending' %}
                                                        <button class="btn btn-sm btn-success" onclick="submitReservation('{{ res.reservation_id }}')">
                                                            <i class="bi bi-send"></i>
                                                        </button>
                                                    {% endif %}
                                                    <button class="btn btn-sm btn-danger" onclick="deleteReservation('{{ res.reservation_id }}')">
                                                        <i class="bi bi-trash"></i>
                                                    </button>
                                                </td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                                
                                <!-- الترقيم -->
                                {% if total_pages > 1 %}
                                <nav>
                                    <ul class="pagination justify-content-center">
                                        {% if current_page > 1 %}
                                        <li class="page-item">
                                            <a class="page-link" href="?status={{ current_status }}&page={{ current_page-1 }}">السابق</a>
                                        </li>
                                        {% endif %}
                                        
                                        {% for page_num in range(1, total_pages+1) %}
                                            {% if page_num == current_page %}
                                                <li class="page-item active">
                                                    <span class="page-link">{{ page_num }}</span>
                                                </li>
                                            {% else %}
                                                <li class="page-item">
                                                    <a class="page-link" href="?status={{ current_status }}&page={{ page_num }}">{{ page_num }}</a>
                                                </li>
                                            {% endif %}
                                        {% endfor %}
                                        
                                        {% if current_page < total_pages %}
                                        <li class="page-item">
                                            <a class="page-link" href="?status={{ current_status }}&page={{ current_page+1 }}">التالي</a>
                                        </li>
                                        {% endif %}
                                    </ul>
                                </nav>
                                {% endif %}
                                
                            {% else %}
                                <div class="text-center py-5">
                                    <i class="bi bi-inbox" style="font-size: 3rem; color: #ccc;"></i>
                                    <p class="text-muted mt-3">لا توجد حجوزات</p>
                                </div>
                            {% endif %}
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Modal لرفع الملف -->
        <div class="modal fade" id="uploadModal" tabindex="-1">
            <div class="modal-dialog">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">رفع ملف CSV</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <p>رفع ملف CSV يحتوي على الحجوزات. التنسيق:</p>
                        <pre>البائع,المشتري,رقم اللوحة,الأولوية(اختياري)</pre>
                        <form id="uploadForm" enctype="multipart/form-data">
                            <div class="mb-3">
                                <input type="file" class="form-control" id="csvFile" accept=".csv" required>
                            </div>
                            <div class="form-check">
                                <input class="form-check-input" type="checkbox" id="hasHeader" checked>
                                <label class="form-check-label" for="hasHeader">
                                    الملف يحتوي على عناوين
                                </label>
                            </div>
                        </form>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">إلغاء</button>
                        <button type="button" class="btn btn-primary" onclick="uploadCSV()">رفع</button>
                    </div>
                </div>
            </div>
        </div>
        
        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            document.getElementById('addReservationForm').addEventListener('submit', async function(e) {
                e.preventDefault();
                
                const data = {
                    seller_name: document.getElementById('seller').value,
                    buyer_name: document.getElementById('buyer').value,
                    plate_number: document.getElementById('plate').value,
                    priority: parseInt(document.getElementById('priority').value)
                };
                
                const response = await fetch('/api/reservations', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                
                const result = await response.json();
                if (result.success) {
                    alert('تم إضافة الحجز بنجاح');
                    location.reload();
                } else {
                    alert('فشل إضافة الحجز: ' + result.message);
                }
            });
            
            async function submitReservation(reservationId) {
                if (confirm('هل تريد إرسال هذا الحجز؟')) {
                    const response = await fetch(`/api/reservations/${reservationId}/submit`, {
                        method: 'POST'
                    });
                    
                    const result = await response.json();
                    alert(result.message);
                    if (result.success) {
                        location.reload();
                    }
                }
            }
            
            async function deleteReservation(reservationId) {
                if (confirm('هل أنت متأكد من حذف هذا الحجز؟')) {
                    const response = await fetch(`/api/reservations/${reservationId}`, {
                        method: 'DELETE'
                    });
                    
                    const result = await response.json();
                    if (result.success) {
                        location.reload();
                    } else {
                        alert('فشل حذف الحجز');
                    }
                }
            }
            
            async function uploadCSV() {
                const fileInput = document.getElementById('csvFile');
                const hasHeader = document.getElementById('hasHeader').checked;
                
                if (!fileInput.files[0]) {
                    alert('يرجى اختيار ملف');
                    return;
                }
                
                const formData = new FormData();
                formData.append('file', fileInput.files[0]);
                formData.append('has_header', hasHeader);
                
                const response = await fetch('/api/reservations/upload-csv', {
                    method: 'POST',
                    body: formData
                });
                
                const result = await response.json();
                if (result.success) {
                    alert(`تم رفع ${result.total_count} حجز بنجاح`);
                    location.reload();
                } else {
                    alert('فشل رفع الملف');
                }
            }
        </script>
    </body>
    </html>
    """
    
    (templates_dir / "reservations.html").write_text(reservations_html, encoding="utf-8")
    
    # قوالب أخرى (مختصرة بسبب المساحة)
    for template_name, content in {
        "monitor.html": "<h1>صفحة المراقبة</h1><p>جار تطوير هذه الصفحة...</p>",
        "settings.html": "<h1>صفحة الإعدادات</h1><p>جار تطوير هذه الصفحة...</p>",
        "reports.html": "<h1>صفحة التقارير</h1><p>جار تطوير هذه الصفحة...</p>"
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
                        <a href="/settings" class="btn btn-outline-light me-2">الإعدادات</a>
                        <a href="/reports" class="btn btn-outline-light">التقارير</a>
                    </div>
                </div>
            </nav>
            <div class="container mt-4">
                {content}
                <a href="/dashboard" class="btn btn-primary mt-3">العودة للوحة التحكم</a>
            </div>
        </body>
        </html>
        """, encoding="utf-8")
