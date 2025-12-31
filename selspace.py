import requests
import json
import os
import time
import random
import threading
import logging
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from typing import Dict, Optional, List, Tuple, Any
from dataclasses import dataclass, asdict, field
from enum import Enum
import re
import uuid

# استيراد Selenium مع معالجة الأخطاء
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
    
    # محاولة استيراد ChromeDriver Manager
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        WEBDRIVER_MANAGER_AVAILABLE = True
    except ImportError:
        WEBDRIVER_MANAGER_AVAILABLE = False
    
    SELENIUM_AVAILABLE = True
    print("✅ Selenium متاح")
except ImportError as e:
    print(f"⚠️  Selenium غير مثبت: {e}")
    print("💡 قم بتشغيل: pip install selenium webdriver-manager")
    SELENIUM_AVAILABLE = False

# ==================== إعدادات Codespace ====================

class BypassStatus(Enum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    READY = "ready"
    NEED_OPEN = "need_open"
    NEED_TIME = "need_time"
    CAPTCHA_BLOCKED = "captcha_blocked"
    ERROR = "error"

class ControlMode(Enum):
    AUTO = "auto"
    MANUAL = "manual"
    SEMI_AUTO = "semi_auto"

@dataclass
class FormState:
    is_fieldset_disabled: bool = True
    is_submit_disabled: bool = True
    has_closed_note: bool = True
    is_open_dot_active: bool = False
    time_in_range: bool = False
    remaining_slots: int = 0
    tokens_valid: bool = False
    dwell_time_passed: bool = False
    captcha_present: bool = False
    can_bypass: bool = False
    form_fields: Dict[str, Any] = field(default_factory=dict)

# ==================== النظام المعدل للعمل في Codespace ====================

class SmartFormBypassWithManualControl:
    def __init__(self):
        self.session = requests.Session()
        self.target_url = "https://import-dep.mega-sy.com/registration"
        self.base_url = "https://import-dep.mega-sy.com"
        self.cookies_file = "cookies.json"
        self.session_file = "session_state.json"
        self.control_mode = ControlMode.MANUAL
        self.setup_logging()
        self.setup_advanced_session()
        self.load_session_state()
        
        # إعداد Selenium للعمل في Codespace
        self.selenium_driver = None
        self.selenium_initialized = False
        
        self.form_state = FormState()
        self.bypass_attempts = 0
        self.enabled_fields = set()
        self.field_activation_history = []
        
    def setup_logging(self):
        """إعداد نظام التسجيل"""
        self.logger = logging.getLogger('SmartBypassManual')
        self.logger.setLevel(logging.INFO)
        
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)s: %(message)s',
            datefmt='%H:%M:%S'
        )
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
    
    def setup_advanced_session(self):
        """إعداد جلسة متقدمة"""
        self.session.headers.update({
            "User-Agent": self.get_rotating_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Cache-Control": "max-age=0",
            "DNT": "1",
        })
    
    def get_rotating_user_agent(self):
        """تناوب User-Agent"""
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        ]
        return random.choice(agents)
    
    def load_session_state(self):
        """تحميل حالة الجلسة"""
        if os.path.exists(self.session_file):
            try:
                with open(self.session_file, 'r') as f:
                    self.session_state = json.load(f)
                self.logger.info("📂 تم تحميل حالة الجلسة")
            except:
                self.session_state = {}
        else:
            self.session_state = {}
    
    def save_session_state(self):
        """حفظ حالة الجلسة"""
        try:
            with open(self.session_file, 'w') as f:
                json.dump(self.session_state, f, indent=2)
        except:
            pass
    
    def init_selenium_for_codespace(self):
        """تهيئة Selenium خصيصاً للعمل في Codespace"""
        if not SELENIUM_AVAILABLE:
            self.logger.warning("⚠️  مكتبة Selenium غير مثبتة")
            return None
        
        try:
            self.logger.info("🚀 تهيئة Selenium للعمل في Codespace...")
            
            # خيارات Chrome للعمل في بيئة Codespace/Linux
            chrome_options = Options()
            
            # إعدادات ضرورية للعمل في بيئة headless
            chrome_options.add_argument("--headless")  # وضع بدون واجهة
            chrome_options.add_argument("--no-sandbox")  # مهم لبيئة Docker/Codespace
            chrome_options.add_argument("--disable-dev-shm-usage")  # مهم لبيئة محدودة الذاكرة
            chrome_options.add_argument("--disable-gpu")  # تعطيل GPU في بيئة headless
            chrome_options.add_argument("--window-size=1920,1080")
            
            # إضافة User-Agent
            chrome_options.add_argument(f"user-agent={self.get_rotating_user_agent()}")
            
            # خيارات لمكافحة الكشف
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            
            # محاولات متعددة لتهيئة المتصفح
            
            # المحاولة 1: استخدام Chrome الموجود في النظام
            try:
                self.logger.info("🔧 محاولة استخدام Chrome/Chromium الموجود...")
                driver = webdriver.Chrome(options=chrome_options)
                self.selenium_driver = driver
                self.selenium_initialized = True
                self.logger.info("✅ تم تهيئة Selenium باستخدام Chrome الموجود")
                return driver
            except Exception as e1:
                self.logger.warning(f"⚠️  فشل المحاولة 1: {e1}")
            
            # المحاولة 2: استخدام ChromeDriver Manager
            if WEBDRIVER_MANAGER_AVAILABLE:
                try:
                    self.logger.info("🔧 محاولة استخدام ChromeDriver Manager...")
                    service = Service(ChromeDriverManager().install())
                    driver = webdriver.Chrome(service=service, options=chrome_options)
                    self.selenium_driver = driver
                    self.selenium_initialized = True
                    self.logger.info("✅ تم تهيئة Selenium باستخدام ChromeDriver Manager")
                    return driver
                except Exception as e2:
                    self.logger.warning(f"⚠️  فشل المحاولة 2: {e2}")
            
            # المحاولة 3: استخدام المسار المباشر
            try:
                self.logger.info("🔧 محاولة استخدام المسار المباشر...")
                # محاولة مواقع Chromium الشائعة في Linux
                chrome_locations = [
                    "/usr/bin/chromium-browser",
                    "/usr/bin/chromium",
                    "/usr/bin/google-chrome",
                    "/usr/local/bin/chromedriver"
                ]
                
                for location in chrome_locations:
                    if os.path.exists(location):
                        self.logger.info(f"🔍 وجدت Chrome في: {location}")
                        chrome_options.binary_location = location
                        break
                
                driver = webdriver.Chrome(options=chrome_options)
                self.selenium_driver = driver
                self.selenium_initialized = True
                self.logger.info("✅ تم تهيئة Selenium باستخدام المسار المباشر")
                return driver
            except Exception as e3:
                self.logger.error(f"❌ فشل جميع محاولات تهيئة Selenium: {e3}")
                return None
                
        except Exception as e:
            self.logger.error(f"❌ خطأ عام في تهيئة Selenium: {e}")
            return None
    
    def get_selenium_session(self, force_new=False):
        """الحصول على جلسة Selenium"""
        if force_new:
            self.close_selenium()
        
        if not self.selenium_initialized or not self.selenium_driver:
            return self.init_selenium_for_codespace()
        
        return self.selenium_driver
    
    def close_selenium(self):
        """إغلاق جلسة Selenium"""
        if self.selenium_driver:
            try:
                self.selenium_driver.quit()
                self.logger.info("👋 تم إغلاق جلسة Selenium")
            except:
                pass
            finally:
                self.selenium_driver = None
                self.selenium_initialized = False
    
    # إصلاح الدالة التي تحتوي على الخطأ
    def activate_field_with_selenium(self, field_name: str) -> bool:
        """تفعيل حقل باستخدام Selenium"""
        try:
            driver = self.get_selenium_session()
            if not driver:
                return False
            
            driver.get(self.target_url)
            time.sleep(2)
            
            # البحث عن الحقل بطرق مختلفة - الإصلاح هنا
            selectors = [
                f'[name="{field_name}"]',
                f'#{field_name}',  # تم التصحيح
                f'input[name="{field_name}"]',
                f'select[name="{field_name}"]',
                f'textarea[name="{field_name}"]'
            ]
            
            field_element = None
            for selector in selectors:
                try:
                    field_element = driver.find_element(By.CSS_SELECTOR, selector)
                    if field_element:
                        break
                except:
                    continue
            
            if field_element:
                # تفعيل الحقل باستخدام JavaScript
                js_script = f"""
                var field = document.querySelector('[name="{field_name}"]');
                if (field) {{
                    field.disabled = false;
                    field.readOnly = false;
                    field.style.opacity = '1';
                    field.style.backgroundColor = '#ffffff';
                    return true;
                }}
                return false;
                """
                
                result = driver.execute_script(js_script)
                if result:
                    self.enabled_fields.add(field_name)
                    return True
            
            return False
            
        except Exception as e:
            self.logger.debug(f"⚠️ فشل تفعيل الحقل بـ Selenium: {e}")
            return False
    
    def analyze_form_with_selenium(self):
        """تحليل النموذج باستخدام Selenium"""
        if not SELENIUM_AVAILABLE:
            self.logger.warning("⚠️  Selenium غير متاح، استخدام الطريقة العادية")
            return None
        
        driver = self.get_selenium_session()
        if not driver:
            return None
        
        try:
            driver.get(self.target_url)
            time.sleep(2)
            
            html_content = driver.page_source
            self.form_state = self.analyze_form_state(html_content)
            
            self.logger.info("✅ تم تحليل النموذج باستخدام Selenium")
            return html_content
            
        except Exception as e:
            self.logger.error(f"❌ خطأ في تحليل Selenium: {e}")
            return None
    
    # باقي الدوال كما هي...
    def analyze_form_state(self, html_content: str) -> FormState:
        """تحليل حالة النموذج"""
        state = FormState()
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # تنفيذ التحليل...
        return state
    
    def manual_field_activation(self, field_names: List[str], enable: bool = True):
        """تفعيل/تعطيل حقول يدوياً"""
        # تنفيذ الوظيفة...
        pass
    
    def interactive_mode(self, reservation_data: Dict):
        """وضع تفاعلي"""
        print("\n" + "="*60)
        print("🤖 الوضع التفاعلي مع تحكم يدوي")
        print("="*60)
        
        while True:
            print("\n📋 القائمة الرئيسية:")
            print("1. اختبار اتصال Selenium")
            print("2. عرض حالة النظام")
            print("3. الخروج")
            
            choice = input("\n👉 اختر الخيار: ").strip()
            
            if choice == "1":
                self.test_selenium_connection()
            elif choice == "2":
                self.show_current_status()
            elif choice == "3":
                break
    
    def test_selenium_connection(self):
        """اختبار اتصال Selenium"""
        print("\n🔧 اختبار اتصال Selenium...")
        
        if not SELENIUM_AVAILABLE:
            print("❌ Selenium غير مثبت")
            return
        
        driver = self.get_selenium_session()
        if not driver:
            print("❌ فشل في تهيئة Selenium")
            return
        
        try:
            # اختبار بسيط
            driver.get("https://www.google.com")
            print(f"✅ تم الاتصال بـ Google: {driver.title}")
            
            # اختبار الموقع المستهدف
            driver.get(self.target_url)
            print(f"✅ تم الوصول إلى الموقع المستهدف")
            print(f"📄 العنوان: {driver.title}")
            print(f"🌐 الرابط: {driver.current_url}")
            
        except Exception as e:
            print(f"❌ خطأ في اختبار Selenium: {e}")
    
    def show_current_status(self):
        """عرض الحالة الحالية"""
        print("\n📊 الحالة الحالية:")
        print(f"  • Selenium متاح: {'✅ نعم' if SELENIUM_AVAILABLE else '❌ لا'}")
        print(f"  • Selenium مهيأ: {'✅ نعم' if self.selenium_initialized else '❌ لا'}")
        
        if self.selenium_initialized:
            try:
                driver = self.selenium_driver
                print(f"  • جلسة Selenium نشطة: {'✅ نعم' if driver else '❌ لا'}")
            except:
                print("  • حالة Selenium: غير معروف")

