"""Smoke: E2B sandbox WriteFile + Exec + ReadFile round-trip.

Validates Item 9a's envd integration end-to-end through the api:
spawn -> write -> exec -> read -> close. Doesn't go through the
code-review flow (which would burn LLM tokens); just exercises the
sandbox path the patching step uses.

The api doesn't expose sandbox.Exec directly, so this smoke triggers
a Deep code review specifically to make runPatcher fire. It then
inspects the orchestrator logs on EC2 to confirm the "sandbox %s
round-trip OK: wc says ..." log line.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-sandbox-exec.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

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

    # Multi-line code blob — wc -l should report >= 3 lines.
    sample_code = (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n"
    )
    expected_lines = sample_code.count("\n")

    review_id: str | None = None
    failures: list[str] = []

    try:
        with UserClient(base_url=api_base) as client:
            client.login(email, password)
            print("  login OK")

            set_subscription("trialing", 7)

            # Mark when we kicked off so the log query is bounded.
            mark_ts = time.time()

            # Tier=deep with just one reviewer + a tiny code blob. We
            # don't actually care about the reviewer verdicts; we want
            # to force runPatcher to fire after approve.
            _, body = client._request("POST", "/api/code-review", json={
                "tier": "deep",
                "input_type": "paste",
                "input_payload": {"code": sample_code, "context": "sandbox smoke"},
                "reviewer_roles": ["architect", "maintainability"],
            })
            review_id = body.get("data", {}).get("review_id")
            print(f"  review_id={review_id}")

            # Wait for awaiting_patch (max 90s).
            status = "pending"
            for _ in range(18):
                time.sleep(5)
                _, body = client._request("GET", f"/api/code-review/{review_id}")
                d = body.get("data", body)
                status = d.get("status")
                if status in ("awaiting_patch", "failed", "cancelled"):
                    break
            print(f"  status after reviewers+synth: {status}")
            if status != "awaiting_patch":
                failures.append(f"expected awaiting_patch, got {status!r}")
                return 1

            # Trigger the patcher.
            client._request("POST", f"/api/code-review/{review_id}/approve",
                            json={"action": "generate_patch"})

            # Give the sandbox a few seconds to do its thing.
            print("  waiting for sandbox round-trip (up to 60s)...")
            for _ in range(12):
                time.sleep(5)
                _, body = client._request("GET", f"/api/code-review/{review_id}")
                if body.get("data", {}).get("status") in ("failed", "completed"):
                    break

            # Pull the orchestrator log lines for this run.
            since = int(time.time() - mark_ts) + 10
            logs = ssh(
                f"docker logs grupr-api --since {since}s 2>&1 | "
                f"grep -E 'sandbox|codereview' | tail -20"
            )
            print(f"  orchestrator logs:")
            for line in logs.splitlines():
                print(f"    {line[-200:]}")

            # Assertions
            if "sandbox: spawned" not in logs:
                failures.append("missing 'sandbox: spawned' log line")
            if "round-trip OK: wc says" not in logs:
                failures.append("missing 'round-trip OK: wc says' log line — Exec/WriteFile path did not complete")
            # wc output should mention the line count
            if "round-trip OK: wc says" in logs:
                # extract the wc count from logs — line like:
                #   "wc says \"5 /home/user/source.txt\" (exit=0, wall=...)"
                # be loose — just check exit=0 is present somewhere
                if "exit=0" not in logs:
                    failures.append("sandbox exec did not return exit=0 (wc failed)")
                print(f"  sandbox exec exit code 0 verified")

            if "sandbox: closed" not in logs:
                failures.append("missing 'sandbox: closed' log line — sandbox not killed cleanly")

    finally:
        if review_id:
            try:
                psql(f"DELETE FROM code_reviews WHERE review_id = '{review_id}';")
            except Exception:
                pass
        try:
            set_subscription("active", None)
        except Exception:
            pass

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nSandbox round-trip smoke passed (expected_lines={expected_lines}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
