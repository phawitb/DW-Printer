"""
DeepPrinter FastAPI (single-file full code)

✅ Includes:
- Payment gateway health checker (background thread)
- PDF maintenance worker: size limit + TTL + remove empty user folders
- LINE webhook: Text + File upload -> save pdf -> reply Flex
- Payment flow: /generate_qr -> save mongo -> return QR
- Payment sync: /check_payment/{ref_id}
- Webhook from gateway: /pay_completed -> push LINE + send jobs to printer in background
- Printer registry / auth / manage endpoints
- PDF list/preview endpoints

🔧 Fixes vs your pasted code:
- Remove duplicate startup checker function
- Remove duplicate PDF_DIR/MAX_DISK_USAGE_MB declarations
- Fix TTL default mismatch (default 24h)
- Improve folder cleanup: remove folders with no PDFs (not only empty)
- Fix haversine dlam bug
- Remove duplicate /get_config_authen endpoint (keep one canonical)
"""

from fastapi import (
    FastAPI,
    Request,
    Query,
    HTTPException,
    Form,
    Header,
    UploadFile,
    File,
)
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from linebot import LineBotApi, WebhookHandler
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    FileMessage,
    FlexSendMessage,
)

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
import json

from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode


# =========================================================
# Config
# =========================================================
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

PAYMENT_API_BASE = cfg.get("PAYMENT_API_BASE", "https://lucky-pay.onrender.com")

PDF_DIR = "pdfs"
MAX_DISK_USAGE_MB = int(cfg["MAX_DISK_USAGE_MB"])

# ✅ TTL / Maintenance
PDF_TTL_HOURS = int(cfg.get("PDF_TTL_HOURS", 1))  # default 24h
MAINTENANCE_INTERVAL_SEC = int(cfg.get("MAINTENANCE_INTERVAL_SEC", 600))  # default 10 นาที


# =========================================================
# Mongo
# =========================================================
client = MongoClient(MONGO_URL)
db = client[DB_NAME]
collection_printer = db["printers"]
collection_payment = db["payment_historys"]
collection_config = db["config"]


# =========================================================
# App
# =========================================================
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/images", StaticFiles(directory="images"), name="images")
app.mount("/static", StaticFiles(directory="static"), name="static")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


# =========================================================
# Background workers
# =========================================================
def payment_health_worker():
    """
    ยิง /health ไปที่ PAYMENT_API_BASE ทุก 5 นาที
    """
    url = f"{PAYMENT_API_BASE.rstrip('/')}/health"
    while True:
        try:
            r = requests.get(url, timeout=5)
            txt = (r.text or "")[:200]
            print(f"[PAYMENT_HEALTH] {url} -> {r.status_code} {txt}")
        except Exception as e:
            print(f"[PAYMENT_HEALTH] ERROR: {e}")
        time.sleep(300)


def cleanup_pdfs():
    """Auto-clean PDFs when total size > MAX_DISK_USAGE_MB (delete oldest first)."""
    total_size = 0
    file_list = []

    if not os.path.exists(PDF_DIR):
        return

    for root, _, files in os.walk(PDF_DIR):
        for f in files:
            if not f.lower().endswith(".pdf"):
                continue
            path = os.path.join(root, f)
            try:
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
                total_size += size
                file_list.append((path, size, mtime))
            except Exception:
                pass

    total_mb = total_size / (1024 * 1024)
    if total_mb <= MAX_DISK_USAGE_MB:
        return

    file_list.sort(key=lambda x: x[2])  # oldest first
    while total_mb > MAX_DISK_USAGE_MB and file_list:
        path, size, _ = file_list.pop(0)
        try:
            os.remove(path)
            total_mb -= size / (1024 * 1024)
            print(f"[MAINT] size-limit deleted: {path}")
        except Exception as e:
            print(f"[MAINT] size-limit delete error: {path} -> {e}")


