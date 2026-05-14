"""Smoke: POST /api/subscription/start-trial creates a Stripe Checkout
session configured for the 7-day Pro User trial.

The backend endpoint is idempotent against active paid subs (409s), but for
a free+active user it always creates a fresh Checkout session — the
Stripe-side dedup happens by customer. We just verify the URL is well-shaped.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-trial-start.py
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

    print(f"Trial-start smoke: {email} -> {api_base}")

    with UserClient(base_url=api_base) as client:
        try:
            client.login(email, password)
            print("  login OK")
        except UserClientError as e:
            print(f"  login FAILED: {e}", file=sys.stderr)
            return 1

        # Call directly via the underlying _request — start-trial isn't on
        # the high-level client yet (it's a launch-sprint addition).
        try:
            status, body = client._request(
                "POST",
                "/api/subscription/start-trial",
                json={
                    "success_url": "https://app.grupr.ai/?trial=started",
                    "cancel_url": "https://app.grupr.ai/welcome?trial=cancelled",
                },
                raise_on_error=False,
            )
        except Exception as e:
            print(f"  request FAILED: {e}", file=sys.stderr)
            return 1

        print(f"  HTTP {status}")
        data = body.get("data") if isinstance(body, dict) else None
        url = (data or {}).get("checkout_url") if data else None
        sess = (data or {}).get("session_id") if data else None

        if status == 200 and url and url.startswith("https://checkout.stripe.com/"):
            print(f"  OK checkout_url -> {url[:80]}...")
            print(f"  session_id     -> {sess}")
            print("\nTrial-start endpoint produces a live Stripe Checkout URL.")
            return 0

        if status == 403:
            print("  403 — user is not email-verified. Check seed data.")
            return 1
        if status == 409:
            print(f"  409 — user already has subscription: {body}")
            return 1

        print(f"  Unexpected response: {body}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
