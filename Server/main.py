from fastapi import FastAPI, Request, Query, HTTPException, Form, Header, UploadFile, File
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FileMessage, FlexSendMessage
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId
from pdf2image import convert_from_path
from PyPDF2 import PdfReader
from zoneinfo import ZoneInfo
import time
import threading
import base64
import os
import requests
import math
import re
import folium
import json
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

def load_config():
    path = Path(__file__).resolve().parent / "static" / "config.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


cfg = load_config()

LINE_CHANNEL_SECRET = cfg["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = cfg["LINE_CHANNEL_ACCESS_TOKEN"]
FRONTEND_BASE_URL = cfg["FRONTEND_BASE_URL"]
MONGO_URL = cfg["MONGO_URL"]
DB_NAME = "dimonwall"

MODE_DISCOUNT = "none"

# === UPDATED: base URL ของ Payment Gateway ใหม่ ===
PAYMENT_API_BASE = cfg.get("PAYMENT_API_BASE", "https://lucky-pay.onrender.com")
# ========= Payment Gateway health checker =========

def payment_health_worker():
    """
    ยิง /health ไปที่ PAYMENT_API_BASE ทุก 5 นาที
    เพื่อเช็คว่ายังตอบอยู่ (และช่วยกัน sleep ไม่ให้ dyno หนาวเกิน 😆)
    """
    url = f"{PAYMENT_API_BASE.rstrip('/')}/health"
    while True:
        try:
            r = requests.get(url, timeout=5)
            try:
                txt = r.text[:200]  # กัน log ยาวไป
            except Exception:
                txt = "<no text>"

            print(f"[PAYMENT_HEALTH] {url} -> {r.status_code} {txt}")
        except Exception as e:
            print(f"[PAYMENT_HEALTH] ERROR: {e}")

        # พัก 5 นาที
        time.sleep(300)

client = MongoClient(MONGO_URL)
db = client[DB_NAME]
collection_printer = db["printers"]
collection_payment = db["payment_historys"]
collection_config = db["config"]

PDF_DIR = "pdfs"
MAX_DISK_USAGE_MB = cfg["MAX_DISK_USAGE_MB"]

app = FastAPI()

@app.on_event("startup")
def start_payment_health_checker():
    """
    รันตอน FastAPI start ขึ้นมา
    สร้าง background thread สำหรับ health check
    """
    t = threading.Thread(target=payment_health_worker, daemon=True)
    t.start()
    print("[PAYMENT_HEALTH] background worker started")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # หรือใส่ origin จริงที่ใช้
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/images", StaticFiles(directory="images"), name="images")
app.mount("/static", StaticFiles(directory="static"), name="static")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def convert_data_timezone(data, offset_hours=7):
    """
    แปลงฟิลด์วันที่ทั้งหมดใน list[dict] ให้เป็น timezone +7
    ครอบคลุม: created_at, completed_at, upload_failed_at
    """

    def convert(dt_str_or_obj):
        if isinstance(dt_str_or_obj, str):
            dt = datetime.fromisoformat(dt_str_or_obj)
        elif isinstance(dt_str_or_obj, datetime):
            dt = dt_str_or_obj
        else:
            return dt_str_or_obj
        return (dt + timedelta(hours=offset_hours)).isoformat()

    for d in data:
        for key in ["created_at", "completed_at", "upload_failed_at"]:
            if key in d:
                d[key] = convert(d[key])
    return data


def generate_folium_map(user_lat=None, user_lon=None):
    """
    Fetches printer data and generates a Folium map.
    :param user_lat: User's latitude
    :param user_lon: User's longitude
    :return: A string containing the HTML of the generated map.
    """

    API_BASE = cfg["API_BASE"]
    url = f"{API_BASE}/get_all_printer"

    if user_lat and user_lon:
        url += f"?user_lat={user_lat}&user_lon={user_lon}"

    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        printers = data.get("printers", [])
    except requests.exceptions.RequestException as e:
        print(f"Error fetching printer data: {e}")
        printers = []

    if user_lat and user_lon:
        map_center = [user_lat, user_lon]
        zoom_start = 13
    else:
        map_center = [13.7563, 100.5018]
        zoom_start = 11

    m = folium.Map(location=map_center, zoom_start=zoom_start)

    for printer in printers:
        if "latitude" in printer and "longitude" in printer:
            lat = float(printer["latitude"])
            lon = float(printer["longitude"])

            status = printer.get("status", "offline")
            location_name = printer.get("location_name", "Unknown Printer")

            color = "green" if status == "online" else "red"

            open_time = printer.get("open_time", "N/A")
            close_time = printer.get("close_time", "N/A")

            popup_html = f"""
            <h4>{location_name}</h4>
            <p>Status: {status}</p>
            <p>Open: {open_time} - {close_time}</p>
            <a href="index.html?uid=YOUR_LINE_ID&selected_printer={printer['printer_id']}">Select this printer</a>
            """

            folium.Marker(
                location=[lat, lon],
                popup=popup_html,
                icon=folium.Icon(color=color),
            ).add_to(m)

    if user_lat and user_lon:
        folium.Marker(
            location=[user_lat, user_lon],
            popup="Your Location",
            icon=folium.Icon(color="blue", icon="info-sign"),
        ).add_to(m)

    map_html = m.get_root().render()
    return map_html


# === Utilities ===
def cleanup_pdfs():
    """Auto-clean PDFs when total size > MAX_DISK_USAGE_MB"""
    total_size = 0
    file_list = []
    for root, _, files in os.walk(PDF_DIR):
        for f in files:
            if f.lower().endswith(".pdf"):
                path = os.path.join(root, f)
                try:
                    size = os.path.getsize(path)
                    mtime = os.path.getmtime(path)
                    total_size += size
                    file_list.append((path, size, mtime))
                except Exception:
                    pass
    total_mb = total_size / (1024 * 1024)
    if total_mb > MAX_DISK_USAGE_MB:
        file_list.sort(key=lambda x: x[2])  # oldest first
        while total_mb > MAX_DISK_USAGE_MB and file_list:
            path, size, _ = file_list.pop(0)
            try:
                os.remove(path)
                total_mb -= size / (1024 * 1024)
            except Exception:
                pass


def get_latest_url(printer_id: str):
    doc = collection_printer.find_one({"printer_id": printer_id}, {"_id": 0})
    if doc:
        return doc.get("url"), doc.get("timestamp")
    return None, None


def send_to_printer(PDF_FILE: str, doc: dict):
    printer_url, ts = get_latest_url(doc["printer_id"])
    print(f"Latest URL for {doc['printer_id']} @ {ts} => {printer_url}")
    if not printer_url:
        return False, "No printer URL"

    api_url = f"{printer_url.rstrip('/')}/upload-pdf"

    try:
        with open(PDF_FILE, "rb") as f:
            files = {"file": (os.path.basename(PDF_FILE), f, "application/pdf")}
            data = {
                "doc": json.dumps(doc, ensure_ascii=False, default=str)
            }

            r = requests.post(api_url, files=files, data=data, timeout=(10, 40))

        ok = r.ok
        text = r.text if ok else f"HTTP {r.status_code}: {r.text}"
        return ok, text

    except requests.exceptions.RequestException as e:
        return False, f"Request error: {e}"
    except OSError as e:
        return False, f"File error: {e}"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between 2 coords (km)."""
    R = 6371.0
    phi1, lam1, phi2, lam2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dphi = phi2 - phi1
    dlam = phi2 - lon1
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def _printer_id_number(p) -> int:
    """Extract numeric part from printer_id for sorting; fallback big number."""
    m = re.search(r"(\d+)", str(p.get("printer_id", "")))
    return int(m.group(1)) if m else 10**9


# === Serve HTML pages ===
@app.get("/")
def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/index.html")
def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/map.html")
def serve_map():
    return FileResponse(os.path.join(os.path.dirname(__file__), "map.html"))


@app.get("/historys.html")
def historys():
    return FileResponse(os.path.join(os.path.dirname(__file__), "historys.html"))


# === QR Payment (ใช้ Payment Gateway ใหม่ + ส่ง discount กลับไปให้ frontend) ===
@app.get("/generate_qr")
def generate_qr(
    amount: float = Query(..., gt=0),
    printer_id: str = Query(...),
    line_id: str = Query(...),
    total_pages: int = Query(...),
    jobs: str = Query(...),
    # ✅ เพิ่มให้สามารถเลือก PromptPay ID ได้ (optional)
    prompay_id: Optional[str] = Query(
        None,
        description="PromptPay ID/เบอร์ของร้าน ถ้าไม่ส่งจะใช้ default ของ Payment Gateway",
    ),
):
    """
    1) รับ amount จาก frontend (ราคาที่คำนวณจากงานพิมพ์)
    2) ยิง Payment Gateway /payments/qr เพื่อ random discount + unique suffix
    3) เซฟข้อมูลทั้งหมด (รวม discount, pay_amount) ลง Mongo
    4) ตอบ JSON: { qr_url, base_amount, pay_amount, discount, ... } ให้ index.html เอาไปโชว์
    """
    try:
        jobs_data = json.loads(jobs)

        description = f"Print {total_pages} pages @ {printer_id} ({line_id})"

        # 👇👇 ตรงนี้คือจุดสำคัญ: ส่ง user_id = line_id ไปยัง Payment Gateway
        payload = {
            "amount": amount,
            "description": description,
            "user_id": line_id,   # <--- เพิ่มบรรทัดนี้
            "discount": MODE_DISCOUNT
        }

        # ✅ ถ้ามี prompay_id จาก frontend ให้ส่งไปที่ Payment Gateway ด้วย
        if prompay_id:
            payload["prompay_id"] = prompay_id

        try:
            r = requests.post(
                f"{PAYMENT_API_BASE}/payments/qr",
                json=payload,
                timeout=10,
            )
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error calling payment gateway: {e}")
            raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")

        pay_data = r.json()
        gateway_payment_id = pay_data.get("payment_id")
        base_amount = float(pay_data.get("base_amount", amount))
        pay_amount = float(pay_data.get("pay_amount", amount))
        discount = float(pay_data.get("discount", 0.0))
        unique_suffix = pay_data.get("unique_suffix", 0)

        # ✅ ตอนนี้ Payment Gateway ใช้ฟิลด์ชื่อ prompay_id (ไม่ใช่ phone_number แล้ว)
        gateway_prompay_id = pay_data.get("prompay_id")

        qr_b64 = pay_data.get("qr_base64")
        gateway_status = pay_data.get("status", "PENDING")
        gateway_payload = pay_data.get("payload")

        if not qr_b64 or not gateway_payment_id:
            raise HTTPException(
                status_code=500,
                detail="Invalid response from payment gateway (missing QR or payment_id)",
            )

        # สร้าง data URL สำหรับ frontend
        qr_url = f"data:image/png;base64,{qr_b64}"

        # ref_id ภายในระบบเรา
        ref_id = f"{line_id}_{datetime.utcnow().timestamp()}"

        internal_status = "waiting"
        if gateway_status == "PAID":
            internal_status = "paid"
        elif gateway_status == "CANCELLED":
            internal_status = "cancelled"

        # ✅ เก็บทั้ง prompay_id และ field เดิม phone_number/gateway_phone_number ไว้เพื่อ compat
        payment_doc = {
            "line_id": line_id,
            "printer_id": printer_id,
            "jobs": jobs_data,
            # ราคาเดิมจาก frontend
            "total_amount": float(amount),
            "total_pages": total_pages,
            "status": internal_status,
            "created_at": datetime.utcnow(),
            "ref_id": ref_id,
            "payment_type": "promptpay_gateway",
            # ราคา/ส่วนลดจาก gateway (ใช้แสดงใน history / debug)
            "base_amount": base_amount,
            "pay_amount": pay_amount,
            "discount": discount,
            "unique_suffix": unique_suffix,

            # === NEW FIELDS ===
            "prompay_id": gateway_prompay_id,
            "gateway_prompay_id": gateway_prompay_id,

            # === BACKWARD COMPAT (ใน DB เดิมใช้ phone_number) ===
            "phone_number": gateway_prompay_id,
            "gateway_phone_number": gateway_prompay_id,

            # ข้อมูลดิบของ gateway
            "gateway_payment_id": gateway_payment_id,
            "gateway_base_amount": base_amount,
            "gateway_pay_amount": pay_amount,
            "gateway_discount": discount,
            "gateway_status": gateway_status,
            "gateway_payload": gateway_payload,
        }
        result = collection_payment.insert_one(payment_doc)
        mongo_payment_id = str(result.inserted_id)

        print("Inserted Payment Doc (Mongo _id):", mongo_payment_id)
        print("Gateway payment_id:", gateway_payment_id)

        headers = {
            "X-Payment-Id": gateway_payment_id,
            "X-Ref-Id": ref_id,
            "X-Pay-Amount": str(pay_amount),
            "X-Base-Amount": str(base_amount),
            "X-Discount": str(discount),
        }

        # ✅ ตอบ JSON ให้ index.html ใช้ branch content-type == application/json
        #    ส่งทั้ง prompay_id และ phone_number ให้ front ใช้อย่างใดอย่างหนึ่งตามเวอร์ชัน
        return JSONResponse(
            content={
                "qr_url": qr_url,
                "ref_id": ref_id,
                "payment_id": gateway_payment_id,
                "base_amount": base_amount,
                "pay_amount": pay_amount,
                "discount": discount,
                "unique_suffix": unique_suffix,

                "prompay_id": gateway_prompay_id,     # ใหม่ (ตรงกับ Payment Gateway)
                "phone_number": gateway_prompay_id,   # เก็บไว้เพื่อ compat เดิม
                "status": internal_status,
            },
            headers=headers,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in generate_qr: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})



# === UPDATED: check payment โดย sync กับ Payment Gateway ใหม่ + เก็บ amount/discount ล่าสุด ===
@app.get("/check_payment/{ref_id}")
def check_payment(ref_id: str):
    doc = collection_payment.find_one({"ref_id": ref_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Payment not found")

    status_local = doc.get("status", "waiting")
    gateway_payment_id = doc.get("gateway_payment_id")
    gateway_status = doc.get("gateway_status")

    if gateway_payment_id and status_local in ["waiting", "pending"]:
        try:
            r = requests.get(
                f"{PAYMENT_API_BASE}/payments/{gateway_payment_id}", timeout=10
            )
            if r.ok:
                g = r.json()
                gateway_status = g.get("status", gateway_status)
                gw_pay_amount = g.get("pay_amount")
                gw_base_amount = g.get("base_amount")
                gw_discount = g.get("discount")

                if gateway_status == "PAID":
                    status_local = "paid"
                elif gateway_status == "CANCELLED":
                    status_local = "cancelled"
                elif gateway_status == "PENDING":
                    status_local = "waiting"

                update_fields = {
                    "status": status_local,
                    "gateway_status": gateway_status,
                }
                if gw_pay_amount is not None:
                    update_fields["gateway_pay_amount"] = float(gw_pay_amount)
                    update_fields["pay_amount"] = float(gw_pay_amount)
                if gw_base_amount is not None:
                    update_fields["gateway_base_amount"] = float(gw_base_amount)
                    update_fields["base_amount"] = float(gw_base_amount)
                if gw_discount is not None:
                    update_fields["gateway_discount"] = float(gw_discount)
                    update_fields["discount"] = float(gw_discount)

                collection_payment.update_one(
                    {"_id": doc["_id"]},
                    {"$set": update_fields},
                )
        except requests.exceptions.RequestException as e:
            print(f"Error checking payment gateway for {gateway_payment_id}: {e}")

    print(f"Checking payment for ref_id: {ref_id}")
    print(f"  - current status: {status_local} (gateway_status={gateway_status})")

    return {
        "ref_id": ref_id,
        "status": status_local,
        "gateway_status": gateway_status,
    }


# === New API for Payment Gateway Webhook / Direct Print ===
@app.post("/pay_completed")
async def pay_completed(request: Request):
    try:
        data = await request.json()
        ref_id = data.get("ref_id")
        status = data.get("status", "paid")  # ส่วนใหญ่จะเป็น "paid"
        line_id = data.get("line_id")
        printer_id = data.get("printer_id")
        total_amount = data.get("total_amount", 0)
        total_pages = data.get("total_pages", 0)
        jobs = data.get("jobs", [])

        if not ref_id:
            raise HTTPException(status_code=400, detail="Missing ref_id")

        print(f"🔔 Received pay_completed for ref_id={ref_id}, status={status}")

        # ----- 1) หา / สร้าง payment doc ใน Mongo ก่อน -----
        doc = collection_payment.find_one({"ref_id": ref_id})

        if not doc:
            # กรณี direct (ไม่มี generate_qr มาก่อน)
            payment_doc = {
                "ref_id": ref_id,
                "line_id": line_id,
                "printer_id": printer_id,
                "jobs": jobs,
                "total_amount": total_amount,
                "total_pages": total_pages,
                "status": status,   # "paid"
                "created_at": datetime.utcnow(),
                "completed_at": None,
                "payment_type": "direct",
            }
            collection_payment.insert_one(payment_doc)
            doc = payment_doc
            print("🆕 Created new payment doc for direct print:", payment_doc)
        else:
            # กรณีเคย create ไว้แล้วตอน generate_qr
            # อัปเดตสถานะให้เป็น "paid" และเก็บเวลาที่ได้การแจ้งเตือนจ่ายเสร็จ
            collection_payment.update_one(
                {"ref_id": ref_id},
                {
                    "$set": {
                        "status": status,  # ส่วนใหญ่คือ "paid"
                        "completed_at": datetime.utcnow(),
                        # เผื่อส่ง total_amount/total_pages มาจาก gateway ให้ sync ไว้ด้วย
                        "total_amount": total_amount or doc.get("total_amount", 0),
                        "total_pages": total_pages or doc.get("total_pages", 0),
                    }
                },
            )
            doc.update(
                {
                    "status": status,
                    "total_amount": total_amount or doc.get("total_amount", 0),
                    "total_pages": total_pages or doc.get("total_pages", 0),
                }
            )

        # ----- 2) ส่ง LINE Flex แจ้งว่า "การสั่งพิมพ์ถูกยืนยันแล้ว" (ไม่เกี่ยวกับการพิมพ์เสร็จ) -----
        if line_id:
            try:
                history_url = f"{FRONTEND_BASE_URL}/historys.html"

                flex_contents = {
                    "type": "bubble",
                    "size": "kilo",
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "md",
                        "paddingAll": "16px",
                        "contents": [
                            {
                                "type": "text",
                                "text": "✅ การสั่งพิมพ์ถูกยืนยันแล้ว",
                                "weight": "bold",
                                "size": "lg",
                                "wrap": True,
                            },
                            {
                                "type": "text",
                                "text": "ระบบได้รับการชำระเงินเรียบร้อยแล้ว และกำลังเตรียมส่งงานไปยังเครื่องพิมพ์",
                                "size": "sm",
                                "color": "#666666",
                                "wrap": True,
                                "margin": "md",
                            },
                            {
                                "type": "separator",
                                "margin": "md"
                            },
                            {
                                "type": "box",
                                "layout": "vertical",
                                "margin": "md",
                                "spacing": "xs",
                                "contents": [
                                    {
                                        "type": "text",
                                        "text": f"Ref ID: {ref_id}",
                                        "size": "xs",
                                        "color": "#999999",
                                        "wrap": True,
                                    },
                                    {
                                        "type": "text",
                                        "text": f"ยอดชำระ: {total_amount} บาท",
                                        "size": "xs",
                                        "color": "#999999",
                                        "wrap": True,
                                    },
                                    {
                                        "type": "text",
                                        "text": f"จำนวนหน้า: {total_pages}",
                                        "size": "xs",
                                        "color": "#999999",
                                        "wrap": True,
                                    },
                                ],
                            },
                            {
                                "type": "button",
                                "style": "primary",
                                "height": "sm",
                                "margin": "md",
                                "action": {
                                    "type": "uri",
                                    "label": "ดูสถานะงานพิมพ์",
                                    "uri": history_url,
                                },
                            },
                        ],
                    },
                    "styles": {
                        "body": {
                            "backgroundColor": "#FFFFFF"
                        }
                    },
                }

                line_bot_api.push_message(
                    line_id,
                    FlexSendMessage(
                        alt_text="การสั่งพิมพ์ถูกยืนยันแล้ว",
                        contents=flex_contents,
                    ),
                )
            except Exception as e:
                print("⚠️ LINE push error:", e)

        # ----- 3) สร้าง background worker สำหรับส่งไฟล์เข้าเครื่องพิมพ์ -----
        def _print_worker(payment_doc: dict):
            try:
                print(f"🖨 [WORKER] Start sending print jobs for ref_id={ref_id}")
                pdf_dir = os.path.join(PDF_DIR, payment_doc["line_id"])
                upload_failed = False

                for job in payment_doc.get("jobs", []):
                    pdf_file = os.path.join(pdf_dir, job["filename"])
                    ok, msg = send_to_printer(pdf_file, payment_doc)
                    print("🖨 [WORKER] Send to printer:", pdf_file, ok, msg)

                    if not ok:
                        upload_failed = True
                        collection_payment.update_one(
                            {"ref_id": ref_id},
                            {
                                "$set": {
                                    "status": "uploadfail",
                                    "completed_at": datetime.utcnow(),
                                    "upload_failed_at": datetime.utcnow(),
                                    "upload_error": msg,
                                }
                            },
                        )
                        break

                if not upload_failed:
                    collection_payment.update_one(
                        {"ref_id": ref_id},
                        {
                            "$set": {
                                "status": "uploaded",
                                "completed_at": datetime.utcnow(),
                            }
                        },
                    )
                print(f"🖨 [WORKER] Done for ref_id={ref_id}, upload_failed={upload_failed}")
            except Exception as e:
                print(f"❌ [WORKER] Error in print worker for ref_id={ref_id}: {e}")
                collection_payment.update_one(
                    {"ref_id": ref_id},
                    {
                        "$set": {
                            "status": "uploadfail",
                            "completed_at": datetime.utcnow(),
                            "upload_error": str(e),
                        }
                    },
                )

        # run worker แบบไม่บล็อก response
        threading.Thread(target=_print_worker, args=(doc,), daemon=True).start()

        # ----- 4) ตอบกลับเร็ว ๆ ว่า "โอเคแล้ว" -----
        # จุดนี้ payment_status = paid แล้ว → check_payment เห็นทันที
        return {
            "status": "ok",
            "message": "Payment recorded, printing started in background.",
            "ref_id": ref_id,
            "payment_status": status,
        }

    except Exception as e:
        print(f"❌ Error in pay_completed: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/cancel_payment/{ref_id}")
def cancel_payment(ref_id: str):
    print(f"Cancelling payment for ref_id: {ref_id}")

    doc = collection_payment.find_one({"ref_id": ref_id})
    if not doc:
        return JSONResponse(status_code=404, content={"error": "Payment not found"})

    if doc.get("status") not in ["waiting", "cancelled"]:
        return {"status": "ok", "message": f"Payment already {doc['status']}"}

    collection_payment.update_one(
        {"ref_id": ref_id}, {"$set": {"status": "cancelled"}}
    )

    return {"status": "ok", "message": "Payment cancelled"}


def get_show_offline_setting() -> bool:
    """อ่าน config จาก MongoDB ว่าจะโชว์ offline printer หรือไม่"""
    collection_config = db["config"]
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")})
    if not doc:
        return True
    frontend_cfg = doc.get("frontend", {})
    val = frontend_cfg.get("show_offline_printer", "True")
    return str(val).lower() == "true"


@app.get("/get_all_printer")
def get_all_printer(
    user_lat: Optional[float] = Query(None), user_lon: Optional[float] = Query(None)
):
    printers = list(collection_printer.find({}, {"_id": 0}))

    tz = ZoneInfo("Asia/Bangkok")
    now = datetime.now(tz)

    for p in printers:
        last_seen = p.get("last_seen")
        status = "offline"
        try:
            if last_seen:
                if isinstance(last_seen, str):
                    # รองรับทั้ง
                    # "2025-11-23 15:59:46" (ไม่มี tz)
                    # "2025-11-23 15:59:46+06:42" (มี tz แปลก ๆ จากของเก่า)
                    last_seen = datetime.fromisoformat(last_seen)
                elif not isinstance(last_seen, datetime):
                    last_seen = None

                if last_seen:
                    # ✨ บังคับตีความว่าเวลานี้เป็นเวลาไทย (+07:00) โดย **ไม่เลื่อนเลขชั่วโมง**
                    # เช่น "2025-09-04 17:20:57" -> "2025-09-04 17:20:57+07:00"
                    # หรือ "2025-09-04 17:20:57+06:42" -> "2025-09-04 17:20:57+07:00"
                    last_seen = last_seen.replace(tzinfo=tz)

                    delta = now - last_seen
                    print(f"🕒 now: {now} | last_seen: {last_seen} | delta: {delta}")

                    # ออนไลน์ถ้าไม่เกิน 2 นาที
                    if delta <= timedelta(minutes=2):
                        status = "online"
        except Exception as e:
            print("❌ Error parsing last_seen:", e)
            status = "offline"

        p["status"] = status

    # ด้านล่างเหมือนเดิม
    if not get_show_offline_setting():
        printers = [p for p in printers if p.get("status") == "online"]

    if user_lat is not None and user_lon is not None:
        for p in printers:
            try:
                lat, lon = float(p.get("lat")), float(p.get("lon"))
                p["distance_km"] = round(haversine_km(user_lat, user_lon, lat, lon), 3)
            except Exception:
                p["distance_km"] = None

        nearest = sorted(
            [p for p in printers if p["distance_km"] is not None],
            key=lambda x: x["distance_km"],
        )
        top3 = nearest[:3]
        remaining = [p for p in printers if p not in top3]
        remaining_sorted = sorted(remaining, key=_printer_id_number)
        ordered = top3 + remaining_sorted
        return {"printers": ordered, "sorted_by": "nearest_then_id"}

    ordered = sorted(printers, key=lambda p: str(p.get("location_name", "")))
    return {"printers": ordered, "sorted_by": "location_name"}



@app.get("/list-pdfs/{line_id}")
def list_pdfs(line_id: str):
    folder_path = os.path.join(PDF_DIR, line_id)
    if not os.path.exists(folder_path):
        return JSONResponse(status_code=404, content={"error": "No PDF files found"})
    file_list = []
    for filename in os.listdir(folder_path):
        if filename.lower().endswith(".pdf"):
            file_path = os.path.join(folder_path, filename)
            try:
                mod_time = os.path.getmtime(file_path)
                file_list.append((filename, file_path, mod_time))
            except OSError:
                continue
    file_list.sort(key=lambda x: x[2], reverse=True)
    file_infos = []
    for filename, file_path, _ in file_list:
        try:
            reader = PdfReader(file_path)
            total_pages = len(reader.pages)
        except Exception:
            total_pages = 0
        file_infos.append(
            {
                "filename": filename,
                "url": f"/get-pdf/{line_id}/{filename}",
                "total_pages": total_pages,
                "upload_timestamp": datetime.fromtimestamp(
                    os.path.getmtime(file_path)
                ),
            }
        )
    return {"files": file_infos}


@app.get("/get-pdf/{line_id}/{filename}")
def get_pdf(line_id: str, filename: str):
    file_path = os.path.join(PDF_DIR, line_id, filename)
    if not os.path.exists(file_path):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    with open(file_path, "rb") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.get("/preview-pdf/{line_id}/{filename}")
def preview_pdf(line_id: str, filename: str):
    file_path = os.path.join(PDF_DIR, line_id, filename)
    if not os.path.exists(file_path):
        return JSONResponse(status_code=404, content={"error": "PDF not found"})
    try:
        images = convert_from_path(file_path)
        image_b64_list = []
        for img in images:
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
            image_b64_list.append(f"data:image/png;base64,{encoded}")
        return {"images": image_b64_list}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/callback")
async def callback(request: Request):
    body = await request.body()
    signature = request.headers["X-Line-Signature"]
    try:
        handler.handle(body.decode("utf-8"), signature)
    except Exception as e:
        print("Error:", e)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip()
    if text.startswith("/print"):
        reply = "🖨 สั่งพิมพ์ (mock)"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


@handler.add(MessageEvent, message=FileMessage)
def handle_file_message(event):
    message_id = event.message.id
    file_name = event.message.file_name
    user_id = event.source.user_id

    user_dir = os.path.join(PDF_DIR, user_id)
    os.makedirs(user_dir, exist_ok=True)
    save_path = os.path.join(user_dir, file_name)

    file_content = line_bot_api.get_message_content(message_id).content
    with open(save_path, "wb") as f:
        f.write(file_content)

    cleanup_pdfs()

    # เดิม: reply_text = f"บันทึกไฟล์ {file_name} เรียบร้อยแล้ว!\n{FRONTEND_BASE_URL}"
    # เปลี่ยนเป็น Flex card modern minimal
    query = urlencode({"uid": user_id})
    front_url = f"{FRONTEND_BASE_URL}?{query}"

    flex_contents = {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": "ไฟล์อัปโหลดสำเร็จ",
                    "weight": "bold",
                    "size": "lg",
                },
                {
                    "type": "text",
                    "text": file_name,
                    "size": "sm",
                    "color": "#888888",
                    "wrap": True,
                },
                {
                    "type": "separator",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": "คุณสามารถตั้งค่าการพิมพ์และยืนยันการสั่งพิมพ์ได้จากหน้าเว็บ",
                    "size": "sm",
                    "wrap": True,
                    "margin": "md"
                },
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "margin": "md",
                    "action": {
                        "type": "uri",
                        "label": "เปิดหน้า DeepPrinter",
                        "uri": front_url
                    }
                }
            ],
        },
        "styles": {
            "body": {
                "backgroundColor": "#FFFFFF"
            }
        }
    }

    line_bot_api.reply_message(
        event.reply_token,
        FlexSendMessage(
            alt_text=f"บันทึกไฟล์ {file_name} เรียบร้อยแล้ว!",
            contents=flex_contents,
        ),
    )


def serialize_doc(doc):
    """แปลง ObjectId และ datetime -> str"""
    doc["_id"] = str(doc["_id"])
    if "created_at" in doc and isinstance(doc["created_at"], datetime):
        doc["created_at"] = doc["created_at"].isoformat()
    if "completed_at" in doc and isinstance(doc["completed_at"], datetime):
        doc["completed_at"] = doc["completed_at"].isoformat()
    return doc


@app.get("/get_payment_history/{line_id}")
def get_payment_history(line_id: str):
    docs = list(collection_payment.find({"line_id": line_id}))
    serialized_docs = [serialize_doc(doc) for doc in docs]
    serialized_docs = convert_data_timezone(serialized_docs)
    return {"history": serialized_docs}


@app.get("/feedback.html")
def serve_feedback():
    return FileResponse(os.path.join(os.path.dirname(__file__), "feedback.html"))


@app.post("/sent_feedback")
async def sent_feedback(request: Request):
    try:
        data = await request.json()
        uid = data.get("uid")
        topic = data.get("topic")
        message = data.get("message")

        if not uid or not topic or not message:
            raise HTTPException(status_code=400, detail="Missing required fields")

        feedback_doc = {
            "uid": uid,
            "topic": topic,
            "message": message,
            "created_at": datetime.utcnow(),
        }

        result = db["feedbacks"].insert_one(feedback_doc)
        return {"status": "ok", "feedback_id": str(result.inserted_id)}

    except Exception as e:
        print(f"Error in sent_feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/guide.html")
def serve_guide():
    return FileResponse(os.path.join(os.path.dirname(__file__), "guide.html"))


@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...), uid: str = Form(...)):
    try:
        user_dir = os.path.join(PDF_DIR, uid)
        os.makedirs(user_dir, exist_ok=True)

        file_path = os.path.join(user_dir, file.filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())

        return {"status": "ok", "filename": file.filename}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/update_status/{ref_id}")
def update_status(ref_id: str, status: str = Form(...)):
    doc = collection_payment.find_one({"ref_id": ref_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Payment not found")

    collection_payment.update_one(
        {"ref_id": ref_id}, {"$set": {"status": status, "completed_at": datetime.utcnow()}}
    )

    return {"status": "ok", "message": f"Payment {ref_id} updated to {status}"}


@app.get("/get_config")
def get_config():
    collection_config = db["config"]
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")})
    if not doc:
        return {"frontend": {"use_payment": "True"}}
    return {"frontend": doc.get("frontend", {})}


@app.get("/get_config_authen")
def get_config_authen_alias():
    collection_config = db["config"]
    doc = collection_config.find_one(
        {"_id": ObjectId("68ab0f1c4db5106f558a97a4")}, {"_id": 0}
    )
    if not doc:
        return {
            "frontend": {"use_payment": "True"},
            "node_authen": {},
        }
    return {
        "frontend": doc.get("frontend", {}),
        "node_authen": doc.get("node_authen", {}),
    }

@app.post("/update_cups_url")
def update_cups_url(
    printer_id: str = Form(...),
    url: str = Form(...),
):
    """
    อัปเดต CUPS URL ของเครื่องพิมพ์ (เช่น https://xxxx.trycloudflare.com ที่ proxy ไปยัง :631)
    - แยกจาก url หลัก (API) เพื่อให้เก็บได้ทั้งสองค่า
    """
    try:
        now = datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d %H:%M:%S")

        res = collection_printer.find_one_and_update(
            {"printer_id": printer_id},
            {
                "$setOnInsert": {"created_at": now, "name": printer_id},
                # เก็บเป็นฟิลด์ใหม่ชื่อ cups_url
                "$set": {
                    "cups_url": url,
                    "last_seen": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

        return {
            "status": "ok",
            "printer": {k: v for k, v in res.items() if k != "_id"},
        }
    except Exception as e:
        print(f"❌ Error in update_cups_url: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/update_printer_url")
def update_printer_url(
    printer_id: str = Form(...),
    url: str = Form(...),
):
    try:
        now = datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d %H:%M:%S")
        res = collection_printer.find_one_and_update(
            {"printer_id": printer_id},
            {
                "$setOnInsert": {"created_at": now, "name": printer_id},
                "$set": {"url": url, "last_seen": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return {
            "status": "ok",
            "printer": {k: v for k, v in res.items() if k != "_id"},
        }
    except Exception as e:
        print(f"❌ Error in update_printer_url: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/manage.html")
def serve_manage():
    return FileResponse(os.path.join(os.path.dirname(__file__), "manage.html"))


def check_permission(line_id: str, printer_id: str) -> bool:
    doc = collection_config.find_one(
        {"_id": ObjectId("68ab0f1c4db5106f558a97a4")}
    )
    if not doc:
        return False

    node_authen = doc.get("node_authen", {})

    admin_ids = node_authen.get("admin", [])
    if isinstance(admin_ids, str):
        admin_ids = [admin_ids]
    if line_id in admin_ids:
        return True

    if printer_id not in node_authen:
        print(f"ℹ️ Printer {printer_id} not found in node_authen → allow all")
        return True

    assigned_ids = node_authen.get(printer_id, [])
    if isinstance(assigned_ids, str):
        assigned_ids = [assigned_ids]

    if not assigned_ids:
        print(f"ℹ️ Printer {printer_id} has empty list → allow all")
        return True

    if line_id in assigned_ids:
        return True

    print(f"🚫 Permission denied for {line_id} on {printer_id}")
    return False


@app.get("/get_printer/{printer_id}")
def get_printer(printer_id: str, x_line_uid: str = Header(...)):
    if not check_permission(x_line_uid, printer_id):
        raise HTTPException(status_code=403, detail="Permission denied")

    doc = collection_printer.find_one({"printer_id": printer_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Printer not found")
    return doc


@app.post("/update_printer/{printer_id}")
async def update_printer(
    printer_id: str,
    request: Request,
    x_line_uid: str = Header(...),
):
    if not check_permission(x_line_uid, printer_id):
        raise HTTPException(status_code=403, detail="Permission denied")

    data = await request.json()

    result = collection_printer.update_one({"printer_id": printer_id}, {"$set": data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Printer not found")

    collection_config.update_one(
        {"_id": ObjectId("68ab0f1c4db5106f558a97a4")},
        {"$addToSet": {f"node_authen.{printer_id}": x_line_uid}},
    )

    return {
        "status": "ok",
        "message": f"Updated printer {printer_id} and node_authen",
        "uid": x_line_uid,
    }


@app.get("/get_config_authen")
def get_config_authen():
    collection_config = db["config"]
    doc = collection_config.find_one(
        {"_id": ObjectId("68ab0f1c4db5106f558a97a4")}, {"_id": 0}
    )
    if not doc:
        return {
            "frontend": {"use_payment": "True"},
            "node_authen": {},
        }

    node_authen = doc.get("node_authen", {})
    fixed_authen = {}
    for k, v in node_authen.items():
        if isinstance(v, str):
            fixed_authen[k] = [v]
        elif isinstance(v, list):
            fixed_authen[k] = v
        else:
            fixed_authen[k] = []

    return {
        "frontend": doc.get("frontend", {}),
        "node_authen": fixed_authen,
    }


@app.get("/debug_authen")
def debug_authen():
    doc = collection_config.find_one(
        {"_id": ObjectId("68ab0f1c4db5106f558a97a4")},
        {"node_authen": 1, "_id": 0},
    )

    node_authen = doc.get("node_authen", {}) if doc else {}
    fixed_authen = {}
    for k, v in node_authen.items():
        if isinstance(v, str):
            fixed_authen[k] = [v]
        elif isinstance(v, list):
            fixed_authen[k] = v
        else:
            fixed_authen[k] = []

    return {"node_authen": fixed_authen}


@app.post("/update_printer_status/{printer_id}")
def update_printer_status(
    printer_id: str,
    status: str = Form("online"),
    last_seen: str = Form(None),
):
    try:
        if last_seen is None:
            last_seen = datetime.now(ZoneInfo("Asia/Bangkok")).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        res = collection_printer.find_one_and_update(
            {"printer_id": printer_id},
            {
                "$setOnInsert": {"created_at": last_seen, "name": printer_id},
                "$set": {"last_seen": last_seen, "status": status},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

        return {
            "status": "ok",
            "printer": {k: v for k, v in res.items() if k != "_id"},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/get_printer_name/{printer_id}")
def get_printer_name(printer_id: str):
    doc = collection_printer.find_one(
        {"printer_id": printer_id},
        {"_id": 0, "selected_printer": 1, "list_printers": 1},
    )

    if not doc:
        raise HTTPException(status_code=404, detail="Printer not found")

    selected = doc.get("selected_printer")
    plist = doc.get("list_printers", [])

    if isinstance(plist, str):
        try:
            parsed = json.loads(plist)
            if isinstance(parsed, list):
                plist = parsed
            else:
                raise ValueError
        except Exception:
            plist = [p.strip() for p in plist.split(",") if p.strip()]

    return {
        "printer_id": printer_id,
        "selected_printer": selected,
        "list_printers": plist,
    }


@app.post("/update_printer_name/{printer_id}")
async def update_printer_name(
    printer_id: str,
    request: Request,
):
    current = collection_printer.find_one({"printer_id": printer_id})
    if not current:
        raise HTTPException(status_code=404, detail="Printer not found")

    try:
        data = await request.json()
    except Exception:
        data = {}

    update_fields = {}

    if "selected_printer" in data:
        sel = data.get("selected_printer")
        if sel is not None:
            if isinstance(sel, str):
                sel = sel.strip()
            update_fields["selected_printer"] = sel

    if "list_printers" in data:
        lp = data.get("list_printers")

        def to_list(v):
            if v is None:
                return None
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip() != ""]
            if isinstance(v, str):
                s = v.strip()
                if s == "":
                    return []
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [
                            str(x).strip() for x in parsed if str(x).strip() != ""
                        ]
                except Exception:
                    pass
                return [p.strip() for p in s.split(",") if p.strip() != ""]
            return [str(v).strip()]

        parsed_list = to_list(lp)
        if parsed_list is not None:
            update_fields["list_printers"] = parsed_list

    if not update_fields:
        return {
            "status": "noop",
            "message": "Nothing to update",
            "printer_id": printer_id,
            "selected_printer": current.get("selected_printer"),
            "list_printers": current.get("list_printers", []),
        }

    result = collection_printer.update_one(
        {"printer_id": printer_id},
        {"$set": update_fields},
    )

    updated = collection_printer.find_one(
        {"printer_id": printer_id},
        {"_id": 0, "selected_printer": 1, "list_printers": 1},
    )

    return {
        "status": "ok" if result.matched_count else "not_found",
        "printer_id": printer_id,
        "selected_printer": updated.get("selected_printer"),
        "list_printers": updated.get("list_printers", []),
        "updated_fields": list(update_fields.keys()),
    }


@app.post("/test_printer/{printer_id}")
def test_printer(
    printer_id: str,
    x_line_uid: str = Header(...),
):
    if not check_permission(x_line_uid, printer_id):
        raise HTTPException(status_code=403, detail="Permission denied")

    pdoc = collection_printer.find_one({"printer_id": printer_id}, {"_id": 0})
    if not pdoc:
        raise HTTPException(status_code=404, detail="Printer not found")

    test_path = Path(__file__).resolve().parent / "static" / "test.pdf"
    if not test_path.exists():
        raise HTTPException(status_code=404, detail=f"Test file not found: {test_path}")

    try:
        reader = PdfReader(str(test_path))
        total_pages = len(reader.pages)
    except Exception:
        total_pages = 0

    ref_id = f"TEST_{printer_id}_{int(datetime.utcnow().timestamp())}"
    job_entry = {
        "filename": test_path.name,
        "pages": "all",
        "color": "bw",
        "copies": 1,
        "page_count": total_pages,
        "price_per_page": 0,
        "total_price": 0,
    }

    selected = pdoc.get("selected_printer")

    collection_payment.update_one(
        {"ref_id": ref_id},
        {
            "$setOnInsert": {
                "ref_id": ref_id,
                "printer_id": printer_id,
                "line_id": x_line_uid,
                "jobs": [job_entry],
                "total_amount": 0,
                "total_pages": total_pages,
                "payment_type": "test",
                "created_at": datetime.utcnow(),
            },
            "$set": {
                "status": "submitted",
                "completed_at": None,
            },
        },
        upsert=True,
    )

    send_doc = {
        "ref_id": ref_id,
        "line_id": x_line_uid,
        "printer_id": printer_id,
        "selected_printer": selected,
        "jobs": [job_entry],
        "total_amount": 0,
        "total_pages": total_pages,
        "status": "paid",
        "created_at": datetime.utcnow().isoformat(),
        "payment_type": "direct",
        "note": "test_print_from_server",
    }

    latest_url, latest_ts = get_latest_url(printer_id)
    if not latest_url:
        raise HTTPException(status_code=503, detail="No printer URL available")

    ok, msg = send_to_printer(str(test_path), send_doc)

    return {
        "status": "ok" if ok else "error",
        "printer_id": printer_id,
        "selected_printer": selected,
        "target_url": latest_url,
        "url_timestamp": latest_ts,
        "ref_id": ref_id,
        "total_pages": total_pages,
        "message": msg,
    }


@app.post("/register_printer")
def register_printer(
    printer_id: str = Form(...),
    name: str = Form(None),
    port: int = Form(None),
    status: str = Form("online"),
    url: str = Form(""),
):
    tz = ZoneInfo("Asia/Bangkok")
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    doc = {
        "printer_id": printer_id,
        "name": name or printer_id,
        "port": port,
        "status": status,
        "url": url,
        "last_seen": now,
    }
    res = collection_printer.find_one_and_update(
        {"printer_id": printer_id},
        {
            "$setOnInsert": {"created_at": now},
            "$set": doc,
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return {"ok": True, "printer": res}
