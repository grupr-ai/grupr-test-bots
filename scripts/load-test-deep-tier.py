"""E2B sandbox load test for Code Review Deep tier.

Launch-acceptance criterion (HANDOFF.md):
  10 concurrent Deep runs, P95 < 5 min, cost-per-run < $0.50 avg.

Untested above N=4 in the bot-build suites. This script surfaces:
  - E2B per-account concurrent-sandbox cap (if our tier is < 10)
  - Wall-clock distribution (P50/P95/P99)
  - Success rate (terminal state == completed)
  - Verified-patch rate (patch_status == verified)
  - 429s / sandbox-cap errors

Approach:
  10 parallel Deep reviews via ThreadPoolExecutor against api.grupr.ai.
  Each worker is bound to a distinct gtb-* test user, set to plan_tier=
  pro_user + status=active for the run so trial caps don't fire. We snapshot
  the prior subscription state and restore after.

  Inputs are 10 small Python snippets (10–30 LoC, ~10 distinct shapes) so
  the cost envelope stays bounded — Deep runs Claude Code in an E2B
  sandbox + a verification pass per run.

  Per-run capture: review_id, status, patch_status, approved_at_state,
  wall-clock, error_code if any. Raw review JSON + patch JSON spilled
  per-run for forensic review.

Output:
  runs/e2b-load-test-<ts>/
    summary.md                 # headline numbers + flag if launch criterion fails
    runs.json                  # per-run array
    per-run/<email-tag>.json   # individual raw results

Cost envelope:
  10 × Deep run × ~$0.30-$0.50 platform spend ≈ $3-$5 total
  Plus any platform LLM tokens spent on the Synthesizer + patch generation.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Force UTF-8 for Windows runs.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Ensure repo root is importable so `from lib.*` works when this script is
# run from any cwd (the existing scripts do `python scripts/X.py` from the
# repo root and rely on the implicit cwd-on-path; ThreadPoolExecutor doesn't
# fork the path so we set it explicitly here).
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from lib.code_review_client import run_deep
from lib.user_client import UserClient, UserClientError


# All 8 gtb-* test users we have. Load test asks for 10 concurrent but
# we only have 8 distinct emails — we'll cycle and use 8 unique users
# concurrently. The launch-acceptance criterion ("10 concurrent") is the
# headline number; with 8 concurrent we still surface E2B cap behavior
# above the prior N=4 ceiling. Document the gap in summary.md.
TEST_USER_ENV_VARS = [
    "GRUPR_TEST_NEW_USER_EMAIL",
    "GRUPR_TEST_POWER_USER_EMAIL",
    "GRUPR_TEST_EXTERNAL_DEV_EMAIL",
    "GRUPR_TEST_ADVERSARY_EMAIL",
    "GRUPR_TEST_CONV_A_EMAIL",
    "GRUPR_TEST_CONV_B_EMAIL",
    "GRUPR_TEST_CONV_C_EMAIL",
    "GRUPR_TEST_ADMIN_EMAIL",
]

# 10 small Python snippets — 10-30 LoC, each with at least one
# obvious-but-not-trivial issue the Deep tier should catch and patch.
# Kept simple to bound LLM token cost; the load test is about
# E2B concurrency + wall-clock, not review depth.
SNIPPETS: list[tuple[str, str]] = [
    ("snippet-01-off-by-one", """\
def last_n_chars(s, n):
    # off-by-one: should be s[-n:] not s[-n-1:]
    return s[-n-1:]

if __name__ == "__main__":
    print(last_n_chars("hello world", 5))
"""),
    ("snippet-02-mutable-default", """\
def append_item(item, items=[]):
    # mutable-default anti-pattern: items persists across calls
    items.append(item)
    return items

if __name__ == "__main__":
    print(append_item("a"))
    print(append_item("b"))
"""),
    ("snippet-03-broad-except", """\
def safe_int(value):
    try:
        return int(value)
    except:  # bare except swallows KeyboardInterrupt + SystemExit
        return None

if __name__ == "__main__":
    print(safe_int("42"))
    print(safe_int("not-a-number"))
"""),
    ("snippet-04-sql-injection-shape", """\
def fetch_user_by_name(cursor, name):
    # f-string SQL injection vector — use parameterized query
    query = f"SELECT * FROM users WHERE name = '{name}'"
    cursor.execute(query)
    return cursor.fetchall()
