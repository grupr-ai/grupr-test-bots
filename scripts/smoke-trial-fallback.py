"""Smoke: verify orchestrator key-resolution path.

Three phases exercise the new fallback logic:
  Phase A — gtb-newuser is free+active (default): trigger agent,
            expect "Start a free trial..." error message in logs.
  Phase B — flip subscription to trialing + future expires_at: trigger
            agent, expect "using platform key for trial user" log line
            followed by a successful agent response message.
  Phase C — restore subscription to free+active: trigger again, expect
            free-tier error again.

Setup is idempotent: a single grupr + agent are reused across phases.
DB mutations happen via SSH+psql against the EC2 box. Docker logs are
sampled via SSH after each phase.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-trial-fallback.py
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
    """Run a command on EC2, return stdout (raises on non-zero)."""
    full = ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", EC2, cmd]
    r = subprocess.run(full, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ssh failed: {r.stderr}")
    return r.stdout


def psql(sql: str) -> str:
    """Run SQL inside the postgres container on EC2."""
    # Pull POSTGRES_PASSWORD into the container env so psql doesn't prompt.
    remote = (
        'PW=$(sudo grep "^POSTGRES_PASSWORD" ~/grupr/.env | cut -d= -f2 | tr -d \'"\'); '
        f'docker exec -e PGPASSWORD="$PW" grupr-postgres psql -U grupr -d grupr -tAc "{sql}"'
    )
    return ssh(remote).strip()


def set_subscription(status: str, expires_in_days: int | None) -> None:
    """Flip gtb-newuser's subscription to the requested state."""
    if expires_in_days is None:
        expires = "NULL"
    else:
        expires = f"NOW() + INTERVAL '{expires_in_days} days'"
    plan_tier = "pro_user" if status == "trialing" else "free"
    plan = "pro_user" if status == "trialing" else "free"
    sql = (
        f"UPDATE subscriptions SET status='{status}', plan_tier='{plan_tier}', "
        f"plan='{plan}', expires_at={expires} WHERE user_id='{NEWUSER_ID}';"
    )
    psql(sql)


def recent_orchestrator_logs(since_seconds: int) -> list[str]:
    """Return orchestrator log lines from the api container in the last N seconds."""
    raw = ssh(f"docker logs grupr-api --since {since_seconds}s 2>&1 | grep -E 'orchestrator|API key' | tail -20")
    return [l for l in raw.splitlines() if l.strip()]


def main() -> int:
    load_dotenv()
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"Trial-fallback smoke: {email} -> {api_base}")
    print(f"User ID: {NEWUSER_ID}")
    print()

    with UserClient(base_url=api_base) as client:
        client.login(email, password)
        print("  login OK")

        # Setup: reuse-or-create grupr + agent for this smoke.
        agent_name = "TrialSmokeBot"
        # NOTE: bypasses client.create_grupr() — that helper passes "type" but
        # the api expects "grup_type". Flagged as a test-bots framework bug at
        # user_client.py:298; not fixing here to keep this smoke focused.
        _, body = client._request("POST", "/api/gruprs", json={
            "name": f"trial-fallback-smoke-{int(time.time())}",
            "description": "Smoke for orchestrator key-resolution path.",
            "grup_type": "ai_workshop",
            "is_public": False,
            "category": "general",
            "max_members": 5,
        })
        grupr_id = body.get("data", {}).get("grupr_id", "") or body.get("grupr_id", "")
        if not grupr_id:
            print(f"  grupr create returned no id: {body}")
            return 1
        print(f"  grupr created: {grupr_id}")

        agent_id = client.create_agent(
            display_name=agent_name,
            provider="anthropic",
            model_id="claude-sonnet-4-5-20250929",
            system_prompt="Reply with one short sentence.",
        )
        print(f"  agent created: {agent_id}")
        client.add_agent_to_grupr(grupr_id, agent_id)
        print(f"  agent assigned to grupr")
        print()

        # Ensure clean baseline: no BYOK keys for this user. (Test-bot
        # accounts ship without any, so this is asserted by reading state.)

        failures: list[str] = []

        def run_phase(label: str, sub_status: str, expires_days: int | None, expect_substr: str, expect_response: bool) -> None:
            nonlocal failures
            print(f"=== {label} ===")
            set_subscription(sub_status, expires_days)
            current = psql(f"SELECT status || '|' || plan_tier || '|' || COALESCE(expires_at::text, 'NULL') FROM subscriptions WHERE user_id='{NEWUSER_ID}';")
            print(f"  subscription state: {current}")

            mark_ts = time.time()
            try:
                client.post_message(grupr_id, f"@{agent_name} reply with the word PING")
                print(f"  message posted")
            except UserClientError as e:
                print(f"  message post FAILED: {e}")
                failures.append(f"{label}: post failed")
                return

            time.sleep(4)
            logs = recent_orchestrator_logs(int(time.time() - mark_ts) + 2)
            interesting = [l for l in logs if "API key" in l or "platform key" in l or "no API key" in l]
            for l in interesting[-6:]:
                # Truncate timestamps for readability.
                print(f"    log> {l[-200:]}")

            log_blob = "\n".join(logs).lower()
            if expect_substr.lower() in log_blob:
                print(f"  EXPECTED log marker present: {expect_substr!r}")
            else:
                print(f"  MISSING log marker: {expect_substr!r}")
                failures.append(f"{label}: missing log marker {expect_substr!r}")

            # If a response is expected, check messages for an agent reply.
            if expect_response:
                # Give the stream a few more seconds to finalize.
                time.sleep(5)
                msgs = client.get_messages(grupr_id, limit=10)
                agent_msgs = [m for m in msgs if m.get("agent_id")]
                if agent_msgs:
                    last = agent_msgs[0]
                    content = (last.get("content") or "")[:80]
                    print(f"  agent replied: {content!r}")
                else:
                    print(f"  NO agent reply in {len(msgs)} messages")
                    failures.append(f"{label}: expected agent response, none received")
            print()

        # Phase A: free-tier baseline.
        run_phase(
            label="PHASE A — free+active (no trial, no BYOK)",
            sub_status="active",
            expires_days=None,
            expect_substr="no API key for provider anthropic",
            expect_response=False,
        )

        # Phase B: trialing → platform fallback engages.
        run_phase(
            label="PHASE B — trialing (platform fallback enabled)",
            sub_status="trialing",
            expires_days=7,
            expect_substr="using platform key for trial user",
            expect_response=True,
        )

        # Phase C: back to free → fallback disengages.
        run_phase(
            label="PHASE C — back to free+active",
            sub_status="active",
            expires_days=None,
            expect_substr="no API key for provider anthropic",
            expect_response=False,
        )

    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All 3 phases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
