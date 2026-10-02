"""Flask backend for the Smart Gate staff app.

Storage is SQLAlchemy-backed: SQLite locally by default, Postgres in
production (set DATABASE_URL). This lets the same codebase run on a laptop
for development and on a hosted platform (Render) for the real ESP32/Pi
gate hardware.
"""

import base64
import binascii
import json
import os
import random
import re
import secrets
import string
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

from models import (
    db, Staff, Admin, Vehicle, FingerprintTemplate, Otp, PasswordResetOtp, Log, GateEvent,
    USER_CATEGORIES,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_DATABASE_URL = "sqlite:///" + os.path.join(DATA_DIR, "smart_gate.db")
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
# Render (and most Postgres hosts) hand out "postgres://" / "postgresql://"
# URLs; pin the psycopg (v3) driver explicitly so the choice doesn't depend on
# SQLAlchemy's default dialect driver, which changed between versions.
for prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(prefix):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len(prefix):]
        break

# Shared secret the ESP32 and Raspberry Pi must send as the X-Device-Key
# header on device-facing routes. Left unset locally so laptop testing
# doesn't require configuring a key; must be set once this is hosted publicly.
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY", "")

# Termii credentials for real SMS delivery. When unset (local dev), the
# reset code is printed to the console instead of actually being texted.
TERMII_API_KEY = os.environ.get("TERMII_API_KEY", "")
TERMII_SENDER_ID = os.environ.get("TERMII_SENDER_ID", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")

# Default password assigned to every newly-added staff member. They're
# expected to use the "forgot password" OTP flow to set their own password.
# Real value set via the DEFAULT_STAFF_PASSWORD env var on the server; this
# fallback is only for local dev and is not a real credential.
DEFAULT_STAFF_PASSWORD = os.environ.get("DEFAULT_STAFF_PASSWORD", "local-dev-only")

# Default password for the seeded admin account. Admins have no phone number
# and no forgot-password flow of their own, so this only changes if someone
# updates it directly in the database. Real value set via the
# DEFAULT_ADMIN_PASSWORD env var on the server; this fallback is only for
# local dev and is not a real credential.
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "local-dev-only")

# How long a password-reset OTP stays valid.
RESET_OTP_VALID_MINUTES = 10

# Detection snapshots are kept for only the most recent N detections so the
# database (Render's free Postgres is 1 GB) doesn't fill up with images; the
# event rows themselves are kept.
SNAPSHOT_RETENTION = int(os.environ.get("SNAPSHOT_RETENTION", "300"))

# A device counts as online if it has called in within this many seconds.
DEVICE_ONLINE_SECONDS = 90

MAX_PHOTO_BYTES = 3 * 1024 * 1024
MAX_FRAME_BYTES = 2 * 1024 * 1024
PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

# In-memory session store: token -> {"role": "staff"/"admin", "id": ...}.
# Fine for a small staff app; a Render restart just means everyone logs in
# again.
sessions = {}

# In-memory live state for the admin monitor. Like `sessions`, this assumes
# the app runs as a single process (one gunicorn worker); it's only live
# telemetry, so losing it on a restart is harmless.
live_lock = threading.Lock()
latest_camera_frame = {"data": None, "type": "image/jpeg", "time": 0.0}
device_last_seen = {}


# ---------------------------------------------------------------------------
# Seed data (first run only)
# ---------------------------------------------------------------------------

def ensure_default_admin():
    """Create the GateAdmin account if it doesn't exist yet.

    Runs on every startup (not just on a fresh DB) so it also gets created
    for a database that already had staff/vehicle data before the admin
    account existed as a separate entity.
    """
    if db.session.get(Admin, "GateAdmin") is not None:
        return
    db.session.add(Admin(
        admin_id="GateAdmin",
        name="Gate Admin",
        password_hash=generate_password_hash(DEFAULT_ADMIN_PASSWORD),
    ))
    db.session.commit()


def seed_data():
    """Populate sample staff/admin/vehicles/logs the first time the DB is empty."""
    ensure_default_admin()

    if Staff.query.first() is not None:
        return

    db.session.add_all([
        Staff(
            staff_id="STAFF001",
            name="Adaeze Okafor",
            password_hash=generate_password_hash("password123"),
            fingerprint_template_id="FP1001",
            plate_number="LND-113JN",
            phone_number="+2348011112222",
            category="staff",
            department="Computer Science",
        ),
        Staff(
            staff_id="STAFF002",
            name="Tunde Bakare",
            password_hash=generate_password_hash("password456"),
            fingerprint_template_id="FP1002",
            plate_number="BDG-889HS",
            phone_number="+2348033334444",
            category="resident",
            department="Staff Quarters, Block C",
        ),
    ])
    db.session.add_all([
        Vehicle(vehicle_id="VEH001", staff_id="STAFF001", plate_number="LND-113JN"),
        Vehicle(vehicle_id="VEH002", staff_id="STAFF002", plate_number="BDG-889HS"),
    ])

    now = now_wat()
    db.session.add_all([
        Log(
            staff_id="STAFF001",
            plate_number="LAG123XY",
            method="fingerprint",
            event_type="entry",
            timestamp=(now - timedelta(days=1, hours=3)).isoformat(timespec="seconds"),
            status="success",
        ),
        Log(
            staff_id="STAFF001",
            plate_number="LAG123XY",
            method="otp",
            event_type="exit",
            timestamp=(now - timedelta(days=1, hours=1)).isoformat(timespec="seconds"),
            status="success",
        ),
        Log(
            staff_id="STAFF001",
            plate_number="LAG123XY",
            method="otp",
            event_type="entry",
            timestamp=(now - timedelta(hours=6)).isoformat(timespec="seconds"),
            status="fail",
        ),
    ])
    db.session.commit()


def ensure_schema():
    """Add columns introduced after the first database was created.

    db.create_all() creates missing tables but never alters existing ones,
    so new columns on existing tables are added here by hand.
    """
    blob = "BYTEA" if db.engine.dialect.name == "postgresql" else "BLOB"
    new_columns = {
        "staff": [
            ("email", "VARCHAR(255)"),
            ("category", "VARCHAR(32)"),
            ("department", "VARCHAR(160)"),
            ("passport_photo", blob),
            ("passport_photo_type", "VARCHAR(32)"),
            ("created_time", "VARCHAR(32)"),
        ],
        "vehicles": [
            ("make", "VARCHAR(64)"),
            ("model", "VARCHAR(64)"),
            ("colour", "VARCHAR(32)"),
            ("features", "VARCHAR(255)"),
            ("photo", blob),
            ("photo_type", "VARCHAR(32)"),
        ],
    }
    inspector = db.inspect(db.engine)
    for table, columns in new_columns.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, column_type in columns:
            if name not in existing:
                with db.engine.begin() as connection:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")


