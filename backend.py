from fastapi import FastAPI, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse
import requests, json, os, time
from datetime import datetime
from bs4 import BeautifulSoup
from typing import List, Dict, Any
from pydantic import BaseModel

app = FastAPI(title="Smart Session Watcher Web")

# -------- pydantic model for incoming data --------
class ReservationData(BaseModel):
    seller_name: str
    buyer_name: str
    plate_number: str

# ---------------- الإعدادات ----------------
TARGET_URL = "https://import-dep.mega-sy.com/registration"
COOKIES_FILE = "cookies.json"
OUTPUT_DIR = "pages"
POLL_INTERVAL = 5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; Chrome)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en;q=0.9",
    "Connection": "keep-alive",
}

LOGIN_INDICATORS = ["/login", "accounts.google.com"]

# سجل الأحداث
logs: List[str] = []

# ---------------- الأدوات ----------------
def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"[{ts}] {msg}"
    logs.insert(0, log_message) # Add to the beginning of the list
    print(log_message)

def load_cookies(session):
    if not os.path.exists(COOKIES_FILE):
        log("🚫 لا يوجد ملف cookies.json")
        return False

    try:
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
        log("🍪 الجلسة محملة بالكوكيز")
        return True
    except Exception as e:
        log(f"❌ خطأ في تحميل الكوكيز: {e}")
        return False

def save_page(html: str, prefix: str = "page"):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = int(time.time())
    path = f"{OUTPUT_DIR}/{prefix}_{ts}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"📄 حفظ الهيكلية: {path}")
    return path

def parse_form(html: str, dynamic_form_data: Dict[str, Any]):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", {"id": "orderForm"})
    if not form:
        return None, "❌ الفورم غير موجود"

    fieldset = soup.find("fieldset", {"id": "formFields"})
    if fieldset and fieldset.has_attr("disabled"):
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
        **dynamic_form_data
    }

    return payload, "✅ الفورم جاهز للتعبئة"

def is_logged_out(url: str):
    return any(x in url for x in LOGIN_INDICATORS)

# ---------------- المهمة الخلفية ----------------
def monitor_task(form_data: Dict[str, Any]):
    session = requests.Session()
    session.headers.update(HEADERS)
    if not load_cookies(session):
        log("⛔ لا يمكن البدء بدون كوكيز")
        return

    log(f"👀 بدء المراقبة اللحظية للحجز: {form_data}")

    while True:
        try:
            res = session.get(TARGET_URL, timeout=15)
            final_url = res.url
            log(f"{res.status_code} → {final_url}")

            if is_logged_out(final_url):
                log("🔴 الجلسة انتهت – أعد توفير cookies.json")
                return

            save_page(res.text, prefix="check")
            payload, status = parse_form(res.text, form_data)
            log(f"🧪 حالة الفورم: {status}")

            if payload:
                log("🚀 إرسال الطلب...")
                submit = session.post(TARGET_URL, data=payload, timeout=15)
                log(f"📨 نتيجة الإرسال: {submit.status_code}")
                save_page(submit.text, prefix="submit_result")
                log("✅ تم الإرسال – إيقاف المراقبة")
                return

        except Exception as e:
            log(f"⚠️ خطأ فادح أثناء المراقبة: {e}")

        time.sleep(POLL_INTERVAL)

# ---------------- الواجهات ----------------
@app.get("/", response_class=HTMLResponse)
def home():
    log_content = "\n".join(logs)
    return f"""
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
        <head>
            <meta charset="UTF-8">
            <title>Smart Session Watcher</title>
            <meta http-equiv="refresh" content="5">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 20px; }}
                h1, h2 {{ color: #333; }}
                form {{ background: #f9f9f9; padding: 15px; border-radius: 8px; border: 1px solid #ddd; margin-bottom: 20px; }}
                button {{ background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 5px; cursor: pointer; }}
                button:hover {{ background-color: #0056b3; }}
                pre {{ background:#f0f0f0; padding:10px; height:400px; overflow:auto; border: 1px solid #ccc; border-radius: 5px; white-space: pre-wrap; word-wrap: break-word;}}
            </style>
        </head>
        <body>
            <h1>🧠 Smart Session Watcher – Web</h1>
            <form action="/upload_cookies" method="post" enctype="multipart/form-data">
                <label for="file">تحديث ملف cookies.json:</label><br><br>
                <input type="file" name="file" id="file"/>
                <button type="submit">رفع الملف</button>
            </form>
            <h2>سجل الأحداث (يتم التحديث كل 5 ثواني):</h2>
            <pre>{log_content}</pre>
        </body>
    </html>
    """

@app.post("/upload_cookies")
async def upload_cookies(file: UploadFile = File(...)):
    try:
        content = await file.read()
        with open(COOKIES_FILE, "wb") as f:
            f.write(content)
        log("✅ تم رفع cookies.json بنجاح")
        return {"status": "success"}
    except Exception as e:
        log(f"❌ خطأ في رفع الكوكيز: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/start_monitor")
def start_monitor_endpoint(reservation: ReservationData, background_tasks: BackgroundTasks):
    form_data_dict = reservation.dict()
    background_tasks.add_task(monitor_task, form_data_dict)
    log(f"🟢 تم جدولة المراقبة في الخلفية للبيانات: {form_data_dict}")
    return {"status": "success", "message": "Monitoring task has been started in the background."}
