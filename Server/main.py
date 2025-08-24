from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FileMessage
from pymongo import MongoClient
from bson import ObjectId

import base64, os, requests, math, re
from io import BytesIO
from datetime import datetime
from typing import Optional

from pdf2image import convert_from_path
from PyPDF2 import PdfReader
from promptpay import qrcode

import folium
import requests
import json
import io

from pytz import timezone
from dateutil import parser, tz

# import json
# === CONFIG ===
FIXED_PROMPTPAY_NUMBER = "0805471749"
LINE_CHANNEL_SECRET = "e48db91970c8ff61adee8f9360abeae1"
LINE_CHANNEL_ACCESS_TOKEN = "JEPIUJhhospgCynVPo8Rx7iwrbyvF81Ux29xLQ/mZadS3NiHX07HBYgBz1/eHdiXwbQ6hmxCg0M1A50mR7BCWUMzfWIo3JlUtpQDVj+WE1iVP4BN4RWIrV8Q77PiB14r/HlD4eY+wAkPVDxmUHqNnAdB04t89/1O/w1cDnyilFU="
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://dw-printer.onrender.com")

MONGO_URL = "mongodb+srv://phawitboo:JO3hoCXWCSXECrGB@cluster0.fvc5db5.mongodb.net"
DB_NAME = "dimonwall"

client = MongoClient(MONGO_URL)
db = client[DB_NAME]
collection_printer = db["printers"]
collection_payment = db["payment_historys"]
# collection_users = db["users"]

PDF_DIR = "pdfs"
MAX_DISK_USAGE_MB = 500

app = FastAPI()

# === Enable CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Static (images) ===
app.mount("/images", StaticFiles(directory="images"), name="images")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def generate_folium_map(user_lat=None, user_lon=None):
    """
    Fetches printer data and generates a Folium map.
    :param user_lat: User's latitude
    :param user_lon: User's longitude
    :return: A string containing the HTML of the generated map.
    """
    
    API_BASE = "https://dw-printer-lts.onrender.com"
    url = f"{API_BASE}/get_all_printer"
    
    if user_lat and user_lon:
        url += f"?user_lat={user_lat}&user_lon={user_lon}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)
        data = response.json()
        printers = data.get('printers', [])
    except requests.exceptions.RequestException as e:
        print(f"Error fetching printer data: {e}")
        printers = []
        
    # Set the initial map center. If user coordinates are available, use them.
    # Otherwise, default to a central location like Bangkok, Thailand.
    if user_lat and user_lon:
        map_center = [user_lat, user_lon]
        zoom_start = 13
    else:
        map_center = [13.7563, 100.5018]
        zoom_start = 11

    m = folium.Map(location=map_center, zoom_start=zoom_start)

    # Add markers for each printer
    for printer in printers:
        if 'latitude' in printer and 'longitude' in printer:
            lat = float(printer['latitude'])
            lon = float(printer['longitude'])
            
            status = printer.get('status', 'offline')
            location_name = printer.get('location_name', 'Unknown Printer')
            
            # Determine marker color based on status
            color = 'green' if status == 'online' else 'red'
            
            # Create popup with a link
            status = printer.get('status', 'offline')
            location_name = printer.get('location_name', 'Unknown Printer')
            open_time = printer.get('open_time', 'N/A')
            close_time = printer.get('close_time', 'N/A')

            popup_html = f"""
            <h4>{location_name}</h4>
            <p>Status: {status}</p>
            <p>Open: {open_time} - {close_time}</p>
            <a href="index.html?uid=YOUR_LINE_ID&selected_printer={printer['printer_id']}">Select this printer</a>
            """
            
            folium.Marker(
                location=[lat, lon],
                popup=popup_html,
                icon=folium.Icon(color=color)
            ).add_to(m)
            
    # Add a marker for the user's location if available
    if user_lat and user_lon:
        folium.Marker(
            location=[user_lat, user_lon],
            popup="Your Location",
            icon=folium.Icon(color='blue', icon='info-sign')
        ).add_to(m)

    # Return the map as a string
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
                except:
                    pass
    total_mb = total_size / (1024 * 1024)
    if total_mb > MAX_DISK_USAGE_MB:
        file_list.sort(key=lambda x: x[2])  # oldest first
        while total_mb > MAX_DISK_USAGE_MB and file_list:
            path, size, _ = file_list.pop(0)
            try:
                os.remove(path)
                total_mb -= size / (1024 * 1024)
            except:
                pass


