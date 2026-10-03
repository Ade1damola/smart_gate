"""Continuously watch the gate camera for plates and drive the gate flow.

No road sensor or trigger is needed: the camera runs all the time and the
script decides for itself when a vehicle is worth reading.

Three things run side by side:
  - a grabber thread that reads the camera nonstop and keeps only the
    newest frame (OpenCV otherwise buffers frames, so after a slow OCR pass
    the "next" frame would be seconds old);
  - a pusher thread that sends a small frame to the server every
    CAMERA_PUSH_INTERVAL seconds for the admin's live monitor;
  - the main loop, which runs RapidOCR when there's activity in front of the
    camera, confirms a plate over several reads, reports it to the server
    and then alerts the ESP32 (registered vehicle) or opens the gate
    (visitor).

Meant to run unattended on the gate computer (e.g. as a systemd service);
state changes are printed for journalctl.

Configuration (environment variables):
  CAMERA_BACKEND        "opencv" (USB/UVC webcam, default) or "picamera2"
  CAMERA_INDEX          OpenCV camera index or stream URL (default 0)
  CAPTURE_WIDTH/HEIGHT  requested resolution (default 1280x720)
  SCAN_INTERVAL_SECONDS minimum gap between OCR passes (default 0.5)
  PREFILTER             when to run OCR: "motion" (default), "cascade",
                        "both" (either one triggers) or "none" (every scan)
  ACTIVE_SECONDS        keep reading this long after the last motion, so a
                        car that has stopped at the barrier is still read
  CONFIRM_READS         identical reads needed before acting (default 2)
  CONFIRM_WINDOW_SECONDS  ...within this many seconds (default 8)
  COOLDOWN_SECONDS      ignore the same plate for this long once handled
  CAMERA_PUSH_INTERVAL  seconds between live frames sent to the server
                        (default 1; 0 disables the live feed)
  ANPR_EVENT_TYPE       "entry" or "exit" for this camera (default entry)
"""

import os
import re
import tempfile
import threading
import time
from collections import deque

import cv2

import gate_client
from anpr_engine_rapidocr import run_anpr

CAMERA_BACKEND = os.environ.get("CAMERA_BACKEND", "opencv")
CAMERA_INDEX = os.environ.get("CAMERA_INDEX", "0")
CAPTURE_WIDTH = int(os.environ.get("CAPTURE_WIDTH", "1280"))
CAPTURE_HEIGHT = int(os.environ.get("CAPTURE_HEIGHT", "720"))

SCAN_INTERVAL_SECONDS = float(os.environ.get("SCAN_INTERVAL_SECONDS", "0.5"))
PREFILTER = os.environ.get("PREFILTER", "motion").lower()
ACTIVE_SECONDS = float(os.environ.get("ACTIVE_SECONDS", "8"))
CONFIRM_READS = int(os.environ.get("CONFIRM_READS", "2"))
CONFIRM_WINDOW_SECONDS = float(os.environ.get("CONFIRM_WINDOW_SECONDS", "8"))
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "30"))
CAMERA_PUSH_INTERVAL = float(os.environ.get("CAMERA_PUSH_INTERVAL", "1"))
EVENT_TYPE = os.environ.get("ANPR_EVENT_TYPE", "entry")

# Live-feed frames are kept small: they go over the internet once a second.
PUSH_WIDTH = 640
PUSH_JPEG_QUALITY = 65
# Detection snapshots are kept on the server as evidence, so a bit sharper.
SNAPSHOT_WIDTH = 960
SNAPSHOT_JPEG_QUALITY = 78

# Motion detection: fraction of a downscaled, blurred frame that must change.
MOTION_PIXEL_THRESHOLD = 25
MOTION_AREA_FRACTION = 0.01

# How long to keep retrying a busy ESP32 (it's still verifying the previous
# vehicle) before giving up on this alert.
ESP32_BUSY_RETRY_SECONDS = 20

_CASCADE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "haarcascade_russian_plate_number.xml")
_plate_cascade = cv2.CascadeClassifier(_CASCADE_PATH)
DETECTION_WIDTH = 1000
MIN_BRIGHTNESS = 50


# ---------------------------------------------------------------------------
# Camera backends
# ---------------------------------------------------------------------------

class _OpenCVCamera:
    def __init__(self):
        source = int(CAMERA_INDEX) if CAMERA_INDEX.isdigit() else CAMERA_INDEX
        self._cap = cv2.VideoCapture(source)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
        if not self._cap.isOpened():
            raise RuntimeError(f"Could not open camera {CAMERA_INDEX!r}")

    def read(self):
        ok, frame = self._cap.read()
        return frame if ok else None

    def close(self):
        self._cap.release()