def cleanup_pdfs_by_age(ttl_hours: int = 24):
    """ลบ PDF ที่เก่ากว่า ttl_hours (based on mtime)."""
    if not os.path.exists(PDF_DIR):
        return

    now_ts = time.time()
    ttl_sec = ttl_hours * 3600

    for root, _, files in os.walk(PDF_DIR):
        for f in files:
            if not f.lower().endswith(".pdf"):
                continue
            path = os.path.join(root, f)
            try:
                mtime = os.path.getmtime(path)
                if (now_ts - mtime) > ttl_sec:
                    os.remove(path)
                    print(f"[MAINT] ttl deleted: {path}")
            except Exception as e:
                print(f"[MAINT] ttl delete error: {path} -> {e}")


def cleanup_user_folders_without_pdfs():
    """ลบโฟลเดอร์ใน pdfs/ ที่ไม่มีไฟล์ .pdf เหลือแล้ว"""
    if not os.path.exists(PDF_DIR):
        return

    for name in os.listdir(PDF_DIR):
        folder = os.path.join(PDF_DIR, name)
        if not os.path.isdir(folder):
            continue

        try:
            has_pdf = any(fn.lower().endswith(".pdf") for fn in os.listdir(folder))
            if has_pdf:
                continue

            # ลบไฟล์ขยะที่เหลือ (ถ้ามี)
            for fn in os.listdir(folder):
                try:
                    os.remove(os.path.join(folder, fn))
                except Exception:
                    pass

            if not os.listdir(folder):
                os.rmdir(folder)
                print(f"[MAINT] removed folder (no pdfs): {folder}")
        except Exception as e:
            print(f"[MAINT] folder cleanup error: {folder} -> {e}")


def maintenance_worker():
    """
    งานดูแล server เป็นรอบ ๆ
    - จำกัดขนาดดิสก์ (cleanup_pdfs)
    - ลบไฟล์เก่า (cleanup_pdfs_by_age)
    - ลบโฟลเดอร์ user ที่ไม่มี pdf แล้ว
    """
    while True:
        try:
            cleanup_pdfs()
            cleanup_pdfs_by_age(PDF_TTL_HOURS)
            cleanup_user_folders_without_pdfs()
            print("[MAINT] maintenance done")
        except Exception as e:
            print(f"[MAINT] ERROR: {e}")

        time.sleep(MAINTENANCE_INTERVAL_SEC)


@app.on_event("startup")
def startup_workers():
    t1 = threading.Thread(target=payment_health_worker, daemon=True)
    t1.start()
    print("[PAYMENT_HEALTH] background worker started")

    t2 = threading.Thread(target=maintenance_worker, daemon=True)
    t2.start()
    print("[MAINT] background worker started")


