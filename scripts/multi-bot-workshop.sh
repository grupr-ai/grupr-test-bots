#!/usr/bin/env bash
#
# Orchestrates a three-bot Grupr Workshop conversation.
#
# Setup:
#   1. conv-a (logged in via a one-time setup-only run) creates a
#      public Workshop grupr on a topic.
#   2. All three bots then participate in strict round-robin order
#      for a configurable number of rounds.
#
# This validates:
#   * Three concurrent agents can read/write to the same grupr without
#     stepping on each other
#   * Messages arrive in the order they were posted
#   * The "AIs see each other's messages" thesis holds — each bot's
#     contribution should reference previous ones, not appear in a
#     vacuum
#
# Usage:
#   ./scripts/multi-bot-workshop.sh [rounds]
#     rounds defaults to 3 (each of 3 bots posts that many times)

set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROUNDS="${1:-3}"

if [ -f "$HERE/.venv/Scripts/activate" ]; then
    source "$HERE/.venv/Scripts/activate"
else
    source "$HERE/.venv/bin/activate"
fi

cd "$HERE"

set -a
source "$HERE/.env"
set +a

TOPIC="Should Grupr ship Code Review Deep tier with E2B sandboxing on launch day, or hold it for a fast-follow after the launch sprint?"

echo "=== Workshop setup — conv-a creates the grupr ==="
GRUPR_ID=$(python - <<EOF
from lib.user_client import UserClient
import os

with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
    c.login(os.environ["GRUPR_TEST_CONV_A_EMAIL"], os.environ["GRUPR_TEST_PASSWORD"])
    gid = c.create_grupr(
        name="Workshop: Code Review Deep on launch day?",
        grupr_type="workshop",
        description="${TOPIC}",
        is_public=True,
    )
    print(gid)
EOF
)

if [ -z "$GRUPR_ID" ]; then
    echo "FAILED to create workshop grupr" >&2
    exit 1
fi

echo "Workshop grupr_id: $GRUPR_ID"
echo "Topic: $TOPIC"
echo ""

# Bots b and c need to join the public grupr before they can post.
echo "=== Bots b + c join the grupr ==="
python - <<EOF
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
EOF

echo ""
echo "=== $ROUNDS rounds of round-robin contributions ==="
ROLES=(skeptical_engineer enthusiastic_pm cautious_security)
EMAIL_ENVS=(GRUPR_TEST_CONV_A_EMAIL GRUPR_TEST_CONV_B_EMAIL GRUPR_TEST_CONV_C_EMAIL)

for round in $(seq 1 "$ROUNDS"); do
    for i in 0 1 2; do
        echo "-- round $round, ${ROLES[$i]} --"
        python -m personas.conversational \
            --role "${ROLES[$i]}" \
            --grupr-id "$GRUPR_ID" \
            --email-env "${EMAIL_ENVS[$i]}" \
            || echo "[bot ${ROLES[$i]} round $round failed — continuing]" >&2
    done
done

echo ""
echo "=== Transcript ==="
python - <<EOF
import os
from lib.user_client import UserClient
with UserClient(base_url=os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")) as c:
    c.login(os.environ["GRUPR_TEST_CONV_A_EMAIL"], os.environ["GRUPR_TEST_PASSWORD"])
    msgs = c.get_messages("$GRUPR_ID", limit=50)
    print(f"{len(msgs)} messages in workshop grupr_id=$GRUPR_ID")
    for m in msgs:
        sender = m.get("sender_name") or m.get("user_id", "(unknown)")[:8]
        content = m.get("content", "")
        print(f"  [{sender}] {content[:200]}")
EOF

echo ""
echo "Workshop done. Each conv-* persona's individual run report lives in runs/conv-*"
echo "The transcript above is the human-readable check that messages threaded correctly."
