"""Smoke: verify Enterprise tier Stripe Checkout creation works after the
$100 active price ID swap on 2026-05-14. Pre-swap this returned 500 with
"The price specified is inactive."

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-enterprise-checkout.py
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

    print(f"Enterprise checkout smoke: {email} -> {api_base}")
    failures = 0
    with UserClient(base_url=api_base) as client:
        try:
            client.login(email, password)
            print("  login OK")
        except UserClientError as e:
            print(f"  login FAILED: {e}", file=sys.stderr)
            return 1

        for tier in ("pro_user", "pro_agent", "enterprise"):
            try:
                url = client.start_checkout(tier)
                ok = url.startswith("https://checkout.stripe.com/")
                marker = "OK " if ok else "BAD"
                print(f"  {marker} {tier:12s} -> {url[:80]}{'…' if len(url) > 80 else ''}")
                if not ok:
                    failures += 1
            except UserClientError as e:
                print(f"  ERR {tier:12s} -> {e}", file=sys.stderr)
                failures += 1

    if failures:
        print(f"\n{failures} tier(s) failed.", file=sys.stderr)
        return 1
    print("\nAll 3 paid tiers produce live Stripe Checkout URLs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