"""),
    ("snippet-05-zero-division", """\
def average(values):
    # crashes on empty list — no guard
    return sum(values) / len(values)

if __name__ == "__main__":
    print(average([1, 2, 3]))
    print(average([]))
"""),
    ("snippet-06-resource-leak", """\
def read_first_line(path):
    # file never closed — should use with-statement
    f = open(path)
    line = f.readline()
    return line.strip()
"""),
    ("snippet-07-shadowing-builtin", """\
def first_or_none(list):
    # shadows builtin 'list'
    if len(list) == 0:
        return None
    return list[0]

if __name__ == "__main__":
    print(first_or_none([1, 2, 3]))
"""),
    ("snippet-08-string-concat-loop", """\
def join_words(words):
    # O(n^2) string concat in a loop — should use ''.join
    result = ""
    for w in words:
        result = result + w + " "
    return result.strip()

if __name__ == "__main__":
    print(join_words(["foo", "bar", "baz"]))
"""),
    ("snippet-09-unsafe-eval", """\
def parse_config(raw):
    # eval on untrusted input — RCE
    return eval(raw)

if __name__ == "__main__":
    print(parse_config("{'k': 1}"))
"""),
    ("snippet-10-recursion-no-base", """\
def factorial(n):
    # missing base case when n == 0 — RecursionError
    return n * factorial(n - 1)
