"""Smoke: Smart-tier wiring through the create-review BFF.

Verifies that when the form submits with tier='smart':
  - the BFF accepts the tier field
  - reviewer agents are created with config.tools = [web_search def]
  - Quick-tier comparison: same request with tier='quick' produces
    agents with no tools field

Doesn't run the actual review to completion (that would burn LLM
tokens, and Anthropic web_search costs more per call). The wiring
smoke is enough; behavior in production will be visible via the
verdict_ready event payloads showing search citations from Claude.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-smart-tier-wiring.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import httpx
from dotenv import load_dotenv

from lib.user_client import UserClient


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


def set_subscription(status: str, days: int | None) -> None:
    expires = "NULL" if days is None else f"NOW() + INTERVAL '{days} days'"
    plan = "pro_user" if status == "trialing" else "free"
    psql(
        f"UPDATE subscriptions SET status='{status}', plan_tier='{plan}', "
        f"plan='{plan}', expires_at={expires} WHERE user_id='{NEWUSER_ID}';"
    )


# Minimum reviewer payload to exercise both branches.
SAMPLE_REVIEWERS_WITH_TOOLS = [
    {
        "role": "architect",
        "displayName": "SmartSmokeArchitect",
        "systemPrompt": "Smoke",
        "provider": "anthropic",
        "modelId": "claude-opus-4-7",
        "smartTools": [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
        ],
    },
    {
        "role": "security",
        "displayName": "SmartSmokeSecurity",
        "systemPrompt": "Smoke",
        "provider": "openai",
        "modelId": "gpt-4o",
        # No smartTools — OpenAI provider doesn't take tool defs in this shape
    },
]


def submit_review(token: str, tier: str) -> tuple[int, dict]:
    body = {
        "code": "def hello(): return 1",
        "context": f"smart-tier wiring smoke ({tier})",
        "languageHint": "python",
        "tier": tier,
        "reviewers": SAMPLE_REVIEWERS_WITH_TOOLS,
    }
    r = httpx.post(
        f"{APP_BASE}/api/code-review/create",
        json=body,
        cookies={"grupr_access": token},
        timeout=30.0,
        follow_redirects=False,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text[:200]}


def get_agent_configs(grupr_id: str) -> list[dict]:
    out = psql(
        f"SELECT json_agg(json_build_object('display_name', display_name, 'provider', provider, 'config', config)) "
        f"FROM ai_agents WHERE creator_id = '{NEWUSER_ID}' AND agent_id IN ("
        f"  SELECT agent_id FROM grup_agents WHERE grupr_id = '{grupr_id}'"
        f");"
    )
    if not out or out == "null":
        return []
    try:
        return json.loads(out)
    except Exception:
        return []


def main() -> int:
    load_dotenv()
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"Smart-tier wiring smoke: {email} -> {APP_BASE}")
    failures: list[str] = []
    grupr_ids_to_clean: list[str] = []

    try:
        # Get access token for cookie auth against the BFF.
        with UserClient(base_url="https://api.grupr.ai") as client:
            client.login(email, password)
            token = client.access_token
        if not token:
            print("  no access_token from login", file=sys.stderr)
            return 1
        print("  login OK")

        # The BFF's free-tier 1/day cap would block the second submit;
        # flip to trialing so both succeed (trial cap is 5/day).
        set_subscription("trialing", 7)
        print("  subscription: trialing|pro_user")

        # ── PHASE 1: tier='quick' ──────────────────────────────────────
        print("\n=== PHASE 1: tier=quick (tools should NOT be persisted) ===")
        time.sleep(2)
        status, body = submit_review(token, "quick")
        print(f"  HTTP {status}, body keys: {list(body.keys())}")
        if status != 200 and status != 201:
            failures.append(f"quick submit failed: {status} {body}")
        else:
            quick_grupr_id = body.get("gruprId") or body.get("grupr_id")
            if quick_grupr_id:
                grupr_ids_to_clean.append(quick_grupr_id)
                agents = get_agent_configs(quick_grupr_id)
                print(f"  agents: {len(agents)}")
                for a in agents:
                    cfg = a.get("config") or {}
                    tools = cfg.get("tools")
                    marker = "TOOLS-PRESENT" if tools else "no-tools"
                    print(f"    {a.get('display_name')} ({a.get('provider')}): {marker}")
                    if tools:
                        failures.append(f"quick: {a.get('display_name')} unexpectedly has tools")

        # ── PHASE 2: tier='smart' ──────────────────────────────────────
        print("\n=== PHASE 2: tier=smart (tools SHOULD be on Anthropic-backed agent) ===")
        time.sleep(2)
        status, body = submit_review(token, "smart")
        print(f"  HTTP {status}, body keys: {list(body.keys())}")
        if status != 200 and status != 201:
            failures.append(f"smart submit failed: {status} {body}")
        else:
            smart_grupr_id = body.get("gruprId") or body.get("grupr_id")
            if smart_grupr_id:
                grupr_ids_to_clean.append(smart_grupr_id)
                agents = get_agent_configs(smart_grupr_id)
                print(f"  agents: {len(agents)}")
                for a in agents:
                    cfg = a.get("config") or {}
                    tools = cfg.get("tools")
                    marker = f"TOOLS={tools[0].get('type') if tools and len(tools)>0 else 'none'}" if isinstance(tools, list) else "no-tools"
                    print(f"    {a.get('display_name')} ({a.get('provider')}): {marker}")

                    if a.get("provider") == "anthropic":
                        if not tools or not isinstance(tools, list) or len(tools) == 0:
                            failures.append(f"smart: anthropic reviewer {a.get('display_name')} missing tools")
                        elif tools[0].get("type") != "web_search_20250305":
                            failures.append(f"smart: anthropic reviewer has wrong tool type: {tools[0].get('type')}")
                    else:
                        # Non-anthropic reviewers: BFF passes nothing (reviewer
                        # had no smartTools), so tools key should be absent.
                        if tools:
                            failures.append(f"smart: non-anthropic reviewer {a.get('display_name')} unexpectedly has tools")

    finally:
        # Cleanup: delete the test gruprs + their agents (FK cascade).
        for gid in grupr_ids_to_clean:
            try:
                psql(f"DELETE FROM gruprs WHERE grupr_id = '{gid}';")
            except Exception as e:
                print(f"  cleanup grupr {gid} FAILED: {e}", file=sys.stderr)
        try:
            psql(f"DELETE FROM ai_agents WHERE creator_id = '{NEWUSER_ID}' AND display_name LIKE 'SmartSmoke%';")
            print(f"  cleanup: removed test gruprs + agents")
        except Exception as e:
            print(f"  cleanup agents FAILED: {e}", file=sys.stderr)
        try:
            set_subscription("active", None)
        except Exception:
            pass

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nSmart-tier wiring smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
