#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, tempfile, cups, glob
from datetime import datetime
from typing import Optional, Dict, Callable, List
import requests  # noqa
from utils import update_status, API_BASE, get_rpi_serial_number  # noqa


PRINTER_STATE = {3: "idle", 4: "processing", 5: "stopped"}
JOB_STATE = {
    3: "pending",
    4: "held",
    5: "processing",
    6: "stopped",
    7: "cancelled",
    8: "aborted",
    9: "completed",
}

def get_printer_name():
    """
    พยายามดึง selected_printer จากเซิร์ฟเวอร์ก่อน:
      GET {API_BASE}/get_printer_name/{PRINTER_ID}
    ถ้าไม่ได้ → fallback: CUPS default → ชื่อแรก → "PDF"
    """
    try:
        # local import เพื่อไม่แก้ส่วนบนไฟล์
        

        printer_id = get_rpi_serial_number()
        url = f"{API_BASE.rstrip('/')}/get_printer_name/{printer_id}"
        r = requests.get(url, timeout=10)
        if r.ok:
            data = r.json()
            sel = data.get("selected_printer")
            if isinstance(sel, str) and sel.strip():
                return sel.strip()
    except Exception as e:
        print(f"⚠️ get_printer_name(): API error: {e}")

    # Fallback ไปที่ CUPS
    try:
        c = cups.Connection()
        default = c.getDefault()
        if default:
            return default
        names = list(c.getPrinters().keys())
        if names:
            return names[0]
    except Exception as e:
        print(f"⚠️ get_printer_name(): CUPS fallback error: {e}")

    # Fallback สุดท้าย
    return "PDF"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ---------- Status helper (กันโปรแกรมล้มเมื่อ endpoint ใช้ไม่ได้) ----------
def safe_update_status(ref_id: str, status: str):
    try:
        update_status(ref_id, status)
        print(f"✅ Update success: {status}")
    except Exception as e:
        print(f"❌ Update failed (ignored): {e} | ref:{ref_id} | status={status}")

# ---------- Small helpers ----------
def _color_to_cups(color: str) -> str:
    if not color:
        return "Gray"
    c = color.strip().lower()
    if c in ("bw", "black", "gray", "grey", "kgray", "grayscale"):
        return "Gray"   # บางรุ่นอาจใช้ KGray/GrayScale → ปรับ lpoptions ตามรุ่น
    return "RGB"

def _resolve_path(filename: str, search_dirs: Optional[List[str]] = None) -> str:
    """
    หาไฟล์ดังนี้:
      1) absolute path → คืนทันทีถ้ามีอยู่
      2) exact match ในแต่ละโฟลเดอร์
      3) fuzzy: หา *{filename} (เช่น *_MyDoc.pdf) แล้วเลือกไฟล์ที่แก้ไขล่าสุด
    """
    if os.path.isabs(filename) and os.path.exists(filename):
        return filename

    # exact
    for d in (search_dirs or []):
        p = os.path.join(d, filename)
        if os.path.exists(p):
            return p

    # fuzzy
    target = filename.lower().strip()
    candidates = []
    for d in (search_dirs or []):
        for p in glob.glob(os.path.join(d, f"*{filename}")):
            if os.path.basename(p).lower().endswith(target):
                candidates.append(p)

    if candidates:
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)  # ไฟล์ล่าสุดก่อน
        return candidates[0]

    raise FileNotFoundError(filename)

