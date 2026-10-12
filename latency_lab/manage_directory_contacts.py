"""Interactively add or update an operator-approved caller contact."""
from __future__ import annotations

import getpass
import os
from pathlib import Path

from .directory_email import normalize_phone, save_contact


def main() -> int:
    path = os.environ.get("DIRECTORY_CONTACTS_DB", "").strip()
    business_id = os.environ.get("DIRECTORY_CONTACTS_BUSINESS_ID", "").strip()
    if not path or not business_id:
        raise SystemExit("Set DIRECTORY_CONTACTS_DB and DIRECTORY_CONTACTS_BUSINESS_ID first.")

    name = " ".join(input("Caller name: ").split())[:120]
    phone = normalize_phone(input("Caller phone (international or local): "))
    if not name or not phone:
        raise SystemExit("A name and valid phone number are required.")
    email = getpass.getpass("Saved email (leave blank if not supplied yet): ").strip()
    save_contact(Path(path), business_id, phone, email=email, full_name=name)
    suffix = phone[-4:]
    print(f"Saved caller {name} (number ending {suffix}); saved email: {'yes' if email else 'no'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
