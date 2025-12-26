# complete_auto_system.py
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import requests, json, os, time, sqlite3, asyncio, hashlib, secrets, re, aiofiles
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional, Set
import uvicorn, logging
from enum import Enum
from contextlib import asynccontextmanager
import threading
from pathlib import Path
import asyncio

# ==================== إعدادات النظام ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('auto_monitor.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ==================== النماذج ====================
class PlatformStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ERROR = "error"
    MAINTENANCE = "maintenance"

class CheckType(str, Enum):
    COOKIES = "cookies"
    SESSION = "session"
    FORM = "form"
    CAPACITY = "capacity"
    TIME = "time"
    SECURITY = "security"

class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    PENDING = "pending"

# ==================== إدارة قاعدة البيانات ====================
class DatabaseManager:
    def __init__(self):
        self.db_path = "monitor_system.db"
        self.init_database()
    
    def init_database(self):
        """تهيئة قاعدة البيانات"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # جدول حالة النظام
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS platform_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            remaining_user INTEGER,
            remaining_system INTEGER,
            next_opening TEXT,
            html_snapshot TEXT
        )
        ''')
        
        # جدول نتائج التحقق
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS check_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            check_type TEXT,
            check_name TEXT,
            status TEXT,
            details TEXT,
            duration_ms INTEGER
        )
        ''')
        
        # جدول الحجوزات
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reservation_id TEXT UNIQUE,
            seller_name TEXT,
            buyer_name TEXT,
            plate_number TEXT,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            submitted_at DATETIME,
            result TEXT,
            priority INTEGER DEFAULT 1
        )
        ''')
        
        # جدول السجلات
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            level TEXT,
            source TEXT,
            message TEXT,
            data TEXT
        )
        ''')
        
        # جدول محاولات الحجز
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservation_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reservation_id TEXT,
            attempt_number INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            response_code INTEGER,
            details TEXT
        )
        ''')
        
        conn.commit()
        conn.close()
        self.log("Database initialized")
    
    def log(self, message: str, level: str = "INFO", source: str = "database", data: dict = None):
        """تسجيل في قاعدة البيانات"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO system_logs (level, source, message, data) VALUES (?, ?, ?, ?)",
            (level, source, message, json.dumps(data) if data else None)
        )
        conn.commit()
        conn.close()
    
    def save_platform_status(self, status_data: dict):
        """حفظ حالة النظام"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO platform_status (status, remaining_user, remaining_system, next_opening, html_snapshot)
        VALUES (?, ?, ?, ?, ?)
        ''', (
            status_data.get("status"),
            status_data.get("remaining_user"),
            status_data.get("remaining_system"),
            status_data.get("next_opening"),
            status_data.get("html_snapshot", "")
        ))
        conn.commit()
        conn.close()
    
    def save_check_result(self, check_type: str, check_name: str, status: str, details: str = "", duration: int = 0):
        """حفظ نتيجة تحقق"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO check_results (check_type, check_name, status, details, duration_ms)
        VALUES (?, ?, ?, ?, ?)
        ''', (check_type, check_name, status, details, duration))
        conn.commit()
        conn.close()
    
    def add_reservation(self, reservation_data: dict) -> str:
        """إضافة حجز جديد"""
        reservation_id = f"RES_{int(time.time())}_{secrets.token_hex(4)}"
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO reservations (reservation_id, seller_name, buyer_name, plate_number, priority)
        VALUES (?, ?, ?, ?, ?)
        ''', (
            reservation_id,
            reservation_data["seller_name"],
            reservation_data["buyer_name"],
            reservation_data["plate_number"],
            reservation_data.get("priority", 1)
        ))
        conn.commit()
        conn.close()
        
        self.log(f"Reservation added: {reservation_id}", "INFO", "reservation", reservation_data)
        return reservation_id
    
    def update_reservation_status(self, reservation_id: str, status: str, result: dict = None):
        """تحديث حالة الحجز"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if status == "submitted":
            cursor.execute('''
            UPDATE reservations SET status = ?, submitted_at = CURRENT_TIMESTAMP, result = ?
            WHERE reservation_id = ?
            ''', (status, json.dumps(result) if result else None, reservation_id))
        else:
            cursor.execute('''
            UPDATE reservations SET status = ?, result = ? WHERE reservation_id = ?
            ''', (status, json.dumps(result) if result else None, reservation_id))
        
        conn.commit()
        conn.close()
    
    def log_reservation_attempt(self, reservation_id: str, attempt_num: int, status: str, response_code: int = None, details: str = ""):
        """تسجيل محاولة حجز"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO reservation_attempts (reservation_id, attempt_number, status, response_code, details)
        VALUES (?, ?, ?, ?, ?)
        ''', (reservation_id, attempt_num, status, response_code, details))
        conn.commit()
        conn.close()
    
    def get_latest_status(self):
        """جلب أحدث حالة للنظام"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        SELECT * FROM platform_status ORDER BY timestamp DESC LIMIT 1
        ''')
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                "id": row[0],
                "timestamp": row[1],
                "status": row[2],
                "remaining_user": row[3],
                "remaining_system": row[4],
                "next_opening": row[5],
                "html_snapshot": row[6]
            }
        return None
    
    def get_check_results(self, limit: int = 20):
        """جلب نتائج التحقق"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
        SELECT * FROM check_results ORDER BY timestamp DESC LIMIT ?
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
                "duration_ms": r[6]
            }
            for r in rows
        ]
    
    def get_reservations(self, status: str = None, limit: int = 50):
        """جلب الحجوزات"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if status:
            cursor.execute('''
            SELECT * FROM reservations WHERE status = ? ORDER BY created_at DESC LIMIT ?
            ''', (status, limit))
        else:
            cursor.execute('''
            SELECT * FROM reservations ORDER BY created_at DESC LIMIT ?
            ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        reservations = []
        for r in rows:
            reservation = {
                "id": r[0],
                "reservation_id": r[1],
                "seller_name": r[2],
                "buyer_name": r[3],
                "plate_number": r[4],
                "status": r[5],
                "created_at": r[6],
                "submitted_at": r[7],
                "result": json.loads(r[8]) if r[8] else None,
                "priority": r[9]
            }
            
            # جلب محاولات هذا الحجز
            conn2 = sqlite3.connect(self.db_path)
            cursor2 = conn2.cursor()
            cursor2.execute('''
            SELECT COUNT(*) FROM reservation_attempts WHERE reservation_id = ?
            ''', (r[1],))
            attempt_count = cursor2.fetchone()[0]
            reservation["attempt_count"] = attempt_count
            conn2.close()
            
            reservations.append(reservation)
        
        return reservations
    
    def get_system_logs(self, level: str = None, limit: int = 100):
        """جلب سجلات النظام"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if level:
            cursor.execute('''
            SELECT * FROM system_logs WHERE level = ? ORDER BY timestamp DESC LIMIT ?
            ''', (level, limit))
        else:
            cursor.execute('''
            SELECT * FROM system_logs ORDER BY timestamp DESC LIMIT ?
            ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "level": r[2],
                "source": r[3],
                "message": r[4],
                "data": json.loads(r[5]) if r[5] else None
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
        stats["reservations_by_status"] = dict(cursor.fetchall())
        
        # إحصائيات التحقق
        cursor.execute("SELECT check_type, status, COUNT(*) FROM check_results GROUP BY check_type, status")
        check_stats = {}
        for check_type, status, count in cursor.fetchall():
            if check_type not in check_stats:
                check_stats[check_type] = {}
            check_stats[check_type][status] = count
        stats["check_results"] = check_stats
        
        # عدد المحاولات اليوم
        cursor.execute("SELECT COUNT(*) FROM reservation_attempts WHERE DATE(timestamp) = DATE('now')")
        stats["today_attempts"] = cursor.fetchone()[0]
        
        # حالة النظام الحالية
        cursor.execute("SELECT status, COUNT(*) FROM platform_status GROUP BY status ORDER BY timestamp DESC LIMIT 10")
        status_history = cursor.fetchall()
        stats["status_history"] = status_history
        
        conn.close()
        return stats

# ==================== نواة النظام ====================
class PlatformMonitor:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.session = requests.Session()
        self.target_url = "https://import-dep.mega-sy.com/registration"
        self.setup_session()
        self.cookies_loaded = False
        self.last_check = None
        self.is_monitoring = False
        self.monitor_thread = None
    
    def setup_session(self):
        """إعداد الجلسة"""
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })
    
    def load_cookies(self, cookies_file: str = "cookies.json"):
        """تحميل الكوكيز"""
        try:
            if os.path.exists(cookies_file):
                with open(cookies_file, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                
                self.session.cookies.clear()
                for cookie in cookies:
                    self.session.cookies.set(
                        name=cookie.get("name"),
                        value=cookie.get("value"),
                        domain=cookie.get("domain"),
                        path=cookie.get("path", "/")
                    )
                
                self.cookies_loaded = True
                self.db.save_check_result(
                    CheckType.COOKIES,
                    "تحميل الكوكيز",
                    CheckStatus.PASS,
                    f"تم تحميل {len(cookies)} كوكيز",
                    100
                )
                return True
            else:
                self.db.save_check_result(
                    CheckType.COOKIES,
                    "تحميل الكوكيز",
                    CheckStatus.FAIL,
                    "ملف الكوكيز غير موجود",
                    0
                )
                return False
        except Exception as e:
            self.db.save_check_result(
                CheckType.COOKIES,
                "تحميل الكوكيز",
                CheckStatus.FAIL,
                str(e),
                0
            )
            return False
    
    def perform_checks(self):
        """تنفيذ جميع عمليات التحقق"""
        checks = []
        
        # 1. تحقق الاتصال
        start_time = time.time()
        try:
            response = self.session.get(self.target_url, timeout=10)
            checks.append({
                "type": CheckType.SESSION,
                "name": "اتصال بالمنصة",
                "status": CheckStatus.PASS if response.status_code == 200 else CheckStatus.FAIL,
                "details": f"كود الحالة: {response.status_code}",
                "duration": int((time.time() - start_time) * 1000)
            })
            
            if response.status_code == 200:
                html_content = response.text
                soup = BeautifulSoup(html_content, 'html.parser')
                
                # 2. تحقق حالة النظام
                open_dot = soup.find("span", {"id": "openDot"})
                is_open = bool(open_dot and "dot-open" in open_dot.get("class", []))
                checks.append({
                    "type": CheckType.SESSION,
                    "name": "حالة النظام",
                    "status": CheckStatus.PASS if is_open else CheckStatus.WARNING,
                    "details": "مفتوح" if is_open else "مغلق",
                    "duration": 50
                })
                
                # 3. تحقق النموذج
                fieldset = soup.find("fieldset", {"id": "formFields"})
                form_enabled = not fieldset.has_attr("disabled") if fieldset else False
                checks.append({
                    "type": CheckType.FORM,
                    "name": "تفعيل النموذج",
                    "status": CheckStatus.PASS if form_enabled else CheckStatus.WARNING,
                    "details": "مفعل" if form_enabled else "معطل",
                    "duration": 50
                })
                
                # 4. تحقق السعة
                remaining_user = 0
                remaining_system = 0
                
                user_elem = soup.find("div", {"id": "remainingUser"})
                if user_elem:
                    try:
                        remaining_user = int(user_elem.text.strip())
                    except:
                        pass
                
                system_elem = soup.find("div", {"id": "remainingSystem"})
                if system_elem:
                    try:
                        remaining_system = int(system_elem.text.strip())
                    except:
                        pass
                
                checks.append({
                    "type": CheckType.CAPACITY,
                    "name": "سعة النظام",
                    "status": CheckStatus.PASS if remaining_system > 0 else CheckStatus.WARNING,
                    "details": f"المستخدم: {remaining_user} | النظام: {remaining_system}",
                    "duration": 50
                })
                
                # 5. تحقق التوكنات الأمنية
                form = soup.find("form", {"id": "orderForm"})
                if form:
                    tokens = []
                    for token_name in ["_token", "hmac", "started_at"]:
                        token_input = form.find("input", {"name": token_name})
                        if token_input and token_input.get("value"):
                            tokens.append(token_name)
                    
                    checks.append({
                        "type": CheckType.SECURITY,
                        "name": "التوكنات الأمنية",
                        "status": CheckStatus.PASS if len(tokens) >= 2 else CheckStatus.WARNING,
                        "details": f"تم العثور على {len(tokens)} توكن",
                        "duration": 50
                    })
                
                # 6. تحقق الوقت
                current_hour = datetime.now().hour
                is_working_hours = 18 <= current_hour <= 23
                checks.append({
                    "type": CheckType.TIME,
                    "name": "وقت العمل",
                    "status": CheckStatus.PASS if is_working_hours else CheckStatus.WARNING,
                    "details": f"الساعة الحالية: {current_hour}:00",
                    "duration": 50
                })
                
                # حفظ حالة النظام
                platform_status = {
                    "status": PlatformStatus.OPEN if is_open else PlatformStatus.CLOSED,
                    "remaining_user": remaining_user,
                    "remaining_system": remaining_system,
                    "next_opening": None,
                    "html_snapshot": html_content[:5000]  # حفظ جزء من HTML
                }
                
                # البحث عن الموعد القادم
                next_msg = soup.find("span", {"id": "nextMsg"})
                if next_msg:
                    platform_status["next_opening"] = next_msg.text.strip()
                
                self.db.save_platform_status(platform_status)
                
        except Exception as e:
            checks.append({
                "type": CheckType.SESSION,
                "name": "اتصال بالمنصة",
                "status": CheckStatus.FAIL,
                "details": str(e),
                "duration": int((time.time() - start_time) * 1000)
            })
        
        # حفظ نتائج التحقق
        for check in checks:
            self.db.save_check_result(
                check["type"],
                check["name"],
                check["status"],
                check["details"],
                check["duration"]
            )
        
        self.last_check = datetime.now()
        return checks
    
    def submit_reservation(self, seller_name: str, buyer_name: str, plate_number: str):
        """إرسال حجز إلى المنصة"""
        try:
            # 1. جلب صفحة النموذج
            response = self.session.get(self.target_url, timeout=10)
            if response.status_code != 200:
                return {"success": False, "error": f"HTTP {response.status_code}"}
            
            soup = BeautifulSoup(response.text, 'html.parser')
            form = soup.find("form", {"id": "orderForm"})
            
            if not form:
                return {"success": False, "error": "النموذج غير موجود"}
            
            # 2. استخراج التوكنات
            token = form.find("input", {"name": "_token"})
            hmac = form.find("input", {"name": "hmac"})
            started_at = form.find("input", {"name": "started_at"})
            
            if not all([token, hmac, started_at]):
                return {"success": False, "error": "توكنات الأمان غير مكتملة"}
            
            # 3. الانتظار للوقت الدنيا
            start_time = int(started_at["value"])
            current_time = int(time.time() * 1000)
            time_spent = (current_time - start_time) / 1000
            
            if time_spent < 8:
                wait_time = 8 - time_spent
                time.sleep(wait_time)
            
            # 4. إعداد البيانات
            payload = {
                "_token": token["value"],
                "hmac": hmac["value"],
                "started_at": started_at["value"],
                "seller_name": seller_name,
                "buyer_name": buyer_name,
                "plate_number": plate_number
            }
            
            # إضافة الحقول المخفية
            for hidden in form.find_all("input", type="hidden"):
                name = hidden.get("name")
                if name and name not in payload:
                    payload[name] = hidden.get("value", "")
            
            # 5. الإرسال
            submit_response = self.session.post(self.target_url, data=payload, timeout=15)
            
            # 6. تحليل النتيجة
            if submit_response.status_code == 200:
                result_soup = BeautifulSoup(submit_response.text, 'html.parser')
                success_div = result_soup.find("div", {"id": "appointment-summary"})
                
                if success_div:
                    return {
                        "success": True,
                        "message": "تم الحجز بنجاح",
                        "appointment_info": success_div.text.strip()
                    }
            
            return {"success": False, "error": "فشل الإرسال"}
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def start_auto_monitor(self, interval: int = 60):
        """بدء المراقبة التلقائية"""
        def monitor_loop():
            self.is_monitoring = True
            self.db.log("بدء المراقبة التلقائية", "INFO", "monitor")
            
            while self.is_monitoring:
                try:
                    # تنفيذ التحقق
                    self.perform_checks()
                    
                    # التحقق من الحجوزات المعلقة
                    pending_reservations = self.db.get_reservations("pending", 10)
                    
                    for reservation in pending_reservations:
                        # التحقق من حالة النظام أولاً
                        latest_status = self.db.get_latest_status()
                        
                        if latest_status and latest_status["status"] == PlatformStatus.OPEN:
                            # محاولة الحجز
                            result = self.submit_reservation(
                                reservation["seller_name"],
                                reservation["buyer_name"],
                                reservation["plate_number"]
                            )
                            
                            # تسجيل المحاولة
                            attempt_num = reservation.get("attempt_count", 0) + 1
                            self.db.log_reservation_attempt(
                                reservation["reservation_id"],
                                attempt_num,
                                "success" if result["success"] else "failed",
                                200 if result["success"] else 400,
                                result.get("message", result.get("error", ""))
                            )
                            
                            if result["success"]:
                                self.db.update_reservation_status(
                                    reservation["reservation_id"],
                                    "submitted",
                                    result
                                )
                                self.db.log(f"تم حجز: {reservation['reservation_id']}", "SUCCESS", "reservation")
                    
                    # الانتظار للمرة القادمة
                    time.sleep(interval)
                    
                except Exception as e:
                    self.db.log(f"خطأ في المراقبة: {str(e)}", "ERROR", "monitor")
                    time.sleep(interval * 2)
        
        self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self.monitor_thread.start()
    
    def stop_auto_monitor(self):
        """إيقاف المراقبة التلقائية"""
        self.is_monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        self.db.log("إيقاف المراقبة التلقائية", "INFO", "monitor")

# ==================== واجهة الويب ====================
class WebInterface:
    def __init__(self, app: FastAPI, monitor: PlatformMonitor, db: DatabaseManager):
        self.app = app
        self.monitor = monitor
        self.db = db
        self.active_connections: Set[WebSocket] = set()
        self.setup_routes()
    
    def setup_routes(self):
        """إعداد مسارات الواجهة"""
        
        @self.app.get("/", response_class=HTMLResponse)
        async def dashboard(request: Request):
            return self.get_dashboard_html()
        
        @self.app.get("/api/status")
        async def get_status():
            return JSONResponse({
                "platform_status": self.db.get_latest_status(),
                "check_results": self.db.get_check_results(10),
                "monitor_active": self.monitor.is_monitoring,
                "cookies_loaded": self.monitor.cookies_loaded
            })
        
        @self.app.get("/api/reservations")
        async def get_reservations(status: str = None, limit: int = 50):
            reservations = self.db.get_reservations(status, limit)
            return JSONResponse({"reservations": reservations})
        
        @self.app.post("/api/reservations")
        async def add_reservation(request: Request):
            try:
                data = await request.json()
                reservation_id = self.db.add_reservation(data)
                return JSONResponse({
                    "success": True,
                    "message": "تم إضافة الحجز",
                    "reservation_id": reservation_id
                })
            except Exception as e:
                raise HTTPException(500, str(e))
        
        @self.app.delete("/api/reservations/{reservation_id}")
        async def delete_reservation(reservation_id: str):
            self.db.update_reservation_status(reservation_id, "cancelled")
            return JSONResponse({"success": True, "message": "تم إلغاء الحجز"})
        
        @self.app.get("/api/logs")
        async def get_logs(level: str = None, limit: int = 100):
            logs = self.db.get_system_logs(level, limit)
            return JSONResponse({"logs": logs})
        
        @self.app.get("/api/stats")
        async def get_stats():
            stats = self.db.get_stats()
            return JSONResponse(stats)
        
        @self.app.post("/api/check-now")
        async def check_now():
            checks = self.monitor.perform_checks()
            return JSONResponse({"success": True, "checks": checks})
        
        @self.app.post("/api/upload-cookies")
        async def upload_cookies(file: UploadFile = File(...)):
            try:
                content = await file.read()
                
                # حفظ الملف
                temp_file = f"temp_cookies_{int(time.time())}.json"
                async with aiofiles.open(temp_file, "wb") as f:
                    await f.write(content)
                
                # تحميل الكوكيز
                success = self.monitor.load_cookies(temp_file)
                
                # حذف الملف المؤقت
                try:
                    os.remove(temp_file)
                except:
                    pass
                
                if success:
                    return JSONResponse({"success": True, "message": "تم تحميل الكوكيز"})
                else:
                    return JSONResponse({"success": False, "error": "فشل تحميل الكوكيز"})
                    
            except Exception as e:
                raise HTTPException(500, str(e))
        
        @self.app.post("/api/monitor/start")
        async def start_monitor(interval: int = 60):
            if not self.monitor.is_monitoring:
                self.monitor.start_auto_monitor(interval)
            return JSONResponse({"success": True, "message": "تم بدء المراقبة"})
        
        @self.app.post("/api/monitor/stop")
        async def stop_monitor():
            if self.monitor.is_monitoring:
                self.monitor.stop_auto_monitor()
            return JSONResponse({"success": True, "message": "تم إيقاف المراقبة"})
        
        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            self.active_connections.add(websocket)
            
            try:
                while True:
                    # إرسال تحديثات دورية
                    await asyncio.sleep(5)
                    
                    latest_status = self.db.get_latest_status()
                    if latest_status:
                        await websocket.send_json({
                            "type": "status_update",
                            "data": latest_status,
                            "timestamp": datetime.now().isoformat()
                        })
                        
            except WebSocketDisconnect:
                self.active_connections.remove(websocket)
    
    def get_dashboard_html(self):
        """إنشاء واجهة التحكم"""
        return '''
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>نظام المراقبة الآلي</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <style>
                .status-badge {
                    padding: 4px 12px;
                    border-radius: 20px;
                    font-size: 0.9rem;
                    font-weight: bold;
                }
                .status-pass { background: #dcfce7; color: #166534; }
                .status-fail { background: #fee2e2; color: #991b1b; }
                .status-warning { background: #fef3c7; color: #92400e; }
                .status-pending { background: #e0e7ff; color: #3730a3; }
                
                .platform-open { background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; }
                .platform-closed { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); color: white; }
                
                .log-entry {
                    padding: 8px 12px;
                    margin: 4px 0;
                    border-radius: 6px;
                    border-right: 4px solid;
                }
                .log-info { background: #dbeafe; border-color: #3b82f6; }
                .log-success { background: #d1fae5; border-color: #10b981; }
                .log-warning { background: #fef3c7; border-color: #f59e0b; }
                .log-error { background: #fee2e2; border-color: #ef4444; }
            </style>
        </head>
        <body class="bg-gray-50">
            <div class="container mx-auto px-4 py-6">
                <!-- الهيدر -->
                <header class="mb-8">
                    <div class="flex justify-between items-center">
                        <div>
                            <h1 class="text-3xl font-bold text-gray-800">
                                <i class="fas fa-robot mr-2"></i>نظام المراقبة الآلي
                            </h1>
                            <p class="text-gray-600">مراقبة وحجز تلقائي للمنصة</p>
                        </div>
                        <div class="flex items-center space-x-4">
                            <div id="monitorStatus" class="px-4 py-2 rounded-lg bg-gray-100">
                                <i class="fas fa-pause-circle mr-2"></i>
                                <span>مراقبة متوقفة</span>
                            </div>
                            <div id="cookiesStatus" class="px-4 py-2 rounded-lg bg-yellow-100 text-yellow-800">
                                <i class="fas fa-exclamation-triangle mr-2"></i>
                                <span>تحتاج كوكيز</span>
                            </div>
                        </div>
                    </div>
                </header>
                
                <!-- شبكة المحتوى -->
                <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">
                    <!-- حالة النظام -->
                    <div class="lg:col-span-2">
                        <div class="bg-white rounded-xl shadow p-6">
                            <div class="flex justify-between items-center mb-6">
                                <h2 class="text-xl font-semibold text-gray-800">
                                    <i class="fas fa-satellite-dish mr-2"></i>حالة المنصة
                                </h2>
                                <div class="flex space-x-3">
                                    <button onclick="checkNow()" class="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600">
                                        <i class="fas fa-sync-alt mr-2"></i>فحص الآن
                                    </button>
                                    <button onclick="toggleMonitor()" id="monitorBtn" class="px-4 py-2 bg-green-500 text-white rounded-lg hover:bg-green-600">
                                        <i class="fas fa-play mr-2"></i>بدء المراقبة
                                    </button>
                                </div>
                            </div>
                            
                            <div id="platformStatus" class="platform-closed p-6 rounded-xl text-center mb-6">
                                <div class="text-4xl mb-2">🔴</div>
                                <div class="text-2xl font-bold mb-2">جاري التحميل...</div>
                                <div class="text-lg opacity-90">--:--</div>
                            </div>
                            
                            <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
                                <div class="bg-gray-50 p-4 rounded-lg text-center">
                                    <div class="text-sm text-gray-500 mb-1">المتبقي لك</div>
                                    <div id="remainingUser" class="text-2xl font-bold text-blue-600">0</div>
                                </div>
                                <div class="bg-gray-50 p-4 rounded-lg text-center">
                                    <div class="text-sm text-gray-500 mb-1">المتبقي للنظام</div>
                                    <div id="remainingSystem" class="text-2xl font-bold text-green-600">0</div>
                                </div>
                                <div class="bg-gray-50 p-4 rounded-lg text-center">
                                    <div class="text-sm text-gray-500 mb-1">آخر فحص</div>
                                    <div id="lastCheck" class="text-lg font-semibold">--:--</div>
                                </div>
                                <div class="bg-gray-50 p-4 rounded-lg text-center">
                                    <div class="text-sm text-gray-500 mb-1">الموعد القادم</div>
                                    <div id="nextOpening" class="text-lg font-semibold">--</div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- إضافة حجز -->
                    <div class="bg-white rounded-xl shadow p-6">
                        <h2 class="text-xl font-semibold text-gray-800 mb-6">
                            <i class="fas fa-calendar-plus mr-2"></i>إضافة حجز جديد
                        </h2>
                        
                        <form id="reservationForm" class="space-y-4">
                            <div>
                                <label class="block text-sm font-medium text-gray-700 mb-2">اسم البائع</label>
                                <input type="text" id="sellerName" required
                                       class="w-full p-3 border rounded-lg focus:ring-2 focus:ring-blue-500">
                            </div>
                            
                            <div>
                                <label class="block text-sm font-medium text-gray-700 mb-2">اسم المشتري</label>
                                <input type="text" id="buyerName" required
                                       class="w-full p-3 border rounded-lg focus:ring-2 focus:ring-blue-500">
                            </div>
                            
                            <div>
                                <label class="block text-sm font-medium text-gray-700 mb-2">رقم اللوحة</label>
                                <input type="text" id="plateNumber" required
                                       class="w-full p-3 border rounded-lg focus:ring-2 focus:ring-blue-500"
                                       placeholder="أرقام فقط">
                            </div>
                            
                            <div>
                                <label class="block text-sm font-medium text-gray-700 mb-2">الأولوية</label>
                                <select id="priority" class="w-full p-3 border rounded-lg">
                                    <option value="1">عادية</option>
                                    <option value="2">متوسطة</option>
                                    <option value="3">عالية</option>
                                </select>
                            </div>
                            
                            <button type="submit" 
                                    class="w-full py-3 bg-blue-500 text-white rounded-lg hover:bg-blue-600 font-semibold">
                                <i class="fas fa-plus-circle mr-2"></i>إضافة للحجوزات
                            </button>
                        </form>
                        
                        <div class="mt-6">
                            <label class="block text-sm font-medium text-gray-700 mb-2">
                                <i class="fas fa-cookie-bite mr-2"></i>رفع ملف الكوكيز
                            </label>
                            <div class="flex items-center">
                                <input type="file" id="cookiesFile" accept=".json" class="hidden">
                                <button onclick="uploadCookies()" 
                                        class="flex-1 py-3 bg-yellow-500 text-white rounded-lg hover:bg-yellow-600">
                                    <i class="fas fa-upload mr-2"></i>رفع ملف JSON
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
                
                <!-- نتائج التحقق -->
                <div class="bg-white rounded-xl shadow p-6 mb-8">
                    <h2 class="text-xl font-semibold text-gray-800 mb-6">
                        <i class="fas fa-check-circle mr-2"></i>نتائج التحقق
                    </h2>
                    
                    <div id="checkResults" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        <!-- سيتم ملؤها بالنتائج -->
                    </div>
                </div>
                
                <!-- الحجوزات -->
                <div class="bg-white rounded-xl shadow p-6 mb-8">
                    <div class="flex justify-between items-center mb-6">
                        <h2 class="text-xl font-semibold text-gray-800">
                            <i class="fas fa-list-alt mr-2"></i>الحجوزات النشطة
                        </h2>
                        <div class="text-sm text-gray-500" id="reservationsCount">0 حجز</div>
                    </div>
                    
                    <div class="overflow-x-auto">
                        <table class="w-full">
                            <thead>
                                <tr class="bg-gray-50">
                                    <th class="p-3 text-right">رقم الحجز</th>
                                    <th class="p-3 text-right">البائع</th>
                                    <th class="p-3 text-right">المشتري</th>
                                    <th class="p-3 text-right">رقم اللوحة</th>
                                    <th class="p-3 text-right">الحالة</th>
                                    <th class="p-3 text-right">المحاولات</th>
                                    <th class="p-3 text-right">التاريخ</th>
                                    <th class="p-3 text-right">إجراءات</th>
                                </tr>
                            </thead>
                            <tbody id="reservationsTable">
                                <!-- سيتم ملؤها بالبيانات -->
                            </tbody>
                        </table>
                    </div>
                </div>
                
                <!-- السجلات -->
                <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <div class="bg-white rounded-xl shadow p-6">
                        <h2 class="text-xl font-semibold text-gray-800 mb-6">
                            <i class="fas fa-history mr-2"></i>سجلات النظام
                        </h2>
                        
                        <div id="logsContainer" class="h-96 overflow-y-auto space-y-2">
                            <!-- السجلات ستظهر هنا -->
                        </div>
                    </div>
                    
                    <!-- الإحصائيات -->
                    <div class="bg-white rounded-xl shadow p-6">
                        <h2 class="text-xl font-semibold text-gray-800 mb-6">
                            <i class="fas fa-chart-bar mr-2"></i>الإحصائيات
                        </h2>
                        
                        <div class="space-y-4">
                            <div>
                                <div class="flex justify-between mb-1">
                                    <span class="text-sm text-gray-600">الحجوزات المعلقة</span>
                                    <span id="pendingCount" class="text-sm font-semibold">0</span>
                                </div>
                                <div class="w-full bg-gray-200 rounded-full h-2">
                                    <div id="pendingBar" class="bg-yellow-500 h-2 rounded-full" style="width: 0%"></div>
                                </div>
                            </div>
                            
                            <div>
                                <div class="flex justify-between mb-1">
                                    <span class="text-sm text-gray-600">الحجوزات الناجحة</span>
                                    <span id="successCount" class="text-sm font-semibold">0</span>
                                </div>
                                <div class="w-full bg-gray-200 rounded-full h-2">
                                    <div id="successBar" class="bg-green-500 h-2 rounded-full" style="width: 0%"></div>
                                </div>
                            </div>
                            
                            <div>
                                <div class="flex justify-between mb-1">
                                    <span class="text-sm text-gray-600">معدل النجاح</span>
                                    <span id="successRate" class="text-sm font-semibold">0%</span>
                                </div>
                                <div class="w-full bg-gray-200 rounded-full h-2">
                                    <div id="rateBar" class="bg-blue-500 h-2 rounded-full" style="width: 0%"></div>
                                </div>
                            </div>
                            
                            <div class="pt-4 border-t">
                                <div class="text-center">
                                    <div class="text-2xl font-bold text-gray-800" id="totalAttempts">0</div>
                                    <div class="text-sm text-gray-600">محاولة حجز اليوم</div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <script>
                let ws = null;
                let monitorActive = false;
                
                // الاتصال بـ WebSocket
                function connectWebSocket() {
                    ws = new WebSocket(`ws://${window.location.host}/ws`);
                    
                    ws.onmessage = (event) => {
                        const data = JSON.parse(event.data);
                        if (data.type === 'status_update') {
                            updatePlatformStatus(data.data);
                        }
                    };
                    
                    ws.onclose = () => {
                        setTimeout(connectWebSocket, 5000);
                    };
                }
                
                // تحديث حالة المنصة
                function updatePlatformStatus(status) {
                    const statusDiv = document.getElementById('platformStatus');
                    const statusText = statusDiv.querySelector('.text-2xl');
                    const timeText = statusDiv.querySelector('.text-lg');
                    
                    if (status.status === 'open') {
                        statusDiv.className = 'platform-open p-6 rounded-xl text-center mb-6';
                        statusDiv.querySelector('.text-4xl').textContent = '🟢';
                        statusText.textContent = 'النظام مفتوح';
                    } else {
                        statusDiv.className = 'platform-closed p-6 rounded-xl text-center mb-6';
                        statusDiv.querySelector('.text-4xl').textContent = '🔴';
                        statusText.textContent = 'النظام مغلق';
                    }
                    
                    timeText.textContent = new Date(status.timestamp).toLocaleTimeString('ar-SA');
                    
                    // تحديث الأرقام
                    document.getElementById('remainingUser').textContent = status.remaining_user || 0;
                    document.getElementById('remainingSystem').textContent = status.remaining_system || 0;
                    document.getElementById('lastCheck').textContent = new Date(status.timestamp).toLocaleTimeString('ar-SA');
                    document.getElementById('nextOpening').textContent = status.next_opening || 'غير معروف';
                }
                
                // تحميل البيانات
                async function loadData() {
                    try {
                        // حالة النظام
                        const statusRes = await fetch('/api/status');
                        const statusData = await statusRes.json();
                        
                        if (statusData.platform_status) {
                            updatePlatformStatus(statusData.platform_status);
                        }
                        
                        // تحديث حالة المراقبة
                        monitorActive = statusData.monitor_active;
                        updateMonitorStatus();
                        
                        // تحديث حالة الكوكيز
                        updateCookiesStatus(statusData.cookies_loaded);
                        
                        // نتائج التحقق
                        if (statusData.check_results) {
                            updateCheckResults(statusData.check_results);
                        }
                        
                        // الحجوزات
                        const reservationsRes = await fetch('/api/reservations?limit=20');
                        const reservationsData = await reservationsRes.json();
                        updateReservations(reservationsData.reservations);
                        
                        // السجلات
                        const logsRes = await fetch('/api/logs?limit=30');
                        const logsData = await logsRes.json();
                        updateLogs(logsData.logs);
                        
                        // الإحصائيات
                        const statsRes = await fetch('/api/stats');
                        const statsData = await statsRes.json();
                        updateStats(statsData);
                        
                    } catch (error) {
                        console.error('خطأ في تحميل البيانات:', error);
                    }
                }
                
                // تحديث نتائج التحقق
                function updateCheckResults(checks) {
                    const container = document.getElementById('checkResults');
                    container.innerHTML = '';
                    
                    checks.forEach(check => {
                        const checkDiv = document.createElement('div');
                        checkDiv.className = 'bg-gray-50 p-4 rounded-lg';
                        
                        const statusClass = `status-badge status-${check.status}`;
                        
                        checkDiv.innerHTML = `
                            <div class="flex justify-between items-start mb-2">
                                <div class="font-semibold text-gray-800">${check.check_name}</div>
                                <div class="${statusClass}">${check.status === 'pass' ? '✅' : check.status === 'fail' ? '❌' : '⚠️'}</div>
                            </div>
                            <div class="text-sm text-gray-600 mb-1">${check.check_type}</div>
                            <div class="text-sm text-gray-500">${check.details}</div>
                            <div class="text-xs text-gray-400 mt-2">${new Date(check.timestamp).toLocaleTimeString('ar-SA')}</div>
                        `;
                        
                        container.appendChild(checkDiv);
                    });
                }
                
                // تحديث الحجوزات
                function updateReservations(reservations) {
                    const container = document.getElementById('reservationsTable');
                    const countSpan = document.getElementById('reservationsCount');
                    
                    container.innerHTML = '';
                    countSpan.textContent = `${reservations.length} حجز`;
                    
                    reservations.forEach(res => {
                        const statusBadge = res.status === 'pending' ? 
                            '<span class="px-2 py-1 bg-yellow-100 text-yellow-800 rounded-full text-xs">قيد الانتظار</span>' :
                            res.status === 'submitted' ?
                            '<span class="px-2 py-1 bg-green-100 text-green-800 rounded-full text-xs">تم الإرسال</span>' :
                            '<span class="px-2 py-1 bg-gray-100 text-gray-800 rounded-full text-xs">ملغى</span>';
                        
                        const row = document.createElement('tr');
                        row.className = 'border-t';
                        row.innerHTML = `
                            <td class="p-3 text-sm font-mono">${res.reservation_id}</td>
                            <td class="p-3">${res.seller_name}</td>
                            <td class="p-3">${res.buyer_name}</td>
                            <td class="p-3 font-mono">${res.plate_number}</td>
                            <td class="p-3">${statusBadge}</td>
                            <td class="p-3 text-center">${res.attempt_count || 0}</td>
                            <td class="p-3 text-sm text-gray-500">${new Date(res.created_at).toLocaleDateString('ar-SA')}</td>
                            <td class="p-3">
                                ${res.status === 'pending' ? 
                                    `<button onclick="cancelReservation('${res.reservation_id}')" class="px-3 py-1 text-sm bg-red-100 text-red-700 rounded hover:bg-red-200">
                                        إلغاء
                                    </button>` : 
                                    ''
                                }
                            </td>
                        `;
                        
                        container.appendChild(row);
                    });
                }
                
                // تحديث السجلات
                function updateLogs(logs) {
                    const container = document.getElementById('logsContainer');
                    container.innerHTML = '';
                    
                    logs.forEach(log => {
                        const logDiv = document.createElement('div');
                        logDiv.className = `log-entry log-${log.level.toLowerCase()}`;
                        
                        logDiv.innerHTML = `
                            <div class="flex justify-between">
                                <div>
                                    <span class="font-medium">${log.source}</span>
                                    <span class="text-gray-600">: ${log.message}</span>
                                </div>
                                <div class="text-xs text-gray-500">
                                    ${new Date(log.timestamp).toLocaleTimeString('ar-SA')}
                                </div>
                            </div>
                        `;
                        
                        container.appendChild(logDiv);
                    });
                }
                
                // تحديث الإحصائيات
                function updateStats(stats) {
                    const reservations = stats.reservations_by_status || {};
                    const total = Object.values(reservations).reduce((a, b) => a + b, 0);
                    
                    const pending = reservations.pending || 0;
                    const success = reservations.submitted || 0;
                    
                    document.getElementById('pendingCount').textContent = pending;
                    document.getElementById('successCount').textContent = success;
                    document.getElementById('totalAttempts').textContent = stats.today_attempts || 0;
                    
                    // حساب النسب
                    const pendingPercent = total > 0 ? (pending / total * 100) : 0;
                    const successPercent = total > 0 ? (success / total * 100) : 0;
                    const ratePercent = (pending + success) > 0 ? (success / (pending + success) * 100) : 0;
                    
                    document.getElementById('pendingBar').style.width = `${pendingPercent}%`;
                    document.getElementById('successBar').style.width = `${successPercent}%`;
                    document.getElementById('rateBar').style.width = `${ratePercent}%`;
                    document.getElementById('successRate').textContent = `${ratePercent.toFixed(1)}%`;
                }
                
                // تحديث حالة المراقبة
                function updateMonitorStatus() {
                    const statusDiv = document.getElementById('monitorStatus');
                    const button = document.getElementById('monitorBtn');
                    
                    if (monitorActive) {
                        statusDiv.className = 'px-4 py-2 rounded-lg bg-green-100 text-green-800';
                        statusDiv.innerHTML = '<i class="fas fa-play-circle mr-2"></i><span>جاري المراقبة</span>';
                        button.innerHTML = '<i class="fas fa-pause mr-2"></i>إيقاف المراقبة';
                        button.className = 'px-4 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600';
                    } else {
                        statusDiv.className = 'px-4 py-2 rounded-lg bg-gray-100 text-gray-800';
                        statusDiv.innerHTML = '<i class="fas fa-pause-circle mr-2"></i><span>مراقبة متوقفة</span>';
                        button.innerHTML = '<i class="fas fa-play mr-2"></i>بدء المراقبة';
                        button.className = 'px-4 py-2 bg-green-500 text-white rounded-lg hover:bg-green-600';
                    }
                }
                
                // تحديث حالة الكوكيز
                function updateCookiesStatus(loaded) {
                    const statusDiv = document.getElementById('cookiesStatus');
                    
                    if (loaded) {
                        statusDiv.className = 'px-4 py-2 rounded-lg bg-green-100 text-green-800';
                        statusDiv.innerHTML = '<i class="fas fa-check-circle mr-2"></i><span>الكوكيز جاهزة</span>';
                    } else {
                        statusDiv.className = 'px-4 py-2 rounded-lg bg-yellow-100 text-yellow-800';
                        statusDiv.innerHTML = '<i class="fas fa-exclamation-triangle mr-2"></i><span>تحتاج كوكيز</span>';
                    }
                }
                
                // وظائف التحكم
                async function checkNow() {
                    const response = await fetch('/api/check-now', { method: 'POST' });
                    const data = await response.json();
                    
                    if (data.success) {
                        alert('تم الفحص بنجاح');
                        loadData();
                    }
                }
                
                async function toggleMonitor() {
                    if (monitorActive) {
                        await fetch('/api/monitor/stop', { method: 'POST' });
                    } else {
                        await fetch('/api/monitor/start', { method: 'POST' });
                    }
                    
                    monitorActive = !monitorActive;
                    updateMonitorStatus();
                }
                
                async function uploadCookies() {
                    const fileInput = document.createElement('input');
                    fileInput.type = 'file';
                    fileInput.accept = '.json';
                    
                    fileInput.onchange = async (e) => {
                        const file = e.target.files[0];
                        const formData = new FormData();
                        formData.append('file', file);
                        
                        try {
                            const response = await fetch('/api/upload-cookies', {
                                method: 'POST',
                                body: formData
                            });
                            
                            const data = await response.json();
                            alert(data.message || data.error);
                            loadData();
                            
                        } catch (error) {
                            alert('خطأ في رفع الملف');
                        }
                    };
                    
                    fileInput.click();
                }
                
                // إضافة حجز
                document.getElementById('reservationForm').onsubmit = async (e) => {
                    e.preventDefault();
                    
                    const seller = document.getElementById('sellerName').value.trim();
                    const buyer = document.getElementById('buyerName').value.trim();
                    const plate = document.getElementById('plateNumber').value.trim();
                    const priority = document.getElementById('priority').value;
                    
                    if (!seller || !buyer || !plate) {
                        alert('يرجى ملء جميع الحقول');
                        return;
                    }
                    
                    if (!/^\d+$/.test(plate)) {
                        alert('رقم اللوحة يجب أن يحتوي على أرقام فقط');
                        return;
                    }
                    
                    try {
                        const response = await fetch('/api/reservations', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                seller_name: seller,
                                buyer_name: buyer,
                                plate_number: plate,
                                priority: parseInt(priority)
                            })
                        });
                        
                        const data = await response.json();
                        alert(data.message);
                        
                        // مسح الحقول
                        document.getElementById('sellerName').value = '';
                        document.getElementById('buyerName').value = '';
                        document.getElementById('plateNumber').value = '';
                        
                        loadData();
                        
                    } catch (error) {
                        alert('خطأ في إضافة الحجز');
                    }
                };
                
                // إلغاء حجز
                async function cancelReservation(reservationId) {
                    if (confirm('هل تريد إلغاء هذا الحجز؟')) {
                        await fetch(`/api/reservations/${reservationId}`, {
                            method: 'DELETE'
                        });
                        
                        alert('تم إلغاء الحجز');
                        loadData();
                    }
                }
                
                // التهيئة
                connectWebSocket();
                loadData();
                
                // تحديث تلقائي كل 30 ثانية
                setInterval(loadData, 30000);
            </script>
        </body>
        </html>
        '''

# ==================== التطبيق الرئيسي ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """إدارة دورة الحياة"""
    # تهيئة النظام
    db = DatabaseManager()
    monitor = PlatformMonitor(db)
    web_interface = WebInterface(app, monitor, db)
    
    # تحميل الكوكيز إذا وجدت
    if os.path.exists("cookies.json"):
        monitor.load_cookies()
    
    yield
    
    # تنظيف عند الإغلاق
    if monitor.is_monitoring:
        monitor.stop_auto_monitor()

app = FastAPI(title="Auto Monitor System", lifespan=lifespan)

# تشغيل التطبيق
if __name__ == "__main__":
    print("\n" + "="*60)
    print("🚀 نظام المراقبة الآلي - الإصدار الكامل")
    print("="*60)
    print("\n✅ الميزات المتوفرة:")
    print("  1. نظام مراقبة كامل - يراقب المنصة تلقائيًا")
    print("  2. نظام حجز تلقائي - يحجز عند فتح النظام")
    print("  3. واجهة تحكم كاملة - داشبورد تفاعلي")
    print("  4. سجلات كاملة - توريد للواجهة الأمامية")
    print("  5. عرض حالة كل نوع من أنواع التحقق")
    print("  6. إدارة الحجوزات - إرسالها للواجهة الأمامية")
    print("\n🌐 افتح المتصفح على: http://localhost:8000")
    print("="*60)
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="warning"
    )
