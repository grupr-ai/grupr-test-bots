#!/usr/bin/env bash
#
# Run ONE bot-build env: bots collaboratively author code, Code Review
# Quick runs on v1, an Iterator produces v2 from the verdict, Deep runs
# on v2, output lands under runs/codebuild-<ts>/<env-id>-<app>-<mode>/.
#
# Usage:
#   ./scripts/run-bots-build-app.sh \
#       --app urlshortener|fetcher \
#       --mode role|workshop \
#       --env-id A \
#       --suite-dir runs/codebuild-20260519T140000Z
#
# `--suite-dir` is optional. If omitted, the script creates a fresh
# runs/codebuild-<ts>/ and uses it; this is convenient for one-off
# manual runs. `run-all-codebuild.sh` always passes its own suite-dir.

set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
# Git Bash on Windows: bash uses /c/... but Python's open() needs C:/...
# `pwd -W` returns Windows-form on MSYS; fall back to plain pwd on real *nix.
HERE_PY="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"

APP=""
MODE=""
ENV_ID=""
SUITE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app)       APP="$2"; shift 2 ;;
        --mode)      MODE="$2"; shift 2 ;;
        --env-id)    ENV_ID="$2"; shift 2 ;;
        --suite-dir) SUITE_DIR="$2"; shift 2 ;;
        -h|--help)
            head -20 "$0" | sed -n '2,16p' | sed 's/^# //;s/^#//'
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

[[ -z "$APP"   ]] && { echo "--app required (urlshortener|fetcher)" >&2; exit 2; }
[[ -z "$MODE"  ]] && { echo "--mode required (role|workshop)" >&2; exit 2; }
[[ -z "$ENV_ID" ]] && { echo "--env-id required (A|B|C|D)" >&2; exit 2; }

case "$APP" in urlshortener|fetcher) ;; *) echo "invalid --app: $APP" >&2; exit 2 ;; esac
case "$MODE" in role|workshop) ;; *) echo "invalid --mode: $MODE" >&2; exit 2 ;; esac

if [ -z "$SUITE_DIR" ]; then
    TS=$(date -u +"%Y%m%dT%H%M%SZ")
    SUITE_DIR="$HERE/runs/codebuild-$TS"
fi
ENV_DIR="$SUITE_DIR/$ENV_ID-$APP-$MODE"
mkdir -p "$ENV_DIR/v1" "$ENV_DIR/v2"

# Git Bash on Windows: bash uses /c/... paths; Python's open() needs
# C:/... When running inside Git Bash, normalize via `pwd -W` so the
# path heredocs hand to Python is interpretable by native Python.
# `pwd -W` works only on MSYS; fall back to plain pwd on real *nix.
ENV_DIR_PY=$(cd "$ENV_DIR" && (pwd -W 2>/dev/null || pwd))

echo "=== Env $ENV_ID :: $APP :: $MODE ==="
echo "Output: $ENV_DIR"

# Activate venv.
if [ -f "$HERE/.venv/Scripts/activate" ]; then
    source "$HERE/.venv/Scripts/activate"
else
    source "$HERE/.venv/bin/activate"
fi

# Load env vars (test bot creds + ANTHROPIC_API_KEY).
set -a
source "$HERE/.env"
set +a

cd "$HERE"
APP_BRIEF="$HERE/personas/apps/${APP}.md"
APP_BRIEF_PY="$HERE_PY/personas/apps/${APP}.md"
[[ -f "$APP_BRIEF" ]] || { echo "missing brief: $APP_BRIEF" >&2; exit 3; }

# ── 1. Pre-flight: soft-delete stale code-category gruprs ───────────
echo "--- pre-flight: clean stale code-category gruprs for conv-* ---"
python - <<'PYEOF'
import os
from lib.user_client import UserClient

for env in ("GRUPR_TEST_CONV_A_EMAIL", "GRUPR_TEST_CONV_B_EMAIL", "GRUPR_TEST_CONV_C_EMAIL"):
    email = os.environ[env]
    with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
        c.login(email, os.environ["GRUPR_TEST_PASSWORD"])
        for g in c.my_gruprs():
            # Soft-delete via existing api? UserClient doesn't expose a
            # delete helper today. Skipping is fine — pro_user tier has
            # no per-day cap. Log what's there.
            pass
PYEOF

