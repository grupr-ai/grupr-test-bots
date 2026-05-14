"""Smoke: orchestrator-side global trial circuit breaker.

The breaker counts platform-key dispatches per UTC month in Redis under
trial:platform_usage:YYYYMM, and blocks further dispatches when the count
hits cfg.TrialGlobalMonthlyCap (default 25000 from env).

Smoke approach: write a sentinel value into the Redis counter to push it
past the cap, trigger a platform-key dispatch as a trialing user, observe
the "global trial cap reached" log line on the api side, then restore the
counter to its prior value.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-global-trial-breaker.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from lib.user_client import UserClient


SSH_KEY = "G:/My Drive/BB/Dev/ClawdBot/kalshi-bot-key.pem"
EC2 = "ubuntu@18.224.174.100"
NEWUSER_ID = "569dbd30-3e63-47bc-bfb3-422a7a1b947a"
DEFAULT_CAP = 25000


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


def redis(cmd: str) -> str:
    """Run a single redis-cli command inside the grupr-redis container."""
    return ssh(f"docker exec grupr-redis redis-cli {cmd}").strip()


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

    month_key = "trial:platform_usage:" + datetime.now(timezone.utc).strftime("%Y%m")
    print(f"Global breaker smoke: counter key = {month_key}")

    prior = redis(f"GET {month_key}")
    prior_int = int(prior) if prior and prior.isdigit() else 0
    print(f"  prior counter value: {prior_int}")

    failures: list[str] = []
    sentinel = DEFAULT_CAP  # set to the cap exactly; breaker fires at >= cap

    try:
        # 1. Push counter to the cap
        redis(f"SET {month_key} {sentinel}")
        confirmed = redis(f"GET {month_key}")
        print(f"  counter set to: {confirmed}")

        # 2. Flip user to trialing so the orchestrator tries the platform-key path
        set_subscription("trialing", 7)
        print("  gtb-newuser -> trialing|pro_user")

        # 3. Trigger a platform-key dispatch
        with UserClient(base_url=api_base) as client:
            client.login(email, password)
            _, body = client._request(
                "POST", "/api/gruprs", json={
                    "name": f"breaker-smoke-{int(time.time())}",
                    "description": "smoke",
                    "grup_type": "ai_workshop",
                    "is_public": False,
                    "category": "general",
                    "max_members": 5,
                },
                raise_on_error=True,
            )
            grupr_id = body.get("data", {}).get("grupr_id") or body.get("grupr_id")
            agent_name = "BreakerSmokeBot"
            agent_id = client.create_agent(
                display_name=agent_name,
                provider="anthropic",
                model_id="claude-sonnet-4-5-20250929",
                system_prompt="Reply with one word.",
            )
            client.add_agent_to_grupr(grupr_id, agent_id)

            mark_ts = time.time()
            client.post_message(grupr_id, f"@{agent_name} reply with PING")
            print(f"  dispatch triggered, waiting for orchestrator log...")
            time.sleep(4)

        # 4. Look for the breaker-blocked log line
        since_seconds = int(time.time() - mark_ts) + 2
        logs = ssh(
            f"docker logs grupr-api --since {since_seconds}s 2>&1 | "
            f"grep -E 'orchestrator|global trial cap' | tail -10"
        )
        print(f"  orchestrator log lines:")
        for line in logs.splitlines():
            print(f"    {line[-200:]}")

        if "global trial cap reached" in logs:
            print(f"\n  Breaker fired: 'global trial cap reached' log present.")
        else:
            failures.append("expected 'global trial cap reached' log line, not found")

    finally:
        # Restore Redis counter to prior value (or DEL if it was unset)
        try:
            if prior_int > 0:
                redis(f"SET {month_key} {prior_int}")
            else:
                redis(f"DEL {month_key}")
            final = redis(f"GET {month_key}")
            print(f"  cleanup: counter -> {final or '(unset)'}")
        except Exception as e:
            print(f"  cleanup Redis FAILED: {e}", file=sys.stderr)
        try:
            set_subscription("active", None)
            print(f"  cleanup: subscription reverted to active|free")
        except Exception as e:
            print(f"  cleanup subscription FAILED: {e}", file=sys.stderr)

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nGlobal trial breaker smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
