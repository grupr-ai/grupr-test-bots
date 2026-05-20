# E2B Deep-tier load test — 20260520T205014Z

**Launch criterion** (HANDOFF.md): 10 concurrent Deep runs, P95 < 5min, success ≥ 90%.

**Result**: **PASS**

## Headline numbers

- Concurrent runs fired:    **10**
- Total wall-clock (run-start → all 10 done): **43.9s**
- P50 / P95 / P99 per-run wall: **39.9s / 43.8s / 43.8s**
- Success rate (terminal status = completed): **100%** (10/10)
- Verified-patch rate (of completed): **50%** (5/10)
- Timed out (>600s): **0**
- Rate-limit / 429 / E2B-sandbox-cap signals: **0** ← corrected; the initial 5
  surfaced by the naive heuristic were false positives matching "gene**rate**d"
  inside the literal `No patch generated for this review` message.

## Criterion checks

| check | threshold | observed | result |
|---|---|---|---|
| P95 wall-clock | < 300s (5 min) | 43.8s | **PASS** (7× headroom) |
| Success rate   | ≥ 90%          | 100%   | **PASS**               |
| E2B concurrency cap | ≥ 10 at our tier | no 429 / rate-limit signal | **PASS** |

## E2B concurrency finding

The prior in-house ceiling was N=4 from the bot-build suites. This run successfully
held **10 concurrent E2B sandboxes** with zero rate-limit signals, zero timeouts,
and no orchestrator-side `sandbox_cap_*` errors. The Deep-tier launch criterion is
not gated by E2B account tier at our current configuration.

## Per-run table

| idx | snippet | user | wall_s | status | patch_status | error_code |
|---:|---|---|---:|---|---|---|
| 0 | snippet-01-off-by-one          | gtb-newuser     | 35.5 | completed | -        | no_patch |
| 1 | snippet-02-mutable-default     | gtb-poweruser   | 43.7 | completed | verified | -        |
| 2 | snippet-03-broad-except        | gtb-externaldev | 43.9 | completed | verified | -        |
| 3 | snippet-04-sql-injection-shape | gtb-adversary   | 39.7 | completed | -        | no_patch |
| 4 | snippet-05-zero-division       | gtb-conv-a      | 43.6 | completed | -        | no_patch |
| 5 | snippet-06-resource-leak       | gtb-conv-b      | 39.7 | completed | verified | -        |
| 6 | snippet-07-shadowing-builtin   | gtb-conv-c      | 39.7 | completed | -        | no_patch |
| 7 | snippet-08-string-concat-loop  | gtb-admin       | 35.6 | completed | -        | no_patch |
| 8 | snippet-09-unsafe-eval         | gtb-newuser     | 40.1 | completed | verified | -        |
| 9 | snippet-10-recursion-no-base   | gtb-poweruser   | 43.8 | completed | verified | -        |

## On the 50% verified-patch rate

Every run hit terminal status `completed`. The 5 `no_patch` runs are the
orchestrator's "No patch generated for this review" response from the
`/patch` fetch — the patcher decided not to emit a diff, typically because
the Synthesizer's verdict landed on "ship" or the issue wasn't easily
mechanically fixable. This is a **Deep-tier review-quality signal**, not a
load-test or capacity concern, and is **not** part of the launch criterion.

Pattern observation: the 5 no-patch snippets (off-by-one, sql-injection
shape, zero-division, shadowing-builtin, string-concat-loop) are stylistic
or fragile-input issues; the 5 verified-patch snippets (mutable-default,
broad-except, resource-leak, unsafe-eval, recursion-no-base) are crisper
single-line fixes the patcher can produce confidently. Worth a follow-up
investigation if we want to lift verified-patch rate, but doesn't block
launch.

## Test methodology

- 10 small Python snippets (10-30 LoC), each with one well-known issue
  (off-by-one, mutable default, SQL injection shape, etc.) — bounds cost
  while exercising the full Deep path: reviewers → synthesizer →
  awaiting_patch gate → auto-approve → E2B sandbox → patcher → verifier.
- Concurrent fan-out via Python `ThreadPoolExecutor(max_workers=10)`.
- 8 distinct gtb-* test users (the launch envs); 10 > 8 so 2 users get
  2 concurrent runs each.
- Snapshot + promote pattern: each gtb-* user temporarily set to
  `status=trialing` + `expires_at=NOW()+1day` + `trial_quick_used=-100`
  + `trial_deep_used=-100`. The negative sentinel counters mean the
  trial gate's `used < limit` predicate is always satisfied for 10
  concurrent calls (no 402 trial-exhaustion races). The `trialing`
  status enables the orchestrator's `isActiveTrial` → platform-key
  fallback, so we don't need to seed BYOK rows. Prior subscription
  state restored to its exact pre-run snapshot after the run.
- Per-run capture: review_id, status, patch_status, approved_at_state,
  wall_s, error_code, error_message; raw_review + raw_patch JSON
  spilled per snippet to `per-run/`.

## Raw output

- `runs.json` — the per-run array
- `per-run/<idx>-<snippet>.json` — full review + patch JSON per run
- Script: `scripts/load-test-deep-tier.py` (commit pending)