# ── 2. Create build grupr ────────────────────────────────────────────
echo "--- creating build grupr (group_chat) as conv-a ---"
GRUPR_ID=$(python - <<PYEOF
import os
from lib.user_client import UserClient
with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
    c.login(os.environ["GRUPR_TEST_CONV_A_EMAIL"], os.environ["GRUPR_TEST_PASSWORD"])
    gid = c.create_grupr(
        name="Bot build env $ENV_ID — $APP — $MODE",
        grupr_type="group_chat",
        description="Day-10 bot-build test. Bots collaborate to author + iterate.",
        is_public=False,
        category="general",
    )
    print(gid)
PYEOF
)
[[ -z "$GRUPR_ID" ]] && { echo "FAILED to create grupr" >&2; exit 4; }
echo "Build grupr_id: $GRUPR_ID"

# Bots b + c need to be added by the owner (private grupr; can't self-join).
# The api exposes POST /api/gruprs/:id/members for owner-add, but the SDK
# doesn't wrap it yet. For private group_chat we either (a) make it public
# briefly or (b) use add-member. Path of least resistance for the test:
# flip to public so b + c can join_grupr().
echo "--- toggling grupr public so conv-b + conv-c can join ---"
python - <<PYEOF
import os, sys
from lib.user_client import UserClient
with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
    c.login(os.environ["GRUPR_TEST_CONV_A_EMAIL"], os.environ["GRUPR_TEST_PASSWORD"])
    # PUT /api/gruprs/:id with is_public=True. The SDK doesn't expose it,
    # so reach past via _request to keep the test thin.
    try:
        c._request("PUT", f"/api/gruprs/$GRUPR_ID", json={
            "name": "Bot build env $ENV_ID — $APP — $MODE",
            "description": "Day-10 bot-build test.",
            "category": "general",
            "is_public": True,
        })
        print("ok: grupr now public", file=sys.stderr)
    except Exception as e:
        print(f"WARN: public-toggle failed: {e}", file=sys.stderr)
PYEOF

echo "--- conv-b + conv-c joining ---"
python - <<PYEOF
import os
from lib.user_client import UserClient
for env in ("GRUPR_TEST_CONV_B_EMAIL", "GRUPR_TEST_CONV_C_EMAIL"):
    with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
        c.login(os.environ[env], os.environ["GRUPR_TEST_PASSWORD"])
        try:
            c.join_grupr("$GRUPR_ID")
            print(f"{env}: joined")
        except Exception as e:
            print(f"{env}: {e}")
PYEOF

# Save the app brief into the env dir for traceability.
cp "$APP_BRIEF" "$ENV_DIR_PY/spec-stub.md"

# ── 3. Build phase ───────────────────────────────────────────────────
if [ "$MODE" = "role" ]; then
    echo "--- role-divided build: architect -> implementer -> tester ---"
    for spec in "architect:GRUPR_TEST_CONV_A_EMAIL" \
                "implementer:GRUPR_TEST_CONV_B_EMAIL" \
                "tester:GRUPR_TEST_CONV_C_EMAIL"; do
        ROLE="${spec%%:*}"
        EMAIL_VAR="${spec##*:}"
        echo "-- role=$ROLE bot=$EMAIL_VAR --"
        EXTRA_ARGS=()
        if [ "$ROLE" = "architect" ]; then
            EXTRA_ARGS+=(--app-brief "$APP_BRIEF_PY")
        fi
        python -m personas.code_role_bot \
            --role "$ROLE" \
            --grupr-id "$GRUPR_ID" \
            --email-env "$EMAIL_VAR" \
            --run-tag "${ENV_ID}-${APP}-${MODE}" \
            "${EXTRA_ARGS[@]}" \
            || { echo "bot role=$ROLE failed — aborting env" >&2; exit 5; }
    done
else
    echo "--- workshop build: 3 rounds of coder_{a,b,c} ---"
    # Seed: conv-a posts the brief into the thread so all three bots
    # see it via get_messages (the orchestrator passes --app-brief to
    # each bot's goal text too, but the in-thread brief makes the
    # transcript self-contained).
    python - <<PYEOF
import os
from lib.user_client import UserClient
with open("$APP_BRIEF_PY", "r", encoding="utf-8") as f:
    brief = f.read()
with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
    c.login(os.environ["GRUPR_TEST_CONV_A_EMAIL"], os.environ["GRUPR_TEST_PASSWORD"])
    c.post_message("$GRUPR_ID", f"**Workshop brief**:\n\n{brief}")
