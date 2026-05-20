"""Run the multi-LLM disagreement study against the curated PR sample.

For each of the 30 PRs in prs.json:
  1. Reset the test user's trial counters (study-only bypass; gates are
     correct for real users, this is one-off research)
  2. Run a 3-Skill panel review (Architect + Security + Synthesizer)
     via lib/code_review_client.run_quick. Capture the Synthesizer
     verdict as the panel verdict.
  3. Run a single-model Claude Opus baseline review with a generic
     "review this code" prompt. Capture the single-model verdict.
  4. Persist both verdicts + the parsed verdict tag (ship /
     ship-with-changes / block) to runs/.../<repo>-<pr>/result.json

After all 30 complete, an aggregator (`aggregate.py`) computes the
headline disagreement stat.

Cost envelope (estimated):
  * 30 PRs × 1 panel run × ~$0.30 = ~$9 platform spend
  * 30 PRs × 1 single-model baseline × ~$0.10 = ~$3 platform spend
  * Total: ~$12, ~30 min wall clock

Test-user fan-out: the 8 gtb-* users are rotated round-robin so no
single user accumulates many gruprs in the DB; counters reset
between each PR via psql.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Force UTF-8 on stdout/stderr so non-ASCII (verdict glyphs, smart quotes,
# whatever bubbles up from gh CLI) doesn't crash on Windows' default cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
import anthropic

from lib.code_review_client import REVIEWERS, run_quick
from lib.user_client import UserClient


# Pre-committed 3-Skill panel for the study (drops the two groq Skills
# to dodge groq's 100K-TPD daily wall mid-run). The launch product still
# ships the full 5-Skill panel; this is just the panel used for the
# disagreement measurement.
STUDY_PANEL_ROLES = ["architect", "security", "synthesizer"]

# The single-model baseline prompt — what a developer would realistically
# paste into Claude when asking for a code review. Intentionally NOT tuned
# to match Grupr's Synthesizer prompt; that would be a strawman.
SINGLE_MODEL_PROMPT = """\
You are a senior engineer reviewing the diff below for shippability.
Read it carefully and respond with EXACTLY this format:

**Verdict**: ship | ship-with-changes | block

**Top 3 findings** (with line refs from the diff, ordered by importance)

