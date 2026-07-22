"""Flask backend for the Smart Gate staff app.

Storage is SQLAlchemy-backed: SQLite locally by default, Postgres in
production (set DATABASE_URL). This lets the same codebase run on a laptop
for development and on a hosted platform (Render) for the real ESP32/Pi
gate hardware.
"""

import os
import random
import secrets
import string
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

from models import db, Staff, Admin, Vehicle, Otp, PasswordResetOtp, Log

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_DATABASE_URL = "sqlite:///" + os.path.join(DATA_DIR, "smart_gate.db")
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
# Render (and most Postgres hosts) hand out "postgres://" URLs, but SQLAlchemy
# 1.4+ requires the "postgresql://" scheme.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Shared secret the ESP32 and Raspberry Pi must send as the X-Device-Key
# header on device-facing routes. Left unset locally so laptop testing
# doesn't require configuring a key; must be set once this is hosted publicly.
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY", "")

# Termii credentials for real SMS delivery. When unset (local dev), the
# reset code is printed to the console instead of actually being texted.
TERMII_API_KEY = os.environ.get("TERMII_API_KEY", "")
TERMII_SENDER_ID = os.environ.get("TERMII_SENDER_ID", "")

# Default password assigned to every newly-added staff member. They're
# expected to use the "forgot password" OTP flow to set their own password.
DEFAULT_STAFF_PASSWORD = "Welcome123!"

# Default password for the seeded admin account. Admins have no phone number
# and no forgot-password flow of their own, so this only changes if someone
# updates it directly in the database.
DEFAULT_ADMIN_PASSWORD = "Verigate752$"

# How long a password-reset OTP stays valid.
RESET_OTP_VALID_MINUTES = 10

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

# In-memory session store: token -> {"role": "staff"/"admin", "id": ...}.
# Fine for a small staff app; a Render restart just means everyone logs in
# again.
sessions = {}


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
        ),
        Staff(
            staff_id="STAFF002",
            name="Tunde Bakare",
            password_hash=generate_password_hash("password456"),
            fingerprint_template_id="FP1002",
            plate_number="BDG-889HS",
            phone_number="+2348033334444",
        ),
    ])
    db.session.add_all([
        Vehicle(vehicle_id="VEH001", staff_id="STAFF001", plate_number="LND-113JN"),
        Vehicle(vehicle_id="VEH002", staff_id="STAFF002", plate_number="BDG-889HS"),
    ])

    now = datetime.now()
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


def parse_time(value):
    return datetime.fromisoformat(value)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_staff(staff_id):
    staff = db.session.get(Staff, staff_id) if staff_id else None
    return staff.to_dict() if staff else None