PYEOF

    ROLES=(coder_a coder_b coder_c)
    EMAIL_ENVS=(GRUPR_TEST_CONV_A_EMAIL GRUPR_TEST_CONV_B_EMAIL GRUPR_TEST_CONV_C_EMAIL)
    ROUNDS="${WORKSHOP_ROUNDS:-3}"
    for round in $(seq 1 "$ROUNDS"); do
        for i in 0 1 2; do
            echo "-- round $round, ${ROLES[$i]} --"
            python -m personas.code_workshop \
                --role "${ROLES[$i]}" \
                --grupr-id "$GRUPR_ID" \
                --email-env "${EMAIL_ENVS[$i]}" \
                --app-brief "$APP_BRIEF_PY" \
                --run-tag "${ENV_ID}-${APP}-r${round}" \
                || echo "[workshop bot ${ROLES[$i]} round $round failed — continuing]" >&2
        done
    done
fi

# ── 4. Extract v1 code + tests from the build thread ─────────────────
echo "--- extracting v1 code + tests from messages ---"
python - <<PYEOF
import json, os
from lib.user_client import UserClient
from lib.code_extractor import extract_code

with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
    c.login(os.environ["GRUPR_TEST_CONV_A_EMAIL"], os.environ["GRUPR_TEST_PASSWORD"])
    msgs = c.get_messages("$GRUPR_ID", limit=50)

with open("$ENV_DIR_PY/build-messages.json", "w", encoding="utf-8") as f:
    json.dump(msgs, f, indent=2, default=str)

ex = extract_code(msgs)
with open("$ENV_DIR_PY/v1/code.py", "w", encoding="utf-8") as f:
    f.write(ex["code"] or "")
with open("$ENV_DIR_PY/v1/tests.py", "w", encoding="utf-8") as f:
    f.write(ex["tests"] or "")

code_lines = (ex["code"] or "").count("\n")
test_lines = (ex["tests"] or "").count("\n")
print(f"v1 code: {code_lines} lines, tests: {test_lines} lines")
if not ex["code"]:
    raise SystemExit("ABORT: no v1 code extracted from build messages")
PYEOF

# ── 5. Quick-tier review on v1 ───────────────────────────────────────
echo "--- Quick-tier Code Review on v1 ---"
python - <<PYEOF
import json, os
from lib.user_client import UserClient
from lib.code_review_client import run_quick

with open("$ENV_DIR_PY/v1/code.py", "r", encoding="utf-8") as f:
    code = f.read()

with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
    c.login(os.environ["GRUPR_TEST_CONV_A_EMAIL"], os.environ["GRUPR_TEST_PASSWORD"])
    result = run_quick(c, code=code, review_name="Env $ENV_ID — $APP — Quick v1")

