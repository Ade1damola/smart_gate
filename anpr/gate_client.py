"""Shared HTTP client helpers for the Raspberry Pi ANPR scripts.

Talks to two different things:
  - the hosted Flask server (plate lookup, verification, logging)
  - the ESP32 at the gate itself (local network, not the hosted server)

Configured entirely through environment variables so the same scripts work
unchanged on a laptop (testing against localhost, no ESP32 attached) and on
the real Raspberry Pi at the gate.
"""

import os

import requests

SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:5000")
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY", "")
ESP32_URL = os.environ.get("ESP32_URL", "")


def _device_headers():
    headers = {}
    if DEVICE_API_KEY:
        headers["X-Device-Key"] = DEVICE_API_KEY
    return headers


def request_check_plate(plate):
    """GET /check_plate on the hosted server. Returns the parsed JSON dict."""
    response = requests.get(
        f"{SERVER_URL}/check_plate",
        params={"plate": plate},
        headers=_device_headers(),
        timeout=10,
    )
    return response.json()


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


def notify_esp32(check_plate_data, event_type="entry"):
    """GET /staff_alert on the ESP32's local web server so it can alert the
    guard and start fingerprint/OTP verification.

    Returns the ESP32's JSON response on success, or None if ESP32_URL isn't
    configured or the ESP32 couldn't be reached (caller should fall back to
    manual verification in that case).
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
                "fingerprint_id": check_plate_data.get("fingerprint_template_id") or "",
                "event_type": event_type.upper(),
            },
            timeout=5,
        )
        return response.json()
    except requests.RequestException as exc:
        print(f"Could not reach ESP32 at {ESP32_URL}: {exc}")
        return None
