import uuid
import requests
from zoneinfo import ZoneInfo
from datetime import datetime
import time
import cups
import os

API_BASE = "https://dw-printer-lts.onrender.com"

def get_rpi_serial_number():
    mac = ':'.join(['{:02x}'.format((uuid.getnode() >> ele) & 0xff)
                    for ele in range(0,8*6,8)][::-1])
    device_name = f"rpi-{mac.replace(':','')[-6:]}"  # last 6 chars of MAC

    return device_name

def update_printer_url(url, printer_id):
    """เรียก API เพื่อ update url ของ printer"""
    try:
        api_url = f"{API_BASE}/update_printer_url"
        resp = requests.post(api_url, data={"printer_id": printer_id, "url": url}, timeout=10)
        if resp.status_code == 200:
            print(f"✅ Updated URL via API: {resp.json()}")
        else:
            print(f"⚠️ Failed to update URL: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"❌ Error calling API: {e}")

def update_status(ref_id: str, status: str):
    url = f"{API_BASE}/update_status/{ref_id}"
    payload = {"status": status}

    try:
        # ✅ ใช้ data ไม่ใช่ json
        response = requests.post(url, data=payload, headers={"accept": "application/json"})
        response.raise_for_status()
        print(f"✅ Update success: {response.text}")
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ Update failed: {e}")
        return None

def update_printer_status(printer_id,status):
    """อัปเดต last_seen (เวลาไทย) + status=online ทุก ๆ 1 นาที ผ่าน API"""
    tz = ZoneInfo("Asia/Bangkok")
    while True:
        now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        try:
            api_url = f"{API_BASE}/update_printer_status/{printer_id}"
            resp = requests.post(api_url, data={"status": status, "last_seen": now}, timeout=10)
            print(f"⏰ Updated last_seen for {printer_id}: {now} | resp={resp.status_code}")
        except Exception as e:
            print(f"❌ Error updating last_seen via API: {e}")
        time.sleep(60)

# ==================================================================

# ==================================================================

states = {
    3: "Pending",
    4: "Held",
    5: "Processing",
    6: "Stopped",
    7: "Canceled",
    8: "Aborted",
    9: "Completed"
}

def test_printer(printer_name: str):
    """ฟังก์ชันทดสอบพิมพ์ไฟล์ test.txt ไปยัง printer_name"""
    PRINTER_ID = get_rpi_serial_number()
    file_path = "/home/dw/Documents/DW-Printer/Rpi/test.txt"

    try:
        if not os.path.exists(file_path):
            update_printer_status(PRINTER_ID, f"❌ File not found: {file_path}")
            return

        conn = cups.Connection()
        job_id = conn.printFile(printer_name, file_path, "Test Text Job", {})
        update_printer_status(PRINTER_ID, f"🖨 Submitted job ID: {job_id} | Printer: {printer_name} | File: {file_path}")

        while True:
            try:
                job_attrs = conn.getJobAttributes(job_id)
                state = job_attrs.get("job-state")
                reasons = job_attrs.get("job-printer-state-reasons", [])
                status_detail = f"Job {job_id} | Printer: {printer_name} | State: {states.get(state, str(state))} | Reasons: {', '.join(reasons) if reasons else 'None'}"
                print(status_detail)
                update_printer_status(PRINTER_ID, status_detail)
                if state in (7, 8, 9):
                    break
            except cups.IPPError:
                update_printer_status(PRINTER_ID, f"✅ Job {job_id} not found (probably finished/removed)")
                break
            time.sleep(2)

    except Exception as e:
        update_printer_status(PRINTER_ID, f"⚠️ Error: {str(e)}")

def print_pdf(file_path: str, printer_name: str, options: dict = None):
    print('print_pdf....')
    """ฟังก์ชันสั่งพิมพ์ PDF ไปยัง printer_name"""
    PRINTER_ID = get_rpi_serial_number()

    try:
        if not os.path.exists(file_path):
            update_printer_status(PRINTER_ID, f"❌ File not found: {file_path}")
            return

        conn = cups.Connection()
        job_id = conn.printFile(printer_name, file_path, "PDF Print Job", options or {})
        update_printer_status(PRINTER_ID, f"🖨 Submitted job ID: {job_id} | Printer: {printer_name} | File: {file_path}")
        print(f"🖨 Submitted job ID: {job_id} | Printer: {printer_name} | File: {file_path}")

        while True:
            try:
                job_attrs = conn.getJobAttributes(job_id)
                state = job_attrs.get("job-state")
                reasons = job_attrs.get("job-printer-state-reasons", [])
                status_detail = f"Job {job_id} | Printer: {printer_name} | State: {states.get(state, str(state))} | Reasons: {', '.join(reasons) if reasons else 'None'}"
                print(status_detail)
                update_printer_status(PRINTER_ID, status_detail)
                if state in (7, 8, 9):
                    break
            except cups.IPPError:
                print(f"✅ Job {job_id} not found (probably finished/removed)")
                update_printer_status(PRINTER_ID, f"✅ Job {job_id} not found (probably finished/removed)")
                break
            time.sleep(2)

    except Exception as e:
        print(PRINTER_ID, f"⚠️ Error: {str(e)}")
        update_printer_status(PRINTER_ID, f"⚠️ Error: {str(e)}")

# ==============================
# ตัวอย่างการใช้งาน
# ==============================
if __name__ == "__main__":
    conn = cups.Connection()
    printers = list(conn.getPrinters().keys())

    print('printers',printers)

    if not printers:
        print("❌ No printers found")
        exit(1)

    # เลือกเครื่องพิมพ์ตัวที่ 1
    # printer_name = printers[0]
    printer_name = 'PDF'
    print(f"🖨 Selected printer: {printer_name}")

    # ทดสอบพิมพ์ text
    test_printer(printer_name)

    # ทดสอบพิมพ์ PDF
    print_pdf(
        "/home/dw/Documents/DW-Printer/Rpi/GradeEE4903Final.pdf",
        printer_name=printer_name,
        options={
            "page-ranges": "1-3",
            "ColorModel": "Gray"
        }
    )