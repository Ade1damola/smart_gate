"""
ANPR engine: detects and reads a license plate from an image.

Pure image-processing module - no printing, no display, no notebook-only
calls. Callers (the Flask backend, this file's own __main__ block, or
eventually the ESP32-side integration script) decide what to do with the
returned values.
"""

import os
import subprocess
import tempfile

import cv2

# Characters Nigerian plates actually carry: letters, digits, and the dash
# printed between the state code and the serial (e.g. "LND-113JN").
OCR_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"

# OCR results shorter than this are almost always noise picked up from a
# non-plate crop, not a misread plate - treat them as "nothing detected"
# instead of passing garbage on to /check_plate.
MIN_PLATE_LENGTH = 5

# Calls the Tesseract-OCR binary directly via subprocess instead of going
# through the pytesseract package, whose import of pandas gets blocked by
# Windows Smart App Control on this machine. Hardcoded because it isn't
# reliably resolved off PATH across every shell this gets run from.
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Plain contour/edge geometry turned out not to generalize across photos - a
# real plate (text carved into it) is often less "rectangular" by area than
# a screw, grille slat, or reflection, so shape-only heuristics kept picking
# decoys. OpenCV's pretrained cascade (trained on real plate images) is far
# more reliable at telling plate regions apart from those look-alikes.
_CASCADE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "haarcascade_russian_plate_number.xml")
_plate_cascade = cv2.CascadeClassifier(_CASCADE_PATH)

# Working width the cascade runs at - keeps minSize/minNeighbors behaving
# consistently regardless of the source photo's native resolution.
DETECTION_WIDTH = 1000

# Plates are a light/white background - a confident but near-black hit is
# almost always the cascade latching onto a dark grille or shadowed trim.
MIN_BRIGHTNESS = 50


def detect_plate(image_path):
    """Locate and crop the license plate region from an image on disk.

    Returns a grayscale numpy array of the cropped plate, or None if no
    plate-like region could be found.
    """
    image = cv2.imread(image_path)
    if image is None:
        return None

    img_w = image.shape[1]
    scale = DETECTION_WIDTH / img_w if img_w > DETECTION_WIDTH else 1.0
    working = cv2.resize(image, None, fx=scale, fy=scale) if scale != 1.0 else image
    working_gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)

    rects, _, weights = _plate_cascade.detectMultiScale3(
        working_gray, scaleFactor=1.03, minNeighbors=2, minSize=(30, 10), outputRejectLevels=True,
    )

    best = None
    for (x, y, w, h), weight in zip(rects, weights):
        roi = working_gray[y:y + h, x:x + w]
        if roi.mean() < MIN_BRIGHTNESS:
            continue
        if best is None or weight > best[0]:
            best = (weight, x, y, w, h)

    if best is None:
        return None

    _, x, y, w, h = best
    x0, y0 = int(x / scale), int(y / scale)
    x1, y1 = int((x + w) / scale), int((y + h) / scale)

    full_gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cropped = full_gray[y0:y1, x0:x1]
    return cropped if cropped.size else None


def _ocr_text(image):
    """Run tesseract on an image array (single text line) and return raw stdout."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        cv2.imwrite(tmp_path, image)
        result = subprocess.run(
            [TESSERACT_CMD, tmp_path, "stdout", "--psm", "7",
             "-c", f"tessedit_char_whitelist={OCR_WHITELIST}"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout
    finally:
        os.remove(tmp_path)


def _clean_text(text):
    return "".join(ch for ch in text if ch.isalnum() or ch == "-").upper().strip("-")


def _isolate_number_line(gray_crop):
    """Crop down to just the big plate-number characters.

    detect_plate's box isn't consistently margined (the cascade's window
    tightness varies per photo), so a fixed header/footer fraction doesn't
    hold across images. Instead, threshold and find connected components
    that are roughly full-crop-height - the small state-name header and
    federal-republic footer text don't qualify, only the large number line
    does - and crop to their combined bounding box.
    """
    crop_h, crop_w = gray_crop.shape[:2]
    blurred = cv2.GaussianBlur(gray_crop, (3, 3), 0)

    for thresh_flag in (cv2.THRESH_BINARY_INV, cv2.THRESH_BINARY):
        _, thresh = cv2.threshold(blurred, 0, 255, thresh_flag + cv2.THRESH_OTSU)
        num_labels, _, stats, _ = cv2.connectedComponentsWithStats(thresh, connectivity=8)

        boxes = [
            (x, y, w, h) for x, y, w, h, area in (stats[i] for i in range(1, num_labels))
            if 0.28 * crop_h <= h <= 0.95 * crop_h and w < h * 1.5 and area > 20
        ]
        if len(boxes) < 2:
            continue

        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes)
        y1 = max(b[1] + b[3] for b in boxes)

        pad_x, pad_y = int((x1 - x0) * 0.03), int((y1 - y0) * 0.08)
        x0, y0 = max(x0 - pad_x, 0), max(y0 - pad_y, 0)
        x1, y1 = min(x1 + pad_x, crop_w), min(y1 + pad_y, crop_h)
        return gray_crop[y0:y1, x0:x1]

    return gray_crop


def read_plate(cropped_plate):
    """Run OCR on a cropped plate image and return the cleaned plate text.

    Returns None if OCR produced nothing that looks like a real plate.
    """
    if cropped_plate is None:
        return None

    band = _isolate_number_line(cropped_plate)
    if band.size == 0:
        band = cropped_plate

    # Upscale only if the line is small - shrinking already-large text
    # thins out strokes and breaks OCR just as badly as leaving it tiny.
    min_height = 120
    if band.shape[0] < min_height:
        scale = min_height / band.shape[0]
        band = cv2.resize(band, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    blurred = cv2.GaussianBlur(band, (3, 3), 0)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))

    # Plates vary between dark text on a light background and the reverse,
    # and lighting isn't controlled - try both polarities and keep whichever
    # OCR pass produced the longer (more complete) read.
    candidates = []
    for thresh_flag in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        _, thresh = cv2.threshold(blurred, 0, 255, thresh_flag + cv2.THRESH_OTSU)
        closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

        # Tesseract needs whitespace margin around a tightly-cropped line -
        # without it, single-line mode reliably returns nothing at all.
        border_fill = 255 if thresh_flag == cv2.THRESH_BINARY else 0
        padded = cv2.copyMakeBorder(closed, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=border_fill)

        text = _clean_text(_ocr_text(padded))
        if text:
            candidates.append(text)

    if not candidates:
        return None

    best = max(candidates, key=len)
    return best if len(best) >= MIN_PLATE_LENGTH else None


def run_anpr(image_path):
    """Detect and read the plate in one call. Returns the plate string or None."""
    cropped = detect_plate(image_path)
    if cropped is None:
        return None

    return read_plate(cropped)


if __name__ == "__main__":
    import sys

    import gate_client

    default_image = "test_images/LND-113JN.jpeg"
    image_path = sys.argv[1] if len(sys.argv) > 1 else default_image

    plate = run_anpr(image_path)

    if plate is None:
        print("No plate detected.")
        sys.exit(0)

    print(f"Plate detected: {plate}")
    data = gate_client.request_check_plate(plate)
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
