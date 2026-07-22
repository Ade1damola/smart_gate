"""SQLAlchemy models for the Smart Gate backend.

Mirrors the shape of the old staff.json / vehicles.json / otps.json /
password_reset_otps.json / logs.json files, now backed by a real database
(SQLite locally, Postgres in production) so data survives restarts/redeploys.
"""

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Staff(db.Model):
    __tablename__ = "staff"

    staff_id = db.Column(db.String(32), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    fingerprint_template_id = db.Column(db.String(32), nullable=True, default="")
    plate_number = db.Column(db.String(32), nullable=True, default="")
    phone_number = db.Column(db.String(32), nullable=False, default="")

    def to_dict(self):
        return {
            "staff_id": self.staff_id,
            "name": self.name,
            "password_hash": self.password_hash,
            "fingerprint_template_id": self.fingerprint_template_id or "",
            "plate_number": self.plate_number or "",
            "phone_number": self.phone_number or "",
        }


class Admin(db.Model):
    """A management account, entirely separate from gate-access staff.

    Admins can add new staff members, but have no fingerprint/plate/gate-OTP
    concept of their own - they're not staff.
    """

    __tablename__ = "admins"

    admin_id = db.Column(db.String(32), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def to_dict(self):
        return {
            "admin_id": self.admin_id,
            "name": self.name,
            "password_hash": self.password_hash,
        }


class Vehicle(db.Model):
    __tablename__ = "vehicles"

    vehicle_id = db.Column(db.String(32), primary_key=True)
    staff_id = db.Column(db.String(32), db.ForeignKey("staff.staff_id"), nullable=False)
    plate_number = db.Column(db.String(32), nullable=False)


class Otp(db.Model):
    """Gate-access OTPs generated from the staff dashboard."""

    __tablename__ = "otps"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    otp_code = db.Column(db.String(6), nullable=False)
    staff_id = db.Column(db.String(32), db.ForeignKey("staff.staff_id"), nullable=False)
    created_time = db.Column(db.String(32), nullable=False)
    expiry_time = db.Column(db.String(32), nullable=False)
    used = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self):
        return {
            "otp_code": self.otp_code,
            "staff_id": self.staff_id,
            "created_time": self.created_time,
            "expiry_time": self.expiry_time,
            "used": bool(self.used),
        }


class PasswordResetOtp(db.Model):
    __tablename__ = "password_reset_otps"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    otp_code = db.Column(db.String(6), nullable=False)
    staff_id = db.Column(db.String(32), db.ForeignKey("staff.staff_id"), nullable=False)
    created_time = db.Column(db.String(32), nullable=False)
    expiry_time = db.Column(db.String(32), nullable=False)
    used = db.Column(db.Boolean, nullable=False, default=False)


class Log(db.Model):
    __tablename__ = "logs"

    log_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(db.String(32), nullable=True)
    plate_number = db.Column(db.String(32), nullable=True, default="")
    method = db.Column(db.String(32), nullable=False)
    event_type = db.Column(db.String(16), nullable=False)
    timestamp = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(16), nullable=False)
    details = db.Column(db.String(255), nullable=True, default="")

    def to_dict(self):
        return {
            "log_id": self.log_id,
            "staff_id": self.staff_id,
            "plate_number": self.plate_number or "",
            "method": self.method,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "status": self.status,
            "details": self.details or "",
        }