class _Picamera2Camera:
    def __init__(self):
        from picamera2 import Picamera2

        self._picam2 = Picamera2()
        config = self._picam2.create_video_configuration(
            main={"size": (CAPTURE_WIDTH, CAPTURE_HEIGHT), "format": "RGB888"}
        )
        self._picam2.configure(config)
        self._picam2.start()
        time.sleep(1)  # let auto-exposure/white-balance settle

    def read(self):
        # picamera2 hands back RGB888; everything downstream expects BGR.
        return cv2.cvtColor(self._picam2.capture_array(), cv2.COLOR_RGB2BGR)

    def close(self):
        self._picam2.stop()


def _open_camera():
    if CAMERA_BACKEND == "picamera2":
        return _Picamera2Camera()
    if CAMERA_BACKEND == "opencv":
        return _OpenCVCamera()
    raise ValueError(f"Unknown CAMERA_BACKEND: {CAMERA_BACKEND!r}")


class FrameGrabber(threading.Thread):
    """Reads the camera continuously and keeps only the newest frame."""

    def __init__(self, camera):
        super().__init__(daemon=True)
        self._camera = camera
        self._lock = threading.Lock()
        self._frame = None
        self._frame_number = 0
        self.running = True

    def run(self):
        failures = 0
        while self.running:
            frame = self._camera.read()
            if frame is None:
                failures += 1
                if failures % 50 == 1:
                    print("Camera read failed; retrying.")
                time.sleep(0.1)
                continue
            failures = 0
            with self._lock:
                self._frame = frame
                self._frame_number += 1

    def latest(self):
        """Returns (frame_number, frame); frame is None until the first read."""
        with self._lock:
            return self._frame_number, self._frame


class FramePusher(threading.Thread):
    """Sends a small live frame to the server for the admin monitor."""

    def __init__(self, grabber):
        super().__init__(daemon=True)
        self._grabber = grabber
        self.running = True

    def run(self):
        last_pushed = 0
        reported_failure = False
        while self.running:
            started = time.time()
            frame_number, frame = self._grabber.latest()
            if frame is not None and frame_number != last_pushed:
                try:
                    gate_client.push_camera_frame(_encode_jpeg(frame, PUSH_WIDTH, PUSH_JPEG_QUALITY))
                    last_pushed = frame_number
                    if reported_failure:
                        print("Live feed to server restored.")
                    reported_failure = False
                except Exception as exc:
                    if not reported_failure:
                        print(f"Could not send live frame to server: {exc}")
                    reported_failure = True
            time.sleep(max(0.0, CAMERA_PUSH_INTERVAL - (time.time() - started)))


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

def _encode_jpeg(frame, max_width, quality):
    height, width = frame.shape[:2]
    if width > max_width:
        frame = cv2.resize(frame, (max_width, int(height * max_width / width)))
    ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buffer.tobytes() if ok else None


class MotionDetector:
    """Is something moving in front of the camera? Cheap enough to run on
    every scan, unlike OCR."""

    def __init__(self):
        self._previous = None

    def moved(self, frame):
        small = cv2.resize(frame, (320, int(frame.shape[0] * 320 / frame.shape[1])))
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (21, 21), 0)
        previous, self._previous = self._previous, gray
        if previous is None:
            return True
        diff = cv2.absdiff(previous, gray)
        changed = cv2.countNonZero(cv2.threshold(diff, MOTION_PIXEL_THRESHOLD, 255, cv2.THRESH_BINARY)[1])
        return changed > MOTION_AREA_FRACTION * gray.size


def _cascade_sees_plate(frame):
    """Haar-cascade check for anything plate-shaped (the same settings
    anpr_engine.py uses). Trained on Russian plates, so it can miss some
    Nigerian ones - which is why motion is the default pre-filter."""
    img_w = frame.shape[1]
    scale = DETECTION_WIDTH / img_w if img_w > DETECTION_WIDTH else 1.0
    working = cv2.resize(frame, None, fx=scale, fy=scale) if scale != 1.0 else frame
    gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
    rects, _, _ = _plate_cascade.detectMultiScale3(
        gray, scaleFactor=1.03, minNeighbors=2, minSize=(30, 10), outputRejectLevels=True,
    )
    return any(gray[y:y + h, x:x + w].mean() >= MIN_BRIGHTNESS for (x, y, w, h) in rects)


def _read_plate(frame):
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        cv2.imwrite(tmp_path, frame)
        return run_anpr(tmp_path)
    finally:
        if tmp_path:
            os.remove(tmp_path)