# ---------- Core: watcher ----------
def watch_job(
    printer: str,
    job_id: int,
    interval: float = 0.7,
    ref_id: str = "",
    on_update: Optional[Callable[[str], None]] = None,
    prefix: str = "",
):
    """
    ติดตามสถานะงานและเรียก on_update(message) เมื่อมีการเปลี่ยนแปลง
    """
    c = cups.Connection()
    last_state = None
    last_pages = None
    hdr = f"{prefix} " if prefix else ""
    start_ts = time.time()

    msg = f"{hdr}👀 Watching printer='{printer}', job={job_id}"
    print(f"ref:{ref_id} {now()} {msg}")
    if on_update:
        on_update(msg)

    while True:
        try:
            attrs = c.getJobAttributes(job_id)
        except cups.IPPError:
            msg = f"{hdr}[JOB {job_id}] no longer in queue/history."
            print(f"ref:{ref_id} {now()} {msg}")
            if on_update:
                on_update(msg)
            break

        jcode = attrs.get("job-state", 0)
        jstate = JOB_STATE.get(jcode, f"unknown({jcode})")
        pages = attrs.get("job-media-sheets-completed", 0)

        if jstate != last_state:
            msg = f"{hdr}[JOB {job_id}] state={jstate}, pages={pages}"
            print(f"ref:{ref_id} {now()} {msg}")
            if on_update:
                on_update(msg)
            last_state = jstate

        if pages != last_pages and isinstance(pages, int):
            if last_pages is not None and pages > last_pages:
                msg = f"{hdr}[JOB {job_id}] progress: pages={pages}"
                print(f"ref:{ref_id} {now()} {msg}")
                if on_update:
                    on_update(msg)
            last_pages = pages

        if jstate in ("completed", "cancelled", "aborted"):
            elapsed = time.time() - start_ts
            if jstate == "completed":
                msg = f"{hdr}[JOB {job_id}] ✅ completed in {elapsed:.2f}s (pages={pages})"
            elif jstate == "cancelled":
                msg = f"{hdr}[JOB {job_id}] ⚠️ cancelled after {elapsed:.2f}s"
            else:
                msg = f"{hdr}[JOB {job_id}] ❌ aborted after {elapsed:.2f}s"
            print(f"ref:{ref_id} {now()} {msg}")
            if on_update:
                on_update(msg)
            break

        time.sleep(interval)

# ---------- Public APIs ----------
def list_printers():
    """
    คืนรายชื่อ printer ทั้งหมดในระบบ CUPS เป็น list[str] และพิมพ์รายละเอียดสถานะประกอบ
    จากนั้นจะอัปเดต list_printers ไปยัง API:
      POST {API_BASE}/update_printer_name/{PRINTER_ID}
      body: {"list_printers": [...]}
    """
    c = cups.Connection()
    printers = c.getPrinters()
    default = c.getDefault()
    names = list(printers.keys())

    print(f"System default: {default if default else 'None'}\n")
    for name, info in printers.items():
        state = info.get("printer-state", 3)
        reasons = info.get("printer-state-reasons", [])
        accepting = info.get("printer-is-accepting-jobs", False)
        enabled = "enabled" if accepting else "disabled"
        reason_str = ", ".join(reasons) if reasons else "none"
        print(f"- {name}{' (default)' if name == default else ''}")
        print(f"  state      : {state}")
        print(f"  reasons    : {reason_str}")
        print(f"  accepting  : {enabled}")
        print()

    # 👉 อัปเดต list_printers ไปยัง API ตามที่ระบุ
    try:
        

        printer_id = get_rpi_serial_number()
        url = f"{API_BASE.rstrip('/')}/update_printer_name/{printer_id}"
        r = requests.post(url, json={"list_printers": names}, timeout=10)

        # แสดงผลลัพธ์แบบอ่านง่าย
        ct = r.headers.get("content-type", "")
        body = r.json() if ct.startswith("application/json") else r.text
        print("Update list_printers =>", r.status_code, body)
    except Exception as e:
        print(f"⚠️ update list_printers API error: {e}")

    return names




def print_text(
    text: str,
    title: str = "Text Print",
    options: Optional[Dict[str, str]] = None,
):
    """
    ทดสอบเครื่องพิมพ์ด้วยการพิมพ์ข้อความง่าย ๆ
    - ไม่อัปเดตสถานะภายนอก
    - ไม่ใช้ ref_id
    """
    # printer_name = "PDF"
    printer_name = get_printer_name()

    c = cups.Connection()
    with tempfile.NamedTemporaryFile(delete=False, mode="w", suffix=".txt") as f:
        f.write(text.rstrip() + "\n")
        tmp = f.name
    job_id = c.printFile(printer_name, tmp, title, options or {})
    print(f"{now()} 🖨️ Submitted text job #{job_id} to '{printer_name}' (file: {tmp})")
    try:
        # ใช้ watcher แบบไม่ส่ง ref/on_update
        watch_job(printer_name, job_id)
    finally:
        try:
            os.unlink(tmp)
        except Exception as e:
            print(f"{now()} ⚠️ temp cleanup error: {e}")

