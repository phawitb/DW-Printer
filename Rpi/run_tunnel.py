import os
import re
import time
import random
import signal
import subprocess
import threading
import shlex
from datetime import datetime, timezone, timedelta

import utils as config

# ====== CONFIG ======
PRINTER_ID = config.get_rpi_serial_number()
PORT = int(os.getenv("APP_PORT", "8000"))

BACKOFF_START = 5
BACKOFF_MAX = 60

# ค่าควบคุมการตรวจคิว
STALE_MAX_MINUTES = 30          # ถ้ามีงานใดค้างเกิน X นาที -> ยกเลิกทุกงาน
CHECK_INTERVAL_SECONDS = 60     # เช็กทุก ๆ กี่วินาที (รวมกับการส่งสถานะ online)

stop_event = threading.Event()


# ====== Helpers ======
def _sh(cmd: str):
    """Run a shell command and return (returncode, stdout, stderr)."""
    p = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


# รองรับ timezone ท้ายสตริงแบบ "+07" -> "+07:00" เพื่อให้ %z พาร์สได้
_TZ_FIX_RE = re.compile(r'([+-]\d{2})(?!:?\d{2})$')

def _normalize_tz(ts: str) -> str:
    ts = ts.strip()
    if re.search(r'[+-]\d{2}:\d{2}$', ts) or re.search(r'[+-]\d{4}$', ts):
        return ts
    return _TZ_FIX_RE.sub(r'\1:00', ts)

