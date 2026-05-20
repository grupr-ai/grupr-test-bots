#!/usr/bin/env bash
#
# Run all 4 bot-build envs serially and emit a top-level comparison.
#
# Envs:
#   A — urlshortener × role-divided
#   B — urlshortener × free-form workshop
#   C — fetcher × role-divided
#   D — fetcher × free-form workshop
#
# Each env hits Quick (v1) then Deep (v2). Wall-clock ~10–25 min total
# depending on Deep tier latency. Failures in any env do NOT abort the
# others — the top-level summary surfaces what worked + what didn't.
#
# Usage:
#   ./scripts/run-all-codebuild.sh

set -uo pipefail   # no -e — we want failures in one env to not kill the suite

HERE="$(cd "$(dirname "$0")/.." && pwd)"
TS=$(date -u +"%Y%m%dT%H%M%SZ")
SUITE_DIR="$HERE/runs/codebuild-$TS"
mkdir -p "$SUITE_DIR"

LOG="$SUITE_DIR/_suite.log"

echo "Suite dir: $SUITE_DIR"
echo "Tail with: tail -f $LOG"
echo ""

# Format: env-id|app|mode
ENVS=(
    "A|urlshortener|role"
    "B|urlshortener|workshop"
    "C|fetcher|role"
    "D|fetcher|workshop"
)

declare -A ENV_STATUS
for spec in "${ENVS[@]}"; do
    IFS='|' read -r EID APP MODE <<< "$spec"
    echo "=== Env $EID :: $APP :: $MODE ===" | tee -a "$LOG"
    BEGAN=$(date +%s)
    if bash "$HERE/scripts/run-bots-build-app.sh" \
            --app "$APP" --mode "$MODE" --env-id "$EID" \
            --suite-dir "$SUITE_DIR" >> "$LOG" 2>&1; then
        ENV_STATUS["$EID"]="ok"
    else
        ENV_STATUS["$EID"]="failed"
    fi
    ELAPSED=$(( $(date +%s) - BEGAN ))
    echo "Env $EID -> ${ENV_STATUS[$EID]} in ${ELAPSED}s" | tee -a "$LOG"
    echo "" | tee -a "$LOG"
done

# ── Top-level summary ───────────────────────────────────────────────
echo "--- building top-level summary ---" | tee -a "$LOG"
python - <<PYEOF
import json, pathlib

SUITE = pathlib.Path("$SUITE_DIR")
ENV_SPECS = [
    ("A", "urlshortener", "role"),
    ("B", "urlshortener", "workshop"),
    ("C", "fetcher", "role"),
    ("D", "fetcher", "workshop"),
]

rows = []
for eid, app, mode in ENV_SPECS:
    env_dir = SUITE / f"{eid}-{app}-{mode}"
    if not env_dir.exists():
        rows.append({"env": eid, "app": app, "mode": mode, "status": "missing"})
        continue
    quick_path = env_dir / "v1/quick-review.json"
    deep_path  = env_dir / "v2/deep-review.json"
    v1_path    = env_dir / "v1/code.py"
    v2_path    = env_dir / "v2/code.py"
    patch_path = env_dir / "v2/deep-patch.diff"
    row = {
        "env": eid, "app": app, "mode": mode,
        "v1_lines": v1_path.read_text(encoding="utf-8").count("\n") if v1_path.exists() else 0,
        "v2_lines": v2_path.read_text(encoding="utf-8").count("\n") if v2_path.exists() else 0,
        "patch_lines": patch_path.read_text(encoding="utf-8").count("\n") if patch_path.exists() else 0,
    }
    if quick_path.exists():
        q = json.loads(quick_path.read_text(encoding="utf-8"))
        row["quick_reviewers"] = f"{q.get('reviewer_count_returned')}/{q.get('expected_count')}"
        row["quick_elapsed_s"] = q.get("elapsed_s")
        row["quick_synth"] = "ok" if (q.get("synthesizer_verdict") or "").strip() else "empty"
    else:
        row["quick_reviewers"] = "—"
        row["quick_elapsed_s"] = None
        row["quick_synth"] = "—"
    if deep_path.exists():
        d = json.loads(deep_path.read_text(encoding="utf-8"))
        row["deep_status"]   = d.get("status") or "—"
        row["deep_elapsed_s"] = d.get("elapsed_s")
        row["patch_status"]   = d.get("patch_status") or "—"
        row["deep_error"]     = d.get("error_code") or ""
    else:
        row["deep_status"] = "—"
        row["deep_elapsed_s"] = None
        row["patch_status"] = "—"
        row["deep_error"] = ""
    rows.append(row)

lines = []
lines.append(f"# Codebuild suite — $TS")
lines.append("")
lines.append("4-env matrix: {app × mode} → Quick(v1) → Iterator → Deep(v2).")
lines.append("")
lines.append("## Headline table")
lines.append("")
lines.append("| Env | App | Mode | v1 LoC | v2 LoC | Quick (R/T) | Quick synth | Deep status | Patch status | Patch LoC |")
lines.append("|-----|-----|------|--------|--------|-------------|-------------|-------------|--------------|-----------|")
for r in rows:
    lines.append(
        f"| {r['env']} | {r['app']} | {r['mode']} | "
        f"{r.get('v1_lines',0)} | {r.get('v2_lines',0)} | "
        f"{r.get('quick_reviewers','—')} ({r.get('quick_elapsed_s')}s) | {r.get('quick_synth','—')} | "
        f"{r.get('deep_status','—')} ({r.get('deep_elapsed_s')}s) | "
        f"{r.get('patch_status','—')} | {r.get('patch_lines',0)} |"
    )

# Verification: ≥75% of envs should produce a verified patch.
verified = sum(1 for r in rows if r.get("patch_status") == "verified")
total = len(rows)
lines.append("")
lines.append(f"**Verified patches**: {verified}/{total} envs (target: ≥{int(total*0.75)}/4)")

failed = [r for r in rows if r.get("status") == "missing" or r.get("deep_status") not in ("completed", "—") or r.get("deep_error")]
if failed:
    lines.append("")
    lines.append("## Issues")
    for r in failed:
        if r.get("status") == "missing":
            lines.append(f"- **{r['env']}**: env dir not produced (build phase aborted)")
        else:
            err = f"deep_error=[{r.get('deep_error')}]" if r.get("deep_error") else f"deep_status={r.get('deep_status')}"
            lines.append(f"- **{r['env']}**: {err}")

lines.append("")
lines.append("## Per-env links")
for r in rows:
    lines.append(f"- **{r['env']}** ([summary](./{r['env']}-{r['app']}-{r['mode']}/summary.md))")

(SUITE / "summary.md").write_text("\n".join(lines), encoding="utf-8")
print(f"summary -> {SUITE / 'summary.md'}")
PYEOF

echo ""
echo "Suite: $SUITE_DIR/summary.md"
