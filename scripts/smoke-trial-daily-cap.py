"""Smoke: BFF-side per-user trial daily cap on Code Review creation.

Verifies that a trialing user with 5 today's code-category gruprs gets a
402 trial_daily_limit envelope (which the <UpgradeModal /> reads to render
reset_at + recommended_tier).

The cap lives in grupr-web/app/api/code-review/create/route.ts, so the
smoke targets the public app endpoint at app.grupr.ai with a cookie-shaped
auth header (the BFF reads grupr_access from cookies, not Bearer).

Setup is idempotent: flips subscription state via psql, inserts 5 dummy
gruprs, makes the assertion, cleans up. Reverts subscription in a finally.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-trial-daily-cap.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
from dotenv import load_dotenv

from lib.user_client import UserClient, UserClientError


SSH_KEY = "G:/My Drive/BB/Dev/ClawdBot/kalshi-bot-key.pem"
EC2 = "ubuntu@18.224.174.100"
NEWUSER_ID = "569dbd30-3e63-47bc-bfb3-422a7a1b947a"
APP_BASE = "https://app.grupr.ai"


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


def set_subscription(status: str, expires_in_days: int | None) -> None:
    expires = "NULL" if expires_in_days is None else f"NOW() + INTERVAL '{expires_in_days} days'"
    plan_tier = "pro_user" if status == "trialing" else "free"
    plan = "pro_user" if status == "trialing" else "free"
    psql(
        f"UPDATE subscriptions SET status='{status}', plan_tier='{plan_tier}', "
        f"plan='{plan}', expires_at={expires} WHERE user_id='{NEWUSER_ID}';"
    )


def insert_dummy_gruprs(count: int) -> list[str]:
    """Insert N gruprs as gtb-newuser with category='code', created_at=NOW(),
    AND a matching grupr_members row so /api/gruprs/my returns them. Without
    the membership row the gruprs are orphaned to the user's view.
    Returns the grupr_ids so cleanup can remove them.
    """
    ids: list[str] = []
    ts = int(time.time())
    for i in range(count):
        # Note: psql -tAc returns the RETURNING tuple on line 0; tag info
        # ("INSERT 0 1") follows on line 1. Split + take first.
        out = psql(
            f"INSERT INTO gruprs (creator_id, name, description, grup_type, category, is_public, max_members) "
            f"VALUES ('{NEWUSER_ID}', 'trial-cap-smoke-{ts}-{i}', "
            f"'smoke fixture', 'ai_workshop', 'code', false, 5) RETURNING grupr_id;"
        )
        gid = out.split("\n")[0].strip()
        ids.append(gid)
        # Add gtb-newuser as owner of the dummy grupr so /api/gruprs/my picks
        # it up — that endpoint filters on membership, not creator_id.
        psql(
            f"INSERT INTO grupr_members (grupr_id, user_id, role) "
            f"VALUES ('{gid}', '{NEWUSER_ID}', 'owner');"
        )
    return ids


def delete_gruprs(ids: list[str]) -> None:
    if not ids:
        return
    quoted = ", ".join(f"'{x}'" for x in ids)
    # FK from grupr_members to gruprs cascades, so deleting the grupr deletes
    # its membership rows too. No need to delete from grupr_members first.
    psql(f"DELETE FROM gruprs WHERE grupr_id IN ({quoted});")


def main() -> int:
    load_dotenv()
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"Trial daily-cap smoke: {email} -> {APP_BASE}")

    dummy_ids: list[str] = []
    try:
        # 1. Get an access token (via api.grupr.ai), then use it as a cookie
        #    when hitting the BFF on app.grupr.ai.
        with UserClient(base_url="https://api.grupr.ai") as client:
            client.login(email, password)
            token = client.access_token
        if not token:
            print("  no access_token from login", file=sys.stderr)
            return 1
        print("  login OK")

        # 2. Flip subscription to trialing + 7d
        set_subscription("trialing", 7)
        state = psql(
            f"SELECT status || '|' || plan_tier FROM subscriptions WHERE user_id='{NEWUSER_ID}';"
        )
        print(f"  subscription: {state}")

        # 3. Seed 5 today's code gruprs (the cap is >=5 → block on 6th call)
        dummy_ids = insert_dummy_gruprs(5)
        print(f"  inserted 5 dummy code-category gruprs")

        # 4. Call BFF on app.grupr.ai with cookie auth. Body has the minimum
        #    valid shape (code + at least one reviewer); cap check fires before
        #    any reviewer is touched, so reviewer config doesn't matter here.
        body = {
            "code": "function test() { return 1 + 1; }",
            "context": "smoke",
            "reviewers": [
                {
                    "role": "architect",
                    "displayName": "Smoke Architect",
                    "systemPrompt": "x",
                    "provider": "anthropic",
                    "modelId": "claude-sonnet-4-5-20250929",
                }
            ],
        }
        r = httpx.post(
            f"{APP_BASE}/api/code-review/create",
            json=body,
            cookies={"grupr_access": token},
            timeout=15.0,
            follow_redirects=False,
        )
        print(f"  HTTP {r.status_code}")
        try:
            envelope = r.json()
        except Exception:
            envelope = {"_raw": r.text[:200]}
        print(f"  envelope: {envelope}")

        # 5. Assertions
        if r.status_code != 402:
            print(f"  EXPECTED 402, got {r.status_code}", file=sys.stderr)
            return 1
        if envelope.get("code") != "trial_daily_limit":
            print(f"  EXPECTED code=trial_daily_limit, got {envelope.get('code')!r}", file=sys.stderr)
            return 1
        if not envelope.get("reset_at"):
            print(f"  EXPECTED reset_at to be set", file=sys.stderr)
            return 1
        if envelope.get("recommended_tier") != "pro_user":
            print(f"  EXPECTED recommended_tier=pro_user, got {envelope.get('recommended_tier')!r}", file=sys.stderr)
            return 1

        print("\n  All assertions passed:")
        print(f"    status:            402 OK")
        print(f"    code:              trial_daily_limit OK")
        print(f"    reset_at:          {envelope['reset_at']} OK")
        print(f"    recommended_tier:  {envelope['recommended_tier']} OK")
        return 0

    finally:
        try:
            delete_gruprs(dummy_ids)
            if dummy_ids:
                print(f"  cleanup: deleted {len(dummy_ids)} dummy gruprs")
        except Exception as e:
            print(f"  cleanup gruprs FAILED: {e}", file=sys.stderr)
        try:
            set_subscription("active", None)
            final = psql(
                f"SELECT status || '|' || plan_tier FROM subscriptions WHERE user_id='{NEWUSER_ID}';"
            )
            print(f"  cleanup: subscription -> {final}")
        except Exception as e:
            print(f"  cleanup subscription FAILED: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