def _parse_lpstat_time(ts: str):
    """
    แปลง datetime string จาก lpstat เช่น:
      'Thu 04 Sep 2025 08:59:52 AM +07'
    คืนค่า datetime ที่มี tzinfo; ถ้าพาร์สไม่ได้คืน None
    """
    if not ts:
        return None
    ts_norm = _normalize_tz(ts)
    for fmt in ("%a %d %b %Y %I:%M:%S %p %z", "%a %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(ts_norm, fmt)
        except ValueError:
            continue
    # fallback: ตัด timezone ทิ้ง แล้วตีความเป็นเวลา UTC+7
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
    คืน list ของงานค้างจาก CUPS: [{job, user, size_bytes, submitted_at, age_minutes, raw}]
    ใช้ lpstat -W not-completed
    """
    code, out, err = _sh("lpstat -W not-completed")
    # บางระบบจะพ่น "No jobs" ออก stdout/stderr
    if code != 0 and ("No jobs" not in (out or "") and "No jobs" not in (err or "")):
        # มี error จริง
        raise RuntimeError(err or out or "lpstat failed")

    if "No jobs" in (out or "") or not (out or "").strip():
        return []

    now_utc = datetime.now(timezone.utc)
    jobs = []

    for line in (out or "").splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split()
        job_name = parts[0] if parts else None
        user = parts[1] if len(parts) > 1 else None

        size_bytes = None
        ts_text = None
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
    ยกเลิกทุกงานในทุกคิว: ใช้คำสั่ง cancel <JOBNAME> ทีละงาน
    """
    jobs = _list_not_completed_jobs()
    canceled = 0
    errs = []
    for j in jobs:
        name = j.get("job")
        if not name:
            continue
        code, out, err = _sh(f"cancel {name}")
        if code == 0:
            canceled += 1
        else:
            errs.append({"job": name, "error": err or out or "cancel failed"})
    return {"found": len(jobs), "canceled": canceled, "errors": errs}

def _cancel_all_if_stale(threshold_minutes: int = STALE_MAX_MINUTES):
    """
    ถ้างานค้างที่เก่าสุดมีอายุ >= threshold_minutes → ยกเลิกทุกงาน
    """
    try:
        jobs = _list_not_completed_jobs()
    except Exception as e:
        print(f"⚠️ lpstat error: {e}")
        return

    if not jobs:
        print("🧹 No jobs in queue.")
        return

    ages = [j["age_minutes"] for j in jobs if isinstance(j.get("age_minutes"), (int, float))]
    max_age = max(ages) if ages else 0
    print(f"⏱️ Queue check: {len(jobs)} jobs, max_age={max_age:.2f} min, threshold={threshold_minutes} min")

    if max_age >= threshold_minutes:
        print("🛑 Stale queue detected. Canceling ALL jobs...")
        result = _cancel_all_jobs()
        print(f"✅ Cancel result: found={result['found']}, canceled={result['canceled']}, errors={len(result['errors'])}")
    else:
        print("✅ Queue is within threshold.")


# ====== Tunnels & Heartbeat ======
def update_url(url: str):
    try:
        config.update_printer_url(url, PRINTER_ID)
        print(f"✅ Updated public URL: {url} | 🖨 {PRINTER_ID}")
    except Exception as e:
        print(f"⚠️ update_printer_url failed: {e}")

def status_heartbeat():
    """
    - ส่งสถานะ online ทุก ๆ CHECK_INTERVAL_SECONDS
    - ตรวจคิว CUPS และยกเลิกทุกงาน ถ้าพบงานค้างเกิน STALE_MAX_MINUTES
    (รวมไว้ในเธรดเดียวตามคำขอ)
    """
    while not stop_event.is_set():
        try:
            config.update_printer_status(PRINTER_ID, "online")
        except Exception as e:
            print(f"⚠️ update_printer_status failed: {e}")

        # ตรวจคิวและเคลียร์ถ้าค้างเกินเกณฑ์
        try:
            _cancel_all_if_stale(STALE_MAX_MINUTES)
        except Exception as e:
            print(f"⚠️ stale-cancel check failed: {e}")

        stop_event.wait(CHECK_INTERVAL_SECONDS)

def run_tunnel(cmd, url_pattern, name):
    """generic tunnel runner"""
    backoff = BACKOFF_START
    while not stop_event.is_set():
        print(f"🚀 Starting {name} tunnel for {PRINTER_ID} on port {PORT}...")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except Exception as e:
            print(f"❌ Failed to spawn {name}: {e}")
            sleep_for = min(backoff, BACKOFF_MAX) + random.uniform(0, 3)
            print(f"⏳ Retry {name} in {sleep_for:.1f}s")
            time.sleep(sleep_for)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        got_url = False
        try:
            for line in iter(proc.stdout.readline, ''):
                if stop_event.is_set():
                    break
                s = line.strip()
                if not s:
                    continue
                print(f"{name}:", s)
                m = url_pattern.search(s)
                if m:
                    update_url(m.group(0))
                    got_url = True
                    backoff = BACKOFF_START
        finally:
            proc.terminate()
            proc.wait()
            if not got_url:
                return False  # signal fail
            sleep_for = min(backoff, BACKOFF_MAX) + random.uniform(0, 5)
            if not stop_event.is_set():
                print(f"⚠️ {name} ended. Restarting in {sleep_for:.1f}s...")
                time.sleep(sleep_for)
    return True

def run_auto_tunnel():
    # patterns
    pat_serveo = re.compile(r"https://[a-zA-Z0-9.-]+\.serveo\.net", re.IGNORECASE)
    pat_cf = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)

    while not stop_event.is_set():
        # --- try serveo first ---
        serveo_cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ExitOnForwardFailure=yes",
            "-R", f"80:localhost:{PORT}", "serveo.net", "-p", "22"
        ]
        ok = run_tunnel(serveo_cmd, pat_serveo, "Serveo")
        if ok:
            continue  # keep Serveo if success

        print("❌ Serveo failed. Switching to Cloudflare...")
        cf_cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"]
        run_tunnel(cf_cmd, pat_cf, "Cloudflare")


def handle_signal(sig, frame):
    print(f"\n🛑 Caught signal {sig}. Exiting...")
    stop_event.set()


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

if __name__ == "__main__":
    threading.Thread(target=status_heartbeat, daemon=True).start()
    run_auto_tunnel()
