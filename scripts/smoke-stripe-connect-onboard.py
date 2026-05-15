"""Smoke: Stripe Connect onboarding API surface.

Validates the SAT-precursor endpoints:
  GET  /api/stripe/connect/status   -> connected=false initially
  POST /api/stripe/connect/onboard  -> returns Stripe AccountLink URL
  GET  /api/stripe/connect/status   -> connected=true, ready=false
                                        (real onboarding hasn't happened)

Doesn't complete the actual Stripe-hosted onboarding form (that requires
human interaction with Stripe's UI). The smoke just confirms that:
  - the BFF endpoints respond as expected
  - a row lands in stripe_connect_accounts on /onboard
  - the returned URL is a real Stripe-hosted onboarding link

Cleanup removes both the DB row + the Stripe account (best-effort delete
via API; Stripe occasionally rejects deletion of in-onboarding accounts,
so a 4xx there is logged but doesn't fail the smoke).

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-stripe-connect-onboard.py
"""

from __future__ import annotations

import os
import subprocess
import sys

from dotenv import load_dotenv

from lib.user_client import UserClient


SSH_KEY = "G:/My Drive/BB/Dev/ClawdBot/kalshi-bot-key.pem"
EC2 = "ubuntu@18.224.174.100"
NEWUSER_ID = "569dbd30-3e63-47bc-bfb3-422a7a1b947a"


def ssh(cmd: str) -> str:
    full = ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", EC2, cmd]
    r = subprocess.run(full, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ssh failed: {r.stderr}")
    return r.stdout


def psql(sql: str) -> str:
    remote = (
        'PW=$(sudo grep "^POSTGRES_PASSWORD" ~/grupr/.env | cut -d= -f2 | tr -d \'"\'); '
        f'docker exec -e PGPASSWORD="$PW" grupr-postgres psql -U grupr -d grupr -tAc "{sql}"'
    )
    return ssh(remote).strip()


def main() -> int:
    load_dotenv()
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"Stripe Connect onboarding smoke: {email}")
    failures: list[str] = []
    account_id: str | None = None

    try:
        with UserClient(base_url=api_base) as client:
            client.login(email, password)
            print("  login OK")

            # Cleanup any prior smoke leftovers.
            psql(f"DELETE FROM stripe_connect_accounts WHERE user_id = '{NEWUSER_ID}';")

            # 1. Status — should be connected=false
            _, body = client._request("GET", "/api/stripe/connect/status")
            d = body.get("data", body)
            print(f"  initial status: connected={d.get('connected')}, ready={d.get('ready')}")
            if d.get("connected") is True:
                failures.append("expected connected=false initially")

            # 2. Onboard — should return a Stripe URL
            _, body = client._request("POST", "/api/stripe/connect/onboard", json={
                "return_url": "https://app.grupr.ai/settings?connect=done",
                "refresh_url": "https://app.grupr.ai/settings?connect=refresh",
            })
            d = body.get("data", body)
            account_id = d.get("connect_account_id")
            url = d.get("onboarding_url", "")
            print(f"  onboard: account_id={account_id}")
            print(f"  onboarding_url: {url[:80]}...")
            if not account_id or not account_id.startswith("acct_"):
                failures.append(f"expected acct_... account_id, got {account_id!r}")
            if not url.startswith("https://connect.stripe.com/setup/") and not url.startswith("https://connect.stripe.com/express/"):
                failures.append(f"unexpected onboarding URL shape: {url[:100]}")

            # 3. Status — should now be connected=true, ready=false (not onboarded yet)
            _, body = client._request("GET", "/api/stripe/connect/status")
            d = body.get("data", body)
            print(f"  post-onboard status: connected={d.get('connected')}, ready={d.get('ready')}, charges={d.get('charges_enabled')}, payouts={d.get('payouts_enabled')}")
            if d.get("connected") is not True:
                failures.append("expected connected=true after onboard")
            if d.get("ready") is True:
                failures.append("expected ready=false before real onboarding completes")
            if d.get("connect_account_id") != account_id:
                failures.append("account_id mismatch between onboard + status")

            # 4. Calling onboard again should reuse the account, not create a second.
            _, body = client._request("POST", "/api/stripe/connect/onboard", json={})
            d = body.get("data", body)
            second_id = d.get("connect_account_id")
            print(f"  onboard call 2: account_id={second_id} (should match)")
            if second_id != account_id:
                failures.append(f"onboard not idempotent: first={account_id} second={second_id}")

            # 5. Confirm DB row
            row = psql(
                f"SELECT connect_account_id || '|' || account_type || '|' || charges_enabled || '|' || payouts_enabled "
                f"FROM stripe_connect_accounts WHERE user_id = '{NEWUSER_ID}';"
            )
            print(f"  DB row: {row}")
            if account_id not in row:
                failures.append(f"DB row missing or doesn't match account_id")

    finally:
        # Cleanup the DB row. The Stripe account itself can be deleted via
        # the API but Stripe sometimes rejects deletion of accounts that
        # have started onboarding flow — that's fine for a test bot.
        try:
            psql(f"DELETE FROM stripe_connect_accounts WHERE user_id = '{NEWUSER_ID}';")
            print(f"  cleanup: removed DB row")
        except Exception as e:
            print(f"  cleanup DB FAILED: {e}", file=sys.stderr)
        if account_id:
            print(f"  note: leaving Stripe account {account_id} in Stripe dashboard")
            print(f"        (delete manually via Stripe dashboard if needed)")

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nStripe Connect onboarding smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
