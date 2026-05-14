"""Smoke: verify the new Groq provider adapter is wired into the gateway
registry and that a trialing user can complete a streaming request through
the platform Groq key.

Runs in one shot:
  1. Flip gtb-newuser to trialing +7d.
  2. Create a grupr + agent with provider="groq", model="llama-3.3-70b-versatile".
  3. Post a message @-mentioning the agent.
  4. Assert orchestrator log shows "using platform key for trial user
     (provider=groq)" and an agent reply lands.
  5. Revert subscription to free+active.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-groq-provider.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

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


def set_subscription(status: str, expires_in_days: int | None) -> None:
    expires = "NULL" if expires_in_days is None else f"NOW() + INTERVAL '{expires_in_days} days'"
    plan_tier = "pro_user" if status == "trialing" else "free"
    plan = "pro_user" if status == "trialing" else "free"
    psql(
        f"UPDATE subscriptions SET status='{status}', plan_tier='{plan_tier}', "
        f"plan='{plan}', expires_at={expires} WHERE user_id='{NEWUSER_ID}';"
    )


def main() -> int:
    load_dotenv()
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"Groq provider smoke: {email} -> {api_base}")
    failures: list[str] = []

    try:
        with UserClient(base_url=api_base) as client:
            client.login(email, password)
            print("  login OK")

            # Ensure trial is active.
            set_subscription("trialing", 7)
            state = psql(
                f"SELECT status || '|' || plan_tier || '|' || COALESCE(expires_at::text, 'NULL') "
                f"FROM subscriptions WHERE user_id='{NEWUSER_ID}';"
            )
            print(f"  subscription: {state}")

            # Create grupr (bypass user_client.create_grupr — see grup_type bug).
            _, body = client._request("POST", "/api/gruprs", json={
                "name": f"groq-smoke-{int(time.time())}",
                "description": "Smoke for Groq provider via platform-key trial fallback.",
                "grup_type": "ai_workshop",
                "is_public": False,
                "category": "general",
                "max_members": 5,
            })
            grupr_id = body.get("data", {}).get("grupr_id", "") or body.get("grupr_id", "")
            print(f"  grupr: {grupr_id}")

            # Groq agent.
            agent_name = "GroqSmokeBot"
            agent_id = client.create_agent(
                display_name=agent_name,
                provider="groq",
                model_id="llama-3.3-70b-versatile",
                system_prompt="Reply with one short sentence.",
            )
            client.add_agent_to_grupr(grupr_id, agent_id)
            print(f"  agent: {agent_id} (groq/llama-3.3-70b-versatile)")

            # Trigger the agent.
            mark_ts = time.time()
            client.post_message(grupr_id, f"@{agent_name} reply with the word PONG")
            print(f"  message posted, waiting for orchestrator + Groq stream...")
            time.sleep(8)

            # Check orchestrator logs.
            since_seconds = int(time.time() - mark_ts) + 2
            log_blob = ssh(
                f"docker logs grupr-api --since {since_seconds}s 2>&1 | "
                f"grep -E 'orchestrator|groq|API key' | tail -10"
            )
            print(f"  orchestrator log lines:")
            for line in log_blob.splitlines():
                print(f"    {line[-200:]}")

            if "using platform key for trial user" in log_blob and "provider=groq" in log_blob:
                print(f"  EXPECTED: platform-key fallback engaged for Groq")
            else:
                failures.append("log marker 'using platform key ... provider=groq' not found")

            # Check for an agent reply.
            time.sleep(3)
            msgs = client.get_messages(grupr_id, limit=10)
            agent_msgs = [m for m in msgs if m.get("agent_id")]
            if agent_msgs:
                content = (agent_msgs[0].get("content") or "")[:120]
                print(f"  agent replied: {content!r}")
            else:
                print(f"  NO agent reply found in {len(msgs)} messages")
                failures.append("expected Groq agent reply, none received")

    finally:
        # Always revert subscription state, even on failure.
        try:
            set_subscription("active", None)
            final = psql(
                f"SELECT status || '|' || plan_tier FROM subscriptions WHERE user_id='{NEWUSER_ID}';"
            )
            print(f"  cleanup: subscription reverted to {final}")
        except Exception as e:
            print(f"  cleanup FAILED: {e}", file=sys.stderr)

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nGroq provider smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
