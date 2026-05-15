"""Smoke: Deep-tier Code Review end-to-end (without patching).

Walks the state machine pending -> reviewing -> synthesizing -> awaiting_patch,
then approves the patch and confirms the transition to patching (which
fails fast on sandbox.ErrExecNotImplemented — Item 9 wires the Claude Code
agent there).

Costs ~$0.20 per run (2 real LLM dispatches + synthesizer via Claude Opus 4
and Llama 70B). Uses gtb-newuser flipped to trialing so the orchestrator
hits the platform-key fallback path — no BYOK required.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-deep-tier-flow.py
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

    print(f"Deep-tier flow smoke: {email} -> {api_base}")
    failures: list[str] = []
    review_id: str | None = None

    sample_code = (
        "def get_user(user_id):\n"
        "    sql = f\"SELECT * FROM users WHERE id = '{user_id}'\"  # injection!\n"
        "    return db.execute(sql)\n"
    )

    try:
        with UserClient(base_url=api_base) as client:
            client.login(email, password)
            print("  login OK")

            # 1. Flip to trialing so platform keys engage
            set_subscription("trialing", 7)
            print(f"  subscription: trialing|pro_user")

            # 2. Create a deep review with 2 reviewers (cheapest config)
            _, body = client._request("POST", "/api/code-review", json={
                "tier": "deep",
                "input_type": "paste",
                "input_payload": {
                    "code": sample_code,
                    "context": "Toy SQL injection example for smoke",
                },
                "reviewer_roles": ["architect", "maintainability"],
            })
            review_id = body.get("data", {}).get("review_id")
            print(f"  created: review_id={review_id}")

            # 3. Poll until status is awaiting_patch (or failed)
            print("  polling for reviewer fan-out + synthesizer (up to 3 min)...")
            terminal_or_awaiting = {"awaiting_patch", "completed", "failed", "cancelled"}
            status = "pending"
            for i in range(36):  # 36 * 5s = 3 min
                time.sleep(5)
                _, body = client._request("GET", f"/api/code-review/{review_id}")
                d = body.get("data", body)
                new_status = d.get("status")
                if new_status != status:
                    print(f"    [{i*5:>3}s] status: {status} -> {new_status}")
                    status = new_status
                if status in terminal_or_awaiting:
                    break

            if status != "awaiting_patch":
                failures.append(f"expected awaiting_patch after reviewers+synth, got {status!r}")
                # Get the error if any
                _, body = client._request("GET", f"/api/code-review/{review_id}")
                d = body.get("data", body)
                if d.get("error_code"):
                    print(f"    error: {d.get('error_code')} — {d.get('error_message')}")

            # 4. Read events to verify the state machine traversed correctly
            _, body = client._request("GET", f"/api/code-review/{review_id}/events")
            events = body.get("data", [])
            event_types = [e.get("event_type") for e in events]
            print(f"  events: {event_types}")

            expected_events = [
                "review_started",
                "reviewer_started",  # at least one
                "reviewer_completed",  # at least one
                "reviewers_completed",
                "synthesizer_started",
                "verdict_ready",
            ]
            for evt in expected_events:
                if evt not in event_types:
                    failures.append(f"missing expected event {evt!r}")

            # 5. If we got to awaiting_patch, approve generate_patch
            if status == "awaiting_patch":
                _, body = client._request(
                    "POST", f"/api/code-review/{review_id}/approve",
                    json={"action": "generate_patch", "note": "smoke"},
                )
                d = body.get("data", body)
                print(f"  approve: status={d.get('status')}")
                if d.get("status") != "patching":
                    failures.append(f"expected status=patching after approve, got {d.get('status')!r}")

                # 6. The patcher fails clean since sandbox exec isn't wired (Item 9).
                time.sleep(3)
                _, body = client._request("GET", f"/api/code-review/{review_id}")
                final_status = body.get("data", {}).get("status")
                print(f"  post-approve final status: {final_status}")
                # Either 'failed' (sandbox spawn worked, exec stub fired) or
                # 'patching' (still in-flight). Both are acceptable — the
                # locked design says deep_unavailable is a known surface
                # until Item 9 lands.

    finally:
        # Always cleanup — restore subscription + delete review row.
        try:
            set_subscription("active", None)
            print(f"  cleanup: subscription -> active|free")
        except Exception as e:
            print(f"  cleanup subscription FAILED: {e}", file=sys.stderr)
        if review_id:
            try:
                # FK cascade deletes runs, artifacts, audit, approvals.
                psql(f"DELETE FROM code_reviews WHERE review_id = '{review_id}';")
                print(f"  cleanup: deleted review {review_id}")
            except Exception as e:
                print(f"  cleanup review FAILED: {e}", file=sys.stderr)

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nDeep-tier flow smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
