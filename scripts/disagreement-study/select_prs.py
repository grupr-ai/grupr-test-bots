"""Curate a deterministic sample of real, recently-merged public OSS PRs
for the multi-LLM disagreement study.

Methodology (pre-committed, NOT tuned after seeing results):
  * 10 active OSS repos across Python + TypeScript + Go + Rust + JS
  * Recent merged PRs only (last 30 days — reduces train-set contamination
    for models with older cutoff dates)
  * 20–200 LoC diff size filter (small enough to fit context windows
    without truncation; large enough to give reviewers signal)
  * Random sample of 30 from the qualified pool, deterministic seed
  * No manual cherry-pick; selection is fully scripted

Output: a JSON file at runs/disagreement-study-<ts>/prs.json with
{repo, pr_number, url, title, diff_loc, diff} for each of the 30 PRs.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path


# 10 repos curated for the study. Picked for: (a) active development,
# (b) merged PRs frequently, (c) varied languages, (d) varied domains,
# (e) NOT owned by Grupr / any model provider (avoid self-bias).
REPOS = [
    "django/django",            # Python — web framework
    "pallets/flask",            # Python — web micro-framework
    "fastapi/fastapi",          # Python — async web framework
    "psf/requests",             # Python — HTTP lib
    "vercel/next.js",           # TypeScript — React framework
    "denoland/deno",            # TypeScript / Rust — runtime
    "remix-run/react-router",   # TypeScript — routing
    "gohugoio/hugo",            # Go — static site generator
    "spf13/cobra",              # Go — CLI lib
    "expressjs/express",        # JavaScript — web framework
]

SAMPLE_SIZE = 30
MIN_DIFF_LOC = 20
MAX_DIFF_LOC = 200
LOOKBACK_DAYS = 30
RANDOM_SEED = 42
PR_FETCH_LIMIT_PER_REPO = 50  # raw PRs to fetch per repo before filtering


def sh(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Run a shell command, return (returncode, stdout, stderr).

    Force UTF-8 decode because gh outputs PR titles + diffs that
    routinely contain non-ASCII (em-dashes, smart quotes, emoji); the
    Windows default cp1252 codec crashes on them.
    """
    p = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    return p.returncode, p.stdout or "", p.stderr or ""


def list_recent_merged_prs(repo: str, limit: int) -> list[dict]:
    """gh pr list --state merged → most-recent N merged PRs in a repo.

    Returns minimal {number, title, mergedAt, url} dicts.
    """
    rc, out, err = sh([
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "merged",
        "--limit", str(limit),
        "--json", "number,title,mergedAt,url",
    ], timeout=60)
    if rc != 0:
        print(f"  WARN: gh pr list failed for {repo}: {err.strip()[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def pr_diff(repo: str, pr_number: int) -> str | None:
    """gh pr diff → the unified diff text. Returns None on failure."""
    rc, out, _ = sh(
        ["gh", "pr", "diff", str(pr_number), "--repo", repo],
        timeout=60,
    )
    if rc != 0 or not out.strip():
        return None
    return out


def count_diff_loc(diff: str) -> int:
    """Count added + removed lines in a unified diff (excluding context)."""
    n = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            n += 1
    return n


def is_within_lookback(merged_at: str, days: int) -> bool:
    """merged_at is RFC3339; compare to NOW - days."""
    try:
        from datetime import datetime, timezone, timedelta
        merged = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return merged >= cutoff
    except Exception:
        return True  # be lenient on parse failure


def main() -> int:
    print(f"=== Selecting {SAMPLE_SIZE} PRs across {len(REPOS)} repos ===")
    print(f"Filter: {MIN_DIFF_LOC}-{MAX_DIFF_LOC} LoC diff, merged within {LOOKBACK_DAYS} days")
    print(f"Seed: {RANDOM_SEED}")
    print()

    # Step 1 — collect candidate PRs per repo
    candidates: list[dict] = []
    for repo in REPOS:
        print(f"  fetching {repo}…", end=" ", flush=True)
        prs = list_recent_merged_prs(repo, PR_FETCH_LIMIT_PER_REPO)
        recent = [p for p in prs if is_within_lookback(p.get("mergedAt", ""), LOOKBACK_DAYS)]
        print(f"got {len(prs)} raw, {len(recent)} within {LOOKBACK_DAYS}d")
        for p in recent:
            p["repo"] = repo
            candidates.append(p)
        time.sleep(0.4)  # be polite to GH api

    print(f"\nTotal candidates before diff-filter: {len(candidates)}")

    # Step 2 — pull diff for each + filter on LoC
    print(f"\n  filtering by diff LoC ({MIN_DIFF_LOC}-{MAX_DIFF_LOC})…")
    qualified: list[dict] = []
    for i, c in enumerate(candidates):
        diff = pr_diff(c["repo"], c["number"])
        if diff is None:
            continue
        loc = count_diff_loc(diff)
        if not (MIN_DIFF_LOC <= loc <= MAX_DIFF_LOC):
            continue
        qualified.append({
            "repo": c["repo"],
            "pr_number": c["number"],
            "url": c["url"],
            "title": c["title"],
            "merged_at": c["mergedAt"],
            "diff_loc": loc,
            "diff": diff,
        })
        if (i + 1) % 20 == 0:
            print(f"    progress: {i+1}/{len(candidates)}, qualified so far: {len(qualified)}")

    print(f"\nQualified pool: {len(qualified)}")

    if len(qualified) < SAMPLE_SIZE:
        print(f"ERROR: only {len(qualified)} PRs in qualified pool, need {SAMPLE_SIZE}", file=sys.stderr)
        print("  → either widen lookback, widen LoC band, or add more repos", file=sys.stderr)
        return 2

    # Step 3 — random sample of 30, deterministic seed
    rng = random.Random(RANDOM_SEED)
    sample = rng.sample(qualified, SAMPLE_SIZE)
    # Sort by repo+pr for stable output
    sample.sort(key=lambda p: (p["repo"], p["pr_number"]))

    # Step 4 — write to disk
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = Path(__file__).parent.parent.parent / "runs" / f"disagreement-study-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "prs.json"

    # Strip diff bodies from the top-level summary file (too big for git);
    # save full diffs separately as one JSON per PR.
    summary = []
    for p in sample:
        diff_path = out_dir / f"diffs/{p['repo'].replace('/', '__')}-{p['pr_number']}.diff"
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(p["diff"], encoding="utf-8")
        summary.append({k: v for k, v in p.items() if k != "diff"})

    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nWrote {SAMPLE_SIZE} PRs to {out_path}")
    print(f"Diffs written to {out_dir / 'diffs/'}")
    print(f"\nSample distribution by repo:")
    from collections import Counter
    for repo, count in sorted(Counter(p["repo"] for p in sample).items()):
        print(f"  {repo:35s} {count}")

    print(f"\nNEXT: bash scripts/disagreement-study/run_study.sh {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