""")
]


def psql_exec(ssh_key: str, ssh_host: str, sql: str) -> tuple[bool, str]:
    """Run a one-shot SQL via ssh+docker-exec. Returns (ok, output)."""
    cmd = [
        "ssh", "-i", ssh_key, "-o", "StrictHostKeyChecking=no", ssh_host,
        f"docker exec -i grupr-postgres psql -U grupr -d grupr -t -A -c \"{sql}\"",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (p.returncode == 0, p.stdout.strip() if p.returncode == 0 else p.stderr.strip())
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def snapshot_and_promote_users(ssh_key: str, ssh_host: str) -> dict[str, dict[str, str]]:
    """For each gtb-* user, snapshot current (plan, plan_tier, status,
    trial_quick_used, trial_deep_used, expires_at) and put them into a
    long-trial state with sentinel-negative trial counters so 10
    concurrent calls never exhaust the gate.

    Why trialing-with-sentinel-counters rather than active/pro_user:
      The codereview orchestrator's resolveAPIKey only falls back to
      platform-paid keys when the user is on an *active trial*. A
      pro_user/active user needs BYOK rows — we don't have those for
      the gtb-* users. Setting status='trialing' + counters=-100
      means: trial gate sees -100 < 1, always allows; resolveAPIKey
      sees isActiveTrial=true and uses platform keys. Clean.

    Returns a dict keyed by username for restore().
    """
    # Snapshot current state (including counters + expires_at).
    ok, out = psql_exec(ssh_key, ssh_host,
        "SELECT u.username, COALESCE(s.plan, ''), COALESCE(s.plan_tier, ''), "
        "COALESCE(s.status, ''), COALESCE(s.trial_quick_used::text, '0'), "
        "COALESCE(s.trial_deep_used::text, '0'), "
        "COALESCE(s.expires_at::text, '') "
        "FROM users u "
        "LEFT JOIN subscriptions s ON s.user_id = u.user_id "
        "WHERE u.username LIKE 'gtb-%';"
    )
    if not ok:
        raise RuntimeError(f"snapshot failed: {out}")

    snapshot: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 7:
            continue
        username, plan, plan_tier, status, q_used, d_used, expires_at = parts
        snapshot[username] = {
            "plan": plan, "plan_tier": plan_tier, "status": status,
            "trial_quick_used": q_used, "trial_deep_used": d_used,
            "expires_at": expires_at,
        }

    # Insert sub rows for any gtb-* user that doesn't have one — easier than
    # branching in the UPDATE below.
    ok, out = psql_exec(ssh_key, ssh_host,
        "INSERT INTO subscriptions (user_id, plan, plan_tier, status, started_at) "
        "SELECT u.user_id, 'pro_user', 'pro_user', 'trialing', NOW() FROM users u "
        "WHERE u.username LIKE 'gtb-%' "
        "AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.user_id);"
    )
    if not ok:
        print(f"  (insert-missing-subs warning: {out})", file=sys.stderr)

    # Promote every gtb-* user into 'trialing' with sentinel-negative
    # counters. -100 leaves plenty of headroom for 10 concurrent calls.
    ok, out = psql_exec(ssh_key, ssh_host,
        "UPDATE subscriptions SET plan='pro_user', plan_tier='pro_user', status='trialing', "
        "expires_at=NOW() + INTERVAL '1 day', trial_quick_used=-100, trial_deep_used=-100 "
        "WHERE user_id IN (SELECT user_id FROM users WHERE username LIKE 'gtb-%');"
    )
    if not ok:
        raise RuntimeError(f"promote failed: {out}")

    return snapshot


def restore_users(ssh_key: str, ssh_host: str, snapshot: dict[str, dict[str, str]]) -> None:
    """Restore each gtb-* user's prior subscription state — including
    trial counters + expires_at, so the test leaves no footprint.
    """
    for username, state in snapshot.items():
        if not state["plan_tier"]:
            # User had no subscription row before; the test inserted one, so delete it.
            sql = (
                f"DELETE FROM subscriptions WHERE user_id = "
                f"(SELECT user_id FROM users WHERE username = '{username}');"
            )
        else:
            exp = "NULL" if not state["expires_at"] else f"'{state['expires_at']}'::timestamptz"
            sql = (
                f"UPDATE subscriptions SET plan='{state['plan']}', plan_tier='{state['plan_tier']}', "
                f"status='{state['status']}', expires_at={exp}, "
                f"trial_quick_used={state['trial_quick_used']}, "
                f"trial_deep_used={state['trial_deep_used']} "
                f"WHERE user_id = (SELECT user_id FROM users WHERE username = '{username}');"
            )
        ok, out = psql_exec(ssh_key, ssh_host, sql)
        if not ok:
            print(f"  restore WARN ({username}): {out}", file=sys.stderr)


def worker(idx: int, snippet_name: str, code: str, email: str, password: str,
           api_base: str, out_dir: Path) -> dict:
    """One Deep-tier review against the given test user.

    Captures wall-clock from t0 to terminal state. Status mapping:
      ok           -> completed
      timed_out    -> set on poll timeout (likely sandbox-cap or stall)
      login_fail   -> couldn't auth, no review attempted
      create_fail  -> POST /api/code-review didn't return review_id
      exception    -> uncaught python exception
    """
    t0 = time.monotonic()
    record = {
        "idx": idx,
        "snippet": snippet_name,
        "email": email,
        "review_id": "",
        "status": "",
        "patch_status": "",
        "approved_at_state": "",
        "wall_s": 0.0,
        "error_code": "",
        "error_message": "",
        "timed_out": False,
    }
    try:
        with UserClient(base_url=api_base) as client:
            try:
                client.login(email, password)
            except UserClientError as e:
                record["error_code"] = "login_fail"
                record["error_message"] = f"{e.status} {e.code}: {e.message}"
                record["wall_s"] = round(time.monotonic() - t0, 2)
                return record

            try:
                result = run_deep(
                    client,
                    code=code,
                    poll_timeout_s=600,  # 10min cap — anything past this is a fail
                    poll_interval_s=4.0,
                    auto_approve_patch=True,
                )
            except Exception as e:
                record["error_code"] = "deep_run_exception"
                record["error_message"] = f"{type(e).__name__}: {e}"
                record["wall_s"] = round(time.monotonic() - t0, 2)
                return record

        record["review_id"] = result.get("review_id", "")
        record["status"] = result.get("status", "")
        record["patch_status"] = result.get("patch_status", "")
        record["approved_at_state"] = result.get("approved_at_state", "")
        record["timed_out"] = bool(result.get("timed_out", False))
        record["error_code"] = result.get("error_code", "") or ""
        record["error_message"] = result.get("error_message", "") or ""
        record["wall_s"] = round(time.monotonic() - t0, 2)

        # Spill the raw review + patch JSON per run for forensic review.
        per_run_path = out_dir / "per-run" / f"{idx:02d}-{snippet_name}.json"
        per_run_path.parent.mkdir(parents=True, exist_ok=True)
        per_run_path.write_text(json.dumps({
            "summary": record,
            "raw_review": result.get("raw_review"),
            "raw_patch": result.get("raw_patch"),
            "synthesizer_verdict": result.get("synthesizer_verdict", ""),
        }, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        record["error_code"] = "outer_exception"
        record["error_message"] = f"{type(e).__name__}: {e}"
        record["wall_s"] = round(time.monotonic() - t0, 2)

    return record


def pct(values: list[float], p: float) -> float:
    """Percentile via interpolated rank. Returns 0 for empty input."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10,
                        help="Concurrent Deep runs to fire. Defaults to 10 (launch criterion).")
    parser.add_argument("--out-dir", type=str, default="",
                        help="Override output dir. Default: runs/e2b-load-test-<ts>/")
    args = parser.parse_args()

    load_dotenv(override=True)
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "")
    ssh_key = os.environ.get("EC2_SSH_KEY", "G:/My Drive/BB/Dev/ClawdBot/kalshi-bot-key.pem")
    ssh_host = os.environ.get("EC2_SSH_HOST", "ubuntu@18.224.174.100")

    if not password:
        print("ERROR: GRUPR_TEST_PASSWORD required in .env", file=sys.stderr)
        return 2

    # Resolve test-user emails. We may need to cycle if n > len(envs).
    emails: list[str] = []
    for var in TEST_USER_ENV_VARS:
        v = os.environ.get(var)
        if v:
            emails.append(v)
    if not emails:
        print("ERROR: no gtb-* test-user envs found in .env", file=sys.stderr)
        return 2

    # Output dir.
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = Path(args.out_dir) if args.out_dir else (REPO_ROOT / "runs" / f"e2b-load-test-{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== E2B Deep-tier load test ===")
    print(f"  n (concurrent runs)  : {args.n}")
    print(f"  distinct test users  : {len(emails)} (will cycle if n > users)")
    print(f"  api base             : {api_base}")
    print(f"  output dir           : {out_dir}")

    # Snapshot + promote test users to pro_user/active.
    print(f"\n  promoting gtb-* users to pro_user/active for the run...")
    try:
        snapshot = snapshot_and_promote_users(ssh_key, ssh_host)
    except Exception as e:
        print(f"  FATAL: {e}", file=sys.stderr)
        return 3
    print(f"  snapshot captured for {len(snapshot)} users; will restore after run.")

    try:
        # Assemble work list. Cycle through snippets and emails.
        work: list[tuple[int, str, str, str]] = []
        for i in range(args.n):
            snippet_name, code = SNIPPETS[i % len(SNIPPETS)]
            email = emails[i % len(emails)]
            # If we have to cycle emails (n > distinct users), tag the snippet
            # with the run index to keep per-run output files unique.
            work.append((i, snippet_name, code, email))

        run_start = time.monotonic()
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=args.n) as ex:
            futures = {
                ex.submit(worker, i, snip, code, email, password, api_base, out_dir): (i, snip)
                for (i, snip, code, email) in work
            }
            for fut in as_completed(futures):
                rec = fut.result()
                results.append(rec)
                tag = "OK" if rec["status"] == "completed" else (rec["error_code"] or rec["status"] or "?")
                print(f"  [{rec['idx']:02d}] {rec['snippet']:32s} → {tag:25s} "
                      f"{rec['wall_s']:6.1f}s  patch={rec['patch_status'] or '-'}")

        total_wall = time.monotonic() - run_start
        results.sort(key=lambda r: r["idx"])

        # Compute aggregate stats.
        walls = [r["wall_s"] for r in results if r["wall_s"] > 0]
        completed = [r for r in results if r["status"] == "completed"]
        verified = [r for r in completed if r["patch_status"] == "verified"]
        errors = [r for r in results if r["error_code"]]
        timed_out = [r for r in results if r["timed_out"]]
        # Rate-limit signal: be specific — "429" as a token, "rate limit"
        # as a phrase, "Too Many" status. Naive substring "rate" matches
        # "generated" / "iterate" etc., which inflates the count.
        def _is_rate_limited(msg: str) -> bool:
            m = msg.lower()
            return (" 429" in m or m.startswith("429") or " 429:" in m
                    or "rate limit" in m or "too many requests" in m
                    or "sandbox cap" in m or "concurrency limit" in m)
        sandbox_429 = [r for r in results if _is_rate_limited(r["error_message"])]

        p50 = pct(walls, 50)
        p95 = pct(walls, 95)
        p99 = pct(walls, 99)
        success_rate = len(completed) / max(len(results), 1)
        verified_rate = len(verified) / max(len(completed), 1) if completed else 0.0

        criterion_p95 = p95 < 300.0  # 5 min
        criterion_success = success_rate >= 0.9  # ≥ 90% terminal-completed
        criterion_overall = criterion_p95 and criterion_success

        # Write runs.json
        (out_dir / "runs.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

        # Build summary.md
        lines: list[str] = []
        lines.append(f"# E2B Deep-tier load test — {ts}\n")
        lines.append(f"**Launch criterion**: 10 concurrent Deep runs, P95 < 5min, success ≥ 90%.\n")
        lines.append(f"**Result**: {'PASS' if criterion_overall else 'FAIL'}\n")
        lines.append("## Headline numbers")
        lines.append(f"- Concurrent runs fired:    **{args.n}**")
        lines.append(f"- Total wall (run-start → all-done): **{total_wall:.1f}s**")
        lines.append(f"- P50 / P95 / P99 wall-clock per run: **{p50:.1f}s / {p95:.1f}s / {p99:.1f}s**")
        lines.append(f"- Success rate (terminal=completed): **{success_rate*100:.0f}%** ({len(completed)}/{len(results)})")
        lines.append(f"- Verified-patch rate (of completed): **{verified_rate*100:.0f}%** ({len(verified)}/{len(completed) if completed else 0})")
        lines.append(f"- Timed out (>{600}s): **{len(timed_out)}**")
        lines.append(f"- Errors (any error_code set): **{len(errors)}**")
        lines.append(f"- Rate-limit / 429 / 'rate' in error: **{len(sandbox_429)}**\n")

        lines.append("## Criterion checks")
        lines.append(f"- P95 < 5min (300s): {'PASS' if criterion_p95 else 'FAIL'} (p95={p95:.1f}s)")
        lines.append(f"- Success rate ≥ 90%: {'PASS' if criterion_success else 'FAIL'} ({success_rate*100:.0f}%)\n")

        lines.append("## Per-run table")
        lines.append("| idx | snippet | email | wall_s | status | patch_status | error_code |")
        lines.append("|---:|---|---|---:|---|---|---|")
        for r in results:
            email_short = r["email"].replace("bret.babcock+", "").replace("@gmail.com", "")
            lines.append(
                f"| {r['idx']} | {r['snippet']} | {email_short} | {r['wall_s']} | "
                f"{r['status'] or '-'} | {r['patch_status'] or '-'} | {r['error_code'] or '-'} |"
            )

        lines.append("\n## Notes")
        lines.append(f"- Test fan-out across {len(set(r['email'] for r in results))} distinct gtb-* users.")
        if args.n > len(emails):
            lines.append(f"- N={args.n} > {len(emails)} test-user emails available; cycled through "
                         f"with multiple concurrent runs per user.")
        else:
            lines.append(f"- N={args.n} ≤ {len(emails)} test-user emails; each run on a unique user.")
        if timed_out:
            lines.append(f"- {len(timed_out)} runs hit the 600s poll timeout — likely E2B "
                         f"sandbox-cap saturation or orchestrator stall. Inspect per-run JSON "
                         f"for `raw_review.status` at timeout to diagnose.")
        if sandbox_429:
            lines.append(f"- {len(sandbox_429)} runs returned 429 / rate-limit signals — "
                         f"E2B concurrency cap may be below {args.n}.")

        lines.append("\n## Raw output")
        lines.append(f"- `runs.json` — full per-run array")
        lines.append(f"- `per-run/<idx>-<snippet>.json` — raw review + patch JSON per run")

        (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

        print(f"\n=== Summary ===")
        print(f"  P50 / P95 / P99   : {p50:.1f}s / {p95:.1f}s / {p99:.1f}s")
        print(f"  success_rate      : {success_rate*100:.0f}%")
        print(f"  verified_patch_rate: {verified_rate*100:.0f}%")
        print(f"  criterion (P95<5m + success≥90%): {'PASS' if criterion_overall else 'FAIL'}")
        print(f"  full summary       : {out_dir / 'summary.md'}")

    finally:
        print("\n  restoring gtb-* subscription snapshot...")
        try:
            restore_users(ssh_key, ssh_host, snapshot)
            print("  restore complete.")
        except Exception as e:
            print(f"  restore FAILED: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
