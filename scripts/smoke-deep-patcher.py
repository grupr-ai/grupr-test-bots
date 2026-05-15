"""Smoke: full Deep-tier review including the Claude Code patcher.

End-to-end: create deep review -> reviewers + synthesizer (~10s) ->
approve generate_patch -> patcher runs Claude Code agent loop inside
the sandbox -> diff captured -> status=completed.

Pulls the resulting diff out of the database and prints it. This is
the moment-of-truth smoke for Item 9b.

Cost: ~$0.50 per run. ~$0.20 for reviewers + synth (as before) plus
~$0.20-0.30 for the patcher's Claude Opus 4.7 agent loop (3-6 turns
with Messages API).

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-deep-patcher.py
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

# A toy SQL injection that the patcher can fix in one turn: replace the
# f-string with a parameterized query. Easy enough that we expect a
# successful diff in 2-4 agent iterations.
SAMPLE_CODE = """def get_user(user_id):
    # TODO: this concatenates user input into SQL — SQL injection risk
    sql = f"SELECT * FROM users WHERE id = '{user_id}'"
    return db.execute(sql)
"""


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

    print(f"Deep patcher smoke (~$0.50): {email}")
    review_id: str | None = None
    failures: list[str] = []

    try:
        with UserClient(base_url=api_base) as client:
            client.login(email, password)
            set_subscription("trialing", 7)
            print("  setup OK (trialing)")

            _, body = client._request("POST", "/api/code-review", json={
                "tier": "deep",
                "input_type": "paste",
                "input_payload": {"code": SAMPLE_CODE, "context": "patcher smoke — SQL injection"},
                "reviewer_roles": ["architect", "security"],
            })
            review_id = body.get("data", {}).get("review_id")
            print(f"  review_id={review_id}")

            # 1. Wait for awaiting_patch (reviewer fan-out + synth, ~10-30s)
            status = "pending"
            for _ in range(24):  # 24 * 5s = 2 min
                time.sleep(5)
                _, body = client._request("GET", f"/api/code-review/{review_id}")
                status = body.get("data", {}).get("status")
                if status in ("awaiting_patch", "failed", "cancelled"):
                    break
            print(f"  after reviewers+synth: {status}")
            if status != "awaiting_patch":
                d = body.get("data", body)
                print(f"    error: {d.get('error_code')} — {d.get('error_message')}")
                failures.append(f"expected awaiting_patch, got {status!r}")
                return 1

            # 2. Approve generate_patch — kicks off the Claude Code agent loop
            t_approve = time.time()
            client._request("POST", f"/api/code-review/{review_id}/approve",
                            json={"action": "generate_patch"})
            print(f"  approved generate_patch — polling for completion (up to 5 min)...")

            # 3. Wait for completed (or failed). Agent loop is multi-turn:
            #    expect 30s-3min depending on iteration count.
            status = "patching"
            for i in range(60):  # 60 * 5s = 5 min
                time.sleep(5)
                _, body = client._request("GET", f"/api/code-review/{review_id}")
                d = body.get("data", body)
                new_status = d.get("status")
                if new_status != status:
                    print(f"    [+{int(time.time()-t_approve):>3}s] status: {status} -> {new_status}")
                    status = new_status
                if status in ("completed", "failed", "cancelled"):
                    break

            elapsed = int(time.time() - t_approve)
            print(f"  final status: {status} (patching+verifying took {elapsed}s)")

            if status == "failed":
                _, body = client._request("GET", f"/api/code-review/{review_id}")
                d = body.get("data", body)
                print(f"    error_code: {d.get('error_code')}")
                print(f"    error_msg:  {d.get('error_message')}")
                failures.append(f"patcher failed: {d.get('error_code')}")
                return 1
            if status != "completed":
                failures.append(f"expected completed, got {status!r}")
                return 1

            # 4. Pull the diff from the DB
            patch_row = psql(
                f"SELECT patch_id || '|' || array_length(files_changed, 1) || '|' || length(diff) "
                f"FROM code_review_patches WHERE review_id = '{review_id}' "
                f"ORDER BY created_at DESC LIMIT 1;"
            )
            print(f"  patch row: {patch_row}")
            if not patch_row or patch_row.strip() == "":
                failures.append("no patch row found in code_review_patches")
                return 1

            # Display the actual diff
            diff_text = psql(
                f"SELECT diff FROM code_review_patches WHERE review_id = '{review_id}' "
                f"ORDER BY created_at DESC LIMIT 1;"
            )
            print(f"\n  --- DIFF (first 1500 chars) ---")
            print(diff_text[:1500])
            print(f"  --- END DIFF (total {len(diff_text)} chars) ---\n")

            if "SELECT" not in diff_text or "+" not in diff_text:
                failures.append("diff doesn't look like it touched the SQL — agent may have no-op'd")

            # 5. Check audit events
            _, body = client._request("GET", f"/api/code-review/{review_id}/events")
            events = body.get("data", [])
            event_types = [e.get("event_type") for e in events]
            print(f"  events ({len(events)}): {event_types}")
            expected = ["patch_generating", "patch_ready", "verification_running", "review_completed"]
            for e in expected:
                if e not in event_types:
                    failures.append(f"missing expected event {e!r}")

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
    print("\nDeep patcher smoke passed — agent generated a real diff end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
