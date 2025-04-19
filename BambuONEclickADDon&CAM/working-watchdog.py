#!/usr/bin/env python3
import os
import time
import subprocess
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime
import requests

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────
WATCH_DIR      = "/octoprint/octoprint/uploads" # Make sure you find out where your Uploads folder is!!
API_KEY        = "ENTER_YOUR_OCTOPRINT_API"                      
API_URL_SD     = "http://localhost:80/api/files/sdcard"
API_JOB       = "http://localhost:80/api/job"
LOG_FILE       = os.path.expanduser("~/print_watcher.log")
VALID_EXTS     = {".3mf", ".gcode", ".stl"}     # Accepted file types
STABILIZE_SEC  =  2    # seconds between size checks
STABILIZE_CNT  =  3    # consecutive identical-size checks
# ────────────────────────────────────────────────────────────────────────────────

def log_message(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts}: {msg}\n")

def wait_for_file(fp: Path, timeout: int=30) -> bool:
    """Wait until file size stabilizes (no change for STABILIZE_CNT checks)."""
    start = time.time()
    last_size = -1
    stable = 0

    while time.time() - start < timeout:
        if not fp.exists():
            return False
        size = fp.stat().st_size
        if size == last_size:
            stable += 1
            if stable >= STABILIZE_CNT:
                return True
        else:
            stable = 0
            last_size = size
        time.sleep(STABILIZE_SEC)
    return False

def convert_to_short(name: str) -> str:
    """Convert to 8.3 style: first 6 chars lowercase + ~1 + lower ext."""
    base, ext = os.path.splitext(name)
    short = f"{base[:6].lower()}~1{ext.lower()}"
    log_message(f"Converted '{name}' → '{short}'")
    return short

def upload_file_to_sd(fp: Path) -> bool:
    """Upload via requests to OctoPrint SD endpoint."""
    log_message(f"Uploading '{fp.name}' to SD card...")
    try:
        with open(fp, "rb") as f:
            r = requests.post(
                API_URL_SD,
                headers={"X-Api-Key": API_KEY},
                files={"file": (fp.name, f)},
                data={"select":"false","print":"false"}
            )
        log_message(f" Upload response: {r.status_code} {r.text}")
        return r.status_code == 201
    except Exception as e:
        log_message(f" Error uploading to SD: {e}")
        return False

def cancel_print_bash():
    cmd = [
        "curl","-s","-X","POST", API_JOB,
        "-H", f"X-Api-Key: {API_KEY}",
        "-H","Content-Type: application/json",
        "-d", '{"command":"cancel"}'
    ]
    log_message("Cancelling active print (if any)…")
    res = subprocess.run(cmd, capture_output=True, text=True)
    log_message(f" Cancel stdout: {res.stdout.strip()}")
    if res.stderr.strip():
        log_message(f" Cancel stderr: {res.stderr.strip()}")

def start_print_bash(short_name: str):
    cmd = [
        "curl","-s","-X","POST", f"{API_URL_SD}/{short_name}",
        "-H", f"X-Api-Key: {API_KEY}",
        "-H", "Content-Type: application/json",
        "-d", '{"command":"select","print":true}'
    ]
    log_message(f"Starting print of '{short_name}'…")
    res = subprocess.run(cmd, capture_output=True, text=True)
    log_message(f" Start stdout: {res.stdout.strip()}")
    if res.stderr.strip():
        log_message(f" Start stderr: {res.stderr.strip()}")

class PrintWatcher(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return

        fp = Path(event.src_path)
        name = fp.name
        log_message(f"Detected new file: {name}")

        # ignore temps & metadata/json
        if name.startswith("tmp") or name.lower().endswith(".json"):
            log_message(f"Ignoring temp/json: {name}")
            return
        if fp.suffix.lower() not in VALID_EXTS:
            log_message(f"Ignoring unsupported extension: {name}")
            return

        # wait until fully written
        if not wait_for_file(fp):
            log_message(f"File not stable/vanished: {name}")
            return

        # upload to SD
        if not upload_file_to_sd(fp):
            log_message(f"Upload failed, skipping print: {name}")
            return

        # convert & cancel / start
        short = convert_to_short(name)
        cancel_print_bash()
        time.sleep(5)
        start_print_bash(short)
        log_message(f"Print command sent for '{short}'.")
        # optionally remove local copy:
        # fp.unlink()
        # log_message(f"Removed local: {name}")

if __name__ == "__main__":
    log_message(f"Watcher starting on {WATCH_DIR}")
    obs = Observer()
    obs.schedule(PrintWatcher(), WATCH_DIR, recursive=False)
    obs.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()
    log_message("Watcher stopped.")
