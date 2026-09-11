"""ANPR engine using RapidOCR for plate detection and recognition.

RapidOCR's detector returns text boxes, so this module uses the most
plate-like detected text box as the plate crop and runs RapidOCR again on
that crop. It intentionally exposes the same detect_plate / read_plate /
run_anpr interface as the other ANPR engines.
"""

import cv2

OCR_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
MIN_PLATE_LENGTH = 5
MAX_PLATE_LENGTH = 12
CROP_PADDING_FRAC = 0.15

_ocr = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr = RapidOCR()
    return _ocr


def _clean_text(text):
    return "".join(ch for ch in str(text) if ch.isalnum() or ch == "-").upper().strip("-")


def _looks_like_plate(text):
    if not (MIN_PLATE_LENGTH <= len(text) <= MAX_PLATE_LENGTH):
        return False
    return any(ch.isalpha() for ch in text) and any(ch.isdigit() for ch in text)


def _run_ocr(image):
    """Return RapidOCR's text rows, or an empty list when it finds nothing."""
    result, _ = _get_ocr()(image)
    return result or []


def _result_parts(item):
    """Normalize RapidOCR rows across its list and tuple result variants."""
    if len(item) < 3:
        return None
    bbox, text, confidence = item[0], item[1], item[2]
    try:
        return bbox, str(text), float(confidence)
    except (TypeError, ValueError):
        return None


def _best_candidate(results):
    best = None
    for item in results:
        parts = _result_parts(item)
        if parts is None:
            continue
        bbox, text, confidence = parts
        cleaned = _clean_text(text)
        if _looks_like_plate(cleaned) and (best is None or confidence > best[0]):
            best = (confidence, bbox, cleaned)
    return best


def _crop_from_bbox(image, bbox):
    points = [(float(point[0]), float(point[1])) for point in bbox]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    pad_x = int((x1 - x0) * CROP_PADDING_FRAC)
    pad_y = int((y1 - y0) * CROP_PADDING_FRAC)
    img_h, img_w = image.shape[:2]
    x0 = max(int(x0) - pad_x, 0)
    y0 = max(int(y0) - pad_y, 0)
    x1 = min(int(x1) + pad_x, img_w)
    y1 = min(int(y1) + pad_y, img_h)
    cropped = image[y0:y1, x0:x1]
    return cropped if cropped.size else None


def _best_row_text(results):
    """Combine neighboring text boxes when RapidOCR splits the plate number."""
    rows = []
    for item in results:
        parts = _result_parts(item)
        if parts is None:
            continue
        bbox, text, confidence = parts
        points = [(float(point[0]), float(point[1])) for point in bbox]
        x0, x1 = min(point[0] for point in points), max(point[0] for point in points)
        y0, y1 = min(point[1] for point in points), max(point[1] for point in points)
        cy, height = (y0 + y1) / 2, max(y1 - y0, 1)
        for row in rows:
            if abs(cy - row["cy"]) <= 0.6 * min(height, row["height"]):
                row["items"].append((x0, text, confidence))
                break
        else:
            rows.append({"cy": cy, "height": height, "items": [(x0, text, confidence)]})

    best = None
    for row in rows:
        items = sorted(row["items"], key=lambda item: item[0])
        text = _clean_text("".join(item[1] for item in items))
        if not _looks_like_plate(text):
            continue
        confidence = sum(item[2] for item in items) / len(items)
        if best is None or confidence > best[0]:
            best = (confidence, text)
    return best[1] if best else None


def detect_plate(image_path):
    """Locate and crop the most plate-like text region, or return None."""
    image = cv2.imread(image_path)
    if image is None:
        return None

    best = _best_candidate(_run_ocr(image))
    return _crop_from_bbox(image, best[1]) if best else None


def read_plate(cropped_plate):
    """Read a cropped plate and return a cleaned plate string, or None."""
    if cropped_plate is None:
        return None

    results = _run_ocr(cropped_plate)
    best = _best_candidate(results)
    if best:
        return best[2]
    return _best_row_text(results)


def run_anpr(image_path):
    """Detect and read a plate in one call."""
    image = cv2.imread(image_path)
    if image is None:
        return None

    # RapidOCR can recognize the complete plate on the original image even
    # when its tighter second pass splits the number into multiple boxes.
    first_pass = _best_candidate(_run_ocr(image))
    if first_pass:
        return first_pass[2]

    cropped = detect_plate(image_path)
    return read_plate(cropped)


if __name__ == "__main__":
    import sys

    import requests

    import gate_client

    image_path = sys.argv[1] if len(sys.argv) > 1 else "test_images/LND-113JN.jpeg"
    plate = run_anpr(image_path)

    if plate is None:
        print("No plate detected.")
        sys.exit(0)

    print(f"Plate detected: {plate}")
    try:
        data = gate_client.request_check_plate(plate)
    except requests.exceptions.ConnectionError:
        print(f"Could not reach Flask server at {gate_client.SERVER_URL}.")
        print("Start it first with: python backend/app.py")
        sys.exit(1)
    print(f"Server response: {data}")

    if not data.get("is_staff_vehicle"):
        print("Non-staff vehicle. Gate opens normally.")
        if gate_client.open_gate_for_non_staff():
            print("Gate opened.")
        else:
            print("Could not reach ESP32 to open gate - manual override needed.")
        sys.exit(0)

    print(f"STAFF CAR: {data['owner_name']}")
    esp32_response = gate_client.notify_esp32(data, event_type="entry")
    if esp32_response is not None:
        print(f"ESP32 notified: {esp32_response}")
        sys.exit(0)

    print("ESP32_URL not set (or unreachable) - falling back to manual test mode.")
    print("Awaiting verification...")
    method = input("Enter 'f' for fingerprint or 'o' for OTP: ").strip().lower()

    if method == "f":
        template_id = input("Simulated fingerprint scan - enter template ID read by sensor: ").strip()
        result = gate_client.verify_fingerprint(data["staff_id"], template_id, data["plate_number"])
        print(f"Fingerprint result: {result}")
    elif method == "o":
        otp = input("Enter OTP: ").strip()
        result = gate_client.verify_otp(data["staff_id"], otp, data["plate_number"])
        print(f"OTP result: {result}")
    else:
        print("Unknown verification method.")