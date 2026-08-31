"""One-off: directly set a staff member's password, bypassing the OTP flow.

Useful when a staff member's phone number isn't real/reachable yet (so the
normal "forgot password" OTP can't be delivered) but you still want their
account to have a specific password rather than the DEFAULT_STAFF_PASSWORD
assigned at creation.

Usage (run locally, pointed at whichever database you want to update):
    DATABASE_URL=<your Render Postgres URL> ^
    STAFF_ID=STAFF003 ^
    NEW_PASSWORD=<the new password> ^
    python set_staff_password.py
"""

import os
import sys

from werkzeug.security import generate_password_hash

from app import app
from models import db, Staff

staff_id = os.environ.get("STAFF_ID")
new_password = os.environ.get("NEW_PASSWORD")
if not staff_id or not new_password:
    print("Set STAFF_ID and NEW_PASSWORD before running this script.")
    sys.exit(1)

with app.app_context():
    staff = db.session.get(Staff, staff_id)
    if staff is None:
        print(f"No staff member with staff_id={staff_id!r} found - nothing to update.")
        sys.exit(1)

    staff.password_hash = generate_password_hash(new_password)
    db.session.commit()
    print(f"Password set for {staff_id}.")
