from fastapi import FastAPI, Request, Query
# from fastapi.responses import FileResponse, Response
from linebot import LineBotApi, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    FileMessage
)
import os
from datetime import datetime
from fastapi.responses import JSONResponse, FileResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import base64
from io import BytesIO
from pdf2image import convert_from_path
from PIL import Image
import re
from PyPDF2 import PdfReader

# from fastapi import FastAPI, Query
# from fastapi.responses import StreamingResponse
from promptpay import qrcode
# from io import BytesIO

FIXED_PROMPTPAY_NUMBER = "0805471749"

app = FastAPI()

# === Enable CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# === LINE TOKEN ===
LINE_CHANNEL_SECRET = 'e48db91970c8ff61adee8f9360abeae1'
LINE_CHANNEL_ACCESS_TOKEN = "JEPIUJhhospgCynVPo8Rx7iwrbyvF81Ux29xLQ/mZadS3NiHX07HBYgBz1/eHdiXwbQ6hmxCg0M1A50mR7BCWUMzfWIo3JlUtpQDVj+WE1iVP4BN4RWIrV8Q77PiB14r/HlD4eY+wAkPVDxmUHqNnAdB04t89/1O/w1cDnyilFU="

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.get("/generate_qr")
def generate_qr(
    amount: float = Query(..., gt=0, description="จำนวนเงิน (บาท)")
):
    try:
        # ✅ สร้าง payload พร้อมจำนวนเงิน
        payload = qrcode.generate_payload(FIXED_PROMPTPAY_NUMBER, amount)

        # ✅ แปลงเป็น QR Image
        img = qrcode.to_image(payload)

        # ✅ ส่งกลับเป็น PNG
        img_io = BytesIO()
        img.save(img_io, format='PNG')
        img_io.seek(0)

        return StreamingResponse(img_io, media_type="image/png")

    except Exception as e:
        return {"error": str(e)}
    
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
        {
            "id": "P003",
            "location_name": "ชั้น 3 อาคารเรียนรวม",
            "lat": 13.735900,
            "lon": 100.522000,
            "url": "http://printer-003.local",
            "status": "online",
            "bw_price": 1.00,
            "color_price": 4.50
        },
        {
            "id": "P004",
            "location_name": "ฝ่ายทะเบียน",
            "lat": 13.738100,
            "lon": 100.521500,
            "url": "http://printer-004.local",
            "status": "online",
            "bw_price": 0.80,
            "color_price": 3.50
        },
        {
            "id": "P005",
            "location_name": "ห้องคอมพิวเตอร์",
            "lat": 13.736200,
            "lon": 100.523800,
            "url": "http://printer-005.local",
            "status": "offline",
            "bw_price": 1.50,
            "color_price": 7.00
        },
    ]
    return {"printers": printers}



@app.get("/preview-pdf/{line_id}/{filename}")
def preview_pdf_as_images(line_id: str, filename: str):
    file_path = os.path.join("pdfs", line_id, filename)

    if not os.path.exists(file_path):
        return JSONResponse(status_code=404, content={"error": "PDF not found"})

    try:
        # แปลง PDF เป็นภาพแบบ in-memory
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
            "Access-Control-Allow-Origin": "*",  # ✅ CORS ที่ใช้ได้จริง
            "X-Frame-Options": "ALLOWALL"
        }
    )

# @app.get("/list-pdfs/{line_id}")
# def list_pdfs(line_id: str):
#     folder_path = os.path.join("pdfs", line_id)
#     if not os.path.exists(folder_path):
#         return JSONResponse(status_code=404, content={"error": "No PDF files found for this user."})

#     files = [
#         os.path.join("/get-pdf", line_id, filename)
#         for filename in os.listdir(folder_path)
#         if filename.lower().endswith(".pdf")
#     ]

#     return {"files": files}

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

@app.post("/callback")
async def callback(request: Request):
    body = await request.body()
    signature = request.headers['X-Line-Signature']
    try:
        handler.handle(body.decode('utf-8'), signature)
    except Exception as e:
        print(f"Error: {e}")
    return "OK"

