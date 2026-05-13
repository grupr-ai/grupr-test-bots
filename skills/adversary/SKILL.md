# adversary

OpenClaw skill — fast launch-readiness sanity check for obvious failure modes.

## Run

```bash
python -m personas.adversary
```

## What it covers

- Login error paths (wrong password vs nonexistent email — check for info leak)
- `email_verified` hard-gate (uses an unverified seeded account; POSTs should 403)
- Bad inputs to subscription endpoints
- Cross-user access attempts (UUID for nonexistent grupr, path-traversal-style input)
- Burst posting rate-limit probe
- GDPR delete-then-relogin (verifies pseudonymize cuts off access)

~$1–4 per run.

## What it does NOT cover

This is NOT a real pen-test. Deep security review of Code Review Deep tier (sandbox escape, secret exfil, prompt injection scenarios) lives on Day 8 of the launch sprint.