def get_latest_url(printer_id: str):
    doc = collection_printer.find_one({"printer_id": printer_id}, {"_id": 0})
    if doc:
        return doc.get("url"), doc.get("timestamp")
    return None, None


def send_to_printer(PDF_FILE: str, UID: str, printer_id: str):
    printer_url, ts = get_latest_url(printer_id)
    print(f"Latest URL for {printer_id} @ {ts} => {printer_url}")
    if not printer_url:
        return False, "No printer URL"
    API_URL = f"{printer_url}/upload-pdf"
    try:
        with open(PDF_FILE, "rb") as f:
            files = {"file": (os.path.basename(PDF_FILE), f, "application/pdf")}
            data = {"uid": UID}
            r = requests.post(API_URL, files=files, data=data, timeout=30)
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)


# --- Distance helpers for get_all_printer sorting ---
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between 2 coords (km)."""
    R = 6371.0
    phi1, lam1, phi2, lam2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dphi = phi2 - phi1
    dlam = lam2 - lam1
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
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


# === QR Payment ===
@app.get("/generate_qr")
def generate_qr(
    amount: float = Query(..., gt=0),
    printer_id: str = Query(...),
    line_id: str = Query(...),
    total_pages: int = Query(...),
    jobs: str = Query(...),
):
    try:
        qr_image_path = "images/qr.png"
        if not os.path.exists(qr_image_path):
            return JSONResponse(status_code=500, content={"error": "QR image file not found"})
        
        jobs_data = json.loads(jobs)   # ✅ ปลอดภัยกว่า eval
        
        payment_doc = {
            "line_id": line_id,
            "printer_id": printer_id,
            "jobs": jobs_data,
            "total_amount": amount,
            "total_pages": total_pages,
            "status": "waiting",
            "created_at": datetime.utcnow(),
            "ref_id": f"{line_id}_{datetime.utcnow().timestamp()}",
            "payment_type": "dummy_promptpay"
        }
        result = collection_payment.insert_one(payment_doc)
        payment_id = str(result.inserted_id)

        print("Inserted Payment Doc:")
        print(payment_doc)

        headers = {
            "X-Payment-Id": payment_id,
            "X-Ref-Id": payment_doc.get("ref_id"),
        }
        return FileResponse(qr_image_path, media_type="image/png", headers=headers)

    except Exception as e:
        print(f"Error in generate_qr: {e}")
        return {"error": str(e)}


@app.get("/check_payment/{ref_id}")
def check_payment(ref_id: str):
    doc = collection_payment.find_one({"ref_id": ref_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    status = doc.get("status")
    print(f"Checking payment for ref_id: {ref_id}")
    print(f"  - current status: {status}")

    return {"ref_id": ref_id, "status": status}



# === New API for Payment Gateway Webhook ===
@app.post("/pay_completed")
async def pay_completed(request: Request):
    try:
        data = await request.json()
        ref_id = data.get("ref_id")
        
        if not ref_id:
            raise HTTPException(status_code=400, detail="Missing ref_id")

        print(f"Received webhook for ref_id: {ref_id}. Updating status to 'paid'.")

        # หาเอกสารที่ตรงกับ ref_id
        doc = collection_payment.find_one({"ref_id": ref_id})
        if not doc:
            raise HTTPException(status_code=404, detail="Payment document with this ref_id not found")

        # ถ้าจ่ายแล้ว → ข้าม
        if doc.get("status") == "paid":
            return {"status": "ok", "message": "Payment already completed. No action taken."}

        # อัปเดตสถานะเป็น paid
        collection_payment.update_one(
            {"ref_id": ref_id},
            {"$set": {"status": "paid", "completed_at": datetime.utcnow()}},
        )

        pdf_dir = os.path.join(PDF_DIR, doc["line_id"])
        upload_failed = False

        for job in doc["jobs"]:
            pdf_file = os.path.join(pdf_dir, job["filename"])
            ok, msg = send_to_printer(pdf_file, doc["line_id"], doc["printer_id"])
            print("Send to printer:", pdf_file, ok, msg)

            if not ok:
                upload_failed = True
                # update status เป็น uploadfail และเก็บ error
                collection_payment.update_one(
                    {"ref_id": ref_id},
                    {"$set": {
                        "status": "uploadfail",
                        "upload_error": msg,
                        "upload_failed_at": datetime.utcnow()
                    }}
                )
                break  # ไม่ต้องส่งไฟล์ที่เหลือแล้ว

        if upload_failed:
            return {"status": "error", "message": "Payment updated but upload to printer failed."}
        else:
            return {"status": "ok", "message": "Payment updated and print job submitted."}

    except Exception as e:
        print(f"Error in pay_completed: {e}")
        raise HTTPException(status_code=500, detail=str(e))




# @app.post("/pay_completed")
# async def pay_completed(request: Request):
#     """
#     Simulates a webhook from a payment gateway.
#     It receives a ref_id and updates the payment status in MongoDB.
#     """
#     try:
#         data = await request.json()
#         ref_id = data.get("ref_id")
        
