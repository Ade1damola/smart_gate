"""
ANPR engine (EasyOCR variant): detects and reads a license plate from an
image using EasyOCR instead of the Haar-cascade + Tesseract pipeline in
anpr_engine.py.

EasyOCR's readtext() does text detection and recognition in one pass, so
"detection" here means locating the text region on the plate that looks
like a plate number (via a confidence/shape heuristic) rather than a
dedicated plate-shape classifier. Same public interface as anpr_engine.py
(detect_plate / read_plate / run_anpr) so callers can swap engines freely.

Pure image-processing module - no printing, no display, no notebook-only
calls. Callers (the Flask backend, this file's own __main__ block, or
eventually the ESP32-side integration script) decide what to do with the
returned values.
"""

import cv2

# Characters Nigerian plates actually carry: letters, digits, and the dash
# printed between the state code and the serial (e.g. "LND-113JN"). Passed
# to EasyOCR as an allowlist so it never wastes a read on punctuation/symbols
# a plate wouldn't contain.
OCR_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"

# OCR results outside this range are almost always noise (a stray digit, a
# sticker, a dealer logo) rather than a real plate read - clamp to what a
# Nigerian plate string ("LND-113JN") actually looks like.
MIN_PLATE_LENGTH = 5
MAX_PLATE_LENGTH = 12

# Crop margin (as a fraction of the detected text box) kept around the best
# candidate before re-running OCR on it - the tight box EasyOCR returns for
# a first-pass detection sometimes clips a leading/trailing character.
CROP_PADDING_FRAC = 0.15

# Loading EasyOCR's recognition model is slow (multi-second, one-time
# download on first use) - deferred behind a lazy singleton so importing
# this module (e.g. for the whitelist/constants) doesn't pay that cost, and
# so the module stays importable in environments where the model hasn't
# been fetched yet.
_reader = None


def _get_reader():
    global _reader
    if _reader is None:
        import easyocr
        _reader = easyocr.Reader(["en"], gpu=False)
    return _reader


def _clean_text(text):
    return "".join(ch for ch in text if ch.isalnum() or ch == "-").upper().strip("-")


def _looks_like_plate(text):
    """Cheap heuristic to reject non-plate text EasyOCR picks up (logos,
    dealer stickers, model badges) - a real plate reads as a mix of letters
    and digits within a plausible length, not all-one-or-the-other."""
    if not (MIN_PLATE_LENGTH <= len(text) <= MAX_PLATE_LENGTH):
        return False
    has_alpha = any(ch.isalpha() for ch in text)
    has_digit = any(ch.isdigit() for ch in text)
    return has_alpha and has_digit


def _best_candidate(results):
    """Pick the most plate-like result from an EasyOCR readtext() call.

    results is a list of (bbox, text, confidence). Ranks by confidence
    among candidates that pass _looks_like_plate, since a well-formed but
    low-confidence read is still more likely to be the actual plate than a
    high-confidence read of unrelated text.
    """
    best = None
    for bbox, text, confidence in results:
        cleaned = _clean_text(text)
        if not _looks_like_plate(cleaned):
            continue
        if best is None or confidence > best[0]:
            best = (confidence, bbox, cleaned)
    return best


def _row_center_and_height(bbox):
    ys = [point[1] for point in bbox]
    y0, y1 = min(ys), max(ys)
    return (y0 + y1) / 2, max(y1 - y0, 1)


def _group_into_rows(results, tolerance=0.6):
    """Cluster OCR boxes into text rows by vertical position.

    A Nigerian plate crop typically contains several stacked lines (state
    name, "CENTRE OF EXCELLENCE" slogan, the plate number, "FEDERAL
    REPUBLIC OF NIGERIA"). Concatenating every box left-to-right regardless
    of row mixes those lines into one bogus string, so callers need the
    plate-number row in isolation.
    """
    # Use the *smaller* of the two heights being compared, not the larger -
    # a box that spans an unusually tall region (e.g. EasyOCR's detection
    # box for large plate-number digits printed over a background graphic)
    # would otherwise get a huge tolerance and swallow short, unrelated
    # boxes above or below it.
    rows = []
    for bbox, text, confidence in results:
        cy, h = _row_center_and_height(bbox)
        for row in rows:
            if abs(cy - row["cy"]) <= tolerance * min(h, row["h"]):
                row["items"].append((bbox, text, confidence))
                row["cy"] = (row["cy"] * row["h"] + cy * h) / (row["h"] + h)
                row["h"] = max(row["h"], h)
                break
        else:
            rows.append({"cy": cy, "h": h, "items": [(bbox, text, confidence)]})
    return rows


def _best_row_text(results):
    """Combine same-row OCR boxes and return the row text that most looks
    like a plate number, preferring higher average confidence."""
    best = None
    for row in _group_into_rows(results):
        items = sorted(row["items"], key=lambda item: min(point[0] for point in item[0]))
        combined = _clean_text("".join(text for _, text, _ in items))
        if not _looks_like_plate(combined):
            continue
        avg_confidence = sum(confidence for _, _, confidence in items) / len(items)
        if best is None or avg_confidence > best[0]:
            best = (avg_confidence, combined)
    return best[1] if best else None


def detect_plate(image_path):
    """Locate the license plate region in an image on disk.

    Returns a cropped BGR numpy array around the most plate-like text
    EasyOCR finds, or None if nothing plate-like was detected.
    """
    image = cv2.imread(image_path)
    if image is None:
        return None

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    reader = _get_reader()
    results = reader.readtext(rgb, allowlist=OCR_WHITELIST)

    best = _best_candidate(results)
    if best is None:
        return None

    _, bbox, _ = best
    xs = [point[0] for point in bbox]
    ys = [point[1] for point in bbox]
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


def read_plate(cropped_plate):
    """Run OCR on a cropped plate image and return the cleaned plate text.

    Returns None if OCR produced nothing that looks like a real plate.
    """
    if cropped_plate is None:
        return None

    rgb = cv2.cvtColor(cropped_plate, cv2.COLOR_BGR2RGB)
    reader = _get_reader()
    results = reader.readtext(rgb, allowlist=OCR_WHITELIST)

    # A tight crop from detect_plate can still contain other plate text
    # (state name, "CENTRE OF EXCELLENCE" slogan, "FEDERAL REPUBLIC OF
    # NIGERIA") stacked above/below the plate number. If a single detected
    # box already reads as a complete plate on its own, trust it rather
    # than merging in neighboring boxes - those neighbors are usually that
    # other plate text, not part of the number. Only fall back to merging
    # same-row boxes when no single box stands on its own, which happens
    # when the plate number itself got split (e.g. the dash-separated
    # halves land as separate boxes).
    best = _best_candidate(results)
    if best:
        return best[2]

    return _best_row_text(results)


def run_anpr(image_path):
    """Detect and read the plate in one call. Returns the plate string or None."""
    cropped = detect_plate(image_path)
    if cropped is None:
        return None

    return read_plate(cropped)


if __name__ == "__main__":
    import sys

    import requests

    import gate_client

    default_image = "test_images/LND-113JN.jpeg"
    image_path = sys.argv[1] if len(sys.argv) > 1 else default_image

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
        sys.exit(0)

    print(f"STAFF CAR: {data['owner_name']}")

    esp32_response = gate_client.notify_esp32(data, event_type="entry")
    if esp32_response is not None:
        # The ESP32 is configured and reachable - it owns verification
        # (fingerprint sensor + keypad) from here.
        print(f"ESP32 notified: {esp32_response}")
        sys.exit(0)

    # No ESP32 configured/reachable - fall back to simulating verification
    # from the keyboard, same as before hardware was wired up.
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
