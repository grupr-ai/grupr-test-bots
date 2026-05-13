# RUNBOOK

Ten named scenarios the persona sweep is expected to cover end-to-end. Each row says **what to run**, **what it covers**, and **what the expected outcome is** so it's easy to spot drift across sweeps.

The framework is designed to surface findings as LLM-generated markdown; this runbook is the human-side baseline of "what good looks like." Findings in `runs/{persona}-<ts>/summary.md` get triaged against this list.

---

## Pre-flight

| # | Scenario | Run | Expected |
|---|---|---|---|
| 0 | Non-LLM client smoke | `python scripts/smoke-login.py` | Login + me + my_gruprs all succeed against `gtb-newuser`. If this fails, the LLM sweep has no chance. |

## Standalone personas

| # | Scenario | Run | Expected outcome |
|---|---|---|---|
| 1 | First-time user happy path | `./scripts/run-persona.sh new_user` | Logs in → empty home → creates 3 gruprs (workshop/arena/groupchat) → posts a message → views subscription as free tier → Stripe checkout URL returns successfully. Mostly `pass` findings; any P0/P1 is a real regression. |
| 2 | Heavy usage sweep | `./scripts/run-persona.sh power_user` | Multiple gruprs per type, message volume, 2FA enrollment begin, all three subscription tiers' checkouts, GDPR data export. Expect `pass` on everything; `obs` for subjective polish; `P3` for any latency over 2s. |
| 3 | Third-party agent integration | `./scripts/run-persona.sh external_dev` | Logs in → creates an Agent → mints agent token via `Grupr.register(jwt, agent_id)` from the published @grupr SDK → uses SDK to post + poll messages in a public grupr. Validates the SDK against the live api. Any deviation between SDK behavior and README = at least P2. |
| 4 | Admin role privilege-leak probe | `./scripts/run-persona.sh admin` | Logs in as admin-role user → confirms role in `/api/users/me` → exercises normal user flows → confirms admin role does NOT bleed into elevated user-facing privileges (no cross-user visibility, no free subscription upgrade). All `pass` is the expected steady state. Any unexpected admin power = P0. |
| 5 | Failure-mode adversary sweep | `./scripts/run-persona.sh adversary` | Login error paths (no info leak), email_verified hard-gate (POSTs 403 with `email_unverified` for unverified account), bad subscription tier names rejected, cross-user UUID + pathological inputs handled, GDPR delete-then-relogin properly cuts off access. All `pass` is the steady state. |

## Coordinated scenario

| # | Scenario | Run | Expected outcome |
|---|---|---|---|
| 6 | Multi-bot Workshop conversation | `./scripts/multi-bot-workshop.sh 3` | conv-a creates a public Workshop on a real topic → conv-b + conv-c join → three rounds of strict round-robin contributions (skeptical_engineer / enthusiastic_pm / cautious_security). Final transcript should show ~9 messages threaded in order, each referencing what came before. Validates the "AIs see each other in the same room" thesis on the live api. |

## Full sweeps

| # | Scenario | Run | Expected outcome |
|---|---|---|---|
| 7 | Standalone-persona sweep | `./scripts/run-all.sh` | All 5 standalone personas run in sequence; a cross-persona summary lands at `runs/suite-<ts>/summary.md` with P0/P1 findings surfaced up top and per-persona detail collapsed. Total wall-clock ~15–20 min, cost ~$10–15. |
| 8 | Full sweep including coordinated | `./scripts/run-all.sh && ./scripts/multi-bot-workshop.sh 3` | Same as #7 plus the multi-bot scenario. Cost ~$12–18 total. |

## Maintenance

| # | Scenario | Run | Expected outcome |
|---|---|---|---|
| 9 | Reset test state | `./scripts/cleanup-test-accounts.sh && ./scripts/seed-test-accounts.sh` | Wipes all 8 `gtb-*` accounts + their cascade-data, re-seeds them clean. Useful between sweeps so each one starts from a known empty state. |

---

## What "good" looks like on launch day

A pre-launch sweep should land roughly here:

- **0 P0** — anything blocking launch is a "stop the sprint" event
- **0–2 P1** — one or two real bugs caught is normal; zero is great
- **3–8 P2/P3** — small UX issues, latency twitches, error-message polish
- **5–15 obs** — observations about behavior that's neither a bug nor a confirmation, just worth noting
- **30–60 pass** — these are positive confirmations across personas that the critical paths still work

A sweep that produces all-pass and zero-other is *suspicious* — the framework is probably broken or the personas are being too gentle. A sweep that produces all-P0/P1 is either a real regression event or the framework is misconfigured against the wrong api base.

## When findings disagree across personas

If `new_user` says "create_grupr works" (pass) and `power_user` says "create_grupr returns wrong grupr_type" (P2): both can be true — the *create* itself worked, but the *retrieve* response shape is inconsistent. Don't try to reconcile; let both findings stand and let the human triager decide which lens matters for launch.
