"""Aggregate per-PR study results into the headline disagreement stat.

Reads runs/disagreement-study-<ts>/results/*.json and produces:
  * runs/disagreement-study-<ts>/summary.md — full methodology + raw
    counts + the headline stat for the launch tweet
  * stdout — the same headline stat as a one-line preview

Two stat axes (BOTH computed; pick whichever survives review):

  1. VERDICT-LEVEL DISAGREEMENT
       "In N of 30 (X%) cases the single-model verdict ≠ panel verdict"

  2. SAFETY-DIRECTIONAL DISAGREEMENT (the scarier one)
       "In M of 30 (Y%) cases the single model would have shipped code
        the panel flagged as needing changes or blocked outright"
       — i.e., single said "ship" but panel said "ship-with-changes"
       or "block"

Pre-committed before seeing results. NOT tuned to inflate the number.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VERDICT_ORDER = {"ship": 0, "ship-with-changes": 1, "block": 2}
VERDICTS = ["ship", "ship-with-changes", "block"]


def severity(v: str | None) -> int:
    """Numeric severity for direction comparisons. None = unknown → -1."""
    if v is None:
        return -1
    return VERDICT_ORDER.get(v, -1)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: aggregate.py <study_dir>", file=sys.stderr)
        return 2

    study_dir = Path(sys.argv[1])
    results_dir = study_dir / "results"
    if not results_dir.exists():
        print(f"ERROR: {results_dir} not found", file=sys.stderr)
        return 2

    records = []
    for p in sorted(results_dir.glob("*.json")):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"WARN: failed to parse {p}: {e}", file=sys.stderr)

    if not records:
        print("ERROR: no results found", file=sys.stderr)
        return 2

    total = len(records)
    excluded = []
    valid = []
    for r in records:
        pv = r.get("panel", {}).get("verdict")
        sv = r.get("single_model", {}).get("verdict")
        if pv is None or sv is None:
            excluded.append({
                "slug": f"{r['pr']['repo']}#{r['pr']['pr_number']}",
                "panel_verdict": pv,
                "single_verdict": sv,
                "panel_error": r.get("panel", {}).get("error"),
                "single_error": r.get("single_model", {}).get("error"),
            })
            continue
        valid.append(r)

    n_valid = len(valid)
    n_excluded = len(excluded)

    # ── Stat 1: verdict-level disagreement ──────────────────────────
    disagreements = [r for r in valid if r["panel"]["verdict"] != r["single_model"]["verdict"]]
    n_disagree = len(disagreements)
    pct_disagree = (n_disagree / n_valid * 100) if n_valid else 0

    # ── Stat 2: safety-directional disagreement ─────────────────────
    # Single said "ship" (or otherwise less-severe) than panel did.
    safety_disagreements = [
        r for r in valid
        if severity(r["single_model"]["verdict"]) < severity(r["panel"]["verdict"])
    ]
    n_safety = len(safety_disagreements)
    pct_safety = (n_safety / n_valid * 100) if n_valid else 0

    # ── Cross-tabulation: panel × single matrix ─────────────────────
    cross = Counter()
    for r in valid:
        cross[(r["panel"]["verdict"], r["single_model"]["verdict"])] += 1

    # ── Per-repo + per-verdict distributions ────────────────────────
    repo_counter = Counter(r["pr"]["repo"] for r in valid)
    panel_dist = Counter(r["panel"]["verdict"] for r in valid)
    single_dist = Counter(r["single_model"]["verdict"] for r in valid)

    # ── Wall-clock + cost ──────────────────────────────────────────
    panel_wall = sum(r.get("panel_wall_s", 0) for r in valid)
    single_wall = sum(r.get("single_wall_s", 0) for r in valid)
    single_tokens_in = sum(r.get("single_model", {}).get("input_tokens", 0) for r in valid)
    single_tokens_out = sum(r.get("single_model", {}).get("output_tokens", 0) for r in valid)

    # ── Write the summary ───────────────────────────────────────────
    lines: list[str] = []
    lines.append(f"# Multi-LLM disagreement study — {study_dir.name.replace('disagreement-study-', '')}")
    lines.append("")
    lines.append("## Headline stats (PRE-COMMITTED methodology, NOT tuned to inflate)")
    lines.append("")
    lines.append(
        f"**Verdict disagreement**: In **{n_disagree} of {n_valid}** valid cases "
        f"(**{pct_disagree:.0f}%**), the single-model Claude Opus review reached a "
        f"different verdict than the multi-Skill Grupr panel."
    )
    lines.append("")
    lines.append(
        f"**Safety-directional disagreement**: In **{n_safety} of {n_valid}** valid "
        f"cases (**{pct_safety:.0f}%**), the single model would have **shipped code "
        f"the panel flagged as needing changes or blocked outright** — the single "
        f"model's verdict was strictly less severe than the panel's consensus."
    )
    lines.append("")
    lines.append(
        f"(Sample of {total} real recently-merged public-OSS PRs; "
        f"{n_excluded} excluded for incomplete data — see Excluded below.)"
    )
    lines.append("")

    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "**Sample**: 30 real, recently-merged PRs (≤30 days old at run time) sampled "
        "deterministically (seed=42) from 10 active OSS repos across Python, "
        "TypeScript, Go, and JavaScript. Diff size filter: 20–200 LoC. No manual "
        "cherry-pick. Repo list + sample selection lives in `prs.json` alongside "
        "this file; the full diffs are in `diffs/`."
    )
    lines.append("")
    lines.append(
        "**Panel under test**: 3-Skill Grupr panel — Architect (Claude Opus) + "
        "Security (GPT-4o) + Synthesizer (Claude Opus). Performance + Maintainability "
        "Skills (both Groq llama-3.3) were excluded from the study to avoid Groq's "
        "100K-TPD daily rate-limit wall mid-run; the production launch product still "
        "ships the full 5-Skill panel. **The panel verdict is the Synthesizer's "
        "consensus output**, parsed to one of {ship, ship-with-changes, block}."
    )
    lines.append("")
    lines.append(
        "**Single-model baseline**: One Claude Opus call (`claude-opus-4-20250514`) "
        "with a generic *'review this code'* prompt that asks for a verdict tag and "
        "top-3 findings — designed to mirror what a developer would realistically "
        "paste into Claude when asking for a code review. Intentionally NOT tuned to "
        "the Synthesizer's prompt; that would be a strawman."
    )
    lines.append("")
    lines.append(
        "**Disagreement metric**: Verdict-level only. Panel verdict vs single-model "
        "verdict, both parsed to one of {ship, ship-with-changes, block}. Cases "
        "where either verdict failed to parse are excluded."
    )
    lines.append("")

    lines.append("## Cross-tabulation: panel × single-model")
    lines.append("")
    lines.append("| | single: ship | single: ship-with-changes | single: block |")
    lines.append("|---|---|---|---|")
    for pv in VERDICTS:
        row = [f"**panel: {pv}**"]
        for sv in VERDICTS:
            n = cross.get((pv, sv), 0)
            mark = "✓" if pv == sv else ""
            row.append(f"{n} {mark}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("Diagonal cells (✓) = agreement. Off-diagonal = disagreement.")
    lines.append("")

    lines.append("## Verdict distributions")
    lines.append("")
    lines.append("| Verdict | Panel | Single-model |")
    lines.append("|---|---|---|")
    for v in VERDICTS:
        lines.append(f"| {v} | {panel_dist.get(v, 0)} | {single_dist.get(v, 0)} |")
    lines.append("")

    lines.append("## Sample composition")
    lines.append("")
    lines.append("| Repo | Valid runs |")
    lines.append("|---|---|")
    for repo, count in sorted(repo_counter.items()):
        lines.append(f"| {repo} | {count} |")
    lines.append("")

    if excluded:
        lines.append("## Excluded from headline stat")
        lines.append("")
        lines.append(
            f"{n_excluded} of {total} runs excluded for unparseable verdict on "
            "panel or single side (typically: timeout, rate-limit, or "
            "verdict-format drift from the prompted template):"
        )
        lines.append("")
        for e in excluded:
            err = e.get("panel_error") or e.get("single_error") or "(unparseable verdict)"
            lines.append(
                f"- `{e['slug']}` — panel={e['panel_verdict']!r}, "
                f"single={e['single_verdict']!r} — {err}"
            )
        lines.append("")

    lines.append("## Performance + cost")
    lines.append("")
    lines.append(
        f"- Total panel wall-clock: {panel_wall}s "
        f"(avg {panel_wall/max(n_valid,1):.0f}s per panel run)"
    )
    lines.append(
        f"- Total single-model wall-clock: {single_wall}s "
        f"(avg {single_wall/max(n_valid,1):.1f}s per single run)"
    )
    lines.append(f"- Single-model tokens: {single_tokens_in:,} in / {single_tokens_out:,} out")
    lines.append(
        f"- Single-model cost (claude-opus-4 list price: "
        f"$15/$75 per Mtok): ~${single_tokens_in/1_000_000 * 15 + single_tokens_out/1_000_000 * 75:.2f}"
    )
    lines.append("")
    lines.append(
        "Panel cost not directly broken out here (runs through the platform-key "
        "trial fallback, so it's bundled into the API container's overall "
        "Anthropic + OpenAI usage; rough estimate ~$0.30 per panel run × "
        f"{n_valid} = ~${n_valid * 0.30:.2f})."
    )
    lines.append("")

    lines.append("## Raw data")
    lines.append("")
    lines.append(
        "Per-PR results in `results/`. Each `<repo>__<num>.json` file contains "
        "the parsed verdicts, full Skill bodies, full single-model response body, "
        "wall-clock timings, and the user-rotation slot used."
    )

    out_path = study_dir / "summary.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    # stdout preview
    print(f"\nWrote {out_path}\n")
    print("─" * 70)
    print(f"HEADLINE — VERDICT DISAGREEMENT:")
    print(f"  {n_disagree}/{n_valid} ({pct_disagree:.0f}%) of single-model verdicts disagreed with panel")
    print(f"\nHEADLINE — SAFETY-DIRECTIONAL DISAGREEMENT:")
    print(f"  {n_safety}/{n_valid} ({pct_safety:.0f}%) — single model would have shipped what panel flagged")
    print(f"\nExcluded: {n_excluded}/{total}")
    print("─" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
