from fastapi import FastAPI, Request, Query, HTTPException, Form
from fastapi.responses import JSONResponse, Response, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FileMessage
from pymongo import MongoClient
from bson import ObjectId
import base64, os, requests, math, re
from io import BytesIO
from typing import Optional
from pdf2image import convert_from_path
from PyPDF2 import PdfReader
import folium
import requests
import json
from pytz import timezone
from pathlib import Path
from datetime import datetime, timedelta
from fastapi import UploadFile, File, Form, Header
from zoneinfo import ZoneInfo

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

client = MongoClient(MONGO_URL)
db = client[DB_NAME]
collection_printer = db["printers"]
collection_payment = db["payment_historys"]
collection_config = db["config"]

PDF_DIR = "pdfs"
MAX_DISK_USAGE_MB = cfg["MAX_DISK_USAGE_MB"]

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
    
    # API_BASE = "https://3f4f50da0de2.ngrok-free.app"
    API_BASE = cfg["API_BASE"]
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

def send_to_printer(PDF_FILE: str, doc: dict):
    printer_url, ts = get_latest_url(doc['printer_id'])
    print(f"Latest URL for {doc['printer_id']} @ {ts} => {printer_url}")
    if not printer_url:
        return False, "No printer URL"

    api_url = f"{printer_url.rstrip('/')}/upload-pdf"

    try:
        with open(PDF_FILE, "rb") as f:
            files = {
                "file": (os.path.basename(PDF_FILE), f, "application/pdf")
            }
            # IMPORTANT: backend expects `doc` as a *string* form field
            data = {
                "doc": json.dumps(doc, ensure_ascii=False, default=str)
            }

            r = requests.post(
                api_url,
                files=files,
                data=data,              # form fields
                timeout=(10, 40)        # (connect, read) seconds
            )

        ok = r.ok
        # Return more informative text if not 2xx
        text = r.text if ok else f"HTTP {r.status_code}: {r.text}"
        return ok, text

    except requests.exceptions.RequestException as e:
        return False, f"Request error: {e}"
    except OSError as e:
        return False, f"File error: {e}"

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
        status = data.get("status", "paid")
        line_id = data.get("line_id")
        printer_id = data.get("printer_id")
        total_amount = data.get("total_amount", 0)
        total_pages = data.get("total_pages", 0)
        jobs = data.get("jobs", [])

        if not ref_id:
            raise HTTPException(status_code=400, detail="Missing ref_id")

        print(f"🔔 Received pay_completed for ref_id={ref_id}, status={status}")

        # หา doc
        doc = collection_payment.find_one({"ref_id": ref_id})

        if not doc:
            # 👉 กรณี Direct Print (ไม่มีเอกสารใน DB) → สร้างใหม่
            payment_doc = {
                "ref_id": ref_id,
                "line_id": line_id,
                "printer_id": printer_id,
                "jobs": jobs,
                "total_amount": total_amount,
                "total_pages": total_pages,
                "status": status,
                "created_at": datetime.utcnow(),
                "completed_at": datetime.utcnow(),
                "payment_type": "direct"
            }
            collection_payment.insert_one(payment_doc)
            doc = payment_doc
            print("🆕 Created new payment doc for direct print:", payment_doc)

        else:
            # 👉 กรณีมี doc อยู่แล้ว → update status
            collection_payment.update_one(
                {"ref_id": ref_id},
                {"$set": {"status": status, "completed_at": datetime.utcnow()}}
            )
            doc.update({"status": status})

        # ส่งข้อความแจ้งใน LINE
        if line_id:
            try:
                line_bot_api.push_message(
                    line_id,
                    TextSendMessage(text="✅ การสั่งพิมพ์ถูกยืนยันแล้ว\nตรวจสอบสถานะได้ที่: {}/historys.html".format(FRONTEND_BASE_URL))
                )
            except Exception as e:
                print("⚠️ LINE push error:", e)

        # ส่งไฟล์ไปเครื่องพิมพ์
        pdf_dir = os.path.join(PDF_DIR, doc["line_id"])
        upload_failed = False
        for job in doc["jobs"]:
            pdf_file = os.path.join(pdf_dir, job["filename"])
            ok, msg = send_to_printer(pdf_file, doc)
            print("🖨 Send to printer:", pdf_file, ok, msg)

            if not ok:
                upload_failed = True
                collection_payment.update_one(
                    {"ref_id": ref_id},
                    {"$set": {"status": "uploadfail", "completed_at": datetime.utcnow()}}
                )
                break

        if upload_failed:
            return {"status": "error", "message": "Upload to printer failed"}
        else:
            collection_payment.update_one(
                {"ref_id": ref_id},
                {"$set": {"status": "uploaded", "completed_at": datetime.utcnow()}}
            )
            return {"status": "ok", "message": "Payment updated and print job submitted."}

    except Exception as e:
        print(f"❌ Error in pay_completed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === Other existing endpoints (no changes) ===
@app.post("/cancel_payment/{ref_id}")
def cancel_payment(ref_id: str):
    print(f"Cancelling payment for ref_id: {ref_id}")
    
    doc = collection_payment.find_one({"ref_id": ref_id})
    if not doc:
        return JSONResponse(status_code=404, content={"error": "Payment not found"})

    if doc.get("status") not in ["waiting", "cancelled"]:
        return {"status": "ok", "message": f"Payment already {doc['status']}"}

    collection_payment.update_one(
        {"ref_id": ref_id},
        {"$set": {"status": "cancelled"}}
    )

    return {"status": "ok", "message": "Payment cancelled"}

def get_show_offline_setting() -> bool:
    """อ่าน config จาก MongoDB ว่าจะโชว์ offline printer หรือไม่"""
    collection_config = db["config"]
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")})
    if not doc:
        return True  # ถ้าไม่เจอ config ให้ default = True
    frontend_cfg = doc.get("frontend", {})
    val = frontend_cfg.get("show_offline_printer", "True")
    return str(val).lower() == "true"

@app.get("/get_all_printer")
def get_all_printer(user_lat: Optional[float] = Query(None), user_lon: Optional[float] = Query(None)):
    printers = list(collection_printer.find({}, {"_id": 0}))

    # === ตรวจสอบ last_seen ===
    tz = timezone("Asia/Bangkok")
    now = datetime.now(tz)
    for p in printers:
        last_seen = p.get("last_seen")
        status = "offline"
        try:
            if last_seen:
                # last_seen อาจจะเป็น str หรือ datetime
                if isinstance(last_seen, str):
                    last_seen = datetime.fromisoformat(last_seen)
                    if last_seen.tzinfo is None:
                        last_seen = last_seen.replace(tzinfo=tz)
                elif isinstance(last_seen, datetime):
                    if last_seen.tzinfo is None:
                        last_seen = last_seen.replace(tzinfo=tz)
                else:
                    last_seen = None

                if last_seen:
                    last_seen = last_seen.astimezone(tz)
                    delta = now - last_seen
                    print(f"🕒 now: {now} | last_seen: {last_seen} | delta: {delta}")

                    if delta <= timedelta(minutes=2):
                        status = "online"
        except Exception as e:
            print("❌ Error parsing last_seen:", e)
            status = "offline"

        p["status"] = status

    # ✅ check config ว่าจะแสดง offline ไหม
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
# def get_all_printer(user_lat: Optional[float] = Query(None), user_lon: Optional[float] = Query(None)):
#     printers = list(collection_printer.find({}, {"_id": 0}))

#     # === ตรวจสอบ last_seen ===
#     tz = timezone("Asia/Bangkok")
#     now = datetime.now(tz)
#     for p in printers:
#         last_seen = p.get("last_seen")
#         status = "offline"
#         try:
#             if last_seen:
#                 # last_seen อาจจะเป็น str หรือ datetime
#                 if isinstance(last_seen, str):
#                     last_seen = datetime.fromisoformat(last_seen)
#                 elif isinstance(last_seen, datetime):
#                     pass
#                 else:
#                     last_seen = None

#                 if last_seen:
#                     print("last_seen (original):", last_seen, type(last_seen))
#                     print("now:", now, type(now))
#                     last_seen = last_seen.astimezone(tz)
#                     if now - last_seen <= timedelta(minutes=2):
#                         status = "online"
#         except Exception:
#             status = "offline"

#         p["status"] = status

#     # ✅ check config ว่าจะแสดง offline ไหม
#     if not get_show_offline_setting():
#         printers = [p for p in printers if p.get("status") == "online"]

#     # --- คำนวณระยะทาง ---
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

# @app.get("/get_all_printer")
# def get_all_printer( user_lat: Optional[float] = Query(None), user_lon: Optional[float] = Query(None)):
#     printers = list(collection_printer.find({}, {"_id": 0}))

#     # ✅ check config
#     if not get_show_offline_setting():
#         printers = [p for p in printers if p.get("status") == "online"]

#     # --- คำนวณระยะทาง ---
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
    # else:
    #     reply = f"คุณพิมพ์ว่า: {text}"
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
        f"บันทึกไฟล์ {file_name} เรียบร้อยแล้ว!\n"
        f"{FRONTEND_BASE_URL}"
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
    serialized_docs = convert_data_timezone(serialized_docs)

    return {"history": serialized_docs}


# === Serve feedback.html ===
@app.get("/feedback.html")
def serve_feedback():
    return FileResponse(os.path.join(os.path.dirname(__file__), "feedback.html"))


# === API: Sent Feedback ===
@app.post("/sent_feedback")
async def sent_feedback(request: Request):
    try:
        data = await request.json()
        uid = data.get("uid")
        topic = data.get("topic")
        message = data.get("message")
        # timestamp = data.get("timestamp")

        if not uid or not topic or not message:
            raise HTTPException(status_code=400, detail="Missing required fields")

        feedback_doc = {
            "uid": uid,
            "topic": topic,
            "message": message,
            # "timestamp": datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp,
            "created_at": datetime.utcnow()
        }

        result = db["feedbacks"].insert_one(feedback_doc)
        return {"status": "ok", "feedback_id": str(result.inserted_id)}

    except Exception as e:
        print(f"Error in sent_feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    

# === Serve guide.html ===
@app.get("/guide.html")
def serve_guide():
    return FileResponse(os.path.join(os.path.dirname(__file__), "guide.html"))


# === API: Upload PDF ===
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
        {"ref_id": ref_id},
        {"$set": {"status": status, "completed_at": datetime.utcnow()}}
    )

    return {"status": "ok", "message": f"Payment {ref_id} updated to {status}"}

# @app.get("/get_config")
# def get_config():
#     collection_config = db["config"]
#     doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")}, {"_id": 0})
#     if not doc:
#         return {"frontend": {"show_offline_printer": "True", "use_payment": "True"}}
#     return doc

@app.get("/get_config")
def get_config():
    collection_config = db["config"]
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")})
    if not doc:
        return {"frontend": {"use_payment": "True"}}  # ค่า default
    return {"frontend": doc.get("frontend", {})}

@app.get("/get_config_authen")
def get_config():
    collection_config = db["config"]
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")}, {"_id": 0})
    if not doc:
        return {
            "frontend": {"use_payment": "True"},
            "node_authen": {}
        }
    return {
        "frontend": doc.get("frontend", {}),
        "node_authen": doc.get("node_authen", {})
    }



@app.post("/update_printer_url")
def update_printer_url(
    printer_id: str = Form(...),
    url: str = Form(...)
):
    try:
        result = collection_printer.update_one(
            {"printer_id": printer_id},
            {"$set": {"url": url}}
        )

        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"Printer {printer_id} not found")

        return {"status": "ok", "message": f"Updated URL for {printer_id} to {url}"}

    except Exception as e:
        print(f"❌ Error in update_printer_url: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === Serve manage.html ===
# === Serve manage.html ===
@app.get("/manage.html")
def serve_manage():
    return FileResponse(os.path.join(os.path.dirname(__file__), "manage.html"))

# === Permission Helper ===
def check_permission(line_id: str, printer_id: str) -> bool:
    """ตรวจสอบสิทธิ์การเข้าถึง printer ตาม rule ใหม่:
       - admin → เข้าได้ทุกเครื่อง
       - printer_id ไม่มีใน node_authen → เข้าได้
       - printer_id มี แต่ [] → เข้าได้
       - printer_id มี และ list ไม่ว่าง → ต้องมี line_id อยู่ใน list
    """
    doc = collection_config.find_one(
        {"_id": ObjectId("68ab0f1c4db5106f558a97a4")}
    )
    if not doc:
        return False

    node_authen = doc.get("node_authen", {})

    # ✅ admin → list
    admin_ids = node_authen.get("admin", [])
    if isinstance(admin_ids, str):
        admin_ids = [admin_ids]
    if line_id in admin_ids:
        return True

    # ✅ ถ้า printer_id ไม่มี key → เข้าได้ทุกคน
    if printer_id not in node_authen:
        print(f"ℹ️ Printer {printer_id} not found in node_authen → allow all")
        return True

    assigned_ids = node_authen.get(printer_id, [])
    if isinstance(assigned_ids, str):
        assigned_ids = [assigned_ids]

    # ✅ ถ้าเป็น [] → เข้าได้ไม่จำกัด
    if not assigned_ids:
        print(f"ℹ️ Printer {printer_id} has empty list → allow all")
        return True

    # ✅ ถ้า line_id อยู่ใน list → ผ่าน
    if line_id in assigned_ids:
        return True

    # ❌ อื่น ๆ → Forbidden
    print(f"🚫 Permission denied for {line_id} on {printer_id}")
    return False





# === API: Get Printer ===
@app.get("/get_printer/{printer_id}")
def get_printer(printer_id: str, x_line_uid: str = Header(...)):
    if not check_permission(x_line_uid, printer_id):
        raise HTTPException(status_code=403, detail="Permission denied")

    doc = collection_printer.find_one({"printer_id": printer_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Printer not found")
    return doc


# === API: Update Printer ===
@app.post("/update_printer/{printer_id}")
async def update_printer(
    printer_id: str, 
    request: Request, 
    x_line_uid: str = Header(...)
):
    # ✅ ตรวจสิทธิ์
    if not check_permission(x_line_uid, printer_id):
        raise HTTPException(status_code=403, detail="Permission denied")

    # ✅ อ่าน payload
    data = await request.json()

    # ✅ update printer data
    result = collection_printer.update_one(
        {"printer_id": printer_id},
        {"$set": data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Printer not found")

    # ✅ update node_authen mapping เป็น list
    collection_config.update_one(
        {"_id": ObjectId("68ab0f1c4db5106f558a97a4")},
        {"$addToSet": {f"node_authen.{printer_id}": x_line_uid}}
    )

    return {
        "status": "ok",
        "message": f"Updated printer {printer_id} and node_authen",
        "uid": x_line_uid
    }


# === API: Get Config Authen ===
@app.get("/get_config_authen")
def get_config_authen():
    """ดึงค่า config ทั้ง frontend และ node_authen (เป็น list เสมอ)"""
    collection_config = db["config"]
    doc = collection_config.find_one({"_id": ObjectId("68ab0f1c4db5106f558a97a4")}, {"_id": 0})
    if not doc:
        return {
            "frontend": {"use_payment": "True"},
            "node_authen": {}
        }

    node_authen = doc.get("node_authen", {})
    # ✅ ensure ทุกค่าเป็น list
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
        "node_authen": fixed_authen
    }


# === API: Debug Authen ===
@app.get("/debug_authen")
def debug_authen():
    """ตรวจสอบ node_authen ล่าสุดจาก MongoDB (คืนค่าเป็น list เสมอ)"""
    doc = collection_config.find_one(
        {"_id": ObjectId("68ab0f1c4db5106f558a97a4")},
        {"node_authen": 1, "_id": 0}
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
    last_seen: str = Form(None)   # 👈 client อาจส่งเวลามา หรือไม่ส่งก็ได้
):
    try:
        if last_seen is None:
            # ถ้า client ไม่ส่ง → ใช้เวลาประเทศไทยปัจจุบัน
            last_seen = datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d %H:%M:%S")

        result = collection_printer.update_one(
            {"printer_id": printer_id},
            {"$set": {"last_seen": last_seen, "status": status}}
        )

        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"Printer {printer_id} not found")

        return {
            "status": "ok",
            "printer_id": printer_id,
            "last_seen": last_seen,
            "set_status": status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# === API: Get printer name/list from MongoDB ===
@app.get("/get_printer_name/{printer_id}")
def get_printer_name(printer_id: str):
    """
    ดึงชื่อเครื่องพิมพ์ที่เลือก (selected_printer) และรายการเครื่องพิมพ์ทั้งหมด (list_printers)
    จากคอลเลกชัน dimonwall.printers ตาม printer_id ที่ส่งมา
    รูปแบบผลลัพธ์:
    {
        "printer_id": "...",
        "selected_printer": "PDF",
        "list_printers": ["PDF", "EPSON1"]
    }
    """
    doc = collection_printer.find_one(
        {"printer_id": printer_id},
        {"_id": 0, "selected_printer": 1, "list_printers": 1}
    )

    if not doc:
        raise HTTPException(status_code=404, detail="Printer not found")

    selected = doc.get("selected_printer")
    plist = doc.get("list_printers", [])

    # เผื่อกรณีใน DB เก็บเป็น string ให้แปลงเป็น list
    if isinstance(plist, str):
        # รองรับทั้งรูปแบบ "PDF,EPSON1" หรือ "['PDF','EPSON1']"
        try:
            # ลอง parse เป็น JSON ก่อน
            parsed = json.loads(plist)
            if isinstance(parsed, list):
                plist = parsed
            else:
                raise ValueError
        except Exception:
            # fallback แยกด้วย comma
            plist = [p.strip() for p in plist.split(",") if p.strip()]

    return {
        "printer_id": printer_id,
        "selected_printer": selected,
        "list_printers": plist
    }
    
# === API: Update printer selected_printer / list_printers (no auth) ===
@app.post("/update_printer_name/{printer_id}")
async def update_printer_name(
    printer_id: str,
    request: Request
):
    """
    อัปเดตฟิลด์ selected_printer และ/หรือ list_printers ของเครื่องพิมพ์ที่ระบุ
    - ส่งอย่างใดอย่างหนึ่งหรือทั้งคู่ก็ได้
    - ถ้าไม่ได้ส่งฟิลด์นั้นมา -> ไม่อัปเดตฟิลด์นั้น
    Payload ตัวอย่าง:
      {
        "selected_printer": "PDF",
        "list_printers": ["PDF", "EPSON1"]
      }
    หรือ
      {
        "list_printers": "PDF,EPSON1"
      }
    """
    # ✅ หาเอกสารเดิม
    current = collection_printer.find_one({"printer_id": printer_id})
    if not current:
        raise HTTPException(status_code=404, detail="Printer not found")

    # ✅ อ่าน payload
    try:
        data = await request.json()
    except Exception:
        data = {}

    update_fields = {}

    # --- selected_printer: อัปเดตเฉพาะกรณีส่งมาใน payload ---
    if "selected_printer" in data:
        sel = data.get("selected_printer")
        if sel is not None:
            if isinstance(sel, str):
                sel = sel.strip()
            update_fields["selected_printer"] = sel

    # --- list_printers: อัปเดตเฉพาะกรณีส่งมาใน payload ---
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
                # ลอง parse JSON-string ก่อน
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip() != ""]
                except Exception:
                    pass
                # fallback: คั่นด้วย comma
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

    # ✅ ทำการอัปเดต
    result = collection_printer.update_one(
        {"printer_id": printer_id},
        {"$set": update_fields}
    )

    # อ่านค่าล่าสุดคืนให้ผู้เรียก
    updated = collection_printer.find_one(
        {"printer_id": printer_id},
        {"_id": 0, "selected_printer": 1, "list_printers": 1}
    )

    return {
        "status": "ok" if result.matched_count else "not_found",
        "printer_id": printer_id,
        "selected_printer": updated.get("selected_printer"),
        "list_printers": updated.get("list_printers", []),
        "updated_fields": list(update_fields.keys()),
    }

# === API: Test print with static/test.pdf ===
# === API: Test print with static/test.pdf (with payment upsert) ===
@app.post("/test_printer/{printer_id}")
def test_printer(
    printer_id: str,
    x_line_uid: str = Header(...)
):
    """
    พิมพ์ไฟล์ static/test.pdf ไปยัง node ของเครื่องพิมพ์ที่ระบุด้วย printer_id
    และ 'จอง' เอกสาร payment ใน MongoDB ก่อน เพื่อให้ RPi อัปเดตสถานะผ่าน /update_status/{ref_id} ได้เสมอ
    """
    # 1) ตรวจสิทธิ์
    if not check_permission(x_line_uid, printer_id):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 2) ตรวจว่าเครื่องพิมพ์มีอยู่ใน DB
    pdoc = collection_printer.find_one({"printer_id": printer_id}, {"_id": 0})
    if not pdoc:
        raise HTTPException(status_code=404, detail="Printer not found")

    # 3) หาไฟล์ test.pdf
    test_path = (Path(__file__).resolve().parent / "static" / "test.pdf")
    if not test_path.exists():
        raise HTTPException(status_code=404, detail=f"Test file not found: {test_path}")

    # 4) นับจำนวนหน้า (ถ้าทำได้)
    try:
        reader = PdfReader(str(test_path))
        total_pages = len(reader.pages)
    except Exception:
        total_pages = 0

    # 5) เตรียม ref_id + jobs
    ref_id = f"TEST_{printer_id}_{int(datetime.utcnow().timestamp())}"
    job_entry = {
        "filename": test_path.name,   # node อาจเปลี่ยนชื่อไฟล์ตอนอัปโหลด
        "pages": "all",
        "color": "bw",
        "copies": 1,
        "page_count": total_pages,
        "price_per_page": 0,
        "total_price": 0
    }

    selected = pdoc.get("selected_printer")

    # 6) 👇 จองเอกสาร payment ไว้ก่อน (UPSERT)
    #    เพื่อให้ /update_status/{ref_id} จาก RPi ไม่โดน 404 แม้เป็นงาน test
    collection_payment.update_one(
        {"ref_id": ref_id},
        {
            # สร้างครั้งแรกเท่านั้น
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
            # อัปเดตสถานะล่าสุดให้เป็น submitted (กันกรณี ref_id ซ้ำโดยบังเอิญ)
            "$set": {
                "status": "submitted",
                "completed_at": None
            }
        },
        upsert=True
    )

    # 7) จัด payload ให้ node (RPi) ใช้งาน (ฝั่ง node คาดหวังให้ 'doc' เป็น JSON string form field)
    send_doc = {
        "ref_id": ref_id,
        "line_id": x_line_uid,
        "printer_id": printer_id,
        "selected_printer": selected,
        "jobs": [job_entry],
        "total_amount": 0,
        "total_pages": total_pages,
        "status": "paid",   # จำลองเป็นจ่ายแล้วเพื่อทดสอบ pipeline ส่งงานพิมพ์
        "created_at": datetime.utcnow().isoformat(),
        "payment_type": "direct",
        "note": "test_print_from_server"
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
        "message": msg
    }
