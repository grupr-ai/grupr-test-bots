# grupr-test-bots

LLM-driven persona test framework for Grupr. Six personas (`new_user`, `power_user`, `external_dev`, `admin`, `adversary`, `conversational`) exercise real journeys against `api.grupr.ai`, surfacing bugs, friction, and security regressions in everything the launch sprint shipped.

Each persona is a Sonnet 4.5 agent (Anthropic Claude API with tool use) given a role, a goal, and a set of api-calling tools. The agent free-explores within its role and emits findings as it goes — structured markdown reports land in `runs/{persona}-{timestamp}/summary.md`.

This is also the public demo of how a third-party developer can build agents against the Grupr Agent Protocol. The `external_dev` persona dogfoods the published `@grupr` Python SDK end-to-end; the `conversational` persona shows the multi-bot pattern that defines Grupr ("AIs see each other in the same room").

## Quick start

```bash
git clone https://github.com/grupr-ai/grupr-test-bots
cd grupr-test-bots
python -m venv .venv
source .venv/Scripts/activate    # or .venv/bin/activate on Unix
pip install -e .

cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY at minimum

# One-time: seed 8 test accounts into the live DB (SSH + SQL)
./scripts/seed-test-accounts.sh

# Run a single persona
./scripts/run-persona.sh new_user

# Run all 5 in sequence + build cross-persona summary
./scripts/run-all.sh

# Run the 3-bot Workshop scenario
./scripts/multi-bot-workshop.sh 3   # 3 rounds, ~9 contributions total

# Tear down test accounts when done
./scripts/cleanup-test-accounts.sh
```

## Architecture

```
grupr-test-bots/
├── lib/
│   ├── user_client.py     # httpx wrapper over the user-side api (login, gruprs, messages, 2fa, subscription)
│   ├── grupr_client.py    # thin layer over the @grupr PyPI SDK (agent-hub flows)
│   ├── persona_runner.py  # shared LLM agent loop (Anthropic SDK + tool dispatch + caps)
│   └── reporter.py        # structured markdown findings + network journal
├── personas/
│   ├── new_user.py        # first-time user journey
│   ├── power_user.py      # heavy usage, multiple gruprs, all tiers
│   ├── external_dev.py    # third-party agent integration via @grupr SDK
│   ├── admin.py           # role=admin privilege-leak probe
│   ├── adversary.py       # rate limits, auth gates, bad inputs
│   └── conversational.py  # single in-character contribution for multi-bot workshop
├── scripts/
│   ├── seed-test-accounts.sh    # one-time DB seed
│   ├── cleanup-test-accounts.sh # wipe seeded accounts
│   ├── run-persona.sh           # run one persona
│   ├── run-all.sh               # run all standalone personas + summary
│   ├── multi-bot-workshop.sh    # orchestrate the 3-bot workshop scenario
│   └── smoke-login.py           # pre-flight smoke (no LLM in loop)
├── skills/                # OpenClaw skill wrappers (thin SKILL.md per persona)
└── runs/                  # gitignored output (markdown findings per run)
```

## Costs

| Run | Approximate Anthropic spend |
|---|---|
| `new_user` | $1–3 |
| `power_user` | $2–4 |
| `external_dev` | $1–3 |
| `admin` | $0.50–2 |
| `adversary` | $1–4 |
| `conversational` (one contribution) | $0.05–0.15 |
| `multi-bot-workshop` (3 rounds) | $0.50–1.50 |
| **Full sweep** (run-all + workshop) | **$10–20** |

Per-persona caps live in `.env` (`GRUPR_TEST_MAX_COST_USD`, default $5). Hit the cap and the run exits cleanly with a P2 finding noting the abort.

## What this does NOT test

Out of scope for these personas:

- **Signup** — Cloudflare Turnstile gates the register page; we can't pass it programmatically without driving a real browser. The seed script side-loads test accounts directly into the DB. Real signup is covered by a separate Playwright smoke (TBD).
- **Web UI rendering** — these are api-level personas. UI smoke is its own thing.
- **Production load** — these are functional checks, not load tests.
- **CF-Access-gated admin console** — admin.grupr.ai requires a Passkey + IdP-issued JWT; covered by manual QA before launch.
- **Code Review Deep tier sandbox escape** — that's the focus of the Day 8 security review, not these personas.

## Reading findings

Each persona writes `runs/{persona}-{ts}/summary.md` with sections sorted by severity (P0 → P1 → P2 → P3 → obs → pass). `run-all.sh` produces a cross-persona digest under `runs/suite-{ts}/summary.md` that surfaces all P0/P1 findings up top with collapsible per-persona details below.

See `RUNBOOK.md` for the 10 named test scenarios and what each is expected to find.
