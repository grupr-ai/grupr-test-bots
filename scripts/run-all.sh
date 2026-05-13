#!/usr/bin/env bash
#
# Run all 5 standalone personas in sequence. Conversational is NOT
# included — it needs its own orchestration via multi-bot-workshop.sh
# because it's coordinated, not parallel.
#
# Each persona writes its own runs/{persona}-{ts}/ directory. After
# all complete, this script catenates the per-persona summary.md
# files into a top-level cross-persona summary.
#
# Usage:
#   ./scripts/run-all.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
TS=$(date -u +"%Y%m%dT%H%M%SZ")
SUITE_DIR="$HERE/runs/suite-$TS"
mkdir -p "$SUITE_DIR"

PERSONAS=(new_user power_user external_dev admin adversary)

echo "Suite: $SUITE_DIR"
echo "Personas: ${PERSONAS[*]}"
echo ""

for p in "${PERSONAS[@]}"; do
    echo "=== Running: $p ==="
    if "$HERE/scripts/run-persona.sh" "$p"; then
        echo "=== $p done ==="
    else
        echo "=== $p FAILED (continuing with the next) ===" >&2
    fi
    echo ""
done

echo "=== Building cross-persona summary ==="
SUMMARY="$SUITE_DIR/summary.md"
{
    echo "# Persona sweep — $TS"
    echo ""
    echo "Runs included (most recent per persona):"
    echo ""
    for p in "${PERSONAS[@]}"; do
        LATEST=$(ls -1dt "$HERE/runs/${p}-"* 2>/dev/null | head -1)
        if [ -n "$LATEST" ]; then
            echo "- **$p** -> \`$(basename "$LATEST")\`"
        else
            echo "- **$p** -> (no run found)"
        fi
    done
    echo ""
    echo "## P0/P1 findings across the sweep"
    echo ""
    for p in "${PERSONAS[@]}"; do
        LATEST=$(ls -1dt "$HERE/runs/${p}-"* 2>/dev/null | head -1)
        [ -z "$LATEST" ] && continue
        # Pull P0 + P1 sections from the per-persona summary.
        awk '
            /^## 🚨 P0|^## 🔴 P1/ {in_block=1; print "### From " persona; print; next}
            /^## / && in_block {in_block=0}
            in_block {print}
        ' persona="$p" "$LATEST/summary.md" 2>/dev/null || true
    done
    echo ""
    echo "## Per-persona summaries"
    echo ""
    for p in "${PERSONAS[@]}"; do
        LATEST=$(ls -1dt "$HERE/runs/${p}-"* 2>/dev/null | head -1)
        [ -z "$LATEST" ] && continue
        echo "<details>"
        echo "<summary>$p — $(basename "$LATEST")</summary>"
        echo ""
        cat "$LATEST/summary.md"
        echo ""
        echo "</details>"
        echo ""
    done
} > "$SUMMARY"

echo ""
echo "Suite summary: $SUMMARY"