def print_pdf(
    doc: dict,
    base_dirs: Optional[List[str]] = None,
    interval: float = 0.7
):
    """
    รับ doc แล้ว loop พิมพ์ตาม jobs
    โครงสร้าง doc ตัวอย่าง:
    {
      'ref_id': 'direct_...',
      'line_id': 'Uxxx',
      'printer_id': 'rpi-xxxx',
      'jobs': [
          {'filename': 'A.pdf', 'pages': 'all', 'color': 'bw', 'copies': 1},
          {'filename': '/abs/path/B.pdf', 'pages': '1-3', 'color': 'color', 'copies': 2},
      ],
      ...
    }
    """
    # printer_name = "PDF"
    printer_name = get_printer_name()

    ref_id = doc.get("ref_id", "")
    jobs = doc.get("jobs", [])
    total = len(jobs)
    if total == 0:
        safe_update_status(ref_id, "No jobs in doc")
        return

    line_id = doc.get("line_id", "")

    # ลำดับโฟลเดอร์ค้นหาไฟล์: ./pdfs/{line_id} → ./pdfs → ./
    default_dirs: List[str] = []
    if line_id:
        default_dirs.append(os.path.join(os.getcwd(), "pdfs", line_id))
    default_dirs += [os.path.join(os.getcwd(), "pdfs"), os.getcwd()]
    if base_dirs:
        default_dirs = base_dirs + default_dirs  # ให้ base_dirs ที่ส่งมาเองมีลำดับก่อน

    c = cups.Connection()

    for idx, job in enumerate(jobs, start=1):
        fname = job.get("filename")
        pages = str(job.get("pages", "all")).strip().lower()
        color = _color_to_cups(job.get("color", "bw"))
        copies = int(job.get("copies", 1))

        options: Dict[str, str] = {"ColorModel": color, "copies": str(copies)}
        if pages and pages != "all":
            options["page-ranges"] = pages

        prefix = f"file {idx}/{total}"
        try:
            fpath = _resolve_path(fname, default_dirs)
        except FileNotFoundError:
            msg = f"{prefix} ❌ file not found: {fname} (searched: {default_dirs})"
            print(f"ref:{ref_id} {now()} {msg}")
            safe_update_status(ref_id, msg)
            continue

        title = f"Print {os.path.basename(fpath)}"
        try:
            job_id = c.printFile(printer_name, fpath, title, options)
        except Exception as e:
            msg = f"{prefix} ❌ submit failed: {os.path.basename(fpath)} | {e}"
            print(f"ref:{ref_id} {now()} {msg}")
            safe_update_status(ref_id, msg)
            continue

        start_msg = (f"{prefix} 🖨️ submitted #{job_id} | file={os.path.basename(fpath)} "
                     f"| pages={pages} | copies={copies} | color={color}")
        print(f"ref:{ref_id} {now()} {start_msg}")
        safe_update_status(ref_id, start_msg)

        # ติดตามสถานะจนเสร็จ พร้อมอัปเดตทุกการเปลี่ยนแปลง
        watch_job(
            printer_name,
            job_id,
            interval=interval,
            ref_id=ref_id,
            on_update=lambda s, p=prefix: safe_update_status(ref_id, f"{p} {s}"),
            prefix=prefix
        )

    safe_update_status(ref_id, f"✅ all files processed ({total} job{'s' if total>1 else ''})")

# ----------------- Example -----------------
if __name__ == "__main__":
    # แสดงเครื่องพิมพ์
    print(list_printers())

    ## for test ==========================

    # ทดสอบพิมพ์ข้อความ (ไม่ใช้ ref/ไม่อัปเดตสถานะภายนอก)
    print_text("สวัสดีจาก Raspberry Pi!")

    # พิมพ์จาก doc (ไฟล์ใน pdfs/{line_id})
    example_doc = {
        'ref_id': 'direct_1756803601004',
        'line_id': 'Ud4182b71d88670ab7c347c6fcf6752c6',
        'printer_id': 'rpi-cd1ee4',
        'jobs': [
            {'filename': 'Phawit_Schorlarship.pdf', 'pages': 'all', 'color': 'bw', 'copies': 1}
        ]
    }
    print_pdf(example_doc)

    ## =============================================
