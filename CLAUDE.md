# grupr-test-bots — Claude Code rules

LLM-driven persona test framework. Each persona is a Sonnet 4.5 agent with tool use, exploring api.grupr.ai with a goal + the `lib/user_client.py` toolbox. This file tells Claude how to extend, debug, and run the framework correctly.

## Read first

1. `README.md` — what the project is.
2. `RUNBOOK.md` — the 10 named test scenarios + expected outcomes. The "good run" baseline lives here.
3. `pyproject.toml` for deps, `.env.example` for required config.
4. The most recent `runs/{persona}-<ts>/summary.md` to see what last run found.

## Source-of-truth & deploy

- Local `C:/Dev/grupr-test-bots` is the working copy.
- Public repo at `grupr-ai/grupr-test-bots` (also serves as the launch demo of how to build third-party agents against the Grupr Agent Protocol).
- No deployment surface — this is a CLI tool. `pip install -e .` from a clean clone is the install path.
- Commit messages: short why-focused subject, include sweep cost + finding count in body for any commit that's been run, plus `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.

## Architecture invariants — don't break these

- **`lib/user_client.py` is the ONLY direct httpx caller.** Don't add ad-hoc `httpx.get(...)` anywhere else; everything routes through `UserClient._request()` so the call log stays complete for forensics.
- **`lib/persona_runner.py` is model-agnostic in shape but Anthropic-SDK-specific in code.** If you ever need to swap models, fork the runner; don't try to abstract over SDKs in-place.
- **Personas are NOT scripts — they are LLM-driven agents.** Don't add deterministic step-by-step Python to a persona; that defeats the point. Persona = system prompt + initial goal + tools. If a flow needs deterministic testing, write a separate `tests/` Python script outside the persona system.
- **Findings come from the LLM, not from Python asserts.** If you want to programmatically check something, do it via a tool the LLM can call (`report_finding`), not by asserting in handler code.
- **Never log secrets.** Tokens, passwords, API keys never get echoed to stdout, reporter output, or the network journal. The runner redacts agent tokens to a `prefix+...` shape for that reason.

## How to add a new persona

1. Create `personas/<name>.py`. Copy `personas/new_user.py` as the template.
2. Write a `SYSTEM_PROMPT` (the persona's *attitude*) and `INITIAL_GOAL` (the *task list*). Goals get `.format(email=..., password=...)` so the seeded credentials are substituted in.
3. Most personas need only the default toolset. If you need extra tools (e.g. the SDK-side helpers used by `external_dev`), pass them via the runner's `extra_tools` argument as a list of `ToolDef`.
4. Add a row in `pyproject.toml`'s `[project.scripts]`.
5. Add a thin `skills/<name>/SKILL.md` for OpenClaw packaging.
6. Add the persona name to `scripts/run-all.sh`'s `PERSONAS=` array.
7. Add a scenario row in `RUNBOOK.md`.

## How to add a new tool

1. Add the underlying method to `lib/user_client.py`. Use `self._request(...)` so it journals.
2. Add a `ToolDef(...)` to `_build_default_tools()` in `lib/persona_runner.py`. Description should tell the LLM **when** to use the tool, not just what it does.
3. Don't add tools to specific personas via `extra_tools` unless the tool is genuinely persona-specific (e.g. SDK-side helpers for `external_dev`). Most tools should live in the default set.

## Test account hygiene

- The 8 `gtb-*` accounts are seeded once via `scripts/seed-test-accounts.sh` (raw SQL inside the grupr-postgres container, idempotent).
- Cleanup via `scripts/cleanup-test-accounts.sh` (CASCADE-deletes accounts + all their gruprs / messages / agents).
- **Reseed between sweeps** so each persona starts from a known state. The runner doesn't clean up after itself.
- The `gtb-adversary` account is seeded with `email_verified=FALSE` on purpose to exercise the email-verified hard-gate. Don't "fix" this.

## Cost discipline

- Each persona run is capped at `GRUPR_TEST_MAX_COST_USD` (default $5). When hit, the runner emits a P2 finding and exits.
- Each run is capped at `GRUPR_TEST_MAX_TURNS` (default 40). Same idea.
- A full sweep (5 personas + workshop) should land at **$10–20**. If a single run exceeds $4, something is probably looping — investigate before the next sweep.

## When to ask vs. when to act

- **Act**: anything that's a bug in the framework code (wrong endpoint, missing field, broken serializer). Fix it, re-run the affected persona, note the fix in the next commit.
- **Act**: anything that's a finding the persona surfaced about *the product* (api.grupr.ai). Let it ride — that's the framework working as intended. Triage during the sweep summary.
- **Ask**: changing the persona's role / attitude (those define what we're testing). Don't redirect a persona's focus mid-sprint without flagging.
- **Stop and surface**: any time a persona finds a P0 against production. Don't just file it in the report and move on — the launch sprint should hear about it immediately.

## Run smoke before a sweep

If `lib/user_client.py` or `lib/persona_runner.py` was touched, run the non-LLM pre-flight first:

```bash
python scripts/smoke-login.py
```

That validates login + me + my_gruprs against the live api without burning Anthropic credit. If it fails, the LLM sweep has no chance — fix the client first.
