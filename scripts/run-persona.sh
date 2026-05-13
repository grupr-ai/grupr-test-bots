#!/usr/bin/env bash
#
# Run one persona by name. Wrapper over `python -m personas.{name}`
# that activates the venv first.
#
# Usage:
#   ./scripts/run-persona.sh new_user
#   ./scripts/run-persona.sh power_user
#   ./scripts/run-persona.sh external_dev
#   ./scripts/run-persona.sh admin
#   ./scripts/run-persona.sh adversary

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <persona-name>" >&2
    echo "valid: new_user power_user external_dev admin adversary" >&2
    exit 2
fi

PERSONA="$1"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -d "$HERE/.venv" ]; then
    echo "venv missing — run: cd $HERE && python -m venv .venv && source .venv/Scripts/activate && pip install -e ." >&2
    exit 2
fi

# Activate. Cross-platform-ish: Windows Git Bash uses .venv/Scripts, Unix uses .venv/bin.
if [ -f "$HERE/.venv/Scripts/activate" ]; then
    source "$HERE/.venv/Scripts/activate"
else
    source "$HERE/.venv/bin/activate"
fi

cd "$HERE"
exec python -m "personas.$PERSONA"
