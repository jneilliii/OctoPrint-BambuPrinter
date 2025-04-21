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
API_CONNECTION = "http://localhost:80/api/connection"  # Update this URL if your OctoPrint API uses a different host or port
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

def disconnect_serial():
    """Disconnect the serial connection to the printer using the OctoPrint API."""
    try:
        log_message("Sending disconnect command to OctoPrint API...")
        headers = {"X-Api-Key": API_KEY}
        data = {"command": "disconnect"}
        response = requests.post(API_CONNECTION, headers=headers, json=data)
        if response.status_code == 204:
            log_message("Serial connection successfully disconnected.")
        else:
            log_message(f"Failed to disconnect serial connection: {response.status_code} {response.text}")
    except Exception as e:
        log_message(f"Error while disconnecting the serial connection: {e}")
        raise

def connect_serial():
    """Reconnect the serial connection to the printer using the OctoPrint API."""
    try:
        log_message("Sending reconnect command to OctoPrint API...")
        headers = {"X-Api-Key": API_KEY}
        data = {"command": "connect"}  # Optionally specify port and baudrate
        response = requests.post(API_CONNECTION, headers=headers, json=data)
        if response.status_code == 204:
            log_message("Serial connection successfully reconnected.")
        else:
            log_message(f"Failed to reconnect serial connection: {response.status_code} {response.text}")
    except Exception as e:
        log_message(f"Error while reconnecting the serial connection: {e}")
        raise

