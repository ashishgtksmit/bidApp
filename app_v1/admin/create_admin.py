"""Create the first super admin (run once from the bidApp root).

    python -m app_v1.admin.create_admin --email you@wizzride.com --name "Your Name"

Prints a one-time temporary password; the admin must change it on first sign-in.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from ..database import SessionLocal
from .models import ROLES, AdminUser
from .routes_access import _temp_password
from .security import hash_password


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="super_admin", choices=ROLES)
    a = p.parse_args(argv)
    db = SessionLocal()
    try:
        email = a.email.strip().lower()
        if db.query(AdminUser).filter(AdminUser.email == email).first():
            print(f"{email} already exists", file=sys.stderr)
            return 1
        temp = _temp_password()
        db.add(AdminUser(email=email, full_name=a.name.strip(), role=a.role, password_hash=hash_password(temp),
                         is_active=True, must_change_password=True, token_version=1, failed_logins=0,
                         created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        db.commit()
        print(f"Created {a.role} {email}\nTemporary password (shown once): {temp}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
