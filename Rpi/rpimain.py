from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import JSONResponse
import os
import subprocess
import shlex
import re
from datetime import datetime, timezone, timedelta
import json
import utils as config
import pp as printer
import threading

app = FastAPI()

UPLOAD_DIR = "pdfs"
os.makedirs(UPLOAD_DIR, exist_ok=True)

PRINTER_NAME = config.get_rpi_serial_number()


# ========= Helpers =========
def _sh(cmd: str):
    """Run a shell command and return (returncode, stdout, stderr)."""
    p = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


# -- Fix timezone tail like '+07' -> '+07:00' so %z can parse
TZ_FIX_RE = re.compile(r'([+-]\d{2})(?!:?\d{2})$')

def _normalize_tz(ts: str) -> str:
    """
    แปลงท้ายสตริง timezone จาก '+07' ให้เป็น '+07:00' เพื่อให้ %z พาร์สได้
    - ถ้าเป็น +HH:MM หรือ +HHMM อยู่แล้ว จะไม่แตะต้อง
    """
    ts = ts.strip()
    if re.search(r'[+-]\d{2}:\d{2}$', ts) or re.search(r'[+-]\d{4}$', ts):
        return ts
    return TZ_FIX_RE.sub(r'\1:00', ts)


def _parse_lpstat_time(ts: str):
    """
    Parse lpstat's datetime string, e.g.
    'Thu 04 Sep 2025 08:59:52 AM +07'
    คืนค่า datetime ที่มี tzinfo (ถ้าพาร์สไม่ได้จะคืน None)
    """
    if not ts:
        return None

    # ปรับรูปแบบโซนเวลาให้เข้ากับ %z ก่อน
    ts_norm = _normalize_tz(ts)

    # ลองพาร์สทั้งแบบ 12 ชม. (AM/PM) และ 24 ชม.
    for fmt in ("%a %d %b %Y %I:%M:%S %p %z", "%a %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(ts_norm, fmt)
        except ValueError:
            continue

    # Fallback: ถ้าตัด timezone ออก แล้วถือว่าเป็นเวลา Asia/Bangkok (+07:00)
    try:
        parts = ts.split()
        if parts and (parts[-1].startswith('+') or parts[-1].startswith('-')):
            ts_no_tz = " ".join(parts[:-1])
        else:
            ts_no_tz = ts
        for fmt in ("%a %d %b %Y %I:%M:%S %p", "%a %d %b %Y %H:%M:%S"):
            try:
                dt_naive = datetime.strptime(ts_no_tz, fmt)
                return dt_naive.replace(tzinfo=timezone(timedelta(hours=7)))
            except ValueError:
                continue
    except Exception:
        pass

    return None


def _list_not_completed_jobs():
    """
    ดึงรายการงานที่ยังไม่เสร็จทั้งหมดจาก CUPS
    คืน list ของ dict: {job, user, size_bytes, submitted_at, age_minutes, raw}
    """
    code, out, err = _sh("lpstat -W not-completed")
    # บางระบบ "No jobs" อาจถูกเขียนลง stdout/stderr
    if code != 0 and ("No jobs" not in (out or "") and "No jobs" not in (err or "")):
        raise RuntimeError(err or out or "lpstat failed")

    if "No jobs" in (out or "") or not (out or "").strip():
        return []

    now_utc = datetime.now(timezone.utc)
    jobs = []

    for line in (out or "").splitlines():
        s = line.strip()
        if not s:
            continue

        # รูปแบบทั่วไป: <JOBNAME> <USER> <SIZE_BYTES> <DATETIME...>
        # เช่น: DCPT220-269   dw   263168   Thu 04 Sep 2025 08:59:52 AM +07
        parts = s.split()
        job_name = parts[0] if parts else None
        user = parts[1] if len(parts) > 1 else None

        size_bytes = None
        ts_text = None
        # หา token ที่เป็น size (ตัวเลข) แล้วส่วนหลังคือ timestamp
        for i, token in enumerate(parts[2:], start=2):
            if token.isdigit():
                try:
                    size_bytes = int(token)
                except Exception:
                    size_bytes = None
                ts_text = " ".join(parts[i+1:])
                break

        dt = _parse_lpstat_time(ts_text) if ts_text else None
        age_minutes = None
        if dt:
            age_minutes = (now_utc - dt.astimezone(timezone.utc)).total_seconds() / 60.0

        jobs.append({
            "job": job_name,
            "user": user,
            "size_bytes": size_bytes,
            "submitted_at": ts_text,
            "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
            "raw": s
        })

    return jobs


def _cancel_all_jobs():
    """
    ยกเลิก "ทุกงาน" ในทุกคิว โดยวน cancel ตามชื่อ job (ปลอดภัย/ตรงไปตรงมา)
    คืน dict: {found, canceled, errors}
    """
    jobs = _list_not_completed_jobs()
    canceled = 0
    errors = []

    for j in jobs:
        jobname = j["job"]
        if not jobname:
            continue
        code, out, err = _sh(f"cancel {jobname}")
        if code == 0:
            canceled += 1
        else:
            errors.append({"job": jobname, "error": err or out or "cancel failed"})

    return {"found": len(jobs), "canceled": canceled, "errors": errors}


# ========= APIs =========
@app.post("/upload-pdf")
async def upload_pdf(doc: str = Form(...), file: UploadFile = File(...)):
    """
    อัปโหลดไฟล์ PDF แล้วสั่งพิมพ์ใน background thread ผ่าน printer.print_pdf(doc)
    - doc: JSON string (จะถูก json.loads)
    - file: ไฟล์ PDF
    """
    try:
        doc_obj = json.loads(doc)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON in 'doc'"})

    print("doc::", doc_obj)

    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse(status_code=400, content={"error": "Only PDF files allowed"})

    # Create user-specific folder
    uid = doc_obj.get("line_id")
    if not uid:
        return JSONResponse(status_code=400, content={"error": "Missing 'line_id' in doc"})

    user_dir = os.path.join(UPLOAD_DIR, uid)
    os.makedirs(user_dir, exist_ok=True)

    # Save file with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_name = f"{timestamp}_{file.filename}"
    save_path = os.path.join(user_dir, save_name)

    with open(save_path, "wb") as f:
        f.write(await file.read())

    # พิมพ์ใน background
    def task():
        try:
            # ปรับตามโครงสร้าง doc_obj/print_pdf ของคุณ
            printer.print_pdf(doc_obj)
        except Exception as e:
            print(f"❌ print_pdf error: {e}")

    threading.Thread(target=task, daemon=True).start()

    return {
        "status": "uploaded",
        "uid": uid,
        "filename": save_name,
        "message": f"✅ Print job queued for {save_name}"
    }


@app.get("/printer/status")
def printer_status():
    """
    คืนสถานะพรินเตอร์ทั้งหมด และ default destination ในระบบ CUPS
    """
    code, out, err = _sh("lpstat -p -d")
    if code != 0:
        return JSONResponse(status_code=500, content={"error": err or out or "lpstat failed"})

    printers = []
    default_printer = None

    for line in (out or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("printer "):
            # ตัวอย่าง: "printer DCPT220 is idle.  enabled since ..."
            parts = s.split()
            name = parts[1] if len(parts) > 1 else None
            state = " ".join(parts[2:]) if len(parts) > 2 else ""
            printers.append({"name": name, "state": state, "raw": s})
        elif s.startswith("system default destination:"):
            default_printer = s.split(":", 1)[1].strip()

    return {"default": default_printer, "printers": printers}


@app.get("/printer/queue")
def printer_queue(printer: str | None = None):
    """
    คืนรายการงานพิมพ์ที่ยังไม่เสร็จ (pending/processing/held) ใน CUPS
    - ไม่ระบุ printer → ทุกเครื่อง
    - ระบุ printer → เฉพาะคิวของเครื่องนั้น (เทียบชื่อ CUPS)
    """
    base_cmd = "lpstat -W not-completed"
    if printer:
        base_cmd += f" -P {printer}"

    code, out, err = _sh(base_cmd)
    if code != 0:
        # บางระบบ "No jobs" อยู่ใน stdout/stderr
        if "No jobs" in (out or "") or "No jobs" in (err or ""):
            return {"jobs": []}
        return JSONResponse(status_code=500, content={"error": err or out or "lpstat failed"})

    now_utc = datetime.now(timezone.utc)
    jobs = []

    for line in (out or "").splitlines():
        s = line.strip()
        if not s:
            continue

        # รูปแบบโดยทั่วไป:
        # <JOBNAME> <USER> <SIZE_BYTES> <DATETIME...>
        # ตัวอย่าง:
        # DCPT220-269             dw              263168   Thu 04 Sep 2025 08:59:52 AM +07
        parts = s.split()
        job_name = parts[0] if parts else None
        user = parts[1] if len(parts) > 1 else None

        size_bytes = None
        ts_text = None
        # หา token ที่เป็น size (ตัวเลข) แล้วส่วนหลังคือ timestamp
        for i, token in enumerate(parts[2:], start=2):
            if token.isdigit():
                try:
                    size_bytes = int(token)
                except Exception:
                    size_bytes = None
                ts_text = " ".join(parts[i+1:])
                break

        dt = _parse_lpstat_time(ts_text) if ts_text else None
        age_minutes = None
        if dt:
            age_minutes = (now_utc - dt.astimezone(timezone.utc)).total_seconds() / 60.0

        jobs.append({
            "job": job_name,                  # เช่น "DCPT220-269"
            "user": user,
            "size_bytes": size_bytes,
            "submitted_at": ts_text,          # สตริงเวลาแบบ lpstat
            "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
            "raw": s
        })

    return {"jobs": jobs}


@app.post("/printer/cancel/{job_name}")
def cancel_job(job_name: str):
    """
    ยกเลิกงานพิมพ์ตามชื่อ (เช่น 'DCPT220-269')
    """
    code, out, err = _sh(f"cancel {job_name}")
    if code != 0:
        raise HTTPException(status_code=400, detail=err or out or "cancel failed")
    return {"status": "ok", "message": f"Canceled {job_name}"}


# ========= NEW: Cancel ALL queues on ALL printers =========
@app.post("/printer/cancel_all")
def cancel_all_jobs():
    """
    ยกเลิกทุกงานในทุกคิวทุกเครื่องพิมพ์
    """
    try:
        result = _cancel_all_jobs()
        return {"status": "ok", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ========= NEW: Cancel ALL queues IF any job older than X minutes =========
@app.post("/printer/cancel_all_if_older")
def cancel_all_if_older(minutes: int = Query(..., gt=0, description="Threshold in minutes")):
    """
    ถ้ามีงานใด ๆ ในคิวที่อายุมากกว่า/เท่ากับ 'minutes' นาที → ยกเลิก 'ทุกงาน' ทุกคิวทุกเครื่อง
    - minutes: จำนวน นาที ( > 0 )
    """
    try:
        jobs = _list_not_completed_jobs()
        if not jobs:
            return {
                "status": "ok",
                "triggered": False,
                "reason": "no jobs",
                "found": 0,
                "max_age_minutes": 0
            }

        ages = [j["age_minutes"] for j in jobs if isinstance(j.get("age_minutes"), (int, float))]
        max_age = max(ages) if ages else 0
        if max_age >= minutes:
            result = _cancel_all_jobs()
            return {
                "status": "ok",
                "triggered": True,
                "found_before_cancel": len(jobs),
                "max_age_minutes": max_age,
                "cancel_result": result
            }
        else:
            return {
                "status": "ok",
                "triggered": False,
                "found": len(jobs),
                "max_age_minutes": max_age
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