class PrintWatcher(FileSystemEventHandler):
    def __init__(self):
        self.reset_watcher()

    def reset_watcher(self):
        """Reset all tracking variables and state"""
        log_message("Resetting watcher state...")
        self.processed_files = {}  # Track processed files with timestamps
        self.retry_counts = {}  # Track retry counts for each file
        self.printing_files = {}  # Track files currently being printed
        self.last_upload_event = {}  # Track the last upload event timestamp for each file
        self.COOLDOWN = 10  # Cooldown period in seconds
        self.PRINTER_STATUS_COOLDOWN = 5  # Time to wait before rechecking printer status (in seconds)
        self.MAX_RETRIES = 4  # Maximum number of retries for uploading a file
        self.last_printer_state = None
        self.last_reconnect_time = 0

    def check_if_printing_started(self, short_name):
        """Check if print started successfully after a delay."""
        log_message("Checking if printer started printing...")
        
        try:
            response = requests.get(API_JOB, headers={"X-Api-Key": API_KEY})
            if response.status_code == 200:
                data = response.json()
                current_state = data.get("state", "").lower()
                if "printing" in current_state:  # This will catch "Printing from SD" as well
                    log_message("Print started successfully!")
                    return True
                else:
                    log_message(f"Print not started. Current state: {current_state}")
                    return False
        except Exception as e:
            log_message(f"Error checking print status: {e}")
            return False

    def get_printer_status(self):
        """Check the printer's current status."""
        try:
            response = requests.get(API_JOB, headers={"X-Api-Key": API_KEY})
            if response.status_code == 200:
                data = response.json()
                current_state = data.get("state", "").lower()
                
                # If state changed from printing to something else, reset the watcher
                if self.last_printer_state == "printing" and current_state != "printing":
                    log_message("Print job ended. Resetting watcher state...")
                    self.reset_watcher()
                
                # Update last known state
                self.last_printer_state = current_state
                return current_state
            else:
                log_message(f"Failed to fetch printer status: {response.status_code} {response.text}")
                return "unknown"
        except Exception as e:
            log_message(f"Error fetching printer status: {e}")
            return "unknown"

    def reconnect_serial(self):
        """Disconnect and reconnect the serial connection with better error handling."""
        try:
            current_time = time()
            if current_time - self.last_reconnect_time < 5:  # Reduced from 10 to 5 seconds
                log_message("Reconnection attempted too soon. Skipping.")
                return

            log_message("Disconnecting serial connection...")
            disconnect_serial()
            sleep(4)  # Increased from 3 to 4 seconds for more stable reconnection
            log_message("Reconnecting serial connection...")
            connect_serial()
            self.last_reconnect_time = current_time
            log_message("Serial connection successfully reconnected.")
            
            # Add additional delay after reconnection
            sleep(2)  # Add a 2-second delay after reconnection
        except Exception as e:
            log_message(f"Error during serial reconnection: {e}")

    def update_printer_status(self, full_name):
        """Re-check the printer status and update the printing status."""
        log_message(f"Re-checking printer status for '{full_name}'...")
        printer_status = self.get_printer_status()
        if printer_status == "operational":
            log_message(f"Printer is now idle. Resetting printing status for '{full_name}'.")
            self.printing_files[full_name] = False
        else:
            log_message(f"Printer is still busy ({printer_status}). Skipping further actions.")

    def upload_with_retry(self, fp):
        """Attempt to upload a file with retry handling."""
        full_name = fp.name

        # Track retries for this file
        if full_name not in self.retry_counts:
            self.retry_counts[full_name] = 0

        log_message(f"Uploading '{full_name}' to SD card (Attempt {self.retry_counts[full_name] + 1}/{self.MAX_RETRIES})...")
        result = upload_file_to_sd(fp)
        if not result:
            self.retry_counts[full_name] += 1
            if self.retry_counts[full_name] >= self.MAX_RETRIES:
                log_message(f"Max retries reached for '{full_name}'. Upload will not be retried until modification.")
            return False

        # Reset retry count on successful upload
        self.retry_counts[full_name] = 0
        return True

    def process_file(self, event):
        """Process the file when it is created or modified."""
        if event.is_directory:
            return

        fp = Path(event.src_path)
        full_name = fp.name
        current_time = time()
        log_message(f"Detected file: {full_name}")

        # Check printer status first
        printer_status = self.get_printer_status()
        if printer_status in ["printing", "paused"] or "printing" in printer_status.lower():
            log_message(f"Printer is currently {printer_status}. Skipping processing.")
            return
        
        # If we recently reset due to print ending, add a longer delay
        if self.last_printer_state == "operational" and not self.processed_files:
            log_message("Recently reset watcher detected. Adding delay...")
            sleep(5)  # Increased from 2 to 5 seconds

        # Ignore temporary or unsupported files
        if full_name.startswith("tmp") or full_name.lower().endswith(".json"):
            log_message(f"Ignoring temp/json: {full_name}")
            return
        if fp.suffix.lower() not in VALID_EXTS:
            log_message(f"Ignoring unsupported extension: {full_name}")
            return

        # Update the last upload event timestamp
        self.last_upload_event[full_name] = current_time

        # Wait until the file size stabilizes
        if not wait_for_file(fp):
            log_message(f"File not stable/vanished: {full_name}")
            return

        # Check and handle retries
        if full_name in self.retry_counts and self.retry_counts[full_name] >= self.MAX_RETRIES:
            log_message(f"Max retries reached for '{full_name}', skipping further attempts until modification.")
            return

        # Fetch the SD card files and find the short name for the existing file
        sd_card_files = get_sd_card_files()
        short_name = find_short_name(full_name, sd_card_files)

        # Delete the existing file if it exists
        if short_name:
            if not delete_file_from_sd(short_name):
                log_message(f"Failed to delete file '{short_name}' from SD card. It may be in use.")
                return

        # Check if we need to reconnect before uploading
        if full_name in self.last_upload_event:
            time_since_last_upload = current_time - self.last_upload_event[full_name]
            if time_since_last_upload < 15:  # Back-to-back upload detected
                log_message(f"Back-to-back upload detected for '{full_name}'. Reconnecting serial...")
                self.reconnect_serial()

        # Attempt to upload the new file to the SD card
        if not self.upload_with_retry(fp):
            log_message(f"Upload failed for '{full_name}', skipping further processing.")
            return

        # Refresh the SD card file list and find the short name for the new file
        sd_card_files = get_sd_card_files()
        short_name = find_short_name(full_name, sd_card_files)

        if not short_name:
            log_message(f"Failed to determine short name for '{full_name}', aborting print.")
            return

        # Start the new print job
        log_message(f"Starting print job for '{short_name}'...")
        start_print_bash(short_name)
        log_message(f"Print command sent for '{short_name}'. Waiting 5 seconds for printer to respond...")
        sleep(5)  # Give printer time to start
        log_message("Checking print status...")

        if self.check_if_printing_started(short_name):
            log_message("Print confirmed started. Stopping further processing.")
            self.printing_files[full_name] = True
            self.processed_files[full_name] = current_time
            return
        else:
            log_message("Print didn't start. Trying serial reconnection...")
            self.reconnect_serial()
            sleep(2)  # Give it a moment after reconnection
            # Try starting print again
            log_message(f"Retrying print job for '{short_name}'...")
            start_print_bash(short_name)
            if self.check_if_printing_started(short_name):
                log_message("Print confirmed started after reconnection. Stopping further processing.")
                self.printing_files[full_name] = True
                self.processed_files[full_name] = current_time
                return
            else:
                log_message("Print failed to start even after reconnection.")

    def on_created(self, event):
        """Handle file creation events."""
        log_message(f"Handling created event for file: {event.src_path}")
        self.process_file(event)

    def on_modified(self, event):
        """Handle file modification events."""
        log_message(f"Handling modified event for file: {event.src_path}")
        self.process_file(event)
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
