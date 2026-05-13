"""Tiny pre-flight: validate that login + me() work against the live api
using the seeded gtb-newuser account. No LLM in the loop — pure
UserClient smoke. If this fails, the persona framework has no chance.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-login.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from lib.user_client import UserClient, UserClientError


def main() -> int:
    load_dotenv()
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"Smoke login: {email} -> {api_base}")
    with UserClient(base_url=api_base) as client:
        try:
            result = client.login(email, password)
            print(f"  login OK: user_id={result.user_id} email_verified={result.email_verified} 2fa={result.has_2fa}")
        except UserClientError as e:
            print(f"  login FAILED: {e}", file=sys.stderr)
            return 1

        try:
            me = client.me()
            print(f"  me OK: username={me.get('username')} email_verified={me.get('email_verified')} role={me.get('role')}")
        except UserClientError as e:
            print(f"  me FAILED: {e}", file=sys.stderr)
            return 1

        try:
            mine = client.my_gruprs()
            print(f"  my_gruprs OK: {len(mine)} gruprs")
        except UserClientError as e:
            print(f"  my_gruprs FAILED: {e}", file=sys.stderr)
            return 1

    print("Smoke OK — framework can talk to api.grupr.ai end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
