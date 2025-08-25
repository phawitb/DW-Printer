import subprocess
import time
import re
import threading
from pymongo import MongoClient

# === MongoDB Config ===
MONGO_URL = "mongodb+srv://phawitboo:JO3hoCXWCSXECrGB@cluster0.fvc5db5.mongodb.net"
DB_NAME = "dimonwall"
COLLECTION_NAME = "printers"

# ตั้งค่า printer_id
PRINTER_ID = "P0001"

client = MongoClient(MONGO_URL)
db = client[DB_NAME]
collection = db[COLLECTION_NAME]

def save_url_to_mongo(url, printer_id):
    """อัปเดต/เพิ่ม document โดยใช้ printer_id เป็น key หลัก"""
    doc = {
        "printer_id": printer_id,
        "url": url,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    }
    collection.update_one({"printer_id": printer_id}, {"$set": doc}, upsert=True)
    print("✅ Updated MongoDB:", doc)

def update_last_seen(printer_id):
    """อัปเดต last_seen ทุก ๆ 1 นาที"""
    while True:
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        collection.update_one(
            {"printer_id": printer_id},
            {"$set": {"last_seen": now}},
            upsert=True
        )
        print(f"⏰ Updated last_seen for {printer_id}: {now}")
        time.sleep(60)

def run_serveo(port=8000):
    while True:
        print(f"🚀 Starting Serveo tunnel for printer {PRINTER_ID}...")

        # เรียก ssh reverse tunnel ไป serveo.net
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-R", f"80:localhost:{port}", "serveo.net"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        url = None
        for line in iter(proc.stdout.readline, ''):
            print("🔌", line.strip())
            # Serveo จะบอก URL เช่น "Forwarding HTTP traffic from https://xxxx.serveo.net"
            m = re.search(r"(https://[a-zA-Z0-9.-]+\.serveo\.net)", line)
            if m:
                url = m.group(1)
                save_url_to_mongo(url, PRINTER_ID)
                print(f"🌍 Serveo URL: {url} | 🖨 printer_id: {PRINTER_ID}")

        print("⚠️ Serveo session ended. Restarting in 5s...")
        time.sleep(5)

if __name__ == "__main__":
    # สร้าง thread สำหรับ update last_seen ทุก 1 นาที
    threading.Thread(target=update_last_seen, args=(PRINTER_ID,), daemon=True).start()

    # รัน Serveo
    run_serveo(8000)
