#!/usr/bin/env bash
#
# Permanently wipes the test bot accounts + all their created data.
# Uses raw DELETE rather than the GDPR-pseudonymize path because
# these are throwaway test rows, not real users.

set -euo pipefail

SSH_KEY="${SSH_KEY:-G:/My Drive/BB/Dev/ClawdBot/kalshi-bot-key.pem}"
SSH_HOST="${SSH_HOST:-ubuntu@18.224.174.100}"

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_HOST" bash <<'EOF'
set -e
echo "===Count BEFORE==="
docker exec -i grupr-postgres psql -U grupr -d grupr -c \
  "SELECT count(*) FROM users WHERE username LIKE 'gtb-%';"

echo "===DELETE (cascade nukes gruprs, messages, agents, subscriptions, etc.)==="
docker exec -i grupr-postgres psql -U grupr -d grupr -c \
  "DELETE FROM users WHERE username LIKE 'gtb-%';"

echo "===Count AFTER==="
docker exec -i grupr-postgres psql -U grupr -d grupr -c \
  "SELECT count(*) FROM users WHERE username LIKE 'gtb-%';"
EOF

echo ""
echo "Test accounts cleaned. Re-seed with ./scripts/seed-test-accounts.sh"
