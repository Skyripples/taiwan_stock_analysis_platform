"""Create or deliberately reset an administrator without exposing a password."""
from __future__ import annotations

import argparse
import getpass
import sys
import uuid

from pwdlib import PasswordHash

from database.connection import transaction


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--reset-password", action="store_true")
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Password: ")
    if len(password) < 14:
        raise SystemExit("Password must contain at least 14 characters")
    digest = PasswordHash.recommended().hash(password)
    with transaction() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT user_id FROM auth_users WHERE lower(username)=lower(%s)", (args.username,))
        row = cursor.fetchone()
        if row and not args.reset_password:
            raise SystemExit("Account already exists; use --reset-password deliberately")
        if row:
            cursor.execute("UPDATE auth_users SET password_hash=%s, failed_attempts=0, locked_until=NULL, updated_at=now() WHERE user_id=%s", (digest, row["user_id"]))
        else:
            cursor.execute("INSERT INTO auth_users(user_id,username,password_hash,role) VALUES (%s,%s,%s,'admin')", (uuid.uuid4(), args.username, digest))
    print(f"Administrator ready: {args.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
