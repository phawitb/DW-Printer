import subprocess
import time
import requests
from pymongo import MongoClient

# === MongoDB Config ===
MONGO_URL = "mongodb+srv://phawitboo:JO3hoCXWCSXECrGB@cluster0.fvc5db5.mongodb.net"
DB_NAME = "dimonwall"
COLLECTION_NAME = "deep_printer"

# ตั้งค่า printer_id (แก้ไขให้ตรงกับเครื่องพิมพ์จริง)
PRINTER_ID = "RPI_PRINTER_01"

client = MongoClient(MONGO_URL)
db = client[DB_NAME]
collection = db[COLLECTION_NAME]

def get_ngrok_url():
    """ดึง public URL ของ ngrok จาก API local"""
    try:
        res = requests.get("http://127.0.0.1:4040/api/tunnels").json()
        return res['tunnels'][0]['public_url']
    except Exception:
        return None

def save_url_to_mongo(url, printer_id):
    """อัปเดต/เพิ่ม document โดยใช้ printer_id เป็น key หลัก"""
    doc = {
        "printer_id": printer_id,
        "url": url,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    }
    collection.update_one({"printer_id": printer_id}, {"$set": doc}, upsert=True)
    print("✅ Updated MongoDB:", doc)

def run_ngrok(port=8000):
    while True:
        print(f"🚀 Starting ngrok for printer {PRINTER_ID}...")
        ngrok = subprocess.Popen(["ngrok", "http", str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # รอให้ ngrok เปิด
        time.sleep(3)
        url = get_ngrok_url()
        if url:
            save_url_to_mongo(url, PRINTER_ID)
            print(f"🌍 Ngrok URL: {url} | 🖨 printer_id: {PRINTER_ID}")
        else:
            print("⚠️ Failed to fetch ngrok URL")

        # รอจนกว่าจะหลุด
        while ngrok.poll() is None:
            time.sleep(5)

        print("⚠️ ngrok session expired. Restarting...")

if __name__ == "__main__":
    run_ngrok(8000)
