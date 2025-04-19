import cv2
from flask import Flask, Response, request

app = Flask(__name__)

# RTSP URL to your Bambu Lab stream
RTSP_URL = "rtsps://bblp:59008066@192.168.2.242:322/streaming/live/1"

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

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8183)