def ensure_staff_email():
    """Attach the configured email to STAFF003 without creating fake details."""
    staff = db.session.get(Staff, "STAFF003")
    if staff and not staff.email:
        staff.email = "adenugaade18@gmail.com"
        db.session.commit()


def parse_time(value):
    return datetime.fromisoformat(value)


# Nigeria (WAT) is a fixed UTC+1 offset with no daylight saving, so we
# compute it from UTC rather than trusting the server's local clock -
# Render's containers run in UTC, which made timestamps and OTP expiry
# read an hour behind actual Nigerian time.
def now_wat():
    return datetime.utcnow() + timedelta(hours=1)


def now_iso():
    return now_wat().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_staff(staff_id):
    staff = db.session.get(Staff, staff_id) if staff_id else None
    return staff.to_dict() if staff else None


def fingerprint_ids(template_field):
    """A staff member can have more than one enrolled fingerprint - they're
    stored as a comma-separated list in the same column."""
    return [template_id for template_id in (template_field or "").split(",") if template_id]


def normalize_plate(plate):
    """Compare plates on letters/digits only, so an OCR read of "LND113JN"
    still matches a plate registered as "LND-113JN"."""
    return re.sub(r"[^A-Z0-9]", "", (plate or "").upper())


def find_vehicle_by_plate(plate, exclude_staff_id=None):
    plate = normalize_plate(plate)
    if not plate:
        return None
    for vehicle in Vehicle.query.all():
        if vehicle.staff_id != exclude_staff_id and normalize_plate(vehicle.plate_number) == plate:
            return {"vehicle_id": vehicle.vehicle_id, "staff_id": vehicle.staff_id, "plate_number": vehicle.plate_number}
    return None


def next_vehicle_id():
    number = Vehicle.query.count() + 1
    while db.session.get(Vehicle, "VEH%03d" % number) is not None:
        number += 1
    return "VEH%03d" % number


def log_event(staff_id, plate_number, method, event_type, status, details=""):
    db.session.add(Log(
        staff_id=staff_id,
        plate_number=plate_number,
        method=method,
        event_type=event_type,
        timestamp=now_iso(),
        status=status,
        details=details,
    ))
    db.session.commit()


def add_gate_event(kind, plate_number="", staff_id=None, event_type="", status="", message="",
                   snapshot=None, snapshot_type=None):
    event = GateEvent(
        timestamp=now_iso(),
        kind=kind,
        plate_number=plate_number or "",
        staff_id=staff_id or None,
        event_type=event_type or "",
        status=status or "",
        message=(message or "")[:255],
        snapshot=snapshot,
        snapshot_type=snapshot_type if snapshot else None,
    )
    db.session.add(event)
    db.session.commit()
    if snapshot:
        prune_snapshots()
    return event


def prune_snapshots():
    stale = (
        GateEvent.query
        .filter(GateEvent.snapshot_type.isnot(None))
        .order_by(GateEvent.id.desc())
        .offset(SNAPSHOT_RETENTION)
        .all()
    )
    for event in stale:
        event.snapshot = None
        event.snapshot_type = None
    if stale:
        db.session.commit()


def normalize_event_type(value, default):
    value = (value or default).strip().lower()
    return value if value in ("entry", "exit") else default


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def sniff_image_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_uploaded_image(field, label):
    """Returns (bytes, mimetype), (None, None) when no file was sent, or
    raises ValueError with a user-facing message."""
    uploaded = request.files.get(field)
    if uploaded is None or not uploaded.filename:
        return None, None
    data = uploaded.read()
    if not data:
        return None, None
    if len(data) > MAX_PHOTO_BYTES:
        raise ValueError(f"{label} is too large (max 3 MB).")
    image_type = sniff_image_type(data)
    if image_type is None:
        raise ValueError(f"{label} must be a JPG, PNG or WEBP image.")
    return data, image_type


def image_response(data, image_type):
    response = Response(data, mimetype=image_type or "image/jpeg")
    response.headers["Cache-Control"] = "private, max-age=60"
    return response


# ---------------------------------------------------------------------------
# SMS delivery (password-reset OTPs)
# ---------------------------------------------------------------------------

def send_reset_sms(phone_number, code):
    message = (
        "Your Smart Gate password reset code is {code}. "
        "It expires in {minutes} minutes.".format(code=code, minutes=RESET_OTP_VALID_MINUTES)
    )

    if not TERMII_API_KEY:
        # No SMS gateway configured (local dev) - simulate delivery.
        print("[SIMULATED SMS to {phone}] {message}".format(phone=phone_number, message=message))
        return

    try:
        requests.post(
            "https://api.ns.termii.com/api/sms/send",
            json={
                "to": phone_number,
                "from": TERMII_SENDER_ID,
                "sms": message,
                "type": "plain",
                "channel": "generic",
                "api_key": TERMII_API_KEY,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        # Don't fail the whole request just because the SMS gateway is down -
        # the OTP is still valid and recoverable (e.g. staff calls the admin).
        print("[SMS ERROR] Could not send reset code to {phone}: {exc}".format(phone=phone_number, exc=exc))


def send_otp_email(staff, code, expiry):
    """Send a gate OTP through the Resend HTTP API when email delivery is configured."""
    email = (staff.get("email") or "").strip()
    if not email:
        return False, "No email address is saved for this staff member."
    if not RESEND_API_KEY:
        print(f"[SIMULATED EMAIL to {email}] OTP {code} expires {expiry}")
        return False, "Resend is not configured; OTP was generated locally."

    expiry_text = expiry.strftime("%I:%M %p, %d %B %Y")
    text_body = (
        f"Hi {staff['name']},\n\n"
        f"You've successfully generated a One-Time Password for your vehicle ({staff['plate_number']}).\n\n"
        f"OTP Code: {code}\n\n"
        f"Valid until: {expiry_text}\n\n"
        "Share this code with the person driving your car. At the gate they will first scan their fingerprint, "
        "then enter this code on the keypad to gain access. "
        "This code is single-use and will expire automatically after the time limit you selected.\n\n"
        "Didn't request this? If you did not generate this OTP, please log in to your Verigate dashboard immediately and revoke it, or contact the security office.\n\n"
        "Verigate Smart Gate Access System"
    )
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [email],
                "subject": "Your Verigate OTP Has Been Generated",
                "text": text_body,
            },
            timeout=15,
        )
        if response.status_code >= 400:
            app.logger.warning("Resend email failed: %s %s", response.status_code, response.text)
            return False, "OTP was generated, but Resend could not send the email. Check the Render logs."
        return True, "OTP emailed successfully."
    except requests.RequestException as exc:
        app.logger.warning("Resend email failed: %s", exc)
        return False, "OTP was generated, but Resend could not send the email. Check the Render logs."


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_token_from_request():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return request.args.get("token")


