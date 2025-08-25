from pymongo import MongoClient
import requests

# === MongoDB Config ===
MONGO_URL = "mongodb+srv://phawitboo:JO3hoCXWCSXECrGB@cluster0.fvc5db5.mongodb.net"
DB_NAME = "dimonwall"
COLLECTION_NAME = "printers"

client = MongoClient(MONGO_URL)
db = client[DB_NAME]
collection = db[COLLECTION_NAME]

def get_latest_url(printer_id):
    """ดึง URL ล่าสุดจาก MongoDB ตาม printer_id"""
    doc = collection.find_one({"printer_id": printer_id}, {"_id": 0})
    if doc:
        return doc["url"], doc["timestamp"]
    else:
        return None, None

def sent_to_printer_server(PDF_FILE, UID, printer_id):
    printer_url, ts = get_latest_url(printer_id)
    print(f"Latest URL for printer_id {printer_id} (timestamp: {ts}): {printer_url}")
    if not printer_url:
        print("❌ No URL found for this printer_id")
        return
    API_URL = f"{printer_url}/upload-pdf"
    print("Sending to:", API_URL)

    with open(PDF_FILE, "rb") as f:
        files = {"file": (PDF_FILE, f, "application/pdf")}
        data = {"uid": UID}
        response = requests.post(API_URL, files=files, data=data)

    print("Status code:", response.status_code)
    try:
        print("Response:", response.json())
    except Exception:
        print("Raw Response:", response.text)

printer_id = "P0001"   # 🔧 ใส่ printer_id ที่ต้องการ
PDF_FILE = "Grade EE4903 Final.pdf"  # path to a PDF file
UID = "user123"        # test user id

sent_to_printer_server(PDF_FILE, UID, printer_id)



# API_URL = "http://0.0.0.0:8000/upload-pdf"  # replace with your Pi IP
# API_URL = "https://23b61586b4b0.ngrok-free.app/upload-pdf"
