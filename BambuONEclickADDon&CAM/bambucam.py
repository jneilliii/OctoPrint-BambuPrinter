import cv2
from flask import Flask, Response, request
import yaml
import os

app = Flask(__name__)

# Function to retrieve config values from config.yaml
def get_config_value(keys, fallback=None):
    possible_paths = [
        "/octoprint/octoprint/config.yaml",  # Docker path
        os.path.expanduser("~/.octoprint/config.yaml")  # Default path
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
                        print(f"Key '{key}' not found, returning fallback.")
                        return fallback
                return value
            except Exception as e:
                print(f"Error reading config at {config_path}: {e}")
                return fallback
    print("No config file found.")
    return fallback

# Get access code and host from config.yaml
ACCESS_CODE = get_config_value(["plugins", "bambu_printer", "access_code"], "00000000")
HOST = get_config_value(["plugins", "bambu_printer", "host"], "192.168.0.100")
RTSP_URL = f"rtsps://bblp:{ACCESS_CODE}@{HOST}:322/streaming/live/1"

# Function to generate frames from the webcam stream
def generate_frames():
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise Exception("Could not open video stream")

    while True:
        success, frame = cap.read()
        if not success:
            continue

        ret, buffer = cv2.imencode('.jpg', frame)
        frame = buffer.tobytes()

        yield (
            b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
        )

# Function to capture a snapshot from the webcam stream
def get_snapshot():
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise Exception("Could not open video stream")

    success, frame = cap.read()
    cap.release()

    if not success:
        raise Exception("Failed to capture snapshot")

    ret, buffer = cv2.imencode('.jpg', frame)
    return buffer.tobytes()

# Route for the webcam feed
@app.route('/webcam/')
def webcam():
    action = request.args.get('action')

    if action == 'snapshot':
        try:
            frame = get_snapshot()
            return Response(frame, mimetype='image/jpeg')
        except Exception as e:
            return str(e), 500
    else:
        return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

# Run the Flask app
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8183)