def unauthorized_response():
    return jsonify({"success": False, "message": "Unauthorized. Please log in again."}), 401


def require_auth():
    """Returns (staff, error_response). error_response is None when a staff
    member (not an admin) is authorised."""
    token = get_token_from_request()
    session = sessions.get(token) if token else None
    if not session or session["role"] != "staff":
        return None, unauthorized_response()
    staff = find_staff(session["id"])
    if not staff:
        return None, unauthorized_response()
    return staff, None


def require_admin():
    """Returns (admin, error_response). error_response is None when an admin
    is authorised. Admins are a separate account type from staff - not a
    flag on a staff record."""
    token = get_token_from_request()
    session = sessions.get(token) if token else None
    if not session or session["role"] != "admin":
        return None, (jsonify({"success": False, "message": "Admin access required."}), 403)
    admin = db.session.get(Admin, session["id"])
    if not admin:
        return None, (jsonify({"success": False, "message": "Admin access required."}), 403)
    return admin.to_dict(), None


def require_device():
    """Gate/keypad/ANPR endpoints called by the ESP32 or Raspberry Pi, not a
    logged-in staff member. Returns error_response, or None when authorised.

    DEVICE_API_KEY is left unset for local development so testing from a
    laptop doesn't require configuring a key; it must be set once this is
    reachable from the public internet.

    Devices identify themselves with an X-Device-Name header ("gate" for the
    ESP32, "camera" for the ANPR box); every authorised call counts as a
    heartbeat for the admin monitor's device status.
    """
    if DEVICE_API_KEY:
        provided = request.headers.get("X-Device-Key", "")
        if provided != DEVICE_API_KEY:
            return jsonify({"success": False, "message": "Invalid or missing device key."}), 401
    device_name = (request.headers.get("X-Device-Name") or "").strip().lower()
    if device_name in ("gate", "camera"):
        with live_lock:
            device_last_seen[device_name] = time.time()
    return None


def device_status():
    now = time.time()
    with live_lock:
        seen = dict(device_last_seen)
        frame_time = latest_camera_frame["time"]
    status = {}
    for name in ("gate", "camera"):
        last = seen.get(name)
        status[name] = {
            "online": bool(last) and now - last < DEVICE_ONLINE_SECONDS,
            "seconds_ago": int(now - last) if last else None,
        }
    status["camera"]["frame_seconds_ago"] = int(now - frame_time) if frame_time else None
    return status


def start_session(role, account_id):
    token = secrets.token_hex(16)
    sessions[token] = {"role": role, "id": account_id}
    return token


# ---------------------------------------------------------------------------
# Route 1: logins. Staff and admins have entirely separate login pages and
# endpoints - an admin ID can't sign in through the staff login and vice
# versa.
# ---------------------------------------------------------------------------

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    login_id = (data.get("login_id") or "").strip()
    password = data.get("password") or ""

    staff = find_staff(login_id)
    if staff and check_password_hash(staff["password_hash"], password):
        return jsonify({
            "success": True,
            "token": start_session("staff", staff["staff_id"]),
            "role": "staff",
            "staff_id": staff["staff_id"],
            "name": staff["name"],
        })

    return jsonify({"success": False, "message": "Invalid ID or password"}), 401


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    admin_id = (data.get("admin_id") or "").strip()
    password = data.get("password") or ""

    admin = db.session.get(Admin, admin_id) if admin_id else None
    if admin and check_password_hash(admin.password_hash, password):
        return jsonify({
            "success": True,
            "token": start_session("admin", admin.admin_id),
            "admin_id": admin.admin_id,
            "name": admin.name,
        })

    return jsonify({"success": False, "message": "Invalid admin ID or password"}), 401


@app.route("/api/logout", methods=["POST"])
def logout_route():
    token = get_token_from_request()
    if token:
        sessions.pop(token, None)
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Route 1a: forgot password - request a reset OTP sent to the staff member's
# registered phone number. No login required (that's the whole point).
# ---------------------------------------------------------------------------

GENERIC_RESET_REQUEST_MESSAGE = "If that staff ID exists, a reset code has been sent to the registered phone number."


@app.route("/api/request_password_reset", methods=["POST"])
def request_password_reset():
    data = request.get_json(silent=True) or {}
    staff_id = (data.get("staff_id") or "").strip()

    staff_row = db.session.get(Staff, staff_id) if staff_id else None
    if staff_row:
        created = now_wat()
        expiry = created + timedelta(minutes=RESET_OTP_VALID_MINUTES)
        code = "".join(random.choices(string.digits, k=6))

        db.session.add(PasswordResetOtp(
            otp_code=code,
            staff_id=staff_row.staff_id,
            created_time=created.isoformat(timespec="seconds"),
            expiry_time=expiry.isoformat(timespec="seconds"),
            used=False,
        ))
        db.session.commit()

        send_reset_sms(staff_row.phone_number, code)

    # Always return the same generic response, whether or not staff_id
    # matched, so this endpoint can't be used to find out which staff IDs
    # are real.
    return jsonify({"success": True, "message": GENERIC_RESET_REQUEST_MESSAGE})


# ---------------------------------------------------------------------------
# Route 1b: forgot password - complete the reset with the OTP just received.
# ---------------------------------------------------------------------------

