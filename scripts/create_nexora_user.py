"""Interactively create a password hash to place in NEXORA_USERS_JSON."""

import getpass
import json
import re

from pwdlib import PasswordHash


def main() -> None:
    username = input("Username: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,80}", username):
        raise SystemExit("Use 3–80 letters, digits, dots, underscores, @ signs, or hyphens.")
    role = input("Role [viewer/analyst/admin]: ").strip().lower()
    if role not in {"viewer", "analyst", "admin"}:
        raise SystemExit("Role must be viewer, analyst, or admin.")
    password = getpass.getpass("Password (12+ characters): ")
    if len(password) < 12:
        raise SystemExit("Use a password with at least 12 characters.")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords did not match.")
    entry = {username: {"role": role, "password_hash": PasswordHash.recommended().hash(password)}}
    print(json.dumps(entry, indent=2))
    print("Copy this entry into private NEXORA_USERS_JSON configuration. The plaintext password is not stored.")


if __name__ == "__main__":
    main()
