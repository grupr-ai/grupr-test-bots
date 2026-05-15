"""Smoke: External SAT — agent invokes a SAT tool via @grupr Agent Protocol.

Walks the full Slice-3 flow without a real Connect-ready account:

  1. Setup: gtb-newuser -> pro_agent tier + synthetic Connect-ready row
  2. Create an AI agent + mint an agent token via /agent-hub/register
  3. Agent calls /api/v1/hub/sat/invoke with NO grant
                                            -> 403 consent_required
  4. JWT user creates a grant for [agent, retrieve_balance, call_cap=1]
                                            via POST /api/sat/grants
  5. Agent calls /api/v1/hub/sat/invoke   -> gets past gating, Stripe
                                            fails because the Connect
                                            account is synthetic, audit
                                            row written with error_message
  6. Agent calls again                    -> 429 call_cap_exceeded
                                            (call cap of 1 hit)
  7. JWT user revokes the grant via POST /api/sat/grants/:id/revoke
  8. Agent calls again                    -> 403 consent_required again

Cleanup deletes the agent + agent_tokens + grants + Connect row +
restores subscription.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-sat-external.py
"""

from __future__ import annotations

import os
import subprocess
import sys

import httpx
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
    plan = tier if tier != "free" else "free"
    psql(
        f"UPDATE subscriptions SET plan_tier='{tier}', plan='{plan}', "
        f"status='active', expires_at=NULL WHERE user_id='{NEWUSER_ID}';"
    )


def insert_fake_connect_ready() -> None:
    psql(f"DELETE FROM stripe_connect_accounts WHERE user_id = '{NEWUSER_ID}';")
    psql(
        f"INSERT INTO stripe_connect_accounts "
        f"(connect_account_id, user_id, account_type, charges_enabled, payouts_enabled, details_submitted) "
        f"VALUES ('acct_smokeext001', '{NEWUSER_ID}', 'express', true, true, true);"
    )


def cleanup_db(agent_id: str | None) -> None:
    psql(f"DELETE FROM sat_invocations WHERE user_id = '{NEWUSER_ID}';")
    psql(f"DELETE FROM sat_agent_grants WHERE user_id = '{NEWUSER_ID}';")
    psql(f"DELETE FROM stripe_connect_accounts WHERE user_id = '{NEWUSER_ID}';")
    if agent_id:
        psql(f"DELETE FROM ai_agents WHERE agent_id = '{agent_id}';")
    set_tier("free")


def agent_invoke(agent_token: str, payload: dict) -> tuple[int, dict]:
    r = httpx.post(
        "https://api.grupr.ai/api/v1/agent-hub/sat/invoke",
        json=payload,
        headers={
            "Authorization": f"Bearer {agent_token}",
            "Content-Type": "application/json",
        },
        timeout=15.0,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text[:200]}


def main() -> int:
    load_dotenv()
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"External SAT smoke: {email}")
    failures: list[str] = []
    agent_id: str | None = None
    grant_id: str | None = None

    sample = {"tool": "retrieve_balance", "input": {}}

    try:
        with UserClient(base_url="https://api.grupr.ai") as client:
            client.login(email, password)
            print("  login OK")

            # 1. Setup
            set_tier("pro_agent")
            insert_fake_connect_ready()
            print("  setup: tier=pro_agent, Connect synthetic-ready")

            # 2. Create agent + mint token
            agent_id = client.create_agent(
                display_name="ExtSATSmokeBot",
                provider="anthropic",
                model_id="claude-haiku-3-5-20241022",
                system_prompt="smoke",
            )
            print(f"  agent created: {agent_id}")

            _, body = client._request("POST", "/api/v1/agent-hub/register", json={"agent_id": agent_id})
            agent_token = body.get("data", {}).get("token", "")
            if not agent_token.startswith("gat_"):
                failures.append(f"unexpected token shape: {agent_token[:20]}")
                return 1
            print(f"  agent token: {agent_token[:12]}...")

            # ── PHASE 1: no grant -> 403 consent_required ────────────────
            print("\n=== PHASE 1: no grant (expect 403 consent_required) ===")
            status, body = agent_invoke(agent_token, sample)
            print(f"  HTTP {status}, body: {body}")
            code = body.get("errors", [{}])[0].get("code") if isinstance(body, dict) and body.get("errors") else None
            if status != 403 or code != "consent_required":
                failures.append(f"expected 403 consent_required, got {status} {code!r}")

            # ── PHASE 2: create grant with call_cap=1, retry ──────────────
            print("\n=== PHASE 2: grant with call_cap=1 (expect agent past gating) ===")
            _, body = client._request("POST", "/api/sat/grants", json={
                "agent_id": agent_id,
                "tool": "retrieve_balance",
                "monthly_call_cap": 1,
                "note": "smoke grant",
            })
            grant_id = body.get("data", {}).get("grant_id")
            print(f"  grant: {grant_id}")

            status, body = agent_invoke(agent_token, sample)
            print(f"  HTTP {status}, body keys: {list(body.keys())}")
            # Expected: 400 sat_error (Stripe rejects the synthetic acct ID).
            # The KEY assertion is we got PAST the gating — i.e. status is
            # NOT 403/consent_required/connect_not_ready.
            errcode = body.get("errors", [{}])[0].get("code") if isinstance(body, dict) and body.get("errors") else None
            if errcode in ("consent_required", "connect_not_setup", "connect_not_ready", "tier_required"):
                failures.append(f"PHASE 2 blocked by gating: got {errcode!r}")
            else:
                print(f"  passed gating; reached Stripe (error from synthetic acct: {errcode!r}, expected)")

            # ── PHASE 3: call_cap exceeded ───────────────────────────────
            print("\n=== PHASE 3: second call (call_cap=1 already used) -> 429 call_cap_exceeded ===")
            status, body = agent_invoke(agent_token, sample)
            print(f"  HTTP {status}")
            errcode = body.get("errors", [{}])[0].get("code") if isinstance(body, dict) and body.get("errors") else None
            print(f"  error code: {errcode}")
            if status != 429 or errcode != "call_cap_exceeded":
                failures.append(f"expected 429 call_cap_exceeded, got {status} {errcode!r}")

            # ── PHASE 4: revoke grant -> back to consent_required ────────
            print("\n=== PHASE 4: revoke grant -> back to 403 consent_required ===")
            _, body = client._request("POST", f"/api/sat/grants/{grant_id}/revoke", json={})
            print(f"  revoked: {body.get('data', {}).get('revoked')}")
            status, body = agent_invoke(agent_token, sample)
            errcode = body.get("errors", [{}])[0].get("code") if isinstance(body, dict) and body.get("errors") else None
            print(f"  HTTP {status}, error: {errcode}")
            if status != 403 or errcode != "consent_required":
                failures.append(f"after revoke: expected 403 consent_required, got {status} {errcode!r}")

    finally:
        try:
            cleanup_db(agent_id)
            print(f"\n  cleanup: agent + grants + Connect + invocations cleared")
        except Exception as e:
            print(f"  cleanup FAILED: {e}", file=sys.stderr)

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nExternal SAT smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