@app.route("/api/reset_password", methods=["POST"])
def reset_password():
    data = request.get_json(silent=True) or {}
    staff_id = (data.get("staff_id") or "").strip()
    code = (data.get("otp_code") or "").strip()
    new_password = data.get("new_password") or ""

    if not staff_id or not code or not new_password:
        return jsonify({"success": False, "message": "staff_id, otp_code and new_password are required"}), 400

    if len(new_password) < 6:
        return jsonify({"success": False, "message": "new_password must be at least 6 characters"}), 400

    staff_row = db.session.get(Staff, staff_id)
    if not staff_row:
        return jsonify({"success": False, "message": "Invalid or expired reset code"}), 400

    now = now_wat()
    matched = (
        PasswordResetOtp.query
        .filter_by(otp_code=code, staff_id=staff_id)
        .order_by(PasswordResetOtp.id.desc())
        .first()
    )

    if (
        matched is None
        or matched.used
        or parse_time(matched.expiry_time) <= now
    ):
        return jsonify({"success": False, "message": "Invalid or expired reset code"}), 400

    matched.used = True
    staff_row.password_hash = generate_password_hash(new_password)
    db.session.commit()

    return jsonify({"success": True, "message": "Password reset successfully. You can now log in."})


# ---------------------------------------------------------------------------
# Route 1c: admin - registered users (staff, residents, shop owners...)
# ---------------------------------------------------------------------------

def primary_vehicle(staff_id):
    return Vehicle.query.filter_by(staff_id=staff_id).order_by(Vehicle.vehicle_id).first()


def user_profile(staff_row):
    data = staff_row.to_admin_dict()
    data["vehicles"] = [
        vehicle.to_admin_dict()
        for vehicle in Vehicle.query.filter_by(staff_id=staff_row.staff_id).order_by(Vehicle.vehicle_id)
    ]
    data["fingerprints"] = [
        template.to_admin_dict()
        for template in FingerprintTemplate.query.filter_by(staff_id=staff_row.staff_id).order_by(FingerprintTemplate.id)
    ]
    data["legacy_fingerprint_ids"] = fingerprint_ids(staff_row.fingerprint_template_id)
    return data


