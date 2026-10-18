"""Operator CLI. There is no public signup: users are created here.

    python -m app.cli create-user --email admin@example.com --name "Admin" --role admin
    python -m app.cli generate-encryption-key
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from app.core.crypto import generate_key
from app.db.session import get_sessionmaker
from app.domain.states import UserRole
from app.services.auth import create_user


def _create_user(args: argparse.Namespace) -> int:
    # Non-interactive use (e.g. CI) may supply the password via env var; it is never accepted as an argv
    # flag because argv is visible in process listings and shell history.
    password = os.environ.get("OUTREACH_NEW_USER_PASSWORD") or getpass.getpass("Password (min 12 chars): ")
    with get_sessionmaker()() as db:
        try:
            user = create_user(db, email=args.email, password=password, display_name=args.name,
                               role=UserRole(args.role), workspace_name=args.workspace)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"created {user.role.value} user {user.email} ({user.id})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="outreach")
    sub = parser.add_subparsers(dest="command", required=True)

    cu = sub.add_parser("create-user", help="create a dashboard user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--name", required=True)
    cu.add_argument("--role", choices=[r.value for r in UserRole], default=UserRole.MEMBER.value)
    cu.add_argument("--workspace", help="workspace name, used only when creating the first workspace")

    sub.add_parser("generate-encryption-key", help="print a new CREDENTIAL_ENCRYPTION_KEYS entry")

    args = parser.parse_args(argv)
    if args.command == "create-user":
        return _create_user(args)
    if args.command == "generate-encryption-key":
        print(f"v1:{generate_key()}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
