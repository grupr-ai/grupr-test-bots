"""Smoke-test for lib/code_review_client.py — Quick + Deep tier wrappers.

Runs both tiers end-to-end against a tiny intentionally-buggy snippet
to confirm:
  - SDK helpers reach the right endpoints
  - Quick: ai_workshop+code grupr created, 5 agents respond
  - Deep: orchestrator kicks off and returns terminal status

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-code-review-wrappers.py
"""

from __future__ import annotations

import json
import os
import sys
import time

from dotenv import load_dotenv

from lib.code_review_client import run_quick, run_deep
from lib.user_client import UserClient


# Two deliberate bugs so the reviewers + verifier have something to say.
SNIPPET = '''\
def divide(a, b):
    # bug 1: no zero-check
    # bug 2: returns int even when float would be more correct
    return a / b


def parse_age(s):
    # bug: no validation; "abc" raises ValueError that callers don't expect
    return int(s)
'''


def main() -> int:
    load_dotenv(dotenv_path=".env", override=True)
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL")
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    if not (email and password):
        print("ERROR: GRUPR_TEST_NEW_USER_EMAIL + GRUPR_TEST_PASSWORD required", file=sys.stderr)
        return 2

    with UserClient(base_url=api_base) as c:
        c.login(email, password)
        me = c.me()
        print(f"smoke: logged in as {me.get('username')} (id={me.get('user_id', '')[:8]})")

        # ── Quick tier ───────────────────────────────────────────
        print("\n[Quick tier] starting…")
        t0 = time.monotonic()
        try:
            quick = run_quick(c, code=SNIPPET, poll_timeout_s=120)
        except Exception as e:
            print(f"  QUICK FAIL: {type(e).__name__}: {e}")
            return 3
        elapsed = int(time.monotonic() - t0)
        print(f"  done in {elapsed}s — {quick['reviewer_count_returned']}/{quick['expected_count']} reviewers; timed_out={quick['timed_out']}")
        print(f"  grupr_id={quick['grupr_id']}")
        for v in quick["verdicts"]:
            head = (v["content"] or "").splitlines()[0][:120] if v["content"] else "(empty)"
            print(f"    [{v['reviewer']}] {head}")
        if quick["synthesizer_verdict"]:
            print(f"  synth verdict head: {quick['synthesizer_verdict'].splitlines()[0][:160]}")
        else:
            print("  WARN: no synthesizer verdict")

        # ── Deep tier ────────────────────────────────────────────
        print("\n[Deep tier] starting (this can take 3–8 min)…")
        try:
            deep = run_deep(c, code=SNIPPET, poll_timeout_s=600)
        except Exception as e:
            print(f"  DEEP FAIL: {type(e).__name__}: {e}")
            return 4
        print(f"  done in {deep['elapsed_s']}s — status={deep['status']} timed_out={deep['timed_out']}")
        print(f"  review_id={deep['review_id']}")
        if deep["error_code"]:
            print(f"  error: [{deep['error_code']}] {deep['error_message']}")
        if deep["patch_diff"]:
            n_lines = len(deep["patch_diff"].splitlines())
            print(f"  patch: status={deep['patch_status']}, {n_lines} diff lines")
            print(f"  first 6 diff lines:")
            for line in deep["patch_diff"].splitlines()[:6]:
                print(f"    {line}")
        else:
            print(f"  no patch returned (patch_status={deep['patch_status']!r})")

    # Drop a JSON dump in /tmp for easy inspection.
    out_path = "/tmp/smoke-code-review-wrappers.json" if os.name != "nt" else "smoke-code-review-wrappers.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"quick": quick, "deep": {k: v for k, v in deep.items() if k not in ("raw_review", "raw_patch")}}, f, indent=2, default=str)
    print(f"\ndump: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