def decode_template(encoded):
    try:
        template = base64.b64decode(encoded or "", validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Fingerprint template is not valid base64.")
    if not 256 <= len(template) <= 4096:
        raise ValueError("Fingerprint template has an unexpected size.")
    return template


def read_user_form(creating):
    """Validate the multipart registration/edit form.

    Returns a dict of cleaned fields, or raises ValueError with a message.
    """
    form = request.form
    fields = {
        "staff_id": (form.get("staff_id") or "").strip(),
        "name": (form.get("name") or "").strip(),
        "category": (form.get("category") or "").strip().lower(),
        "department": (form.get("department") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "phone_number": (form.get("phone_number") or "").strip(),
        "plate_number": (form.get("plate_number") or "").strip().upper(),
        "vehicle_make": (form.get("vehicle_make") or "").strip(),
        "vehicle_model": (form.get("vehicle_model") or "").strip(),
        "vehicle_colour": (form.get("vehicle_colour") or "").strip(),
        "vehicle_features": (form.get("vehicle_features") or "").strip(),
    }

    required = {
        "staff_id": "User ID number",
        "name": "Full name",
        "category": "Category",
        "department": "Department / campus address",
        "email": "Email",
        "phone_number": "Phone number",
        "plate_number": "Plate number",
        "vehicle_make": "Vehicle brand",
        "vehicle_colour": "Vehicle colour",
    }
    if not creating:
        required.pop("staff_id")
    missing = [label for key, label in required.items() if not fields[key]]
    if missing:
        raise ValueError("Missing: " + ", ".join(missing))
    if fields["category"] not in USER_CATEGORIES:
        raise ValueError("Choose a valid category.")
    if not EMAIL_PATTERN.match(fields["email"]):
        raise ValueError("Enter a valid email address.")
    if not PHONE_PATTERN.match(fields["phone_number"]):
        raise ValueError("Enter a valid phone number (digits only, optional leading +).")
    if len(normalize_plate(fields["plate_number"])) < 4:
        raise ValueError("Enter a valid plate number.")

    fields["passport_photo"], fields["passport_photo_type"] = read_uploaded_image("passport_photo", "Passport photo")
    fields["vehicle_photo"], fields["vehicle_photo_type"] = read_uploaded_image("vehicle_photo", "Vehicle photo")
    if creating and fields["passport_photo"] is None:
        raise ValueError("A passport photograph is required.")
    if creating and fields["vehicle_photo"] is None:
        raise ValueError("A vehicle photo showing the plate is required.")

    try:
        fingerprints = json.loads(form.get("fingerprints") or "[]")
    except ValueError:
        raise ValueError("Fingerprint data is malformed.")
    fields["fingerprints"] = [
        ((item.get("label") or "").strip()[:64], decode_template(item.get("template")))
        for item in fingerprints
    ]
    return fields


def apply_user_fields(staff_row, fields):
    staff_row.name = fields["name"]
    staff_row.category = fields["category"]
    staff_row.department = fields["department"]
    staff_row.email = fields["email"]
    staff_row.phone_number = fields["phone_number"]
    staff_row.plate_number = fields["plate_number"]
    if fields["passport_photo"] is not None:
        staff_row.passport_photo = fields["passport_photo"]
        staff_row.passport_photo_type = fields["passport_photo_type"]

    vehicle = primary_vehicle(staff_row.staff_id)
    if vehicle is None:
        vehicle = Vehicle(vehicle_id=next_vehicle_id(), staff_id=staff_row.staff_id)
        db.session.add(vehicle)
    vehicle.plate_number = fields["plate_number"]
    vehicle.make = fields["vehicle_make"]
    vehicle.model = fields["vehicle_model"]
    vehicle.colour = fields["vehicle_colour"]
    vehicle.features = fields["vehicle_features"]
    if fields["vehicle_photo"] is not None:
        vehicle.photo = fields["vehicle_photo"]
        vehicle.photo_type = fields["vehicle_photo_type"]

    for label, template in fields["fingerprints"]:
        db.session.add(FingerprintTemplate(
            staff_id=staff_row.staff_id, label=label, template=template, created_time=now_iso(),
        ))


@app.route("/api/admin/users", methods=["GET"])
def admin_list_users():
    admin, error = require_admin()
    if error:
        return error

    query = (request.args.get("q") or "").strip().lower()
    vehicles_by_owner = {}
    for vehicle in Vehicle.query.order_by(Vehicle.vehicle_id).all():
        vehicles_by_owner.setdefault(vehicle.staff_id, []).append(vehicle.to_admin_dict())
    fingerprint_counts = {}
    for (staff_id,) in db.session.query(FingerprintTemplate.staff_id).all():
        fingerprint_counts[staff_id] = fingerprint_counts.get(staff_id, 0) + 1

    users = []
    for staff_row in Staff.query.order_by(Staff.name).all():
        data = staff_row.to_admin_dict()
        data["vehicles"] = vehicles_by_owner.get(staff_row.staff_id, [])
        data["fingerprint_count"] = (
            fingerprint_counts.get(staff_row.staff_id, 0)
            + len(fingerprint_ids(staff_row.fingerprint_template_id))
        )
        if query:
            haystack = " ".join([
                data["staff_id"], data["name"], data["department"], data["email"], data["phone_number"],
                " ".join(v["plate_number"] + " " + v["make"] + " " + v["colour"] for v in data["vehicles"]),
            ]).lower()
            if query not in haystack and normalize_plate(query) not in normalize_plate(haystack):
                continue
        users.append(data)

    # A list rather than a dict: jsonify sorts dict keys, which would lose
    # the display order (e.g. "Other" last).
    categories = [{"key": key, "label": label} for key, label in USER_CATEGORIES.items()]
    return jsonify({"success": True, "users": users, "categories": categories})


@app.route("/api/admin/users", methods=["POST"])
def admin_create_user():
    admin, error = require_admin()
    if error:
        return error

    try:
        fields = read_user_form(creating=True)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    if db.session.get(Staff, fields["staff_id"]) is not None:
        return jsonify({"success": False, "message": "A user with that ID number already exists"}), 409
    if find_vehicle_by_plate(fields["plate_number"]):
        return jsonify({"success": False, "message": "That plate number is already registered to another user"}), 409

    staff_row = Staff(
        staff_id=fields["staff_id"],
        password_hash=generate_password_hash(DEFAULT_STAFF_PASSWORD),
        fingerprint_template_id="",
        created_time=now_iso(),
    )
    with db.session.no_autoflush:
        apply_user_fields(staff_row, fields)
    db.session.add(staff_row)
    db.session.commit()

    return jsonify({
        "success": True,
        "message": "User registered",
        "staff_id": staff_row.staff_id,
        "default_password": DEFAULT_STAFF_PASSWORD,
        "user": user_profile(staff_row),
    })


@app.route("/api/admin/users/<staff_id>", methods=["GET"])
def admin_get_user(staff_id):
    admin, error = require_admin()
    if error:
        return error

    staff_row = db.session.get(Staff, staff_id)
    if not staff_row:
        return jsonify({"success": False, "message": "User not found"}), 404

    logs = [
        log.to_dict()
        for log in Log.query.filter_by(staff_id=staff_id).order_by(Log.timestamp.desc()).limit(20)
    ]
    return jsonify({"success": True, "user": user_profile(staff_row), "recent_activity": logs})


@app.route("/api/admin/users/<staff_id>", methods=["POST"])
def admin_update_user(staff_id):
    admin, error = require_admin()
    if error:
        return error

    staff_row = db.session.get(Staff, staff_id)
    if not staff_row:
        return jsonify({"success": False, "message": "User not found"}), 404

    try:
        fields = read_user_form(creating=False)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    if find_vehicle_by_plate(fields["plate_number"], exclude_staff_id=staff_id):
        return jsonify({"success": False, "message": "That plate number is already registered to another user"}), 409

    apply_user_fields(staff_row, fields)
    db.session.commit()
    return jsonify({"success": True, "message": "User updated", "user": user_profile(staff_row)})


@app.route("/api/admin/users/<staff_id>", methods=["DELETE"])
def admin_delete_user(staff_id):
    admin, error = require_admin()
    if error:
        return error

    staff_row = db.session.get(Staff, staff_id)
    if not staff_row:
        return jsonify({"success": False, "message": "User not found"}), 404

    # Access logs are kept as an audit trail; everything that grants access
    # (vehicles, fingerprints, OTPs) goes with the user.
    for model in (Vehicle, FingerprintTemplate, Otp, PasswordResetOtp):
        model.query.filter_by(staff_id=staff_id).delete()
    db.session.delete(staff_row)
    db.session.commit()
    for token, session in list(sessions.items()):
        if session["role"] == "staff" and session["id"] == staff_id:
            sessions.pop(token, None)
    return jsonify({"success": True, "message": "User removed"})


@app.route("/api/admin/users/<staff_id>/fingerprints", methods=["POST"])
def admin_add_fingerprint(staff_id):
    admin, error = require_admin()
    if error:
        return error

    if db.session.get(Staff, staff_id) is None:
        return jsonify({"success": False, "message": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    try:
        template = decode_template(data.get("template"))
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    row = FingerprintTemplate(
        staff_id=staff_id,
        label=(data.get("label") or "").strip()[:64],
        template=template,
        created_time=now_iso(),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({"success": True, "message": "Fingerprint enrolled", "fingerprint": row.to_admin_dict()})


@app.route("/api/admin/users/<staff_id>/fingerprints/<int:template_id>", methods=["DELETE"])
def admin_delete_fingerprint(staff_id, template_id):
    admin, error = require_admin()
    if error:
        return error

    row = FingerprintTemplate.query.filter_by(id=template_id, staff_id=staff_id).first()
    if not row:
        return jsonify({"success": False, "message": "Fingerprint not found"}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({"success": True, "message": "Fingerprint removed"})


@app.route("/api/admin/users/<staff_id>/legacy_fingerprints", methods=["DELETE"])
def admin_clear_legacy_fingerprints(staff_id):
    admin, error = require_admin()
    if error:
        return error

    staff_row = db.session.get(Staff, staff_id)
    if not staff_row:
        return jsonify({"success": False, "message": "User not found"}), 404
    staff_row.fingerprint_template_id = ""
    db.session.commit()
    return jsonify({"success": True, "message": "Legacy gate-sensor fingerprints unlinked"})


@app.route("/api/admin/users/<staff_id>/photo", methods=["GET"])
def admin_user_photo(staff_id):
    admin, error = require_admin()
    if error:
        return error

    staff_row = db.session.get(Staff, staff_id)
    if not staff_row or not staff_row.passport_photo_type:
        return jsonify({"success": False, "message": "No photo"}), 404
    return image_response(staff_row.passport_photo, staff_row.passport_photo_type)


@app.route("/api/admin/vehicles/<vehicle_id>/photo", methods=["GET"])
def admin_vehicle_photo(vehicle_id):
    admin, error = require_admin()
    if error:
        return error

    vehicle = db.session.get(Vehicle, vehicle_id)
    if not vehicle or not vehicle.photo_type:
        return jsonify({"success": False, "message": "No photo"}), 404
    return image_response(vehicle.photo, vehicle.photo_type)


# ---------------------------------------------------------------------------
# Route 1d: admin - who am I (used by the admin page to confirm the session)
# ---------------------------------------------------------------------------

@app.route("/api/admin/me", methods=["GET"])
def admin_me():
    admin, error = require_admin()
    if error:
        return error
    return jsonify({"success": True, "admin_id": admin["admin_id"], "name": admin["name"]})


# ---------------------------------------------------------------------------
# Route 1e: admin - surveillance (stats, access log, detections, live feed)
# ---------------------------------------------------------------------------

def today_stats():
    today = now_wat().date().isoformat()
    todays_logs = Log.query.filter(Log.timestamp >= today).all()
    todays_detections = GateEvent.query.filter(GateEvent.kind == "detection", GateEvent.timestamp >= today).all()
    return {
        "users": Staff.query.count(),
        "vehicles": Vehicle.query.count(),
        "entries": sum(1 for log in todays_logs if log.event_type == "entry" and log.status == "success"),
        "exits": sum(1 for log in todays_logs if log.event_type == "exit" and log.status == "success"),
        "denied": sum(1 for log in todays_logs if log.status != "success"),
        "detections": len(todays_detections),
        "visitors": sum(1 for event in todays_detections if not event.staff_id),
    }


def name_lookup():
    return {staff_id: name for staff_id, name in db.session.query(Staff.staff_id, Staff.name).all()}


@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    admin, error = require_admin()
    if error:
        return error

    names = name_lookup()
    events = [event.to_dict() for event in GateEvent.query.order_by(GateEvent.id.desc()).limit(12)]
    for event in events:
        event["owner_name"] = names.get(event["staff_id"], "")
    return jsonify({"success": True, "stats": today_stats(), "devices": device_status(), "recent_events": events})


@app.route("/api/admin/logs", methods=["GET"])
def admin_logs():
    admin, error = require_admin()
    if error:
        return error

    query = Log.query
    status = (request.args.get("status") or "").strip().lower()
    method = (request.args.get("method") or "").strip().lower()
    event_type = (request.args.get("event_type") or "").strip().lower()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    if status == "success":
        query = query.filter(Log.status == "success")
    elif status == "fail":
        query = query.filter(Log.status != "success")
    if method:
        query = query.filter(db.func.lower(Log.method) == method)
    if event_type:
        query = query.filter(db.func.lower(Log.event_type) == event_type)
    if date_from:
        query = query.filter(Log.timestamp >= date_from)
    if date_to:
        # Inclusive of the whole end day.
        query = query.filter(Log.timestamp < date_to + "T99")

    names = name_lookup()
    search = (request.args.get("q") or "").strip().lower()
    logs = []
    for log in query.order_by(Log.timestamp.desc()).limit(1000):
        data = log.to_dict()
        data["owner_name"] = names.get(log.staff_id, "")
        if search and search not in " ".join([
            data["staff_id"] or "", data["owner_name"], data["plate_number"], data["details"],
        ]).lower():
            continue
        logs.append(data)
        if len(logs) >= 300:
            break
    return jsonify({"success": True, "log": logs})


@app.route("/api/admin/detections", methods=["GET"])
def admin_detections():
    admin, error = require_admin()
    if error:
        return error

    search = normalize_plate(request.args.get("q"))
    names = name_lookup()
    detections = []
    for event in GateEvent.query.filter_by(kind="detection").order_by(GateEvent.id.desc()).limit(500):
        if search and search not in normalize_plate(event.plate_number):
            continue
        data = event.to_dict()
        data["owner_name"] = names.get(event.staff_id, "")
        detections.append(data)
        if len(detections) >= 60:
            break
    return jsonify({"success": True, "detections": detections})


@app.route("/api/admin/events/<int:event_id>/snapshot", methods=["GET"])
def admin_event_snapshot(event_id):
    admin, error = require_admin()
    if error:
        return error

    event = db.session.get(GateEvent, event_id)
    if not event or not event.snapshot_type:
        return jsonify({"success": False, "message": "No snapshot"}), 404
    response = image_response(event.snapshot, event.snapshot_type)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


@app.route("/api/admin/camera/latest.jpg", methods=["GET"])
def admin_camera_frame():
    admin, error = require_admin()
    if error:
        return error

    with live_lock:
        data, image_type = latest_camera_frame["data"], latest_camera_frame["type"]
    if data is None:
        return jsonify({"success": False, "message": "No camera frame yet"}), 404
    response = Response(data, mimetype=image_type)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/admin/live", methods=["GET"])
def admin_live():
    """Everything the large-screen monitor needs, in one poll.

    The "session" is the most recent vehicle detection plus every gate
    event that followed it, so the monitor can show the current vehicle's
    progress (detected -> fingerprint -> OTP -> granted/denied).
    """
    admin, error = require_admin()
    if error:
        return error

    names = name_lookup()

    def with_name(event):
        data = event.to_dict()
        data["owner_name"] = names.get(event.staff_id, "")
        return data

    session = None
    detection = GateEvent.query.filter_by(kind="detection").order_by(GateEvent.id.desc()).first()
    if detection is not None:
        steps = GateEvent.query.filter(GateEvent.id >= detection.id).order_by(GateEvent.id).all()
        session = {"detection": with_name(detection), "steps": [with_name(step) for step in steps], "owner": None}
        owner = db.session.get(Staff, detection.staff_id) if detection.staff_id else None
        if owner is not None:
            session["owner"] = user_profile(owner)

    events = [with_name(event) for event in GateEvent.query.order_by(GateEvent.id.desc()).limit(25)]
    return jsonify({
        "success": True,
        "server_time": now_iso(),
        "session": session,
        "events": events,
        "stats": today_stats(),
        "devices": device_status(),
    })


# ---------------------------------------------------------------------------
# Route 2: dashboard
# ---------------------------------------------------------------------------

@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    staff, error = require_auth()
    if error:
        return error

    now = now_wat()
    active_otps = [
        otp.to_dict() for otp in Otp.query.filter_by(staff_id=staff["staff_id"], used=False).all()
        if parse_time(otp.expiry_time) > now
    ]

    logs = [log.to_dict() for log in Log.query.filter_by(staff_id=staff["staff_id"]).all()]
    logs.sort(key=lambda log: log["timestamp"], reverse=True)

    return jsonify({
        "success": True,
        "staff_id": staff["staff_id"],
        "name": staff["name"],
        "plate_number": staff["plate_number"],
        "active_otps": active_otps,
        "recent_activity": logs[:5],
    })


# ---------------------------------------------------------------------------
# Route 3: generate OTP
# ---------------------------------------------------------------------------

@app.route("/api/generate_otp", methods=["POST"])
def generate_otp():
    staff, error = require_auth()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    try:
        time_limit = int(data.get("time_limit"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "time_limit must be a whole number of minutes"}), 400

    if time_limit <= 0 or time_limit > 1440:
        return jsonify({"success": False, "message": "time_limit must be between 1 and 1440 minutes"}), 400

    created = now_wat()
    expiry = created + timedelta(minutes=time_limit)
    code = "".join(random.choices(string.digits, k=6))

    db.session.add(Otp(
        otp_code=code,
        staff_id=staff["staff_id"],
        created_time=created.isoformat(timespec="seconds"),
        expiry_time=expiry.isoformat(timespec="seconds"),
        used=False,
    ))
    db.session.commit()

    email_sent, email_message = send_otp_email(staff, code, expiry)

    return jsonify({
        "success": True,
        "otp_code": code,
        "created_time": created.isoformat(timespec="seconds"),
        "expiry_time": expiry.isoformat(timespec="seconds"),
        "email_sent": email_sent,
        "email_message": email_message,
    })


# ---------------------------------------------------------------------------
# Route 4: revoke OTP
# ---------------------------------------------------------------------------

@app.route("/api/revoke_otp", methods=["POST"])
def revoke_otp():
    staff, error = require_auth()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    code = (data.get("otp_code") or "").strip()
    if not code:
        return jsonify({"success": False, "message": "otp_code is required"}), 400

    otp = Otp.query.filter_by(otp_code=code, staff_id=staff["staff_id"]).first()
    if not otp:
        return jsonify({"success": False, "message": "OTP not found"}), 404

    if otp.used:
        return jsonify({"success": False, "message": "OTP is already used or revoked"}), 400

    otp.used = True
    db.session.commit()
    return jsonify({"success": True, "message": "OTP revoked"})


# ---------------------------------------------------------------------------
# Route 5: activity log
# ---------------------------------------------------------------------------

@app.route("/api/activity_log", methods=["GET"])
def activity_log():
    staff, error = require_auth()
    if error:
        return error

    logs = [log.to_dict() for log in Log.query.filter_by(staff_id=staff["staff_id"]).all()]
    logs.sort(key=lambda log: log["timestamp"], reverse=True)

    return jsonify({"success": True, "plate_number": staff["plate_number"], "log": logs})


# ---------------------------------------------------------------------------
# Route 6: plate lookup (called by the ANPR / Raspberry Pi side)
# ---------------------------------------------------------------------------

def plate_lookup(plate):
    vehicle = find_vehicle_by_plate(plate)
    if not vehicle:
        return {"is_staff_vehicle": False, "plate_number": plate}

    staff = find_staff(vehicle["staff_id"])
    return {
        "is_staff_vehicle": True,
        "plate_number": vehicle["plate_number"],
        "staff_id": vehicle["staff_id"],
        "owner_name": staff["name"] if staff else None,
    }


@app.route("/check_plate", methods=["GET"])
def check_plate():
    error = require_device()
    if error:
        return error

    plate = (request.args.get("plate") or "").strip()
    if not plate:
        return jsonify({"success": False, "message": "plate query parameter is required"}), 400

    return jsonify(plate_lookup(plate))


@app.route("/api/device/detection", methods=["POST"])
def device_detection():
    """The ANPR box reports a confirmed plate read (plus a snapshot of the
    frame) and gets back the same answer as /check_plate. Recorded as a gate
    event so the admin monitor sees every vehicle, staff or visitor."""
    error = require_device()
    if error:
        return error

    plate = (request.form.get("plate") or "").strip().upper()
    if not plate:
        return jsonify({"success": False, "message": "plate is required"}), 400
    event_type = normalize_event_type(request.form.get("event_type"), "entry")

    snapshot, snapshot_type = None, None
    uploaded = request.files.get("image")
    if uploaded is not None:
        data = uploaded.read()
        if data and len(data) <= MAX_FRAME_BYTES and sniff_image_type(data):
            snapshot, snapshot_type = data, sniff_image_type(data)

    result = plate_lookup(plate)
    if result["is_staff_vehicle"]:
        message = f"Registered vehicle of {result['owner_name']} - verification required"
    else:
        message = "Unregistered vehicle (visitor) - gate opened"
    add_gate_event(
        "detection",
        plate_number=result["plate_number"],
        staff_id=result.get("staff_id"),
        event_type=event_type,
        status="registered" if result["is_staff_vehicle"] else "visitor",
        message=message,
        snapshot=snapshot,
        snapshot_type=snapshot_type,
    )
    return jsonify(result)


@app.route("/api/device/camera_frame", methods=["POST"])
def device_camera_frame():
    """Latest frame from the ANPR camera, for the admin monitor's live view.
    Body is the raw JPEG. Kept in memory only - never written to the DB."""
    error = require_device()
    if error:
        return error

    data = request.get_data()
    if not data or len(data) > MAX_FRAME_BYTES or sniff_image_type(data) is None:
        return jsonify({"success": False, "message": "Send a JPEG/PNG/WEBP body under 2 MB"}), 400
    with live_lock:
        latest_camera_frame.update(data=data, type=sniff_image_type(data), time=time.time())
    return jsonify({"success": True})


@app.route("/api/device/heartbeat", methods=["POST"])
def device_heartbeat():
    error = require_device()
    if error:
        return error
    return jsonify({"success": True, "server_time": now_iso()})


# ---------------------------------------------------------------------------
# Route 7: verify OTP (called from the gate keypad flow, after the driver's
# fingerprint didn't match the owner)
# ---------------------------------------------------------------------------

@app.route("/api/verify_otp", methods=["POST"])
def verify_otp():
    error = require_device()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    code = (data.get("otp_code") or "").strip()
    staff_id = (data.get("staff_id") or "").strip()
    plate_number = (data.get("plate_number") or "").strip()
    event_type = normalize_event_type(data.get("event_type"), "exit")

    if not code or not staff_id:
        return jsonify({"success": False, "message": "otp_code and staff_id are required"}), 400

    now = now_wat()
    matched = Otp.query.filter_by(otp_code=code, staff_id=staff_id).first()

    success = False
    if matched is None:
        message = "Invalid OTP"
    elif matched.used:
        message = "OTP has already been used or revoked"
    elif parse_time(matched.expiry_time) <= now:
        message = "OTP has expired"
    else:
        matched.used = True
        db.session.commit()
        success = True
        message = "OTP verified successfully"

    log_event(staff_id, plate_number, "otp", event_type, "success" if success else "fail", message)
    add_gate_event(
        "otp", plate_number=plate_number, staff_id=staff_id, event_type=event_type,
        status="success" if success else "fail",
        message="OTP accepted - access granted" if success else message + " - access denied",
    )

    return jsonify({"success": success, "message": message})


# ---------------------------------------------------------------------------
# Route 8: fingerprints. Templates live on the server; the gate ESP32
# downloads the vehicle owner's templates into its sensor and does a 1:1
# match there, then reports the result.
# ---------------------------------------------------------------------------

@app.route("/api/device/users/<staff_id>/fingerprints", methods=["GET"])
def device_user_fingerprints(staff_id):
    error = require_device()
    if error:
        return error

    staff_row = db.session.get(Staff, staff_id)
    if not staff_row:
        return jsonify({"success": False, "message": "Unknown staff_id"}), 404

    templates = [
        {"id": row.id, "template": base64.b64encode(row.template).decode("ascii")}
        for row in FingerprintTemplate.query.filter_by(staff_id=staff_id).order_by(FingerprintTemplate.id)
    ]
    # Fingerprints enrolled the old way, straight into the gate sensor's own
    # library, still work until they're re-enrolled through the admin page.
    legacy_ids = [int(value) for value in fingerprint_ids(staff_row.fingerprint_template_id) if value.isdigit()]

    if request.args.get("plate"):
        add_gate_event(
            "awaiting_fingerprint",
            plate_number=request.args.get("plate"),
            staff_id=staff_id,
            event_type=normalize_event_type(request.args.get("event_type"), "entry"),
            message="Gate waiting for driver's fingerprint" if templates or legacy_ids
            else "No fingerprint on file - OTP required",
        )

    return jsonify({"success": True, "staff_id": staff_id, "templates": templates, "legacy_ids": legacy_ids})


@app.route("/api/verify_fingerprint", methods=["POST"])
def verify_fingerprint():
    error = require_device()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    staff_id = (data.get("staff_id") or "").strip()
    plate_number = (data.get("plate_number") or "").strip()
    event_type = normalize_event_type(data.get("event_type"), "entry")

    if not staff_id:
        return jsonify({"success": False, "message": "staff_id is required"}), 400

    staff = find_staff(staff_id)
    if "matched" in data:
        # Current gate firmware: the sensor already did the 1:1 match
        # against the templates it downloaded from us.
        success = bool(staff) and bool(data.get("matched"))
        details = "Matched {ref} (score {score})".format(
            ref=data.get("template_ref") or "?", score=data.get("score", "?")) if success else "Driver is not the owner"
    else:
        # Older path: the sensor searched its own library and reports a slot.
        template_id = (data.get("fingerprint_template_id") or "").strip()
        if not template_id:
            return jsonify({"success": False, "message": "matched or fingerprint_template_id is required"}), 400
        success = bool(staff) and template_id in fingerprint_ids(staff.get("fingerprint_template_id"))
        details = f"Gate sensor slot {template_id}"

    message = "Fingerprint verified successfully" if success else "Fingerprint does not match staff record"
    plate_number = plate_number or (staff["plate_number"] if staff else "")
    log_event(staff_id, plate_number, "fingerprint", event_type, "success" if success else "fail", details)
    add_gate_event(
        "fingerprint", plate_number=plate_number, staff_id=staff_id, event_type=event_type,
        status="success" if success else "fail",
        message="Owner's fingerprint matched - access granted" if success
        else "Fingerprint is not the owner's - waiting for OTP",
    )

    return jsonify({"success": success, "message": message})


# ---------------------------------------------------------------------------
# Route 9: log a standalone gate event (timeouts, failures with no OTP/FP
# attempt at all) - called by the ESP32.
# ---------------------------------------------------------------------------

@app.route("/api/log_event", methods=["POST"])
def log_event_route():
    error = require_device()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    staff_id = (data.get("staff_id") or "").strip()
    plate_number = (data.get("plate_number") or "").strip()
    method = (data.get("method") or "none").strip().lower()
    event_type = normalize_event_type(data.get("event_type"), "entry")
    status = (data.get("status") or "fail").strip().lower()
    details = (data.get("details") or "").strip()

    if not staff_id:
        return jsonify({"success": False, "message": "staff_id is required"}), 400

    log_event(staff_id, plate_number, method, event_type, status, details)
    add_gate_event(
        "timeout" if "timeout" in details.lower() else "note",
        plate_number=plate_number, staff_id=staff_id, event_type=event_type, status=status,
        message=details or f"{method} {status}",
    )
    return jsonify({"success": True, "message": "Event logged"})


# ---------------------------------------------------------------------------
# Static frontend (the ESP32 will later serve these same files from SPIFFS)
# ---------------------------------------------------------------------------

@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "login.html")


@app.route("/admin")
def serve_admin_index():
    return send_from_directory(FRONTEND_DIR, "admin-login.html")


@app.route("/<path:filename>")
def serve_frontend(filename):
    return send_from_directory(FRONTEND_DIR, filename)


with app.app_context():
    db.create_all()
    ensure_schema()
    seed_data()
    ensure_staff_email()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
