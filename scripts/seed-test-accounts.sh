#!/usr/bin/env bash
#
# One-shot seed of the 8 test accounts the personas use. Runs SQL
# inside the grupr-postgres container via SSH — no need to expose
# port 5432 or share DB creds outside the box.
#
# Idempotent: ON CONFLICT (email) DO NOTHING. Re-running with no
# accounts removed is a safe no-op. To rotate the password, run
# cleanup-test-accounts.sh first.
#
# Password hashing: pgcrypto's `crypt(pw, gen_salt('bf', 12))` is
# bcrypt-compatible at cost factor 12, matching the api's normal
# signup path. Login flows through the api's standard verifier; it
# never knows these rows were side-loaded.

set -euo pipefail

SSH_KEY="${SSH_KEY:-G:/My Drive/BB/Dev/ClawdBot/kalshi-bot-key.pem}"
SSH_HOST="${SSH_HOST:-ubuntu@18.224.174.100}"
TEST_PASSWORD="${GRUPR_TEST_PASSWORD:-test-bot-password-2026}"
EMAIL_PREFIX="${GRUPR_TEST_EMAIL_PREFIX:-bret.babcock+gtb-}"
EMAIL_SUFFIX="${GRUPR_TEST_EMAIL_SUFFIX:-@gmail.com}"

# Build the SQL inline. Avoids heredoc-nesting pitfalls in Git Bash.
# adversary is the only account seeded with email_verified=false —
# it exists specifically to exercise the unverified-user gate.
SQL="
CREATE EXTENSION IF NOT EXISTS pgcrypto;

INSERT INTO users (
  user_id, email, email_verified, username, display_name, password_hash,
  bio, role, is_active, created_at, updated_at
) VALUES
  (gen_random_uuid(), '${EMAIL_PREFIX}newuser${EMAIL_SUFFIX}',     TRUE,  'gtb-newuser',     'Test Bot — newuser',     crypt('${TEST_PASSWORD}', gen_salt('bf', 12)), 'Persona test bot. Auto-seeded.', 'user',  TRUE, NOW(), NOW()),
  (gen_random_uuid(), '${EMAIL_PREFIX}poweruser${EMAIL_SUFFIX}',   TRUE,  'gtb-poweruser',   'Test Bot — poweruser',   crypt('${TEST_PASSWORD}', gen_salt('bf', 12)), 'Persona test bot. Auto-seeded.', 'user',  TRUE, NOW(), NOW()),
  (gen_random_uuid(), '${EMAIL_PREFIX}externaldev${EMAIL_SUFFIX}', TRUE,  'gtb-externaldev', 'Test Bot — externaldev', crypt('${TEST_PASSWORD}', gen_salt('bf', 12)), 'Persona test bot. Auto-seeded.', 'user',  TRUE, NOW(), NOW()),
  (gen_random_uuid(), '${EMAIL_PREFIX}admin${EMAIL_SUFFIX}',       TRUE,  'gtb-admin',       'Test Bot — admin',       crypt('${TEST_PASSWORD}', gen_salt('bf', 12)), 'Persona test bot. Auto-seeded.', 'admin', TRUE, NOW(), NOW()),
  (gen_random_uuid(), '${EMAIL_PREFIX}adversary${EMAIL_SUFFIX}',   FALSE, 'gtb-adversary',   'Test Bot — adversary',   crypt('${TEST_PASSWORD}', gen_salt('bf', 12)), 'Persona test bot. Auto-seeded.', 'user',  TRUE, NOW(), NOW()),
  (gen_random_uuid(), '${EMAIL_PREFIX}conv-a${EMAIL_SUFFIX}',      TRUE,  'gtb-conv-a',      'Test Bot — conv-a',      crypt('${TEST_PASSWORD}', gen_salt('bf', 12)), 'Persona test bot. Auto-seeded.', 'user',  TRUE, NOW(), NOW()),
  (gen_random_uuid(), '${EMAIL_PREFIX}conv-b${EMAIL_SUFFIX}',      TRUE,  'gtb-conv-b',      'Test Bot — conv-b',      crypt('${TEST_PASSWORD}', gen_salt('bf', 12)), 'Persona test bot. Auto-seeded.', 'user',  TRUE, NOW(), NOW()),
  (gen_random_uuid(), '${EMAIL_PREFIX}conv-c${EMAIL_SUFFIX}',      TRUE,  'gtb-conv-c',      'Test Bot — conv-c',      crypt('${TEST_PASSWORD}', gen_salt('bf', 12)), 'Persona test bot. Auto-seeded.', 'user',  TRUE, NOW(), NOW())
ON CONFLICT (email) DO NOTHING;

SELECT username, email_verified, role FROM users WHERE username LIKE 'gtb-%' ORDER BY username;
"

# Hand the SQL to remote psql via stdin. No nested heredocs.
echo "$SQL" | ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_HOST" \
  "docker exec -i grupr-postgres psql -U grupr -d grupr"

echo ""
echo "Done. Personas can log in with the shared password from GRUPR_TEST_PASSWORD."
echo "To clean up: ./scripts/cleanup-test-accounts.sh"