def _normalize(plate):
    return re.sub(r"[^A-Z0-9]", "", plate.upper())


class PlateConfirmer:
    """A single OCR read can be wrong; only act on a plate once it has been
    read the same way CONFIRM_READS times within CONFIRM_WINDOW_SECONDS."""

    def __init__(self):
        self._reads = deque()

    def add(self, plate, now):
        self._reads.append((now, _normalize(plate)))
        while self._reads and now - self._reads[0][0] > CONFIRM_WINDOW_SECONDS:
            self._reads.popleft()
        key = _normalize(plate)
        return sum(1 for _, read in self._reads if read == key) >= CONFIRM_READS

    def forget(self, plate):
        key = _normalize(plate)
        self._reads = deque(item for item in self._reads if item[1] != key)


# ---------------------------------------------------------------------------
# Gate flow
# ---------------------------------------------------------------------------

def _alert_esp32(data):
    deadline = time.time() + ESP32_BUSY_RETRY_SECONDS
    while True:
        response = gate_client.notify_esp32(data, event_type=EVENT_TYPE)
        if response is None:
            print("ESP32 unreachable - registered car needs a manual/guard override.")
            return
        if "error" not in response:
            print(f"ESP32 notified: {response}")
            return
        if time.time() >= deadline:
            print(f"ESP32 still busy ({response['error']}) - giving up on this alert.")
            return
        print("ESP32 busy with the previous vehicle; retrying...")
        time.sleep(2)


def _handle_plate(plate, frame):
    print(f"Plate confirmed: {plate}")
    try:
        data = gate_client.report_detection(
            plate, _encode_jpeg(frame, SNAPSHOT_WIDTH, SNAPSHOT_JPEG_QUALITY), event_type=EVENT_TYPE,
        )
    except gate_client.ServerError as exc:
        # Don't guess: without a real answer the car could be registered,
        # so the gate stays shut and the guard handles it.
        print(f"Server refused the lookup for {plate} ({exc}) - gate NOT opened. Check DEVICE_API_KEY.")
        return False
    except Exception as exc:
        # Real network blips will happen in a long-running deployment; log
        # and keep scanning rather than die.
        print(f"Could not reach server to check {plate}: {exc}")
        return False

    if not data.get("is_staff_vehicle"):
        print("Visitor vehicle. Opening gate.")
        if not gate_client.open_gate_for_non_staff():
            print("Could not reach ESP32 to open gate - manual override needed.")
        return True

    print(f"REGISTERED VEHICLE: {data.get('owner_name')}")
    _alert_esp32(data)
    return True


def _worth_reading(frame, motion, active_until, now):
    if PREFILTER == "none":
        return True, active_until
    moved = PREFILTER in ("motion", "both") and motion.moved(frame)
    if moved:
        active_until = now + ACTIVE_SECONDS
    if moved or now < active_until:
        return True, active_until
    if PREFILTER in ("cascade", "both") and _cascade_sees_plate(frame):
        return True, active_until
    return False, active_until


def main():
    camera = _open_camera()
    grabber = FrameGrabber(camera)
    grabber.start()
    pusher = None
    if CAMERA_PUSH_INTERVAL > 0:
        pusher = FramePusher(grabber)
        pusher.start()

    print(
        f"ANPR live capture started (backend={CAMERA_BACKEND}, prefilter={PREFILTER}, "
        f"confirm={CONFIRM_READS} reads, live feed={'every %ss' % CAMERA_PUSH_INTERVAL if pusher else 'off'})."
    )

    motion = MotionDetector()
    confirmer = PlateConfirmer()
    handled_at = {}
    active_until = 0.0
    last_frame_number = 0

    try:
        while True:
            started = time.time()
            frame_number, frame = grabber.latest()
            if frame is not None and frame_number != last_frame_number:
                last_frame_number = frame_number
                worth_it, active_until = _worth_reading(frame, motion, active_until, started)
                if worth_it:
                    plate = _read_plate(frame)
                    now = time.time()
                    key = _normalize(plate) if plate else ""
                    recently_handled = key and now - handled_at.get(key, 0) < COOLDOWN_SECONDS
                    if plate and not recently_handled and confirmer.add(plate, now):
                        if _handle_plate(plate, frame):
                            handled_at[key] = time.time()
                            confirmer.forget(plate)

            time.sleep(max(0.0, SCAN_INTERVAL_SECONDS - (time.time() - started)))
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        grabber.running = False
        if pusher:
            pusher.running = False
        camera.close()


if __name__ == "__main__":
    main()
