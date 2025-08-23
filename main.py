from fastapi import FastAPI, Request, Query
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FileMessage
import os
from datetime import datetime
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import base64
from io import BytesIO
from pdf2image import convert_from_path
from PIL import Image
from PyPDF2 import PdfReader
from promptpay import qrcode

# === CONFIG ===
FIXED_PROMPTPAY_NUMBER = "0805471749"
LINE_CHANNEL_SECRET = 'e48db91970c8ff61adee8f9360abeae1'
LINE_CHANNEL_ACCESS_TOKEN = "JEPIUJhhospgCynVPo8Rx7iwrbyvF81Ux29xLQ/mZadS3NiHX07HBYgBz1/eHdiXwbQ6hmxCg0M1A50mR7BCWUMzfWIo3JlUtpQDVj+WE1iVP4BN4RWIrV8Q77PiB14r/HlD4eY+wAkPVDxmUHqNnAdB04t89/1O/w1cDnyilFU="
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://dw-printer-1.onrender.com")

app = FastAPI()

# === Enable CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ================= QR ===================
@app.get("/generate_qr")
def generate_qr(amount: float = Query(..., gt=0, description="จำนวนเงิน (บาท)")):
    try:
        payload = qrcode.generate_payload(FIXED_PROMPTPAY_NUMBER, amount)
        img = qrcode.to_image(payload)
        img_io = BytesIO()
        img.save(img_io, format='PNG')
        img_io.seek(0)
        return StreamingResponse(img_io, media_type="image/png")
    except Exception as e:
        return {"error": str(e)}

# ================= Printer ===================
@app.get("/get_all_printer")
def get_all_printer():
    printers = [
        {
            "id": "P001",
            "location_name": "สำนักงานชั้น 1",
            "lat": 13.736717,
            "lon": 100.523186,
            "url": "http://printer-001.local",
            "status": "online",
            "bw_price": 1.00,
            "color_price": 5.00
        },
        {
            "id": "P002",
            "location_name": "ห้องสมุดกลาง",
            "lat": 13.737100,
            "lon": 100.524200,
            "url": "http://printer-002.local",
            "status": "offline",
            "bw_price": 1.20,
            "color_price": 6.00
        },
    ]
    return {"printers": printers}

# ================= PDF ===================
@app.get("/preview-pdf/{line_id}/{filename}")
def preview_pdf_as_images(line_id: str, filename: str):
    file_path = os.path.join("pdfs", line_id, filename)
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

@app.get("/get-pdf/{line_id}/{filename}")
def get_pdf(line_id: str, filename: str):
    file_path = os.path.join("pdfs", line_id, filename)
    if not os.path.exists(file_path):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    with open(file_path, "rb") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Access-Control-Allow-Origin": "*",
            "X-Frame-Options": "ALLOWALL"
        }
    )

@app.get("/list-pdfs/{line_id}")
def list_pdfs(line_id: str):
    folder_path = os.path.join("pdfs", line_id)
    if not os.path.exists(folder_path):
        return JSONResponse(status_code=404, content={"error": "No PDF files found for this user."})

    file_infos = []
    for filename in os.listdir(folder_path):
        if filename.lower().endswith(".pdf"):
            file_path = os.path.join(folder_path, filename)
            total_pages = 0
            try:
                reader = PdfReader(file_path)
                total_pages = len(reader.pages)
            except Exception as e:
                print(f"⚠️ อ่าน {filename} ไม่ได้: {e}")
            file_infos.append({
                "filename": filename,
                "url": f"/get-pdf/{line_id}/{filename}",
                "total_pages": total_pages
            })
    return {"files": file_infos}

# ================= LINE CALLBACK ===================
@app.post("/callback")
async def callback(request: Request):
    body = await request.body()
    signature = request.headers['X-Line-Signature']
    try:
        handler.handle(body.decode('utf-8'), signature)
    except Exception as e:
        print(f"Error: {e}")
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    if text.startswith("/print"):
        reply = "🖨 สั่งพิมพ์ (mock)"
    else:
        reply = f"คุณพิมพ์ว่า: {text}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

@handler.add(MessageEvent, message=FileMessage)
def handle_file_message(event):
    message_id = event.message.id
    file_name = event.message.file_name
    user_id = event.source.user_id

    # save user file
    user_dir = os.path.join("pdfs", user_id)
    os.makedirs(user_dir, exist_ok=True)
    save_path = os.path.join(user_dir, file_name)

    file_content = line_bot_api.get_message_content(message_id).content
    with open(save_path, 'wb') as f:
        f.write(file_content)

    # file list
    all_files = [f for f in os.listdir(user_dir) if f.lower().endswith(".pdf")]
    all_files.sort(key=lambda f: os.path.getmtime(os.path.join(user_dir, f)), reverse=True)
    file_list_text = "\n".join([f"{idx+1}. {fname}" for idx, fname in enumerate(all_files)])

    preview_link = f"{FRONTEND_BASE_URL}/index.html?uid={user_id}"

    reply_text = f"""📄 บันทึกไฟล์ `{file_name}` เรียบร้อยแล้ว

Your files:
{file_list_text}

🔗 ดูทั้งหมดที่: {preview_link}
"""
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
