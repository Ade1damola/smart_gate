"""One-off migration: copy the old JSON data files into the SQLAlchemy DB.

Run this once, by hand, right after the first deploy to a fresh Postgres
instance (or a fresh local SQLite file) to bring over any staff/vehicle/log
records that existed before the storage layer moved off flat JSON files.

Usage:
    python migrate_json_to_db.py
"""

import json
import os

from app import app, ensure_default_admin
from models import db, Staff, Vehicle, Log

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STAFF_FILE = os.path.join(DATA_DIR, "staff.json")
VEHICLES_FILE = os.path.join(DATA_DIR, "vehicles.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")


def load_json(path):
    if not os.path.exists(path):
        print(f"  (no {os.path.basename(path)} found, skipping)")
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def migrate_staff():
    records = load_json(STAFF_FILE)
    migrated = 0
    for record in records:
        staff = db.session.get(Staff, record["staff_id"])
        if staff is None:
            staff = Staff(staff_id=record["staff_id"])
            db.session.add(staff)
        staff.name = record["name"]
        staff.password_hash = record["password_hash"]
        staff.fingerprint_template_id = record.get("fingerprint_template_id", "")
        staff.plate_number = record.get("plate_number", "")
        staff.phone_number = record.get("phone_number", "")
        migrated += 1
    db.session.commit()
    print(f"  Staff: {migrated} record(s) upserted")


def migrate_vehicles():
    records = load_json(VEHICLES_FILE)
    migrated = 0
    for record in records:
        vehicle = db.session.get(Vehicle, record["vehicle_id"])
        if vehicle is None:
            vehicle = Vehicle(vehicle_id=record["vehicle_id"])
            db.session.add(vehicle)
        vehicle.staff_id = record["staff_id"]
        vehicle.plate_number = record["plate_number"]
        migrated += 1
    db.session.commit()
    print(f"  Vehicles: {migrated} record(s) upserted")


def migrate_logs():
    if Log.query.first() is not None:
        print("  Logs: table already has data, skipping to avoid duplicates")
        return
    records = load_json(LOGS_FILE)
    for record in records:
        db.session.add(Log(
            staff_id=record.get("staff_id"),
            plate_number=record.get("plate_number", ""),
            method=record.get("method", "unknown"),
            event_type=record.get("event_type", "entry"),
            timestamp=record.get("timestamp"),
            status=record.get("status", "unknown"),
            details=record.get("details", ""),
        ))
    db.session.commit()
    print(f"  Logs: {len(records)} record(s) inserted")


if __name__ == "__main__":
    with app.app_context():
        print("Migrating staff.json / vehicles.json / logs.json into the database...")
        migrate_staff()
        migrate_vehicles()
        migrate_logs()
        ensure_default_admin()
        print("  Admin: GateAdmin account ensured")
        print("Done.")
