"""Shared HTTP client helpers for the ANPR scripts.

Talks to two different things:
  - the hosted Flask server (plate lookup, detections, live camera feed)
  - the ESP32 at the gate itself (local network, not the hosted server)

Configured entirely through environment variables so the same scripts work
unchanged on a laptop (testing against localhost, no ESP32 attached) and on
the real gate computer.
"""

import os

import requests

SERVER_URL = os.environ.get("SERVER_URL", "https://verigate-ry5y.onrender.com")
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY", "")
ESP32_URL = os.environ.get("ESP32_URL", "http://verigate.local")


def _device_headers():
    # X-Device-Name lets the server show the camera as online on the admin
    # monitor whenever it calls in.
    headers = {"X-Device-Name": "camera"}
    if DEVICE_API_KEY:
        headers["X-Device-Key"] = DEVICE_API_KEY
    return headers


class ServerError(Exception):
    """The server answered, but refused the request (wrong device key, bad
    data...). Never treat this as a "not registered" answer - that would
    open the gate for a registered car."""


def _lookup_result(response):
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or "is_staff_vehicle" not in data:
        message = data.get("message") or response.reason
        raise ServerError(f"{response.status_code} {message}")
    return data


def request_check_plate(plate):
    """GET /check_plate on the hosted server. Returns the parsed JSON dict."""
    response = requests.get(
        f"{SERVER_URL}/check_plate",
        params={"plate": plate},
        headers=_device_headers(),
        timeout=10,
    )
    return _lookup_result(response)


def report_detection(plate, jpeg_bytes=None, event_type="entry"):
    """POST /api/device/detection: record a confirmed plate read (with the
    frame it came from) on the admin monitor, and look the plate up.

    Returns the same dict as request_check_plate.
    """
    files = {"image": ("frame.jpg", jpeg_bytes, "image/jpeg")} if jpeg_bytes else None
    response = requests.post(
        f"{SERVER_URL}/api/device/detection",
        data={"plate": plate, "event_type": event_type},
        files=files,
        headers=_device_headers(),
        timeout=30,
    )
    return _lookup_result(response)


def push_camera_frame(jpeg_bytes):
    """POST one live frame for the admin monitor. Raises ServerError if the
    server refuses it."""
    headers = _device_headers()
    headers["Content-Type"] = "image/jpeg"
    response = requests.post(
        f"{SERVER_URL}/api/device/camera_frame",
        data=jpeg_bytes,
        headers=headers,
        timeout=10,
    )
    if response.status_code != 200:
        try:
            message = response.json().get("message")
        except ValueError:
            message = response.reason
        raise ServerError(f"{response.status_code} {message}")


def verify_fingerprint(staff_id, template_id, plate_number, event_type="entry"):
    """POST /api/verify_fingerprint on the hosted server (manual/no-ESP32 test path)."""
    response = requests.post(
        f"{SERVER_URL}/api/verify_fingerprint",
        json={
            "staff_id": staff_id,
            "fingerprint_template_id": template_id,
            "plate_number": plate_number,
            "event_type": event_type,
        },
        headers=_device_headers(),
        timeout=10,
    )
    return response.json()


def verify_otp(staff_id, otp_code, plate_number, event_type="entry"):
    """POST /api/verify_otp on the hosted server (manual/no-ESP32 test path)."""
    response = requests.post(
        f"{SERVER_URL}/api/verify_otp",
        json={
            "staff_id": staff_id,
            "otp_code": otp_code,
            "plate_number": plate_number,
            "event_type": event_type,
        },
        headers=_device_headers(),
        timeout=10,
    )
    return response.json()


def open_gate_for_non_staff():
    """GET /open_gate on the ESP32 so it opens the barrier directly for a
    visitor vehicle - no fingerprint/OTP verification needed.

    Returns True if the ESP32 acknowledged it, False if ESP32_URL isn't
    configured, the ESP32 is unreachable, or it's busy verifying another
    vehicle (caller should fall back to a manual/physical override).
    """
    if not ESP32_URL:
        return False

    try:
        response = requests.get(f"{ESP32_URL}/open_gate", timeout=5)
        return response.status_code == 200
    except requests.RequestException as exc:
        print(f"Could not reach ESP32 at {ESP32_URL}: {exc}")
        return False


def notify_esp32(check_plate_data, event_type="entry"):
    """GET /staff_alert on the ESP32's local web server so it alerts the
    guard and starts fingerprint (then OTP) verification. The ESP32 fetches
    the owner's fingerprints from the server itself.

    Returns the ESP32's JSON response (which has an "error" key if it's
    busy with another vehicle), or None if ESP32_URL isn't configured or the
    ESP32 couldn't be reached.
    """
    if not ESP32_URL:
        return None

    try:
        response = requests.get(
            f"{ESP32_URL}/staff_alert",
            params={
                "staff_id": check_plate_data["staff_id"],
                "name": check_plate_data.get("owner_name") or "",
                "plate": check_plate_data["plate_number"],
                "event_type": event_type.upper(),
            },
            timeout=5,
        )
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"Could not reach ESP32 at {ESP32_URL}: {exc}")
        return None
