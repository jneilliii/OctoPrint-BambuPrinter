#!/usr/bin/env python3
import os
import yaml
from time import sleep, time
import subprocess
import requests
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime

def get_octoprint_setting(keys, fallback=None):
    # Use Docker-specific path or fallback to default
    possible_paths = [
        "/octoprint/octoprint/config.yaml",  # Your Docker mount
        os.path.expanduser("~/.octoprint/config.yaml")  # Default OctoPrint path
    ]
    
    for config_path in possible_paths:
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    config = yaml.safe_load(f)
                value = config
                for key in keys:
                    value = value.get(key)
                    if value is None:
                        return fallback
                return value
            except Exception as e:
                print(f"Error reading config at {config_path}: {e}")
                return fallback
    print("No config file found.")
    return fallback

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────
WATCH_DIR = get_octoprint_setting(["folder", "uploads"], "/octoprint/octoprint/uploads")
API_KEY = get_octoprint_setting(["api", "key"], "")
API_URL_SD     = "http://localhost:80/api/files/sdcard"
API_JOB        = "http://localhost:80/api/job"
LOG_FILE       = os.path.expanduser("~/print_watcher.log")
VALID_EXTS     = {".3mf", ".gcode", ".stl"}     # Accepted file types
STABILIZE_SEC  =  2    # seconds between size checks
STABILIZE_CNT  =  3    # consecutive identical-size checks
# ────────────────────────────────────────────────────────────────────────────────

def log_message(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts}: {msg}\n")

def delete_file_from_sd(short_name: str):
    """Delete a file from the SD card using its short name."""
    try:
        log_message(f"Attempting to delete file '{short_name}' from SD card...")
        headers = {"X-Api-Key": API_KEY}
        response = requests.delete(f"{API_URL_SD}/{short_name}", headers=headers)
        if response.status_code == 204:
            log_message(f"Successfully deleted '{short_name}' from SD card.")
            return True
        else:
            log_message(f"Failed to delete '{short_name}' from SD card: {response.status_code} {response.text}")
            return False
    except Exception as e:
        log_message(f"Error deleting file from SD card: {e}")
        return False

def wait_for_file(fp: Path, timeout: int=30) -> bool:
    """Wait until file size stabilizes (no change for STABILIZE_CNT checks)."""
    start = time()
    last_size = -1
    stable = 0

    while time() - start < timeout:
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
        sleep(STABILIZE_SEC)
    return False

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

def get_sd_card_files():
    """Retrieve the list of files on the SD card."""
    try:
        log_message("Fetching list of files on SD card...")
        headers = {"X-Api-Key": API_KEY}
        
        # Force a refresh of the SD card file list
        refresh_response = requests.post(API_URL_SD, headers=headers, json={"command": "refresh"})
        if refresh_response.status_code != 204:
            log_message(f"Failed to refresh SD card file list: {refresh_response.status_code} {refresh_response.text}")
        
        # Wait for the SD card to refresh
        sleep(3)  # Add a delay to ensure the file list is updated
        
        # Fetch the file list
        response = requests.get(API_URL_SD, headers=headers)
        if response.status_code == 200:
            files = response.json().get("files", [])
            log_message(f"Retrieved {len(files)} file(s) from SD card.")
            return files
        else:
            log_message(f"Failed to fetch SD card files: {response.status_code} {response.text}")
            return []
    except Exception as e:
        log_message(f"Error fetching SD card files: {e}")
        return []

def find_short_name(full_name: str, sd_files: list):
    """
    Find the short name for a given full file name.
    """
    log_message(f"DEBUG: Looking for short name matching full name: {full_name}")
    for file_entry in sd_files:
        log_message(f"DEBUG: Checking SD card file - Full Name: {file_entry['display']}, Short Name: {file_entry['name']}")
        if file_entry["display"] == full_name:
            short_name = file_entry["name"]
            log_message(f"Matched full name '{full_name}' to short name '{short_name}'")
            return short_name
    log_message(f"ERROR: Could not find a matching short name for '{full_name}' on the SD card.")
    return None

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
    def __init__(self):
        self.processed_files = {}  # Track processed files with timestamps
        self.COOLDOWN = 10  # Cooldown period in seconds
        self.debounce_time = 2  # Debounce period for rapid modification events (in seconds)

    def process_file(self, event):
        """Process the file when it is created."""
        if event.is_directory:
            return

        fp = Path(event.src_path)
        full_name = fp.name
        current_time = time()
        log_message(f"Detected file: {full_name}")

        # Ignore temporary or unsupported files
        if full_name.startswith("tmp") or full_name.lower().endswith(".json"):
            log_message(f"Ignoring temp/json: {full_name}")
            return
        if fp.suffix.lower() not in VALID_EXTS:
            log_message(f"Ignoring unsupported extension: {full_name}")
            return

        # Remove expired entries from the processed_files dictionary
        self.processed_files = {
            file: timestamp
            for file, timestamp in self.processed_files.items()
            if current_time - timestamp < self.COOLDOWN
        }

        # Skip files still within the cooldown period
        if full_name in self.processed_files:
            log_message(f"Skipping file within cooldown period: {full_name}")
            return

        # Wait until the file size stabilizes
        if not wait_for_file(fp):
            log_message(f"File not stable/vanished: {full_name}")
            return

        # Fetch the SD card files and find the short name for the existing file
        sd_card_files = get_sd_card_files()
        short_name = find_short_name(full_name, sd_card_files)

        # Delete the existing file if it exists
        if short_name:
            delete_file_from_sd(short_name)

        # Upload the new file to the SD card
        if not upload_file_to_sd(fp):
            log_message(f"Upload failed, skipping print: {full_name}")
            return

        # Refresh the SD card file list and find the short name for the new file
        sd_card_files = get_sd_card_files()
        short_name = find_short_name(full_name, sd_card_files)

        if not short_name:
            log_message(f"Failed to determine short name for '{full_name}', aborting print.")
            return

        # Ensure the printer is idle before starting a new print
        printer_status = self.get_printer_status()
        if printer_status in ["printing", "paused"]:
            log_message("Printer is busy, canceling the current print job...")
            cancel_print_bash()
            sleep(5)

        # Start the new print job
        log_message(f"Starting print job for '{short_name}'...")
        start_print_bash(short_name)
        log_message(f"Print command sent for '{short_name}'.")

        # Mark the file as processed with the current timestamp
        self.processed_files[full_name] = current_time

    def on_created(self, event):
        """Handle file creation events."""
        log_message(f"File created: {event.src_path}")
        self.process_file(event)

    def on_modified(self, event):
        """Ignore file modification events."""
        pass

    def get_printer_status(self):
        """Check the printer's current status."""
        try:
            response = requests.get(API_JOB, headers={"X-Api-Key": API_KEY})
            if response.status_code == 200:
                return response.json().get("state", {}).get("text", "").lower()
        except Exception as e:
            log_message(f"Error fetching printer status: {e}")
        return "unknown"
        # Optionally remove local copy:
        # fp.unlink()
        # log_message(f"Removed local: {full_name}")

if __name__ == "__main__":
    log_message(f"Watcher starting on {WATCH_DIR}")
    obs = Observer()
    obs.schedule(PrintWatcher(), WATCH_DIR, recursive=False)
    obs.start()
    try:
        while True:
            sleep(1)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()
    log_message("Watcher stopped.")