#         if not ref_id:
#             raise HTTPException(status_code=400, detail="Missing ref_id")

#         print(f"Received webhook for ref_id: {ref_id}. Updating status to 'paid'.")
        
#         doc = collection_payment.find_one({"ref_id": ref_id})
#         if not doc:
#             raise HTTPException(status_code=404, detail="Payment document with this ref_id not found")

#         # Check if the payment has already been completed to prevent duplicates
#         if doc.get("status") == "paid":
#             return {"status": "ok", "message": "Payment already completed. No action taken."}

#         # Update the status
#         update_result = collection_payment.update_one(
#             {"ref_id": ref_id},
#             {"$set": {"status": "paid", "completed_at": datetime.utcnow()}},
#         )

#         if update_result.matched_count == 0:
#             raise HTTPException(status_code=404, detail="Payment document with this ref_id not found")

#         # --- THIS IS THE CRUCIAL CHANGE ---
#         # Save history and send print jobs ONLY when the status is updated to completed.
#         history_doc = doc.copy()
#         history_doc['status'] = 'paid'
#         history_doc['completed_at'] = datetime.utcnow()
#         history_doc.pop('_id')
#         collection_users.insert_one(history_doc)

#         # Send PDFs to printer
#         pdf_dir = os.path.join(PDF_DIR, doc["line_id"])
#         for job in doc["jobs"]:
#             pdf_file = os.path.join(pdf_dir, job["filename"])
#             ok, msg = send_to_printer(pdf_file, doc["line_id"], doc["printer_id"])
#             print("Send to printer:", pdf_file, ok, msg)
        
#         return {"status": "ok", "message": "Payment status updated and print job submitted."}

#     except Exception as e:
#         print(f"Error in pay_completed: {e}")
#         raise HTTPException(status_code=500, detail=str(e))

