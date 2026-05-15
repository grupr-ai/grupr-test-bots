"""Smoke: code review create -> get -> cancel -> events flow + state machine.

Exercises the api-side code-review surface introduced in Item 8a:
  POST   /api/code-review            — create a row (status=pending)
  GET    /api/code-review/:id        — fetch current state
  POST   /api/code-review/:id/cancel — transition to cancelled (state machine)
  GET    /api/code-review/:id/events — read the audit trail

Uses tier=quick to skip the Deep orchestrator Start (which would transition
to 'reviewing' before we can cancel from 'pending'). The state machine
shape is the same regardless of tier.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-code-review-state-machine.py
"""

from __future__ import annotations

import os
import subprocess
import sys

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


def main() -> int:
    load_dotenv()
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"Code review state-machine smoke: {email} -> {api_base}")
    failures: list[str] = []
    review_id: str | None = None

    try:
        with UserClient(base_url=api_base) as client:
            client.login(email, password)
            print("  login OK")

            # 1. Create
            _, body = client._request(
                "POST", "/api/code-review",
                json={
                    "tier": "quick",
                    "input_type": "paste",
                    "input_payload": {"code": "def add(a, b): return a + b", "context": "smoke"},
                    "reviewer_roles": ["architect", "security"],
                },
            )
            data = body.get("data", body)
            review_id = data.get("review_id")
            if not review_id:
                print(f"  create returned no review_id: {body}", file=sys.stderr)
                return 1
            print(f"  created: review_id={review_id} status={data.get('status')}")

            # 2. Get
            _, body = client._request("GET", f"/api/code-review/{review_id}")
            d = body.get("data", body)
            if d.get("status") != "pending":
                failures.append(f"expected pending after create, got {d.get('status')!r}")
            if d.get("tier") != "quick":
                failures.append(f"expected tier=quick, got {d.get('tier')!r}")
            if d.get("user_id") != NEWUSER_ID:
                failures.append(f"expected user_id={NEWUSER_ID}, got {d.get('user_id')!r}")
            print(f"  get: tier={d.get('tier')} status={d.get('status')} input_type={d.get('input_type')}")

            # 3. Cancel
            _, body = client._request(
                "POST", f"/api/code-review/{review_id}/cancel", json={},
            )
            d = body.get("data", body)
            if d.get("status") != "cancelled":
                failures.append(f"cancel response status mismatch: {d}")
            print(f"  cancel: status={d.get('status')}")

            # 4. Get again -> should be cancelled
            _, body = client._request("GET", f"/api/code-review/{review_id}")
            d = body.get("data", body)
            if d.get("status") != "cancelled":
                failures.append(f"expected cancelled after cancel, got {d.get('status')!r}")
            if not d.get("completed_at"):
                failures.append("expected completed_at stamp on cancellation")
            print(f"  get after cancel: status={d.get('status')} completed_at={d.get('completed_at')}")

            # 5. Events
            _, body = client._request("GET", f"/api/code-review/{review_id}/events")
            events = body.get("data", [])
            kinds = [e.get("event_type") for e in events]
            print(f"  events: {kinds}")
            if "review_cancelled" not in kinds:
                failures.append(f"expected 'review_cancelled' in audit events, got {kinds}")

    finally:
        # Belt-and-suspenders cleanup — delete the test row even if assertions
        # failed, so re-runs are idempotent. The cancelled status would
        # otherwise just linger as historical data.
        if review_id:
            try:
                ssh(
                    'PW=$(sudo grep "^POSTGRES_PASSWORD" ~/grupr/.env | cut -d= -f2 | tr -d \'"\'); '
                    f'docker exec -e PGPASSWORD="$PW" grupr-postgres '
                    f"psql -U grupr -d grupr -c \"DELETE FROM code_reviews WHERE review_id = '{review_id}';\""
                )
                print(f"  cleanup: deleted review {review_id}")
            except Exception as e:
                print(f"  cleanup FAILED: {e}", file=sys.stderr)

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nCode review state-machine smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
