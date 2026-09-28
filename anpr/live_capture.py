"""Continuously watch a camera feed for plates and drive the gate flow.

Runs the same detect -> check_plate -> notify_esp32/open_gate_for_non_staff
flow as anpr_engine_rapidocr.py's __main__ block, but against a live camera
feed instead of a single image file. Meant to run unattended on the
Raspberry Pi at the gate (wrapped in a systemd service - no console attached
in the field, so state changes are just printed and picked up via
journalctl).

Camera access is pluggable via CAMERA_BACKEND so the capture loop itself
doesn't need to change once the camera model is settled:
  - "opencv" (default): any USB/UVC webcam, via cv2.VideoCapture
  - "picamera2": the Pi's CSI ribbon camera, via picamera2

A cheap Haar-cascade pass (the same cascade/settings anpr_engine.py uses)
gates whether a frame is worth handing to RapidOCR at all - running RapidOCR
on every frame from a live feed is too slow on a Pi 4 CPU.
"""

import os
import tempfile
import time

import cv2

import gate_client
from anpr_engine_rapidocr import run_anpr

CAMERA_BACKEND = os.environ.get("CAMERA_BACKEND", "opencv")
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
CAPTURE_WIDTH = int(os.environ.get("CAPTURE_WIDTH", "1280"))
CAPTURE_HEIGHT = int(os.environ.get("CAPTURE_HEIGHT", "720"))

# How often to even look at a frame - RapidOCR is too slow to run on every
# frame from a live feed on a Pi 4 CPU, so this throttles how often the
# cascade pre-filter (and, on a hit, RapidOCR) runs at all.
SCAN_INTERVAL_SECONDS = float(os.environ.get("SCAN_INTERVAL_SECONDS", "0.5"))

# Once a plate has been successfully processed, ignore repeats of it for
# this long - a car idling at the gate would otherwise re-trigger
# notify_esp32/open_gate_for_non_staff on every scan.
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "30"))

EVENT_TYPE = os.environ.get("ANPR_EVENT_TYPE", "entry")

_CASCADE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "haarcascade_russian_plate_number.xml")
_plate_cascade = cv2.CascadeClassifier(_CASCADE_PATH)

# Same cascade settings anpr_engine.py's detect_plate uses.
DETECTION_WIDTH = 1000
MIN_BRIGHTNESS = 50


def _looks_like_a_plate_is_present(frame):
    """Cheap pre-filter: does this frame contain anything plate-shaped?

    Only decides whether the frame is worth handing to RapidOCR - the
    actual read still comes from run_anpr().
    """
    img_w = frame.shape[1]
    scale = DETECTION_WIDTH / img_w if img_w > DETECTION_WIDTH else 1.0
    working = cv2.resize(frame, None, fx=scale, fy=scale) if scale != 1.0 else frame
    gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)

    rects, _, weights = _plate_cascade.detectMultiScale3(
        gray, scaleFactor=1.03, minNeighbors=2, minSize=(30, 10), outputRejectLevels=True,
    )
    for (x, y, w, h), weight in zip(rects, weights):
        if gray[y:y + h, x:x + w].mean() >= MIN_BRIGHTNESS:
            return True
    return False


class _OpenCVCamera:
    def __init__(self):
        self._cap = cv2.VideoCapture(CAMERA_INDEX)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
        if not self._cap.isOpened():
            raise RuntimeError(f"Could not open camera index {CAMERA_INDEX}")

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
        # picamera2 hands back RGB888; run_anpr's cv2.imread path expects
        # BGR, so swap channels before the frame gets written to disk.
        frame = self._picam2.capture_array()
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

    def close(self):
        self._picam2.stop()


def _open_camera():
    if CAMERA_BACKEND == "picamera2":
        return _Picamera2Camera()
    if CAMERA_BACKEND == "opencv":
        return _OpenCVCamera()
    raise ValueError(f"Unknown CAMERA_BACKEND: {CAMERA_BACKEND!r}")


def _handle_plate(plate):
    print(f"Plate detected: {plate}")
    try:
        data = gate_client.request_check_plate(plate)
    except Exception as exc:
        # A long-running field deployment will see real network blips
        # against the hosted server; log and keep scanning rather than die.
        print(f"Could not reach server to check {plate}: {exc}")
        return

    if not data.get("is_staff_vehicle"):
        print("Non-staff vehicle. Opening gate.")
        if not gate_client.open_gate_for_non_staff():
            print("Could not reach ESP32 to open gate - manual override needed.")
        return

    print(f"STAFF CAR: {data.get('owner_name')}")
    response = gate_client.notify_esp32(data, event_type=EVENT_TYPE)
    if response is None:
        print("ESP32 unreachable - staff car needs a manual/guard override.")
    else:
        print(f"ESP32 notified: {response}")


def main():
    camera = _open_camera()
    print(f"ANPR live capture started (backend={CAMERA_BACKEND}, scanning every {SCAN_INTERVAL_SECONDS}s).")

    last_plate = None
    last_plate_time = 0.0

    try:
        while True:
            frame = camera.read()
            if frame is None:
                print("Camera read failed; retrying.")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            if _looks_like_a_plate_is_present(frame):
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                        cv2.imwrite(tmp.name, frame)
                        tmp_path = tmp.name
                    plate = run_anpr(tmp_path)
                finally:
                    if tmp_path:
                        os.remove(tmp_path)

                now = time.time()
                already_handled_recently = plate == last_plate and now - last_plate_time < COOLDOWN_SECONDS
                if plate and not already_handled_recently:
                    _handle_plate(plate)
                    last_plate, last_plate_time = plate, now

            time.sleep(SCAN_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        camera.close()


if __name__ == "__main__":
    main()
