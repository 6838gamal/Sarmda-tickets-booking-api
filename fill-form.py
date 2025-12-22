import requests
import json
import os
import time
from datetime import datetime
from bs4 import BeautifulSoup

# ================== الإعدادات ==================
TARGET_URL = "https://import-dep.mega-sy.com/registration"
COOKIES_FILE = "cookies.json"
OUTPUT_DIR = "pages"
POLL_INTERVAL = 5

FORM_DATA = {
    "seller_name": "اسم البائع التجريبي",
    "buyer_name": "اسم المشتري التجريبي",
    "plate_number": "123456"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; Chrome)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en;q=0.9",
    "Connection": "keep-alive",
}

LOGIN_INDICATORS = ["/login", "accounts.google.com"]
# ==============================================


def banner():
    print("\n" + "=" * 65)
    print("🧠 SMART SESSION WATCHER – Auto Form Filler")
    print("=" * 65)


def wait_for_cookies():
    banner()
    print("🚫 لا يمكن المتابعة بدون Cookies")
    print("\n🔗 افتح الرابط وسجل الدخول من Chrome:")
    print(TARGET_URL)
    print("\nثم صدّر Cookies بصيغة JSON باسم cookies.json")
    print("-" * 65)

    while not os.path.exists(COOKIES_FILE):
        time.sleep(2)

    print("✅ cookies.json جاهز")


def load_cookies(session):
    with open(COOKIES_FILE, "r", encoding="utf-8") as f:
        cookies = json.load(f)

    session.cookies.clear()
    for c in cookies:
        session.cookies.set(
            name=c["name"],
            value=c["value"],
            domain=c.get("domain"),
            path=c.get("path", "/"),
        )
    print("🍪 الجلسة محملة بالكوكيز")


def is_logged_out(url):
    return any(x in url for x in LOGIN_INDICATORS)


def save_page(html):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = int(time.time())
    path = f"{OUTPUT_DIR}/page_{ts}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def parse_form(html):
    soup = BeautifulSoup(html, "html.parser")

    form = soup.find("form", {"id": "orderForm"})
    if not form:
        return None, "❌ الفورم غير موجود"

    disabled = soup.find("fieldset", {"id": "formFields"}).has_attr("disabled")
    if disabled:
        return None, "⏳ الفورم موجود لكن غير مفعل"

    submit_btn = soup.find("button", {"id": "submitBtn"})
    if submit_btn and submit_btn.has_attr("disabled"):
        return None, "⏳ زر الإرسال غير مفعل"

    token = form.find("input", {"name": "_token"})
    hmac = form.find("input", {"name": "hmac"})
    started_at = form.find("input", {"name": "started_at"})

    if not token or not hmac or not started_at:
        return None, "⚠️ عناصر الحماية غير جاهزة"

    payload = {
        "_token": token["value"],
        "hmac": hmac["value"],
        "started_at": started_at["value"],
        **FORM_DATA
    }

    return payload, "✅ الفورم جاهز للتعبئة"


def monitor():
    session = requests.Session()
    session.headers.update(HEADERS)
    load_cookies(session)

    print("\n👀 بدء المراقبة اللحظية...\n")

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            res = session.get(TARGET_URL, timeout=15)
            final_url = res.url

            print(f"[{now}] {res.status_code} → {final_url}")

            if is_logged_out(final_url):
                print("🔴 الجلسة انتهت – أعد توفير cookies.json")
                return

            saved = save_page(res.text)
            print(f"📄 حفظ الهيكلية: {saved}")

            payload, status = parse_form(res.text)
            print(f"🧪 حالة الفورم: {status}")

            if payload:
                print("🚀 إرسال الطلب...")
                submit = session.post(TARGET_URL, data=payload, timeout=15)

                print(f"📨 نتيجة الإرسال: {submit.status_code}")
                save_page(submit.text)
                print("✅ تم الإرسال – إيقاف المراقبة")
                return

        except Exception as e:
            print(f"⚠️ خطأ: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    wait_for_cookies()
    monitor()
