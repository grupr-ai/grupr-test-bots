# new-user

OpenClaw skill — runs the first-time-user persona against the live Grupr api.

## Run

```bash
cd /path/to/grupr-test-bots
source .venv/Scripts/activate   # or .venv/bin/activate on Unix
python -m personas.new_user
```

## Required env

See `../../.env.example`. Critical: `ANTHROPIC_API_KEY`, `GRUPR_TEST_NEW_USER_EMAIL`, `GRUPR_TEST_PASSWORD`.

## Output

`runs/new_user-<timestamp>/summary.md` — markdown report of findings.

## What it covers

Login → home → trending → create grupr of each type → post message → subscription view → start_checkout → finish. Reports any friction encountered. Roughly 15–30 tool calls per run, ~$1–4 in Anthropic credit.

## What it does NOT cover

Signup (Cloudflare Turnstile gates the register page; covered by separate Playwright smoke). Email verification flow itself (no api endpoint to test it in isolation; covered by manual QA).
