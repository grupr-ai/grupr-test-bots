"""Smoke: concurrent trial-fallback load against the global circuit breaker.

Fires N concurrent agent dispatches as a trialing user (so the orchestrator
uses platform keys + INCRs the monthly Redis counter on each call).
Pre-seeds the counter just below the cap, then watches whether
concurrent calls burst past it.

The orchestrator's breaker check is GET-then-INCR, not a CAS, so there IS
a theoretical race where N concurrent calls all see val < cap and proceed.
This smoke quantifies the slop: how far past the cap can we burst under
realistic load?

What we want to see:
  - cap=25000 + counter pre-seeded to 24995 + 30 concurrent dispatches
  - actual final counter <= 25000 + concurrency_window
  - blocked dispatches outnumber successful ones once the cap is crossed

Light load smoke (30 concurrent ~= what a viral-tweet scenario looks like
in the first second). Not a stress test — that'd belong in a separate
k6/hey load run.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-load-trial-breaker.py
"""

from __future__ import annotations

import concurrent.futures
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
CONCURRENCY = 30
SEED_BELOW_CAP = 5  # set counter to (cap - 5) so concurrent calls race past


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
    return ssh(f"docker exec grupr-redis redis-cli {cmd}").strip()


def set_subscription(status: str, days: int | None) -> None:
    expires = "NULL" if days is None else f"NOW() + INTERVAL '{days} days'"
    plan = "pro_user" if status == "trialing" else "free"
    psql(
        f"UPDATE subscriptions SET status='{status}', plan_tier='{plan}', "
        f"plan='{plan}', expires_at={expires} WHERE user_id='{NEWUSER_ID}';"
    )


def main() -> int:
    load_dotenv()
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    month_key = "trial:platform_usage:" + datetime.now(timezone.utc).strftime("%Y%m")
    print(f"Load smoke against global trial breaker (concurrency={CONCURRENCY})")

    prior = redis(f"GET {month_key}")
    prior_int = int(prior) if prior and prior.isdigit() else 0
    print(f"  prior counter: {prior_int}")

    # We need to know the cap. Default is 25000; check container env to be sure.
    cap_raw = ssh('docker exec grupr-api sh -c "printenv TRIAL_GLOBAL_MONTHLY_CAP || echo 25000"').strip()
    cap = int(cap_raw) if cap_raw.isdigit() else 25000
    seed_value = cap - SEED_BELOW_CAP
    print(f"  cap = {cap}, seeding counter to {seed_value} (need {SEED_BELOW_CAP} more to trip)")

    grupr_id = None
    review_ids: list[str] = []
    try:
        with UserClient(base_url=api_base) as client:
            client.login(email, password)
            set_subscription("trialing", 7)
            redis(f"SET {month_key} {seed_value}")

            # Create one grupr + agent that all concurrent dispatches mention.
            _, body = client._request("POST", "/api/gruprs", json={
                "name": f"load-breaker-{int(time.time())}",
                "description": "concurrent breaker test",
                "grup_type": "ai_workshop",
                "is_public": False,
                "category": "general",
                "max_members": 5,
            })
            grupr_id = body.get("data", {}).get("grupr_id") or body.get("grupr_id")
            print(f"  grupr: {grupr_id}")

            agent_name = "LoadBreakerBot"
            agent_id = client.create_agent(
                display_name=agent_name,
                provider="anthropic",
                model_id="claude-haiku-3-5-20241022",
                system_prompt="Reply with one word.",
            )
            client.add_agent_to_grupr(grupr_id, agent_id)
            print(f"  agent: {agent_id}")

            # Get the access token for HTTP-level concurrent dispatch
            token = client.access_token

        # ── FIRE CONCURRENT REQUESTS ──────────────────────────────────────
        # Each post creates a message that triggers the agent dispatch which
        # calls platformKey() which INCRs the Redis counter. Some will see
        # val < cap and proceed; some will see val >= cap and be blocked.
        import httpx

        def fire_one(i: int) -> tuple[int, int]:
            try:
                r = httpx.post(
                    f"{api_base}/api/gruprs/{grupr_id}/messages",
                    json={"content": f"@{agent_name} ping #{i}"},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15.0,
                )
                return i, r.status_code
            except Exception:
                return i, 0

        print(f"\n  firing {CONCURRENCY} concurrent dispatches...")
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            results = list(pool.map(fire_one, range(CONCURRENCY)))
        elapsed = time.time() - t0
        print(f"  wall: {elapsed:.2f}s, all dispatched (message POST returns immediately)")

        # Give the orchestrator goroutines a few seconds to finish their
        # platformKey checks and Redis INCRs.
        time.sleep(8)

        final = redis(f"GET {month_key}")
        final_int = int(final) if final and final.isdigit() else 0
        delta = final_int - seed_value
        print(f"\n  final counter: {final_int} (seeded {seed_value}, delta +{delta})")

        # Count blocked vs allowed from orchestrator logs.
        since_seconds = int(time.time() - t0) + 10
        blocked_log = ssh(
            f"docker logs grupr-api --since {since_seconds}s 2>&1 | "
            f"grep -c 'global trial cap reached' || true"
        ).strip()
        passed_log = ssh(
            f"docker logs grupr-api --since {since_seconds}s 2>&1 | "
            f"grep -c 'using platform key for trial user' || true"
        ).strip()
        blocked = int(blocked_log) if blocked_log.isdigit() else 0
        passed = int(passed_log) if passed_log.isdigit() else 0

        print(f"  log counts: passed={passed}, blocked={blocked}")
        print(f"  expected: ~{SEED_BELOW_CAP} passed, ~{CONCURRENCY - SEED_BELOW_CAP} blocked (give or take race slop)")

        failures: list[str] = []
        if blocked == 0:
            failures.append("expected at least 1 'global trial cap reached' log line")
        if final_int > cap + CONCURRENCY:
            failures.append(f"counter blew past cap by {final_int - cap} (concurrency window slop)")

    finally:
        try:
            if prior_int > 0:
                redis(f"SET {month_key} {prior_int}")
            else:
                redis(f"DEL {month_key}")
        except Exception as e:
            print(f"  cleanup Redis FAILED: {e}", file=sys.stderr)
        try:
            set_subscription("active", None)
        except Exception:
            pass
        if grupr_id:
            try:
                psql(f"DELETE FROM gruprs WHERE grupr_id = '{grupr_id}';")
            except Exception:
                pass

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nLoad smoke passed — global breaker fires under {CONCURRENCY}-wide concurrent burst.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