# ==================== التشغيل الرئيسي ====================

def main():
    print("\n" + "="*60)
    print("🎮 نظام التحكم اليدوي - نسخة Codespace")
    print("="*60)
    
    # عرض معلومات النظام
    import platform
    print(f"\n📋 معلومات النظام:")
    print(f"  • النظام: {platform.system()} {platform.release()}")
    print(f"  • بايثون: {platform.python_version()}")
    
    # تحقق من تثبيت الحزم
    print("\n🔍 التحقق من الحزم المثبتة...")
    import pkg_resources
    
    packages = ['selenium', 'requests', 'beautifulsoup4']
    for pkg in packages:
        try:
            version = pkg_resources.get_distribution(pkg).version
            print(f"  ✅ {pkg}: {version}")
        except:
            print(f"  ❌ {pkg}: غير مثبت")
    
    bypass = SmartFormBypassWithManualControl()
    
    # اختبار الاتصال
    print("\n🔍 جاري اختبار الاتصال...")
    try:
        response = bypass.session.get(bypass.target_url, timeout=10)
        if response.status_code == 200:
            print("✅ الاتصال ناجح")
            print(f"📄 عنوان الصفحة: {BeautifulSoup(response.text, 'html.parser').title.string}")
        else:
            print(f"⚠️  كود الاستجابة: {response.status_code}")
    except Exception as e:
        print(f"❌ فشل الاتصال: {e}")
        print("💡 تأكد من اتصالك بالإنترنت والصفحة متاحة")
    
    # بدء الوضع التفاعلي
    reservation_data = {
        "seller_name": "رامي علي العمر",
        "buyer_name": "احمد عابدين اغا بن مصطفى",
        "plate_number": "5138939"
    }
    
    bypass.interactive_mode(reservation_data)

if __name__ == "__main__":
    main()
