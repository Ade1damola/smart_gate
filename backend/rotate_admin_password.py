"""One-off: reset the live GateAdmin account's password.

ensure_default_admin() only ever creates GateAdmin if the row doesn't exist
yet, so changing DEFAULT_ADMIN_PASSWORD (env var) alone does nothing for an
account that's already in the database - admins have no phone number and no
self-service "forgot password" flow, so this is the only way to rotate it.

Usage (run locally, pointed at whichever database you want to update):
    DATABASE_URL=<your Render Postgres URL> ^
    NEW_ADMIN_PASSWORD=<the new password> ^
    python rotate_admin_password.py
"""

import os
import sys

from werkzeug.security import generate_password_hash

from app import app
from models import db, Admin

new_password = os.environ.get("NEW_ADMIN_PASSWORD")
if not new_password:
    print("Set NEW_ADMIN_PASSWORD before running this script.")
    sys.exit(1)

with app.app_context():
    admin = db.session.get(Admin, "GateAdmin")
    if admin is None:
        print("No GateAdmin account found in this database - nothing to rotate.")
        sys.exit(1)

    admin.password_hash = generate_password_hash(new_password)
    db.session.commit()
    print("GateAdmin password rotated.")
