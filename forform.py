# smart_bypass_with_manual_control.py
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
import curses
import sys
import select
import termios
import tty

# ==================== الإعدادات ====================
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

# ==================== النظام الذكي مع تحكم يدوي ====================
class SmartFormBypassWithManualControl:
    def __init__(self):
        self.session = requests.Session()
        self.target_url = "https://import-dep.mega-sy.com/registration"
        self.base_url = "https://import-dep.mega-sy.com"
        self.cookies_file = "cookies.json"
        self.session_file = "session_state.json"
        self.control_mode = ControlMode.MANUAL  # الوضع الافتراضي: يدوي
        self.setup_logging()
        self.setup_advanced_session()
        self.load_session_state()
        self.form_state = FormState()
        self.bypass_attempts = 0
        self.enabled_fields = set()  # الحقول المفعلة يدوياً
        self.field_activation_history = []
        
    def setup_logging(self):
        """إعداد نظام التسجيل"""
        self.logger = logging.getLogger('SmartBypassManual')
        self.logger.setLevel(logging.INFO)
        
        # معالج للكونسول
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
    
    def analyze_form_state(self, html_content: str) -> FormState:
        """تحليل حالة النموذج بدقة"""
        state = FormState()
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 1. فحص fieldset
        fieldset = soup.find("fieldset", {"id": "formFields"})
        if fieldset:
            state.is_fieldset_disabled = fieldset.has_attr("disabled")
        
        # 2. فحص زر الإرسال
        submit_btn = soup.find("button", {"id": "submitBtn"})
        if submit_btn:
            state.is_submit_disabled = submit_btn.has_attr("disabled")
        
        # 3. فحص رسالة الإغلاق
        closed_note = soup.find("p", {"id": "closedNote"})
        state.has_closed_note = closed_note is not None
        
        # 4. فحص نقطة الفتح
        open_dot = soup.find("span", {"id": "openDot"})
        if open_dot:
            state.is_open_dot_active = "dot-open" in open_dot.get("class", [])
        
        # 5. التحقق من الوقت
        state.time_in_range = self.check_time_range(html_content)
        
        # 6. السعة المتبقية
        remaining_elem = soup.find("div", {"id": "remainingSystem"})
        if remaining_elem:
            try:
                state.remaining_slots = int(re.search(r'\d+', remaining_elem.text).group())
            except:
                state.remaining_slots = 0
        
        # 7. التحقق من التوكنات
        state.tokens_valid = self.validate_tokens(soup)
        
        # 8. فحص CAPTCHA
        state.captcha_present = "cf-turnstile" in html_content.lower()
        
        # 9. استخراج الحقول
        state.form_fields = self.extract_form_fields(html_content)
        
        # 10. تحديد إمكانية الـ bypass
        state.can_bypass = self.can_bypass_form(state)
        
        return state
    
    def extract_form_fields(self, html_content: str) -> Dict[str, Dict]:
        """استخراج جميع حقول النموذج"""
        fields = {}
        soup = BeautifulSoup(html_content, 'html.parser')
        
        form = soup.find("form", {"id": "orderForm"}) or soup.find("form")
        if not form:
            return fields
        
        # جميع حقول input
        inputs = form.find_all("input")
        for inp in inputs:
            name = inp.get("name", "").strip()
            if not name:
                continue
                
            fields[name] = {
                "type": inp.get("type", "text"),
                "id": inp.get("id", ""),
                "value": inp.get("value", ""),
                "maxlength": inp.get("maxlength"),
                "pattern": inp.get("pattern"),
                "required": inp.has_attr("required"),
                "disabled": inp.has_attr("disabled"),
                "readonly": inp.has_attr("readonly"),
                "placeholder": inp.get("placeholder", ""),
                "classes": inp.get("class", [])
            }
        
        # حقول select
        selects = form.find_all("select")
        for select in selects:
            name = select.get("name", "").strip()
            if not name:
                continue
                
            fields[name] = {
                "type": "select",
                "id": select.get("id", ""),
                "options": [
                    {"value": opt.get("value", ""), "text": opt.text.strip()}
                    for opt in select.find_all("option")
                ],
                "disabled": select.has_attr("disabled"),
                "required": select.has_attr("required")
            }
        
        # حقول textarea
        textareas = form.find_all("textarea")
        for textarea in textareas:
            name = textarea.get("name", "").strip()
            if not name:
                continue
                
            fields[name] = {
                "type": "textarea",
                "id": textarea.get("id", ""),
                "value": textarea.text.strip(),
                "disabled": textarea.has_attr("disabled"),
                "required": textarea.has_attr("required"),
                "rows": textarea.get("rows"),
                "cols": textarea.get("cols")
            }
        
        return fields
    
    def manual_field_activation(self, field_names: List[str], enable: bool = True):
        """تفعيل/تعطيل حقول يدوياً"""
        try:
            # 1. جلب الصفحة الحالية
            response = self.session.get(self.target_url, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 2. استخراج التوكنات
            tokens = self.extract_tokens(response.text)
            
            # 3. إنشاء طلب تفعيل يدوي
            activation_data = {
                "_token": tokens.get("_token", ""),
                "started_at": tokens.get("started_at", ""),
                "hmac": tokens.get("hmac", ""),
                "_manual_action": "field_activation",
                "_timestamp": str(int(time.time() * 1000))
            }
            
            # 4. إضافة الحقول المطلوب تفعيلها
            for i, field_name in enumerate(field_names):
                activation_data[f"fields[{i}]"] = field_name
                activation_data[f"enable[{i}]"] = "1" if enable else "0"
            
            # 5. إرسال طلب التفاعيل
            headers = {
                "User-Agent": self.get_rotating_user_agent(),
                "Referer": self.target_url,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "X-Manual-Activation": "true"
            }
            
            # 6. إرسال الطلب
            activation_url = f"{self.base_url}/field/manage"
            response = self.session.post(
                activation_url,
                data=activation_data,
                headers=headers,
                timeout=10
            )
            
            # 7. تسجيل النشاط
            self.field_activation_history.append({
                "timestamp": datetime.now().isoformat(),
                "fields": field_names,
                "action": "enable" if enable else "disable",
                "success": response.status_code == 200
            })
            
            if response.status_code == 200:
                # تحديث الحقول المفعلة
                if enable:
                    self.enabled_fields.update(field_names)
                else:
                    self.enabled_fields.difference_update(field_names)
                
                self.logger.info(f"✅ تم {'تفعيل' if enable else 'تعطيل'} الحقول: {', '.join(field_names)}")
                return True
            else:
                self.logger.warning(f"⚠️ فشل في {'تفعيل' if enable else 'تعطيل'} الحقول")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ خطأ في التحكم اليدوي: {e}")
            return False
    
    def force_enable_all_fields(self):
        """إجبار تفعيل جميع الحقول"""
        try:
            # جلب الصفحة وتحليلها
            response = self.session.get(self.target_url, timeout=10)
            self.form_state = self.analyze_form_state(response.text)
            
            all_fields = list(self.form_state.form_fields.keys())
            
            if not all_fields:
                self.logger.warning("⚠️ لم يتم العثور على حقول")
                return False
            
            # تفعيل جميع الحقول
            success = self.manual_field_activation(all_fields, enable=True)
            
            if success:
                # محاولة تفعيل fieldset إذا كان معطلاً
                if self.form_state.is_fieldset_disabled:
                    self.force_enable_fieldset()
                
                # محاولة تفعيل زر الإرسال إذا كان معطلاً
                if self.form_state.is_submit_disabled:
                    self.force_enable_submit_button()
            
            return success
            
        except Exception as e:
            self.logger.error(f"❌ خطأ في تفعيل جميع الحقول: {e}")
            return False
    
    def force_enable_fieldset(self):
        """إجبار تفعيل fieldset"""
        try:
            # استخدام JavaScript injection عبر POST request
            js_payload = """
            <script>
            document.getElementById('formFields').disabled = false;
            document.getElementById('formFields').style.opacity = '1';
            </script>
            """
            
            activation_data = {
                "_js_payload": js_payload,
                "_action": "enable_fieldset",
                "_timestamp": str(int(time.time() * 1000))
            }
            
            response = self.session.post(
                f"{self.base_url}/js-execute",
                data=activation_data,
                timeout=10
            )
            
            if response.status_code == 200:
                self.form_state.is_fieldset_disabled = False
                self.logger.info("✅ تم تفعيل fieldset")
                return True
            
            return False
            
        except Exception as e:
            self.logger.debug(f"⚠️ فشل تفعيل fieldset: {e}")
            return False
    
    def force_enable_submit_button(self):
        """إجبار تفعيل زر الإرسال"""
        try:
            # إزالة attribute disabled من زر الإرسال
            activation_data = {
                "_action": "enable_submit",
                "_element": "submitBtn",
                "_timestamp": str(int(time.time() * 1000))
            }
            
            response = self.session.post(
                f"{self.base_url}/element/modify",
                data=activation_data,
                timeout=10
            )
            
            if response.status_code == 200:
                self.form_state.is_submit_disabled = False
                self.logger.info("✅ تم تفعيل زر الإرسال")
                return True
            
            return False
            
        except Exception as e:
            self.logger.debug(f"⚠️ فشل تفعيل زر الإرسال: {e}")
            return False
    
    def selective_field_control(self):
        """واجهة التحكم الانتقائي في الحقول"""
        print("\n🎛️  التحكم الانتقائي في الحقول")
        print("=" * 50)
        
        # عرض الحقول المتاحة
        response = self.session.get(self.target_url, timeout=10)
        self.form_state = self.analyze_form_state(response.text)
        
        if not self.form_state.form_fields:
            print("❌ لم يتم العثور على حقول")
            return
        
        print("\n📋 الحقول المتاحة:")
        for i, (field_name, field_info) in enumerate(self.form_state.form_fields.items(), 1):
            status = "✅ مفعل" if field_name in self.enabled_fields else "❌ معطل"
            disabled = " (معطل)" if field_info.get("disabled") else ""
            print(f"{i}. {field_name}: {status}{disabled}")
        
        # خيارات التحكم
        print("\n🔧 خيارات التحكم:")
        print("1. تفعيل حقل محدد")
        print("2. تعطيل حقل محدد")
        print("3. تفعيل جميع الحقول")
        print("4. تعطيل جميع الحقول")
        print("5. العودة")
        
        choice = input("\n👉 اختر الخيار: ").strip()
        
        if choice == "1":
            self.activate_specific_field()
        elif choice == "2":
            self.deactivate_specific_field()
        elif choice == "3":
            self.force_enable_all_fields()
        elif choice == "4":
            self.disable_all_fields()
        elif choice == "5":
            return
        else:
            print("❌ خيار غير صحيح")
    
    def activate_specific_field(self):
        """تفعيل حقل محدد"""
        field_name = input("👉 أدخل اسم الحقل المراد تفعيليه: ").strip()
        
        if field_name in self.form_state.form_fields:
            success = self.manual_field_activation([field_name], enable=True)
            if success:
                print(f"✅ تم تفعيل الحقل '{field_name}'")
            else:
                print(f"❌ فشل في تفعيل الحقل '{field_name}'")
        else:
            print(f"❌ الحقل '{field_name}' غير موجود")
    
    def deactivate_specific_field(self):
        """تعطيل حقل محدد"""
        field_name = input("👉 أدخل اسم الحقل المراد تعطيله: ").strip()
        
        if field_name in self.form_state.form_fields:
            success = self.manual_field_activation([field_name], enable=False)
            if success:
                print(f"✅ تم تعطيل الحقل '{field_name}'")
            else:
                print(f"❌ فشل في تعطيل الحقل '{field_name}'")
        else:
            print(f"❌ الحقل '{field_name}' غير موجود")
    
    def disable_all_fields(self):
        """تعطيل جميع الحقول"""
        confirm = input("⚠️  هل أنت متأكد من تعطيل جميع الحقول؟ (نعم/لا): ").strip().lower()
        
        if confirm == "نعم":
            all_fields = list(self.form_state.form_fields.keys())
            if all_fields:
                success = self.manual_field_activation(all_fields, enable=False)
                if success:
                    print("✅ تم تعطيل جميع الحقول")
                else:
                    print("❌ فشل في تعطيل جميع الحقول")
    
    def check_time_range(self, html_content: str) -> bool:
        """التحقق من نطاق الوقت"""
        try:
            current_time = datetime.now().time()
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # البحث عن وقت العمل اليوم
            time_range_elem = soup.find("span", {"id": "timeRange"})
            if time_range_elem:
                time_text = time_range_elem.text.strip()
                if "–" in time_text:
                    start_str, end_str = time_text.split("–")
                    start_time = datetime.strptime(start_str.strip(), "%H:%M").time()
                    end_time = datetime.strptime(end_str.strip(), "%H:%M").time()
                    
                    # في حالة نهاية الوقت بعد منتصف الليل
                    if end_time < start_time:
                        if current_time >= start_time or current_time <= end_time:
                            return True
                    else:
                        if start_time <= current_time <= end_time:
                            return True
            
            return False
        except:
            return False
    
    def validate_tokens(self, soup) -> bool:
        """التحقق من صحة التوكنات"""
        try:
            # الحصول على التوكنات
            token_input = soup.find("input", {"name": "_token"})
            started_at_input = soup.find("input", {"name": "started_at"})
            hmac_input = soup.find("input", {"name": "hmac"})
            
            # التحقق من وجودها
            if not all([token_input, started_at_input, hmac_input]):
                return False
            
            # التحقق من قيمها
            token_val = token_input.get("value", "")
            started_val = started_at_input.get("value", "")
            hmac_val = hmac_input.get("value", "")
            
            if not all([token_val, started_val, hmac_val]):
                return False
            
            # التحقق من صحة timestamp (started_at)
            try:
                started_time = int(started_val)
                current_time = int(time.time() * 1000)
                # إذا كان الفرق أقل من ساعة (3600000 مللي ثانية)
                if abs(current_time - started_time) > 3600000:
                    return False
            except:
                return False
            
            return True
        except:
            return False
    
    def extract_tokens(self, html_content: str) -> Dict[str, str]:
        """استخراج التوكنات من HTML"""
        tokens = {}
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # توكنات hidden inputs
        hidden_inputs = soup.find_all("input", {"type": "hidden"})
        for inp in hidden_inputs:
            name = inp.get("name", "").strip()
            value = inp.get("value", "").strip()
            if name and value:
                tokens[name] = value
        
        # تجاهل honeypot fields
        honeypot_fields = ["_hp", "website", "company", "topic"]
        for field in honeypot_fields:
            if field in tokens:
                del tokens[field]
        
        return tokens
    
    def can_bypass_form(self, state: FormState) -> bool:
        """تحديد إمكانية كسر التعطيل"""
        # في الوضع اليدوي، يمكن تجاوز بعض القيود
        if self.control_mode == ControlMode.MANUAL:
            # فقط التحقق من التوكنات
            if not state.tokens_valid:
                return False
            return True
        
        # في الوضع التلقائي، التحقق من كل شيء
        if not state.is_open_dot_active and state.has_closed_note:
            return False
        
        if not state.time_in_range:
            return False
        
        if state.remaining_slots <= 0:
            return False
        
        if not state.tokens_valid:
            return False
        
        return True
    
    def manual_submit(self, reservation_data: Dict):
        """إرسال يدوي مع تحكم كامل"""
        try:
            print("\n🎯 بدء الإرسال اليدوي")
            
            # 1. جلب أحدث البيانات
            response = self.session.get(self.target_url, timeout=10)
            self.form_state = self.analyze_form_state(response.text)
            
            # 2. عرض حالة الحقول
            print("\n📊 حالة الحقول:")
            for field_name, field_info in self.form_state.form_fields.items():
                if field_name in reservation_data or field_name in ["_token", "started_at", "hmac"]:
                    status = "✅ سيملأ" if not field_info.get("disabled") else "⚠️ معطل"
                    print(f"  • {field_name}: {status}")
            
            # 3. سؤال المستخدم عن تفعيل الحقول المعطلة
            disabled_fields = [
                name for name, info in self.form_state.form_fields.items()
                if info.get("disabled") and name in reservation_data
            ]
            
            if disabled_fields:
                print(f"\n⚠️  الحقول المعطلة: {', '.join(disabled_fields)}")
                activate = input("هل تريد تفعيلها قبل الإرسال؟ (نعم/لا): ").strip().lower()
                
                if activate == "نعم":
                    success = self.manual_field_activation(disabled_fields, enable=True)
                    if not success:
                        force = input("فشل التفاعيل، هل تريد الإرسال رغم ذلك؟ (نعم/لا): ").strip().lower()
                        if force != "نعم":
                            return False, "تم الإلغاء"
            
            # 4. تحضير البيانات
            tokens = self.extract_tokens(response.text)
            submission_data = {
                "_token": tokens.get("_token", ""),
                "started_at": tokens.get("started_at", ""),
                "hmac": tokens.get("hmac", ""),
            }
            
            # 5. إضافة بيانات الحجز
            for key, value in reservation_data.items():
                if value:
                    submission_data[key] = value
            
            # 6. خيارات إضافية
            print("\n⚙️  خيارات الإرسال:")
            print("1. إرسال عادي")
            print("2. إرسال مع تأخير عشوائي")
            print("3. إرسال مع User-Agent خاص")
            
            send_choice = input("👉 اختر طريقة الإرسال: ").strip()
            
            if send_choice == "2":
                delay = random.uniform(2, 5)
                print(f"⏱️  تأخير {delay:.1f} ثواني...")
                time.sleep(delay)
            elif send_choice == "3":
                custom_ua = input("أدخل User-Agent (أو اترك فارغاً للافتراضي): ").strip()
                if custom_ua:
                    self.session.headers["User-Agent"] = custom_ua
            
            # 7. الإرسال
            headers = {
                "User-Agent": self.session.headers["User-Agent"],
                "Referer": self.target_url,
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "X-Manual-Submit": "true"
            }
            
            print(f"\n📤 جاري إرسال بيانات {reservation_data.get('plate_number', '')}...")
            
            response = self.session.post(
                self.target_url,
                data=submission_data,
                headers=headers,
                timeout=15
            )
            
            # 8. تحليل النتيجة
            if response.status_code in [200, 302, 303]:
                success_indicators = ["success", "ناجح", "تم", "شكرا"]
                response_lower = response.text.lower()
                
                if any(indicator in response_lower for indicator in success_indicators):
                    print(f"✅ تم إرسال حجز {reservation_data.get('plate_number')} بنجاح!")
                    return True, "تم الإرسال بنجاح"
                else:
                    print("⚠️  استجابة غير واضحة")
                    return False, "استجابة غير واضحة"
            else:
                print(f"❌ فشل الإرسال (كود: {response.status_code})")
                return False, f"كود خطأ: {response.status_code}"
            
        except Exception as e:
            print(f"❌ خطأ في الإرسال اليدوي: {e}")
            return False, str(e)
    
    def real_time_control_panel(self):
        """لوحة تحكم في الوقت الحقيقي"""
        print("\n" + "="*60)
        print("🎮 لوحة التحكم اليدوي - الوقت الحقيقي")
        print("="*60)
        
        while True:
            try:
                # تحديث البيانات
                response = self.session.get(self.target_url, timeout=5)
                self.form_state = self.analyze_form_state(response.text)
                
                # عرض المعلومات
                os.system('cls' if os.name == 'nt' else 'clear')
                print("\n" + "="*60)
                print("🎮 لوحة التحكم اليدوي")
                print("="*60)
                print(f"\n📊 حالة النظام:")
                print(f"  • النقطة: {'🟢 أخضر' if self.form_state.is_open_dot_active else '🔴 أحمر'}")
                print(f"  • وقت العمل: {'✅ نعم' if self.form_state.time_in_range else '❌ لا'}")
                print(f"  • السعة: {self.form_state.remaining_slots}")
                print(f"  • Fieldset: {'✅ مفعل' if not self.form_state.is_fieldset_disabled else '❌ معطل'}")
                print(f"  • زر الإرسال: {'✅ مفعل' if not self.form_state.is_submit_disabled else '❌ معطل'}")
                print(f"  • الحقول المفعلة: {len(self.enabled_fields)}")
                
                print(f"\n🎛️  خيارات التحكم:")
                print("  1. تفعيل جميع الحقول")
                print("  2. تعطيل جميع الحقول")
                print("  3. تفعيل fieldset فقط")
                print("  4. تفعيل زر الإرسال فقط")
                print("  5. التحكم الانتقائي في الحقول")
                print("  6. تحديث البيانات")
                print("  7. الخروج")
                
                # قراءة الإدخال بدون انتظار Enter
                try:
                    import msvcrt
                    print("\n👉 اضغط على رقم الخيار (1-7): ", end='', flush=True)
                    choice = msvcrt.getch().decode('utf-8')
                except:
                    choice = input("\n👉 اختر الخيار (1-7): ").strip()
                
                if choice == "1":
                    self.force_enable_all_fields()
                    input("\nاضغط Enter للمتابعة...")
                elif choice == "2":
                    self.disable_all_fields()
                    input("\nاضغط Enter للمتابعة...")
                elif choice == "3":
                    self.force_enable_fieldset()
                    input("\nاضغط Enter للمتابعة...")
                elif choice == "4":
                    self.force_enable_submit_button()
                    input("\nاضغط Enter للمتابعة...")
                elif choice == "5":
                    self.selective_field_control()
                elif choice == "6":
                    continue
                elif choice == "7":
                    print("\n👋 تم الخروج من لوحة التحكم")
                    break
                else:
                    print("❌ خيار غير صحيح")
                    time.sleep(1)
                
            except KeyboardInterrupt:
                print("\n\n⏹️ تم إيقاف لوحة التحكم")
                break
            except Exception as e:
                print(f"\n❌ خطأ: {e}")
                time.sleep(2)
    
    def interactive_mode(self, reservation_data: Dict):
        """وضع تفاعلي مع تحكم يدوي كامل"""
        print("\n" + "="*60)
        print("🤖 الوضع التفاعلي مع تحكم يدوي")
        print("="*60)
        
        while True:
            print("\n📋 القائمة الرئيسية:")
            print("1. عرض حالة النظام الحالية")
            print("2. التحكم في الحقول (تفعيل/تعطيل)")
            print("3. إرسال الحجز يدوياً")
            print("4. لوحة التحكم في الوقت الحقيقي")
            print("5. تغيير وضع التشغيل")
            print("6. حفظ الحالة والخروج")
            
            choice = input("\n👉 اختر الخيار (1-6): ").strip()
            
            if choice == "1":
                self.show_current_status()
            elif choice == "2":
                self.selective_field_control()
            elif choice == "3":
                success, message = self.manual_submit(reservation_data)
                if success:
                    print(f"\n✅ {message}")
                else:
                    print(f"\n❌ {message}")
            elif choice == "4":
                self.real_time_control_panel()
            elif choice == "5":
                self.change_control_mode()
            elif choice == "6":
                self.save_session_state()
                print("\n💾 تم حفظ الحالة")
                break
            else:
                print("❌ خيار غير صحيح")
    
    def show_current_status(self):
        """عرض الحالة الحالية"""
        response = self.session.get(self.target_url, timeout=10)
        self.form_state = self.analyze_form_state(response.text)
        
        print("\n📊 الحالة الحالية:")
        print(f"  • URL: {self.target_url}")
        print(f"  • النقطة: {'🟢 أخضر' if self.form_state.is_open_dot_active else '🔴 أحمر'}")
        print(f"  • وقت العمل: {'✅ في النطاق' if self.form_state.time_in_range else '❌ خارج النطاق'}")
        print(f"  • السعة المتبقية: {self.form_state.remaining_slots}")
        print(f"  • Fieldset: {'✅ مفعل' if not self.form_state.is_fieldset_disabled else '❌ معطل'}")
        print(f"  • زر الإرسال: {'✅ مفعل' if not self.form_state.is_submit_disabled else '❌ معطل'}")
        print(f"  • التوكنات: {'✅ صالحة' if self.form_state.tokens_valid else '❌ غير صالحة'}")
        print(f"  • CAPTCHA: {'⚠️ موجود' if self.form_state.captcha_present else '✅ غير موجود'}")
        print(f"  • عدد الحقول: {len(self.form_state.form_fields)}")
        print(f"  • الحقول المفعلة يدوياً: {len(self.enabled_fields)}")
        print(f"  • وضع التشغيل: {self.control_mode.value}")
        
        if self.enabled_fields:
            print(f"  • قائمة الحقول المفعلة: {', '.join(self.enabled_fields)}")
    
    def change_control_mode(self):
        """تغيير وضع التشغيل"""
        print("\n🔄 تغيير وضع التشغيل:")
        print("1. تلقائي (Auto)")
        print("2. يدوي (Manual)")
        print("3. شبه تلقائي (Semi-Auto)")
        
        choice = input("👉 اختر الوضع: ").strip()
        
        if choice == "1":
            self.control_mode = ControlMode.AUTO
            print("✅ تم التغيير إلى الوضع التلقائي")
        elif choice == "2":
            self.control_mode = ControlMode.MANUAL
            print("✅ تم التغيير إلى الوضع اليدوي")
        elif choice == "3":
            self.control_mode = ControlMode.SEMI_AUTO
            print("✅ تم التغيير إلى الوضع شبه التلقائي")
        else:
            print("❌ خيار غير صحيح")

# ==================== واجهة المستخدم الرئيسية ====================
def main():
    print("\n" + "="*60)
    print("🎮 نظام التحكم اليدوي في كسر التعطيل")
    print("="*60)
    
    bypass = SmartFormBypassWithManualControl()
    
    # اختبار الاتصال
    print("\n🔍 جاري اختبار الاتصال...")
    try:
        response = bypass.session.get(bypass.target_url, timeout=10)
        if response.status_code == 200:
            print("✅ الاتصال ناجح")
        else:
            print(f"⚠️  كود الاستجابة: {response.status_code}")
    except Exception as e:
        print(f"❌ فشل الاتصال: {e}")
        return
    
    # طلب بيانات الحجز
    print("\n📝 أدخل بيانات الحجز:")
    seller = input("  اسم البائع: ").strip()
    buyer = input("  اسم المشتري: ").strip()
    plate = input("  رقم اللوحة: ").strip()
    phone = input("  رقم الهاتف (اختياري): ").strip()
    email = input("  البريد الإلكتروني (اختياري): ").strip()
    
    if not all([seller, buyer, plate]):
        print("❌ جميع الحقول المطلوبة مطلوبة!")
        return
    
    reservation_data = {
        "seller_name": seller,
        "buyer_name": buyer,
        "plate_number": plate,
        "phone": phone if phone else None,
        "email": email if email else None
    }
    
    print("\n🎛️  خيارات التشغيل:")
    print("1. الوضع التفاعلي مع تحكم يدوي كامل")
    print("2. تفعيل جميع الحقول ثم الإرسال")
    print("3. التحكم الانتقائي في الحقول فقط")
    print("4. اختبار النظام فقط")
    
    try:
        choice = input("\n👉 اختر الخيار (1-4): ").strip()
        
        if choice == "1":
            print("\n🚀 بدء الوضع التفاعلي...")
            bypass.interactive_mode(reservation_data)
        
        elif choice == "2":
            print("\n⚡ تفعيل جميع الحقول ثم الإرسال...")
            
            # تفعيل جميع الحقول
            if bypass.force_enable_all_fields():
                print("✅ تم تفعيل جميع الحقول")
                
                # تأكيد الإرسال
                confirm = input("هل تريد الإرسال الآن؟ (نعم/لا): ").strip().lower()
                if confirm == "نعم":
                    success, message = bypass.manual_submit(reservation_data)
                    if success:
                        print(f"\n✅ {message}")
                    else:
                        print(f"\n❌ {message}")
                else:
                    print("\n⏹️ تم الإلغاء")
            else:
                print("❌ فشل في تفعيل الحقول")
        
        elif choice == "3":
            print("\n🔧 التحكم الانتقائي في الحقول...")
            bypass.selective_field_control()
        
        elif choice == "4":
            print("\n🔍 اختبار النظام...")
            bypass.show_current_status()
            
            # عرض الحقول المفصلة
            response = bypass.session.get(bypass.target_url, timeout=10)
            fields = bypass.extract_form_fields(response.text)
            
            if fields:
                print(f"\n📋 تفاصيل الحقول ({len(fields)} حقل):")
                for name, info in fields.items():
                    status = "معطل" if info.get("disabled") else "مفعل"
                    print(f"  • {name}: {status} ({info.get('type', 'text')})")
        
        else:
            print("❌ خيار غير صحيح")
    
    except KeyboardInterrupt:
        print("\n\n⏹️ تم إيقاف النظام")
    except Exception as e:
        print(f"\n❌ خطأ غير متوقع: {e}")

# ==================== التشغيل ====================
if __name__ == "__main__":
    # تحذير بشأن الكوكيز
    if not os.path.exists("cookies.json"):
        print("\n⚠️  تحذير: ملف cookies.json غير موجود")
        print("   يجب أن يكون لديك كوكيز صالحة للوصول للنظام")
        print("   احصل عليها من المتصفح بعد تسجيل الدخول")
    
    main()