**Notes** (lower-priority observations)
"""

SINGLE_MODEL_ID = "claude-opus-4-20250514"
SINGLE_MODEL_PROVIDER = "anthropic"

# All 8 gtb-* test users — rotated round-robin so each PR's run uses a
# fresh user. Limits per-user grupr accumulation in the DB.
TEST_USER_ENV_VARS = [
    "GRUPR_TEST_NEW_USER_EMAIL",
    "GRUPR_TEST_POWER_USER_EMAIL",
    "GRUPR_TEST_EXTERNAL_DEV_EMAIL",
    "GRUPR_TEST_CONV_A_EMAIL",
    "GRUPR_TEST_CONV_B_EMAIL",
    "GRUPR_TEST_CONV_C_EMAIL",
]

VERDICT_RE = re.compile(
    r"\*\*(?:Verdict|Consensus verdict|Overall verdict)\*\*:\s*(ship-with-changes|ship|block)",
    re.IGNORECASE,
)


def parse_verdict(text: str) -> str | None:
    """Pull the verdict tag out of a Skill / single-model review body."""
    if not text:
        return None
    m = VERDICT_RE.search(text)
    if not m:
        return None
    raw = m.group(1).lower()
    # Normalize "ship" so it doesn't match "ship-with-changes" substring
    if raw == "ship":
        return "ship"
    if raw == "ship-with-changes":
        return "ship-with-changes"
    if raw == "block":
        return "block"
    return None


def reset_trial_counters(ssh_key: str, ssh_host: str) -> bool:
    """psql via SSH to reset trial counters for all gtb-* users.

    Study-only bypass of the trial gates I shipped today — the gates
    are correct for real users; the study runs as research outside
    the user-cost-gate envelope. Called between each PR.
    """
    sql = (
        "UPDATE subscriptions SET trial_quick_used=0, trial_deep_used=0 "
        "WHERE user_id IN (SELECT user_id FROM users WHERE username LIKE 'gtb-%');"
    )
    cmd = [
        "ssh", "-i", ssh_key, "-o", "StrictHostKeyChecking=no",
        ssh_host,
        f"docker exec -i grupr-postgres psql -U grupr -d grupr -c \"{sql}\"",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.returncode == 0
    except Exception as e:
        print(f"  WARN: trial-counter reset failed: {e}", file=sys.stderr)
        return False


def run_single_model_baseline(diff: str, api_key: str) -> dict:
    """One Claude Opus call with a generic 'review this code' prompt.

    Returns {"verdict": str|None, "content": str, "elapsed_s": int,
    "input_tokens": int, "output_tokens": int}.
    """
    t0 = time.monotonic()
    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=SINGLE_MODEL_ID,
            max_tokens=2048,
            system=SINGLE_MODEL_PROMPT,
            messages=[
                {"role": "user", "content": f"```diff\n{diff}\n```"},
            ],
        )
    except Exception as e:
        return {
            "verdict": None,
            "content": "",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_s": int(time.monotonic() - t0),
            "input_tokens": 0,
            "output_tokens": 0,
        }
    content_text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            content_text += block.text
    return {
        "verdict": parse_verdict(content_text),
        "content": content_text,
        "elapsed_s": int(time.monotonic() - t0),
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }


def run_panel(client: UserClient, diff: str, review_name: str) -> dict:
    """Run a 3-Skill panel (Architect + Security + Synthesizer) on the
    diff. Returns {"verdict": str|None, "synth_content": str,
    "per_skill": [{reviewer, verdict, content}], "elapsed_s": int,
    "raw_grupr_id": str, "timed_out": bool}.
    """
    # Temporarily mutate REVIEWERS to the study subset. The library lets
    # callers swap the roster by mutating the list directly; cleaner
    # long-term to pass a roster arg into run_quick, but this is a
    # one-off study script.
    from lib import code_review_client
    original_reviewers = code_review_client.REVIEWERS
    study_subset = [r for r in original_reviewers if r["role"] in STUDY_PANEL_ROLES]
    code_review_client.REVIEWERS = study_subset
    try:
        result = run_quick(client, code=diff, review_name=review_name, poll_timeout_s=240)
    finally:
        code_review_client.REVIEWERS = original_reviewers

    synth_verdict_text = result.get("synthesizer_verdict", "") or ""
    return {
        "verdict": parse_verdict(synth_verdict_text),
        "synth_content": synth_verdict_text,
        "per_skill": [
            {
                "reviewer": v["reviewer"],
                "verdict": parse_verdict(v.get("content", "")),
                "content": v.get("content", ""),
            }
            for v in result.get("verdicts", [])
        ],
        "elapsed_s": result.get("elapsed_s", 0),
        "raw_grupr_id": result.get("grupr_id", ""),
        "timed_out": result.get("timed_out", False),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("study_dir", help="Path to runs/disagreement-study-<ts>/ from select_prs.py")
    parser.add_argument("--start", type=int, default=0, help="Start index in prs.json (resume support)")
    parser.add_argument("--limit", type=int, default=0, help="Cap how many to process this run (0=all)")
    args = parser.parse_args()

    load_dotenv(override=True)
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    ssh_key = os.environ.get("EC2_SSH_KEY", "G:/My Drive/BB/Dev/ClawdBot/kalshi-bot-key.pem")
    ssh_host = os.environ.get("EC2_SSH_HOST", "ubuntu@18.224.174.100")

    if not (password and anthropic_key):
        print("ERROR: GRUPR_TEST_PASSWORD + ANTHROPIC_API_KEY required", file=sys.stderr)
        return 2

    study_dir = Path(args.study_dir)
    prs_path = study_dir / "prs.json"
    if not prs_path.exists():
        print(f"ERROR: {prs_path} not found — run select_prs.py first", file=sys.stderr)
        return 2

    prs = json.loads(prs_path.read_text(encoding="utf-8"))
    if args.limit > 0:
        prs = prs[args.start : args.start + args.limit]
    else:
        prs = prs[args.start :]
    print(f"=== Study run: {len(prs)} PRs (starting at index {args.start}) ===\n")

    results_dir = study_dir / "results"
    results_dir.mkdir(exist_ok=True)

    started = time.monotonic()
    for i, pr in enumerate(prs):
        repo = pr["repo"]
        pr_num = pr["pr_number"]
        slug = f"{repo.replace('/', '__')}-{pr_num}"
        result_path = results_dir / f"{slug}.json"

        if result_path.exists():
            print(f"[{i+1}/{len(prs)}] {slug} — already done, skip")
            continue

        diff_path = study_dir / "diffs" / f"{slug}.diff"
        if not diff_path.exists():
            print(f"[{i+1}/{len(prs)}] {slug} — diff file missing, skip")
            continue
        diff = diff_path.read_text(encoding="utf-8")

        # Rotate which test user runs this PR.
        email_var = TEST_USER_ENV_VARS[i % len(TEST_USER_ENV_VARS)]
        email = os.environ.get(email_var)
        if not email:
            print(f"  ERROR: env var {email_var} unset, skip")
            continue

        print(f"[{i+1}/{len(prs)}] {slug} (LoC={pr['diff_loc']}, user={email_var})")

        # 1. Reset trial counters so the panel run isn't gated.
        reset_trial_counters(ssh_key, ssh_host)

        # 2. Panel review.
        try:
            with UserClient(base_url=api_base) as client:
                client.login(email, password)
                panel_t0 = time.monotonic()
                panel = run_panel(client, diff, review_name=f"disagreement-study {slug}")
                panel_wall = int(time.monotonic() - panel_t0)
        except Exception as e:
            print(f"  PANEL FAIL: {type(e).__name__}: {e}")
            panel = {"verdict": None, "error": f"{type(e).__name__}: {e}"}
            panel_wall = 0

        # 3. Single-model baseline.
        try:
            single_t0 = time.monotonic()
            single = run_single_model_baseline(diff, anthropic_key)
            single_wall = int(time.monotonic() - single_t0)
        except Exception as e:
            print(f"  SINGLE FAIL: {type(e).__name__}: {e}")
            single = {"verdict": None, "error": f"{type(e).__name__}: {e}"}
            single_wall = 0

        # 4. Persist.
        record = {
            "pr": {
                "repo": repo,
                "pr_number": pr_num,
                "url": pr["url"],
                "title": pr["title"],
                "merged_at": pr["merged_at"],
                "diff_loc": pr["diff_loc"],
            },
            "panel": panel,
            "panel_wall_s": panel_wall,
            "single_model": single,
            "single_wall_s": single_wall,
            "user_email_var": email_var,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        result_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

        # 5. Print one-line summary
        pv = panel.get("verdict") or "?"
        sv = single.get("verdict") or "?"
        disagree = "DIFFERS" if pv != sv else "agrees"
        print(f"    panel={pv} {disagree} single={sv} (panel {panel_wall}s, single {single_wall}s)")

    total = int(time.monotonic() - started)
    print(f"\n=== Done in {total}s ({total // 60}m {total % 60}s) ===")
    print(f"Aggregate: python scripts/disagreement-study/aggregate.py {study_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