with open("$ENV_DIR_PY/v1/quick-review.json", "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, default=str)
# Save the synthesizer verdict separately for the iterator persona.
with open("$ENV_DIR_PY/v1/synth-verdict.md", "w", encoding="utf-8") as f:
    f.write(result.get("synthesizer_verdict") or "(empty)")

print(f"Quick: {result['reviewer_count_returned']}/{result['expected_count']} reviewers in {result['elapsed_s']}s")
if not result.get("synthesizer_verdict"):
    print("WARN: empty synthesizer verdict — iterator will run with whatever's there")
PYEOF

# ── 6. Iterator produces v2 ──────────────────────────────────────────
echo "--- Iterator producing v2 (conv-a) ---"
python -m personas.code_role_bot \
    --role iterator \
    --grupr-id "$GRUPR_ID" \
    --email-env "GRUPR_TEST_CONV_A_EMAIL" \
    --v1-code "$ENV_DIR_PY/v1/code.py" \
    --review-verdict "$ENV_DIR_PY/v1/synth-verdict.md" \
    --run-tag "${ENV_ID}-${APP}-iter" \
    || { echo "iterator failed — continuing with whatever's there" >&2; }

# Extract v2 from the (now updated) build thread.
python - <<PYEOF
import json, os
from lib.user_client import UserClient
from lib.code_extractor import extract_code

with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
    c.login(os.environ["GRUPR_TEST_CONV_A_EMAIL"], os.environ["GRUPR_TEST_PASSWORD"])
    msgs = c.get_messages("$GRUPR_ID", limit=50)

# The iterator posted the LATEST python block. extract_code already
# picks the latest non-test block.
ex = extract_code(msgs)
with open("$ENV_DIR_PY/v2/code.py", "w", encoding="utf-8") as f:
    f.write(ex["code"] or "")
with open("$ENV_DIR_PY/v2/tests.py", "w", encoding="utf-8") as f:
    # Tests carry over from v1; iterator doesn't rewrite them.
    f.write(ex["tests"] or "")

code_lines = (ex["code"] or "").count("\n")
print(f"v2 code: {code_lines} lines")
if not ex["code"]:
    raise SystemExit("ABORT: no v2 code extracted")
PYEOF

# ── 7. Deep-tier review on v2 ────────────────────────────────────────
echo "--- Deep-tier Code Review on v2 (auto-approve enabled) ---"
python - <<PYEOF
import json, os
from lib.user_client import UserClient
from lib.code_review_client import run_deep

with open("$ENV_DIR_PY/v2/code.py", "r", encoding="utf-8") as f:
    code = f.read()

with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
    c.login(os.environ["GRUPR_TEST_CONV_A_EMAIL"], os.environ["GRUPR_TEST_PASSWORD"])
    result = run_deep(c, code=code, poll_timeout_s=900, auto_approve_patch=True)

with open("$ENV_DIR_PY/v2/deep-review.json", "w", encoding="utf-8") as f:
    # Drop the raw_review payload — it's verbose and lives in the DB.
    serializable = {k: v for k, v in result.items() if k != "raw_review"}
    json.dump(serializable, f, indent=2, default=str)
with open("$ENV_DIR_PY/v2/deep-patch.diff", "w", encoding="utf-8") as f:
    f.write(result.get("patch_diff") or "")
print(f"Deep: status={result['status']} timed_out={result['timed_out']} in {result['elapsed_s']}s")
print(f"Patch: status={result.get('patch_status')!r} diff_lines={(result.get('patch_diff') or '').count(chr(10))}")
PYEOF

# ── 8. Per-env summary ───────────────────────────────────────────────
echo "--- writing per-env summary ---"
ENV_DIR_PY="$ENV_DIR_PY" ENV_ID="$ENV_ID" APP="$APP" MODE="$MODE" \
python - <<'PYEOF'
import json, os, pathlib

ENV_DIR = pathlib.Path(os.environ["ENV_DIR_PY"])
ENV_ID  = os.environ["ENV_ID"]
APP     = os.environ["APP"]
MODE    = os.environ["MODE"]

v1_code = (ENV_DIR / "v1/code.py").read_text(encoding="utf-8")
v2_code = (ENV_DIR / "v2/code.py").read_text(encoding="utf-8")
quick = json.loads((ENV_DIR / "v1/quick-review.json").read_text(encoding="utf-8"))
deep_path = ENV_DIR / "v2/deep-review.json"
deep = json.loads(deep_path.read_text(encoding="utf-8")) if deep_path.exists() else {}
patch = (ENV_DIR / "v2/deep-patch.diff").read_text(encoding="utf-8")
synth = (ENV_DIR / "v1/synth-verdict.md").read_text(encoding="utf-8")

lines = []
lines.append(f"# Env {ENV_ID} — {APP} — {MODE}")
lines.append("")
lines.append(f"- v1 code: {v1_code.count(chr(10))} lines")
lines.append(f"- v2 code: {v2_code.count(chr(10))} lines (delta {v2_code.count(chr(10)) - v1_code.count(chr(10))})")
lines.append("")
lines.append("## Quick review (v1)")
lines.append(f"- reviewers responded: {quick.get('reviewer_count_returned')}/{quick.get('expected_count')}")
lines.append(f"- elapsed: {quick.get('elapsed_s')}s")
lines.append(f"- review grupr_id: {quick.get('grupr_id')}")
lines.append("")
lines.append("### Synthesizer verdict")
lines.append("```")
lines.append((synth or "(empty)")[:4000])
lines.append("```")
lines.append("")
lines.append("## Deep review (v2)")
lines.append(f"- status: {deep.get('status')}")
lines.append(f"- elapsed: {deep.get('elapsed_s')}s")
lines.append(f"- auto-approved at state: {deep.get('approved_at_state') or '(no approve)'}")
lines.append(f"- patch_status: {deep.get('patch_status')!r}")
lines.append(f"- patch diff: {patch.count(chr(10))} lines")
if deep.get("error_code"):
    lines.append(f"- error: [{deep.get('error_code')}] {deep.get('error_message')}")
lines.append("")
if patch.strip():
    lines.append("### Verified patch (first 60 lines)")
    lines.append("```diff")
    lines.append("\n".join(patch.splitlines()[:60]))
    lines.append("```")

(ENV_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")
print(f"summary -> {ENV_DIR / 'summary.md'}")
PYEOF

echo "=== Env $ENV_ID done. ==="