def find_vehicle_by_plate(plate):
    plate = plate.strip().upper()
    for vehicle in Vehicle.query.all():
        if vehicle.plate_number.upper() == plate:
            return {"vehicle_id": vehicle.vehicle_id, "staff_id": vehicle.staff_id, "plate_number": vehicle.plate_number}
    return None


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
    """
    if not DEVICE_API_KEY:
        return None
    provided = request.headers.get("X-Device-Key", "")
    if provided != DEVICE_API_KEY:
        return jsonify({"success": False, "message": "Invalid or missing device key."}), 401
    return None


# ---------------------------------------------------------------------------
# Route 1: login
# ---------------------------------------------------------------------------

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    login_id = (data.get("login_id") or "").strip()
    password = data.get("password") or ""

    staff = find_staff(login_id)
    if staff and check_password_hash(staff["password_hash"], password):
        token = secrets.token_hex(16)
        sessions[token] = {"role": "staff", "id": staff["staff_id"]}
        return jsonify({
            "success": True,
            "token": token,
            "role": "staff",
            "staff_id": staff["staff_id"],
            "name": staff["name"],
        })

    admin = db.session.get(Admin, login_id)
    if admin and check_password_hash(admin.password_hash, password):
        token = secrets.token_hex(16)
        sessions[token] = {"role": "admin", "id": admin.admin_id}
        return jsonify({
            "success": True,
            "token": token,
            "role": "admin",
            "admin_id": admin.admin_id,
            "name": admin.name,
        })

    return jsonify({"success": False, "message": "Invalid ID or password"}), 401


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
        created = datetime.now()
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

    now = datetime.now()
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
# Route 1c: admin - add a new staff member
# ---------------------------------------------------------------------------

@app.route("/api/admin/add_staff", methods=["POST"])
def admin_add_staff():
    admin, error = require_admin()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    staff_id = (data.get("staff_id") or "").strip()
    name = (data.get("name") or "").strip()
    phone_number = (data.get("phone_number") or "").strip()
    plate_number = (data.get("plate_number") or "").strip().upper()
    fingerprint_template_id = (data.get("fingerprint_template_id") or "").strip()

    if not staff_id or not name or not phone_number:
        return jsonify({"success": False, "message": "staff_id, name and phone_number are required"}), 400

    if db.session.get(Staff, staff_id) is not None:
        return jsonify({"success": False, "message": "A staff member with that staff_id already exists"}), 409

    db.session.add(Staff(
        staff_id=staff_id,
        name=name,
        password_hash=generate_password_hash(DEFAULT_STAFF_PASSWORD),
        fingerprint_template_id=fingerprint_template_id,
        plate_number=plate_number,
        phone_number=phone_number,
    ))

    if plate_number:
        next_num = Vehicle.query.count() + 1
        db.session.add(Vehicle(
            vehicle_id="VEH%03d" % next_num,
            staff_id=staff_id,
            plate_number=plate_number,
        ))

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Staff member added",
        "staff_id": staff_id,
        "default_password": DEFAULT_STAFF_PASSWORD,
    })


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
# Route 2: dashboard
# ---------------------------------------------------------------------------

@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    staff, error = require_auth()
    if error:
        return error

    now = datetime.now()
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

    created = datetime.now()
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

    return jsonify({
        "success": True,
        "otp_code": code,
        "created_time": created.isoformat(timespec="seconds"),
        "expiry_time": expiry.isoformat(timespec="seconds"),
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

@app.route("/check_plate", methods=["GET"])
def check_plate():
    error = require_device()
    if error:
        return error

    plate = (request.args.get("plate") or "").strip()
    if not plate:
        return jsonify({"success": False, "message": "plate query parameter is required"}), 400

    vehicle = find_vehicle_by_plate(plate)
    if not vehicle:
        return jsonify({"is_staff_vehicle": False, "plate_number": plate})

    staff = find_staff(vehicle["staff_id"])
    return jsonify({
        "is_staff_vehicle": True,
        "plate_number": vehicle["plate_number"],
        "staff_id": vehicle["staff_id"],
        "owner_name": staff["name"] if staff else None,
        "fingerprint_template_id": staff["fingerprint_template_id"] if staff else None,
    })


# ---------------------------------------------------------------------------
# Route 7: verify OTP (called from the gate keypad flow)
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
    event_type = (data.get("event_type") or "entry").strip().lower()
    if event_type not in ("entry", "exit"):
        event_type = "entry"

    if not code or not staff_id:
        return jsonify({"success": False, "message": "otp_code and staff_id are required"}), 400

    now = datetime.now()
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

    log_event(staff_id, plate_number, "otp", event_type, "success" if success else "fail")

    return jsonify({"success": success, "message": message})


# ---------------------------------------------------------------------------
# Route 8: verify fingerprint (simulates the R307 sensor's match logic)
# ---------------------------------------------------------------------------

@app.route("/api/verify_fingerprint", methods=["POST"])
def verify_fingerprint():
    error = require_device()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    staff_id = (data.get("staff_id") or "").strip()
    template_id = (data.get("fingerprint_template_id") or "").strip()
    plate_number = (data.get("plate_number") or "").strip()
    event_type = (data.get("event_type") or "entry").strip().lower()
    if event_type not in ("entry", "exit"):
        event_type = "entry"

    if not staff_id or not template_id:
        return jsonify({"success": False, "message": "staff_id and fingerprint_template_id are required"}), 400

    staff = find_staff(staff_id)
    success = bool(staff) and staff.get("fingerprint_template_id") == template_id
    message = "Fingerprint verified successfully" if success else "Fingerprint does not match staff record"

    log_event(staff_id, plate_number or (staff["plate_number"] if staff else ""), "fingerprint", event_type,
              "success" if success else "fail")

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
    event_type = (data.get("event_type") or "entry").strip().lower()
    if event_type not in ("entry", "exit"):
        event_type = "entry"
    status = (data.get("status") or "fail").strip().lower()
    details = (data.get("details") or "").strip()

    if not staff_id:
        return jsonify({"success": False, "message": "staff_id is required"}), 400

    log_event(staff_id, plate_number, method, event_type, status, details)
    return jsonify({"success": True, "message": "Event logged"})


# ---------------------------------------------------------------------------
# Route 10: record a newly-enrolled fingerprint template ID - called by the
# ESP32 right after a successful local enrollment at the gate.
# ---------------------------------------------------------------------------

@app.route("/api/device/staff/<staff_id>/fingerprint", methods=["POST"])
def update_staff_fingerprint(staff_id):
    error = require_device()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    template_id = (data.get("fingerprint_template_id") or "").strip()
    if not template_id:
        return jsonify({"success": False, "message": "fingerprint_template_id is required"}), 400

    staff_row = db.session.get(Staff, staff_id)
    if not staff_row:
        return jsonify({"success": False, "message": "Unknown staff_id"}), 404

    staff_row.fingerprint_template_id = template_id
    db.session.commit()

    return jsonify({"success": True, "message": "Fingerprint template recorded"})


# ---------------------------------------------------------------------------
# Static frontend (the ESP32 will later serve these same files from SPIFFS)
# ---------------------------------------------------------------------------

@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "login.html")


@app.route("/<path:filename>")
def serve_frontend(filename):
    return send_from_directory(FRONTEND_DIR, filename)


with app.app_context():
    db.create_all()
    seed_data()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