# === Other existing endpoints (no changes) ===
@app.post("/cancel_payment/{payment_id}")
def cancel_payment(payment_id: str):
    result = collection_payment.update_one(
        {"_id": ObjectId(payment_id)},
        {"$set": {"status": "cancelled", "cancelled_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        return JSONResponse(status_code=404, content={"error": "Payment not found"})
    return {"status": "cancelled"}

collection_config = db["config"]

def get_show_offline_setting() -> bool:
    """อ่าน config จาก MongoDB ว่าจะโชว์ offline printer หรือไม่"""
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")})
    if not doc:
        return True  # ถ้าไม่เจอ config ให้ default = True
    frontend_cfg = doc.get("frontend", {})
    val = frontend_cfg.get("show_offline_printer", "True")
    return str(val).lower() == "true"


@app.get("/get_all_printer")
def get_all_printer(
    user_lat: Optional[float] = Query(None),
    user_lon: Optional[float] = Query(None),
):
    printers = list(collection_printer.find({}, {"_id": 0}))

    # ✅ check config
    if not get_show_offline_setting():
        printers = [p for p in printers if p.get("status") == "online"]

    # --- คำนวณระยะทาง ---
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


# @app.get("/get_all_printer")
# def get_all_printer(
#     user_lat: Optional[float] = Query(None),
#     user_lon: Optional[float] = Query(None),
# ):
#     printers = list(collection_printer.find({}, {"_id": 0}))
#     if user_lat is not None and user_lon is not None:
#         for p in printers:
#             try:
#                 lat, lon = float(p.get("lat")), float(p.get("lon"))
#                 p["distance_km"] = round(haversine_km(user_lat, user_lon, lat, lon), 3)
#             except Exception:
#                 p["distance_km"] = None
#         nearest = sorted(
#             [p for p in printers if p["distance_km"] is not None],
#             key=lambda x: x["distance_km"],
#         )
#         top3 = nearest[:3]
#         remaining = [p for p in printers if p not in top3]
#         remaining_sorted = sorted(remaining, key=_printer_id_number)
#         ordered = top3 + remaining_sorted
#         return {"printers": ordered, "sorted_by": "nearest_then_id"}
#     ordered = sorted(printers, key=lambda p: str(p.get("location_name", "")))
#     return {"printers": ordered, "sorted_by": "location_name"}

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
        except:
            total_pages = 0
        file_infos.append(
            {
                "filename": filename,
                "url": f"/get-pdf/{line_id}/{filename}",
                "total_pages": total_pages,
                "upload_timestamp": datetime.fromtimestamp(os.path.getmtime(file_path)),
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
    else:
        reply = f"คุณพิมพ์ว่า: {text}"
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
    reply_text = (
        f"📄 บันทึกไฟล์ `{file_name}` เรียบร้อยแล้ว\n"
        f"🔗 {FRONTEND_BASE_URL}/index.html?uid={user_id}"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))



def serialize_doc(doc):
    """แปลง ObjectId และ datetime -> str"""
    doc["_id"] = str(doc["_id"])
    if "created_at" in doc and isinstance(doc["created_at"], datetime):
        doc["created_at"] = doc["created_at"].isoformat()
    if "completed_at" in doc and isinstance(doc["completed_at"], datetime):
        doc["completed_at"] = doc["completed_at"].isoformat()
    return doc

# === API: Get Payment History ===
@app.get("/get_payment_history/{line_id}")
def get_payment_history(line_id: str):
    docs = list(collection_payment.find({"line_id": line_id}))
    serialized_docs = [serialize_doc(doc) for doc in docs]
    return {"history": serialized_docs}

# @app.get("/get_print_history/{line_id}")
# def get_print_history(
#     line_id: str,
#     user_timezone: Optional[str] = Query("UTC")
# ):
#     try:
#         user_tz = timezone(user_timezone)
#     except Exception:
#         raise HTTPException(status_code=400, detail="Invalid time zone")

#     history = list(collection_users.find({"line_id": line_id}, {"_id": 0}))
#     all_printers = list(collection_printer.find({}, {"_id": 0}))
#     printer_map = {p.get("printer_id"): p.get("location_name", "ไม่ระบุ") for p in all_printers}
#     history.sort(key=lambda x: x.get("completed_at", datetime.min), reverse=True)
    
#     processed_history = []
#     for item in history:
#         if 'jobs' in item and isinstance(item['jobs'], list):
#             printer_id = item.get('printer_id')
#             item['printer_name'] = printer_map.get(printer_id, "ไม่ระบุ")

#             # Convert UTC datetime to user's local timezone
#             completed_at_utc = item.get('completed_at')
#             if completed_at_utc:
#                 completed_at_local = completed_at_utc.replace(tzinfo=tz.UTC).astimezone(user_tz)
#                 item['completed_at_local'] = completed_at_local.isoformat()
            
#             processed_history.append(item)
    
#     return {"history": processed_history}