# =========================================================
# Helpers
# =========================================================
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


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between 2 coords (km)."""
    R = 6371.0
    phi1, lam1, phi2, lam2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dphi = phi2 - phi1
    dlam = lam2 - lam1  # ✅ fixed
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


def serialize_doc(doc):
    """แปลง ObjectId และ datetime -> str"""
    doc["_id"] = str(doc["_id"])
    if "created_at" in doc and isinstance(doc["created_at"], datetime):
        doc["created_at"] = doc["created_at"].isoformat()
    if "completed_at" in doc and isinstance(doc["completed_at"], datetime):
        doc["completed_at"] = doc["completed_at"].isoformat()
    if "upload_failed_at" in doc and isinstance(doc["upload_failed_at"], datetime):
        doc["upload_failed_at"] = doc["upload_failed_at"].isoformat()
    return doc


def get_latest_url(printer_id: str):
    doc = collection_printer.find_one({"printer_id": printer_id}, {"_id": 0})
    if doc:
        return doc.get("url"), doc.get("timestamp")
    return None, None


def send_to_printer(pdf_file: str, doc: dict):
    printer_url, ts = get_latest_url(doc["printer_id"])
    print(f"Latest URL for {doc['printer_id']} @ {ts} => {printer_url}")
    if not printer_url:
        return False, "No printer URL"

    api_url = f"{printer_url.rstrip('/')}/upload-pdf"

    try:
        with open(pdf_file, "rb") as f:
            files = {"file": (os.path.basename(pdf_file), f, "application/pdf")}
            data = {"doc": json.dumps(doc, ensure_ascii=False, default=str)}
            r = requests.post(api_url, files=files, data=data, timeout=(10, 40))

        ok = r.ok
        text = r.text if ok else f"HTTP {r.status_code}: {r.text}"
        return ok, text

    except requests.exceptions.RequestException as e:
        return False, f"Request error: {e}"
    except OSError as e:
        return False, f"File error: {e}"


def get_show_offline_setting() -> bool:
    """อ่าน config จาก MongoDB ว่าจะโชว์ offline printer หรือไม่"""
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")})
    if not doc:
        return True
    frontend_cfg = doc.get("frontend", {})
    val = frontend_cfg.get("show_offline_printer", "True")
    return str(val).lower() == "true"


def check_permission(line_id: str, printer_id: str) -> bool:
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")})
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


# =========================================================
# HTML pages
# =========================================================
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


@app.get("/feedback.html")
def serve_feedback():
    return FileResponse(os.path.join(os.path.dirname(__file__), "feedback.html"))


@app.get("/guide.html")
def serve_guide():
    return FileResponse(os.path.join(os.path.dirname(__file__), "guide.html"))


@app.get("/manage.html")
def serve_manage():
    return FileResponse(os.path.join(os.path.dirname(__file__), "manage.html"))


# =========================================================
# Payment: Generate QR
# =========================================================
@app.get("/generate_qr")
def generate_qr(
    amount: float = Query(..., gt=0),
    printer_id: str = Query(...),
    line_id: str = Query(...),
    total_pages: int = Query(...),
    jobs: str = Query(...),
    prompay_id: Optional[str] = Query(
        None,
        description="PromptPay ID/เบอร์ของร้าน ถ้าไม่ส่งจะใช้ default ของ Payment Gateway",
    ),
):
    try:
        jobs_data = json.loads(jobs)
        description = f"Print {total_pages} pages @ {printer_id} ({line_id})"

        payload = {
            "amount": amount,
            "description": description,
            "user_id": line_id,
            "discount": MODE_DISCOUNT,
        }
        if prompay_id:
            payload["prompay_id"] = prompay_id

        try:
            r = requests.post(
                f"{PAYMENT_API_BASE.rstrip('/')}/payments/qr",
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
        gateway_prompay_id = pay_data.get("prompay_id")

        qr_b64 = pay_data.get("qr_base64")
        gateway_status = pay_data.get("status", "PENDING")
        gateway_payload = pay_data.get("payload")

        if not qr_b64 or not gateway_payment_id:
            raise HTTPException(
                status_code=500,
                detail="Invalid response from payment gateway (missing QR or payment_id)",
            )

        qr_url = f"data:image/png;base64,{qr_b64}"
        ref_id = f"{line_id}_{datetime.utcnow().timestamp()}"

        internal_status = "waiting"
        if gateway_status == "PAID":
            internal_status = "paid"
        elif gateway_status == "CANCELLED":
            internal_status = "cancelled"

        payment_doc = {
            "line_id": line_id,
            "printer_id": printer_id,
            "jobs": jobs_data,
            "total_amount": float(amount),
            "total_pages": total_pages,
            "status": internal_status,
            "created_at": datetime.utcnow(),
            "ref_id": ref_id,
            "payment_type": "promptpay_gateway",
            "base_amount": base_amount,
            "pay_amount": pay_amount,
            "discount": discount,
            "unique_suffix": unique_suffix,
            "prompay_id": gateway_prompay_id,
            "gateway_prompay_id": gateway_prompay_id,
            # backward compat
            "phone_number": gateway_prompay_id,
            "gateway_phone_number": gateway_prompay_id,
            "gateway_payment_id": gateway_payment_id,
            "gateway_base_amount": base_amount,
            "gateway_pay_amount": pay_amount,
            "gateway_discount": discount,
            "gateway_status": gateway_status,
            "gateway_payload": gateway_payload,
        }

        result = collection_payment.insert_one(payment_doc)
        print("Inserted Payment Doc (Mongo _id):", str(result.inserted_id))
        print("Gateway payment_id:", gateway_payment_id)

        headers = {
            "X-Payment-Id": gateway_payment_id,
            "X-Ref-Id": ref_id,
            "X-Pay-Amount": str(pay_amount),
            "X-Base-Amount": str(base_amount),
            "X-Discount": str(discount),
        }

        return JSONResponse(
            content={
                "qr_url": qr_url,
                "ref_id": ref_id,
                "payment_id": gateway_payment_id,
                "base_amount": base_amount,
                "pay_amount": pay_amount,
                "discount": discount,
                "unique_suffix": unique_suffix,
                "prompay_id": gateway_prompay_id,
                "phone_number": gateway_prompay_id,
                "status": internal_status,
            },
            headers=headers,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in generate_qr: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# =========================================================
# Payment: check + sync with gateway
# =========================================================
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
                f"{PAYMENT_API_BASE.rstrip('/')}/payments/{gateway_payment_id}",
                timeout=10,
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

    return {"ref_id": ref_id, "status": status_local, "gateway_status": gateway_status}


# =========================================================
# Payment: webhook from gateway -> print worker
# =========================================================
@app.post("/pay_completed")
async def pay_completed(request: Request):
    try:
        data = await request.json()
        ref_id = data.get("ref_id")
        status = data.get("status", "paid")
        line_id = data.get("line_id")
        printer_id = data.get("printer_id")
        total_amount = data.get("total_amount", 0)
        total_pages = data.get("total_pages", 0)
        jobs = data.get("jobs", [])

        if not ref_id:
            raise HTTPException(status_code=400, detail="Missing ref_id")

        print(f"🔔 Received pay_completed for ref_id={ref_id}, status={status}")

        doc = collection_payment.find_one({"ref_id": ref_id})

        if not doc:
            payment_doc = {
                "ref_id": ref_id,
                "line_id": line_id,
                "printer_id": printer_id,
                "jobs": jobs,
                "total_amount": total_amount,
                "total_pages": total_pages,
                "status": status,
                "created_at": datetime.utcnow(),
                "completed_at": None,
                "payment_type": "direct",
            }
            collection_payment.insert_one(payment_doc)
            doc = payment_doc
            print("🆕 Created new payment doc for direct print:", payment_doc)
        else:
            collection_payment.update_one(
                {"ref_id": ref_id},
                {
                    "$set": {
                        "status": status,
                        "completed_at": datetime.utcnow(),
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

        # 1) Push LINE flex
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
                            {"type": "separator", "margin": "md"},
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
                    "styles": {"body": {"backgroundColor": "#FFFFFF"}},
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

        # 2) Print worker
        def _print_worker(payment_doc: dict):
            try:
                print(f"🖨 [WORKER] Start sending print jobs for ref_id={ref_id}")
                pdf_dir = os.path.join(PDF_DIR, payment_doc["line_id"])
                upload_failed = False

                jobs_ = payment_doc.get("jobs", [])
                if not isinstance(jobs_, list):
                    jobs_ = []

                for idx, job in enumerate(jobs_, start=1):
                    filename = job.get("filename")
                    if not filename:
                        upload_failed = True
                        msg = f"Missing filename in job #{idx}"
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

                    pdf_file = os.path.join(pdf_dir, filename)

                    one_doc = dict(payment_doc)
                    one_doc["jobs"] = [job]

                    ok, msg = send_to_printer(pdf_file, one_doc)
                    print(f"🖨 [WORKER] ({idx}/{len(jobs_)})", pdf_file, ok, msg)

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

        threading.Thread(target=_print_worker, args=(doc,), daemon=True).start()

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

    collection_payment.update_one({"ref_id": ref_id}, {"$set": {"status": "cancelled"}})
    return {"status": "ok", "message": "Payment cancelled"}


# =========================================================
# Printers list / online status
# =========================================================
@app.get("/get_all_printer")
def get_all_printer(
    user_lat: Optional[float] = Query(None),
    user_lon: Optional[float] = Query(None),
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
                    last_seen = datetime.fromisoformat(last_seen)
                elif not isinstance(last_seen, datetime):
                    last_seen = None

                if last_seen:
                    last_seen = last_seen.replace(tzinfo=tz)
                    delta = now - last_seen
                    if delta <= timedelta(minutes=2):
                        status = "online"
        except Exception as e:
            print("❌ Error parsing last_seen:", e)
            status = "offline"

        p["status"] = status

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


# =========================================================
# PDF endpoints
# =========================================================
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
                ).isoformat(),
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


# =========================================================
# LINE callback
# =========================================================
@app.post("/callback")
async def callback(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature")

    try:
        handler.handle(body.decode("utf-8"), signature)
    except Exception as e:
        print("LINE handler error:", e)
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

    # cleanup by size immediately (optional)
    cleanup_pdfs()

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
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "คุณสามารถตั้งค่าการพิมพ์และยืนยันการสั่งพิมพ์ได้จากหน้าเว็บ",
                    "size": "sm",
                    "wrap": True,
                    "margin": "md",
                },
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "margin": "md",
                    "action": {"type": "uri", "label": "เปิดหน้า DeepPrinter", "uri": front_url},
                },
            ],
        },
        "styles": {"body": {"backgroundColor": "#FFFFFF"}},
    }

    line_bot_api.reply_message(
        event.reply_token,
        FlexSendMessage(
            alt_text=f"บันทึกไฟล์ {file_name} เรียบร้อยแล้ว!",
            contents=flex_contents,
        ),
    )


# =========================================================
# History
# =========================================================
@app.get("/get_payment_history/{line_id}")
def get_payment_history(line_id: str):
    docs = list(collection_payment.find({"line_id": line_id}))
    serialized_docs = [serialize_doc(doc) for doc in docs]
    serialized_docs = convert_data_timezone(serialized_docs)
    return {"history": serialized_docs}


# =========================================================
# Feedback
# =========================================================
@app.post("/sent_feedback")
async def sent_feedback(request: Request):
    try:
        data = await request.json()
        uid = data.get("uid")
        topic = data.get("topic")
        message = data.get("message")

        if not uid or not topic or not message:
            raise HTTPException(status_code=400, detail="Missing required fields")

        feedback_doc = {"uid": uid, "topic": topic, "message": message, "created_at": datetime.utcnow()}
        result = db["feedbacks"].insert_one(feedback_doc)
        return {"status": "ok", "feedback_id": str(result.inserted_id)}

    except Exception as e:
        print(f"Error in sent_feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================
# Upload pdf via frontend form
# =========================================================
@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...), uid: str = Form(...)):
    try:
        user_dir = os.path.join(PDF_DIR, uid)
        os.makedirs(user_dir, exist_ok=True)

        file_path = os.path.join(user_dir, file.filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())

        cleanup_pdfs()
        return {"status": "ok", "filename": file.filename}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# =========================================================
# Config endpoints
# =========================================================
@app.get("/get_config")
def get_config():
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")})
    if not doc:
        return {"frontend": {"use_payment": "True"}}
    return {"frontend": doc.get("frontend", {})}


@app.get("/get_config_authen")
def get_config_authen():
    """
    canonical endpoint:
    - returns frontend + node_authen (ensure each value is list[str])
    """
    doc = collection_config.find_one(
        {"_id": ObjectId("68ab0f1c4db5106f558a97a4")},
        {"_id": 0},
    )
    if not doc:
        return {"frontend": {"use_payment": "True"}, "node_authen": {}}

    node_authen = doc.get("node_authen", {})
    fixed_authen = {}
    for k, v in node_authen.items():
        if isinstance(v, str):
            fixed_authen[k] = [v]
        elif isinstance(v, list):
            fixed_authen[k] = v
        else:
            fixed_authen[k] = []

    return {"frontend": doc.get("frontend", {}), "node_authen": fixed_authen}


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


# =========================================================
# Printer URL update
# =========================================================
@app.post("/update_cups_url")
def update_cups_url(printer_id: str = Form(...), url: str = Form(...)):
    try:
        now = datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d %H:%M:%S")
        res = collection_printer.find_one_and_update(
            {"printer_id": printer_id},
            {
                "$setOnInsert": {"created_at": now, "name": printer_id},
                "$set": {"cups_url": url, "last_seen": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return {"status": "ok", "printer": {k: v for k, v in res.items() if k != "_id"}}
    except Exception as e:
        print(f"❌ Error in update_cups_url: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/update_printer_url")
def update_printer_url(printer_id: str = Form(...), url: str = Form(...)):
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
        return {"status": "ok", "printer": {k: v for k, v in res.items() if k != "_id"}}
    except Exception as e:
        print(f"❌ Error in update_printer_url: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/update_printer_status/{printer_id}")
def update_printer_status(
    printer_id: str,
    status: str = Form("online"),
    last_seen: str = Form(None),
):
    try:
        if last_seen is None:
            last_seen = datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d %H:%M:%S")

        res = collection_printer.find_one_and_update(
            {"printer_id": printer_id},
            {
                "$setOnInsert": {"created_at": last_seen, "name": printer_id},
                "$set": {"last_seen": last_seen, "status": status},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

        return {"status": "ok", "printer": {k: v for k, v in res.items() if k != "_id"}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================
# Manage printer data with permission
# =========================================================
@app.get("/get_printer/{printer_id}")
def get_printer(printer_id: str, x_line_uid: str = Header(...)):
    if not check_permission(x_line_uid, printer_id):
        raise HTTPException(status_code=403, detail="Permission denied")
    doc = collection_printer.find_one({"printer_id": printer_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Printer not found")
    return doc


@app.post("/update_printer/{printer_id}")
async def update_printer(printer_id: str, request: Request, x_line_uid: str = Header(...)):
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

    return {"status": "ok", "message": f"Updated printer {printer_id} and node_authen", "uid": x_line_uid}


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
            plist = parsed if isinstance(parsed, list) else []
        except Exception:
            plist = [p.strip() for p in plist.split(",") if p.strip()]

    return {"printer_id": printer_id, "selected_printer": selected, "list_printers": plist}


@app.post("/update_printer_name/{printer_id}")
async def update_printer_name(printer_id: str, request: Request):
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
            update_fields["selected_printer"] = sel.strip() if isinstance(sel, str) else sel

    if "list_printers" in data:
        lp = data.get("list_printers")

        def to_list(v):
            if v is None:
                return None
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            if isinstance(v, str):
                s = v.strip()
                if s == "":
                    return []
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    pass
                return [p.strip() for p in s.split(",") if p.strip()]
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

    collection_printer.update_one({"printer_id": printer_id}, {"$set": update_fields})

    updated = collection_printer.find_one(
        {"printer_id": printer_id},
        {"_id": 0, "selected_printer": 1, "list_printers": 1},
    )

    return {
        "status": "ok",
        "printer_id": printer_id,
        "selected_printer": updated.get("selected_printer"),
        "list_printers": updated.get("list_printers", []),
        "updated_fields": list(update_fields.keys()),
    }


# =========================================================
# Test printer
# =========================================================
@app.post("/test_printer/{printer_id}")
def test_printer(printer_id: str, x_line_uid: str = Header(...)):
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
            "$set": {"status": "submitted", "completed_at": None},
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


# =========================================================
# Register printer
# =========================================================
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
        {"$setOnInsert": {"created_at": now}, "$set": doc},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return {"ok": True, "printer": {k: v for k, v in res.items() if k != "_id"}}
