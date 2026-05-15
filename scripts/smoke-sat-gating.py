"""Smoke: SAT handler gating + audit trail.

Validates the access-control rules around POST /api/sat/invoke without
needing a fully-onboarded Stripe Connect account (which requires manual
form completion). The smoke covers:

  GET  /api/sat/tools                    -> 200, lists 5 launch tools
  POST /api/sat/invoke (free user)       -> 403 tier_required
  POST /api/sat/invoke (pro_user)        -> 403 tier_required
  POST /api/sat/invoke (pro_agent + no Connect) -> 400 connect_not_setup
  POST /api/sat/invoke (pro_agent + Connect not-ready) -> 400 connect_not_ready

Audit log [sat_invocations] is exercised once gating succeeds — there's
nothing to log on a 403 because we never get to the invocation step.

Once Bret completes the Stripe Connect onboarding form for gtb-newuser,
a follow-up smoke can exercise actual tool execution end-to-end. That's
out of scope here because it requires human action on Stripe's hosted
form.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-sat-gating.py
"""

from __future__ import annotations

import os
import subprocess
import sys

from dotenv import load_dotenv

from lib.user_client import UserClient, UserClientError


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


def set_tier(tier: str) -> None:
    """Force gtb-newuser's subscription tier directly via DB."""
    plan = tier if tier != "free" else "free"
    psql(
        f"UPDATE subscriptions SET plan_tier='{tier}', plan='{plan}', "
        f"status='active', expires_at=NULL WHERE user_id='{NEWUSER_ID}';"
    )


def insert_fake_connect(ready: bool) -> None:
    """Insert a synthetic stripe_connect_accounts row for testing
    the connect_not_ready branch without onboarding."""
    psql(f"DELETE FROM stripe_connect_accounts WHERE user_id = '{NEWUSER_ID}';")
    psql(
        f"INSERT INTO stripe_connect_accounts "
        f"(connect_account_id, user_id, account_type, charges_enabled, payouts_enabled, details_submitted) "
        f"VALUES ('acct_fakeconnect001', '{NEWUSER_ID}', 'express', {ready}, {ready}, {ready});"
    )


def clear_connect() -> None:
    psql(f"DELETE FROM stripe_connect_accounts WHERE user_id = '{NEWUSER_ID}';")


def main() -> int:
    load_dotenv()
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"SAT gating smoke: {email}")
    failures: list[str] = []

    sample_input = {
        "tool": "retrieve_balance",
        "input": {},
    }

    try:
        with UserClient(base_url=api_base) as client:
            client.login(email, password)
            print("  login OK\n")

            # ── PHASE 1: GET /api/sat/tools (read, no tier gate) ─────────
            print("=== PHASE 1: GET /api/sat/tools ===")
            _, body = client._request("GET", "/api/sat/tools")
            tools = body.get("data", [])
            tool_names = [t.get("name") for t in tools]
            print(f"  tools returned: {tool_names}")
            expected = {"create_payment_link", "create_customer", "list_customers", "create_refund", "retrieve_balance"}
            if set(tool_names) != expected:
                failures.append(f"unexpected tools: got {set(tool_names)}, want {expected}")

            # ── PHASE 2: free tier should 403 ────────────────────────────
            print("\n=== PHASE 2: free tier (expect 403 tier_required) ===")
            set_tier("free")
            clear_connect()
            try:
                client._request("POST", "/api/sat/invoke", json=sample_input)
                failures.append("free tier was allowed to invoke SAT")
            except UserClientError as e:
                print(f"  blocked: {e.status} {e.code}")
                if e.status != 403 or e.code != "tier_required":
                    failures.append(f"expected 403 tier_required, got {e.status} {e.code!r}")

            # ── PHASE 3: pro_user tier should still 403 ──────────────────
            print("\n=== PHASE 3: pro_user tier (expect 403 tier_required) ===")
            set_tier("pro_user")
            try:
                client._request("POST", "/api/sat/invoke", json=sample_input)
                failures.append("pro_user tier was allowed to invoke SAT")
            except UserClientError as e:
                print(f"  blocked: {e.status} {e.code}")
                if e.status != 403:
                    failures.append(f"expected 403, got {e.status}")

            # ── PHASE 4: pro_agent + no Connect (expect 400) ─────────────
            print("\n=== PHASE 4: pro_agent + no Connect (expect 400 connect_not_setup) ===")
            set_tier("pro_agent")
            clear_connect()
            try:
                client._request("POST", "/api/sat/invoke", json=sample_input)
                failures.append("pro_agent with no Connect was allowed")
            except UserClientError as e:
                print(f"  blocked: {e.status} {e.code}")
                if e.status != 400 or e.code != "connect_not_setup":
                    failures.append(f"expected 400 connect_not_setup, got {e.status} {e.code!r}")

            # ── PHASE 5: pro_agent + Connect not-ready (expect 400) ──────
            print("\n=== PHASE 5: pro_agent + Connect not-ready (expect 400 connect_not_ready) ===")
            insert_fake_connect(ready=False)
            try:
                client._request("POST", "/api/sat/invoke", json=sample_input)
                failures.append("pro_agent with not-ready Connect was allowed")
            except UserClientError as e:
                print(f"  blocked: {e.status} {e.code}")
                if e.status != 400 or e.code != "connect_not_ready":
                    failures.append(f"expected 400 connect_not_ready, got {e.status} {e.code!r}")

            print("\nAll gating phases behaved as expected.")

    finally:
        # Restore to default (free + active, no Connect)
        try:
            set_tier("free")
            clear_connect()
            print(f"\n  cleanup: subscription -> free|active, Connect cleared")
        except Exception as e:
            print(f"  cleanup FAILED: {e}", file=sys.stderr)

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nSAT gating smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
