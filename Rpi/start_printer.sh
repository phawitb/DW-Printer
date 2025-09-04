#!/bin/bash
# start_printer.sh

# บังคับให้ script หยุดถ้ามี error
set -e

# ====== รอจนกว่า network จะ online ======
echo "[INFO] Waiting for internet connection..."
until ping -c1 8.8.8.8 &>/dev/null; do
    sleep 2
done
echo "[INFO] Network is online!"

# ไปที่โฟลเดอร์ Documents
cd /home/dw/Documents

# เปิดใช้งาน virtual environment
source venv/bin/activate

# รอ 3 วินาที
sleep 3

# ไปที่โปรเจกต์ Rpi
cd DW-Printer/Rpi/

# รัน run_tunnel.py (background)
python run_tunnel.py >> /home/dw/Documents/DW-Printer/Rpi/log.txt 2>&1 &

# รอ 10 วินาที
sleep 10

# รัน uvicorn (foreground)
uvicorn rpimain:app --reload --host 0.0.0.0 --port 8000 &
# uvicorn rpimain:app --reload --host 0.0.0.0 --port 8000 >> /home/dw/Documents/DW-Printer/Rpi/log.txt 2>&1
