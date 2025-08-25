#!/bin/bash
# Script to run servo and start uvicorn server

LOG_FILE="/home/dw/Documents/DW-Printer/Rpi/startup.log"

echo "==== Boot start at $(date) ====" >> "$LOG_FILE"

# รอจนกว่าจะมี network
echo "Waiting for network..." >> "$LOG_FILE"
while ! ping -c 1 -W 1 8.8.8.8 >/dev/null 2>&1; do
    echo "$(date) - No network yet, retrying..." >> "$LOG_FILE"
    sleep 3
done
echo "$(date) - Network is ready!" >> "$LOG_FILE"

# เข้าสู่โฟลเดอร์ Documents
cd /home/dw/Documents

# เปิดใช้งาน virtual environment
source venv/bin/activate

# ไปที่โฟลเดอร์โปรเจกต์
cd /home/dw/Documents/DW-Printer/Rpi

# รัน servo script
echo "$(date) - Starting servo script" >> "$LOG_FILE"
python run_servo.py >> "$LOG_FILE" 2>&1 &

# รอ 10 วินาที
sleep 10

# รัน uvicorn server
echo "$(date) - Starting uvicorn server" >> "$LOG_FILE"
uvicorn rpimain:app --reload --host 0.0.0.0 --port 8000 >> "$LOG_FILE" 2>&1