import re

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    if text.startswith("/print"):
        try:
            parts = text.split()
            if len(parts) < 3:
                raise ValueError("❌ รูปแบบคำสั่งไม่ถูกต้อง\n\nตัวอย่าง:\n/print 1 1-10 bw")

            file_id = int(parts[1])
            page_arg = parts[2]
            color_mode = parts[3] if len(parts) > 3 else "bw"
            color_mode = color_mode.lower()

            if color_mode not in ["bw", "color"]:
                raise ValueError("❌ สีต้องเป็น `bw` หรือ `color` เท่านั้น")

            # === ค้นหาไฟล์ที่ผู้ใช้เคยอัปโหลด ===
            user_dir = os.path.join("pdfs", user_id)
            if not os.path.exists(user_dir):
                raise FileNotFoundError("❌ ไม่พบไฟล์ใดๆ ที่คุณเคยอัปโหลด")

            files = [
                f for f in os.listdir(user_dir)
                if f.lower().endswith(".pdf")
            ]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(user_dir, f)), reverse=True)

            if file_id < 1 or file_id > len(files):
                raise ValueError(f"❌ ไม่มีไฟล์หมายเลข {file_id}. คุณมี {len(files)} ไฟล์")

            selected_file = os.path.join(user_dir, files[file_id - 1])

            # === แปลง page range ===
            def parse_pages(page_str):
                if page_str.lower() == "all":
                    return "all"
                page_nums = set()
                tokens = page_str.split(",")
                for token in tokens:
                    if "-" in token:
                        start, end = token.split("-")
                        page_nums.update(range(int(start), int(end)+1))
                    else:
                        page_nums.add(int(token))
                return sorted(page_nums)

            pages = parse_pages(page_arg)

            # === ตัวอย่าง: แค่ตอบกลับว่าสั่งพิมพ์อะไร ===
            preview_pages = "ทั้งหมด" if pages == "all" else ",".join(map(str, pages))
            reply = f"🖨 กำลังเตรียมพิมพ์ไฟล์ `{files[file_id - 1]}`\n📄 หน้า: {preview_pages}\n🎨 โหมด: {color_mode.upper()}"

        except ValueError as ve:
            reply = str(ve)
        except FileNotFoundError as fe:
            reply = str(fe)
        except Exception as e:
            reply = "❌ เกิดข้อผิดพลาดในการประมวลผลคำสั่ง\nกรุณาใช้รูปแบบ:\n/print <file_id> <pages> <color>\n\nตัวอย่าง:\n/print 1 1-5 bw"

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

    # สร้าง path
    user_dir = os.path.join("pdfs", user_id)
    os.makedirs(user_dir, exist_ok=True)

    save_path = os.path.join(user_dir, file_name)

    # ถ้ามีไฟล์ชื่อเดียวกันอยู่แล้ว จะเขียนทับ
    file_content = line_bot_api.get_message_content(message_id).content
    with open(save_path, 'wb') as f:
        f.write(file_content)

    # ✅ ตรวจ header ว่าเริ่มต้นด้วย %PDF หรือไม่
    with open(save_path, 'rb') as f:
        head = f.read(4)
        if not head.startswith(b'%PDF'):
            print(f"⚠️ WARNING: ไฟล์ {file_name} ไม่ใช่ PDF แท้ (ไม่ขึ้นต้นด้วย %PDF)")

    # ✅ ตรวจขนาดไฟล์
    file_size = os.path.getsize(save_path)
    print(f"📄 บันทึกไฟล์: {file_name} | ขนาด: {file_size} bytes")
    if file_size < 2000:
        print(f"⚠️ WARNING: ไฟล์ {file_name} มีขนาดเล็กผิดปกติ อาจเสียหาย")

    # ✅ ดึงรายการไฟล์และเรียงตามเวลาจากใหม่ไปเก่า
    all_files = [
        f for f in os.listdir(user_dir)
        if f.lower().endswith(".pdf")
    ]
    all_files.sort(key=lambda f: os.path.getmtime(os.path.join(user_dir, f)), reverse=True)

    file_list_text = "\n".join([f"{idx+1}. {fname}" for idx, fname in enumerate(all_files)])

    # ✅ สร้างลิงก์สำหรับเปิด frontend
    preview_link = f"https://4b7322bb4499.ngrok-free.app/index.html?uid={user_id}#"

    reply_text = f"""📄 บันทึกไฟล์ `{file_name}` เรียบร้อยแล้ว

Your files:
{file_list_text}

🔗 ดูทั้งหมดที่: {preview_link}
"""

    # ✅ ตอบกลับผู้ใช้
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )



# ✅ เสิร์ฟ frontend
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
