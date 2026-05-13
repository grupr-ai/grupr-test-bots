# Day 3 — Pre-launch persona sweep

> Run: 2026-05-13 evening, against live `api.grupr.ai` (api commits `575bd78` → `a4ff4c7`, web `8590418`, ops `0ba490b`, landing `9083c0e`). All 7 Day-2 items + 3 pre-launch follow-ups shipped before the sweep started; **two P0 fixes landed mid-sweep as commit `a4ff4c7`**.

## Headline

**Initial sweep surfaced 3 P0s in production, plus 2 additional P0s that turned out to be framework-side wrapper bugs.** The 2 real production P0s were patched + verified mid-sweep (api commit `a4ff4c7`). After re-running the relevant personas against the patched build, **zero P0s remain**.

The framework also caught 4 real DX traps in the `@grupr` SDK developer experience — none are SDK code bugs (the SDK works correctly when used per its README), but each is a footgun that would bite a third-party developer who slightly misreads the docs. Worth surfacing as a DX-improvement candidate for v0.4.0.

Total spend: **~$11.50** across initial sweep + iteration cycle. Well under the $30-50 budget.

## What landed in production this session

`grupr-api` commit **`a4ff4c7`** — *fix: close two P0 gaps surfaced by Day-3 persona sweep*:
1. Added `RequireEmailVerified` middleware to `POST /api/agents`. Pattern matches the same gate already on grupr-create + message-send + agent-hub register.
2. `POST /api/subscription/checkout` no longer defaults empty `tier` to `pro_user` — now returns `400 invalid_tier`. With live Stripe keys, the previous silent default could half-provision a Pro subscription on a client bug.

Both verified via targeted smoke (adversary persona's `email_verified=false` account now gets 403 on `create_agent`; verified user posting `{tier:""}` gets 400 with field-level error) and re-confirmed via full adversary persona re-run.

## Final persona results (after iteration)

| Persona | Cost | Turns | P0/P1/P2/P3/obs/pass | Note |
|---|---|---|---|---|
| `new_user` (initial) | $1.14 | 40 | 0 / 1 / 2 / 1 / 3 / 8 | Surfaced GDPR + create_agent framework bugs |
| `new_user` (post fixes) | $1.33 | 40 | 0 / 1 / 2 / 0 / 2 / 10 | Down to 1 P1 (grupr_type inconsistency) |
| `power_user` | $0.69 | 23 | 0 / 1 / 2 / 0 / 1 / 7 | Anthropic 500 mid-run; finally block preserved 11 findings |
| `external_dev` (initial) | $1.29 | 40 | **2** / 3 / 3 / 1 / 3 / 3 | "P0"s turned out to be framework wrapper bugs |
| `external_dev` (post wrapper fixes) | $1.14 | 40 | **2** / 2 / 4 / 1 / 2 / 2 | One more wrapper bug found + fixed |
| `external_dev` (final) | $1.24 | 40 | **0** / 1 / 3 / 1 / 1 / 4 | Full SDK lifecycle now completes |
| `admin` | $0.21 | 13 | 0 / 0 / 0 / 0 / 2 / 1 | role=admin doesn't bleed into user-facing api — clean |
| `adversary` (initial) | $0.94 | 40 | **1** / 2 / 3 / 0 / 3 / 7 | P0: create_agent missing email gate |
| `adversary` (post api fixes) | $1.16 | 40 | **0** / 2 / 3 / 0 / 4 / 10 | All P0s now PASS |
| Workshop (6 contributions × 3 bots) | $0.23 | 18 | n/a (single-turn) | Multi-bot debate threaded perfectly |
| **Totals across the day** | **~$11.50** | — | **0 P0** / **5 P1** / **10 P2** / **2 P3** / **13 obs** / **34 pass** |

## P0s — initial findings + resolution

| # | Initial classification | Source | Resolution |
|---|---|---|---|
| 1 | **email_verified gate missing on `POST /api/agents`** | adversary | ✅ Real api bug. Fixed in commit `a4ff4c7`. Re-run confirms 403. |
| 2 | **`@grupr` SDK calls wrong endpoint** | external_dev | ❌ **Reclassified — framework-side wrapper bug**, not real SDK bug. The SDK's `DEFAULT_BASE_URL` is `https://api.grupr.ai/api/v1/agent-hub` and works correctly when used with the default. My wrapper in `lib/grupr_client.py` was passing the api root (`https://api.grupr.ai`) as `base_url`, which silently 404s on the user-signup `/register`. Fixed wrapper, no SDK change needed. |
| 3 | **Empty `tier=""` defaults to `pro_user` and creates real Stripe session** | adversary | ✅ Real api bug. Fixed in commit `a4ff4c7`. Re-run confirms 400 invalid_tier. |
| 4 | **Agent assignment to grupr missing** | external_dev (run 2) | ❌ **Reclassified — framework gap**, not real api/SDK bug. The `POST /api/gruprs/:id/agents` endpoint exists; the framework just didn't expose it as a persona tool. Added `add_agent_to_grupr` to `UserClient`. |
| 5 | **SDK `Message.user_id` AttributeError** | external_dev (run 2) | ❌ **Reclassified — framework wrapper bug**, not real SDK bug. The SDK's `Message` dataclass uses `sender_id` (matching the api response). My wrapper accessed `.user_id`. Fixed wrapper. |

**Net real production P0s: 2** — both fixed and verified before this report was written.

## Real DX traps in the SDK worth flagging (no code change required)

The framework surfaced these by accident-by-design — they're shaped exactly like the mistakes a third-party developer reading the README quickly would make. All confirmed correct SDK behavior; none are SDK bugs. But each is worth a README clarification or a fail-louder error:

1. **`base_url` parameter on `Grupr.register()` is the agent-hub root** (`https://api.grupr.ai/api/v1/agent-hub`), not the api root. Developers who override `base_url` to point at a self-hosted Grupr deployment will likely use `https://my-grupr.example.com` (api root), which silently 404s at `/register`. **Suggested SDK improvement:** detect when the constructed URL hits a non-agent-hub endpoint and raise a clearer error than the surface 404.
2. **Agents must be explicitly assigned to a grupr** via `POST /api/gruprs/:id/agents` before they can poll/post via the SDK. The README's quick-start glosses over this prerequisite. **Suggested SDK improvement:** add a helper `client.ensure_in_grupr(grupr_id)` or document this as step 2.5 in the lifecycle.
3. **`Message.user_id` doesn't exist** — the dataclass field is `sender_id`. Developers who try `msg.user_id` instinctively (matching some other chat-API conventions) crash with an AttributeError on first poll. **Suggested SDK improvement:** add a `user_id` property alias that returns `sender_id` for compatibility.
4. **Mint-then-switch model is implicit** — `Grupr.register()` returns a client bound to one agent token; calling register again for a different agent doesn't switch the existing client. Developers operating multiple agents need separate clients. **Suggested SDK improvement:** document this clearly OR add a `client.with_agent(agent_id)` factory.

## P1s remaining (the launch sprint should consider triaging)

| Source | Finding | Suggested fix effort |
|---|---|---|
| new_user / power_user | Grupr `grup_type` returned as `private_chat` regardless of create-time type for private gruprs; public ones show their real type (`ai_arena`, `ai_workshop`). Either preserve the type or document the visibility-dependent mapping. | 30 min |
| adversary (both runs) | Unverified users can begin 2FA enrollment. Same shape as the `create_agent` gate — should add `RequireEmailVerified` to `/api/auth/2fa/enroll/begin`. | 10 min |
| adversary (post-fix) | Path-traversal input (e.g. `grupr_id="../../../etc/passwd"`) returns `404 server_error: Cannot GET /etc/passwd` — leaks internal routing behavior. Should validate `grupr_id` is a UUID before routing. | 20 min |
| new_user | `/api/subscription` response is minimal — only `plan_tier` + `status`. Could include `included_features` + `upgrade_targets` to be self-documenting. | 1 hr |
| external_dev (final) | SDK mint-then-switch ambiguity (see DX trap #4 above). | doc-only |

**Suggested total Day-4-morning P1 fixes:** ~1.5 hr.

## Critical paths confirmed working

These passed across multiple personas — confirmed launch-ready:

- Login + JWT issuance + `/api/users/me` (now including `email_verified`)
- Grupr creation across all three social contracts
- Message posting + retrieval
- Trending public gruprs + joining
- Subscription state read + Stripe Checkout session creation for all three tiers (live Stripe keys, Day-2 Item 6)
- 2FA enrollment begin (correct `otpauth://` URL + QR + secret format)
- GDPR data export with password reconfirm
- The full `@grupr` SDK third-party agent lifecycle: log in as user → create agent → mint agent token → assign agent to grupr → SDK polls + sends messages — **end-to-end working after framework wrapper fixes**
- Multi-bot Workshop conversation — 6 messages threaded in character, each referencing what came before (the launch positioning thesis demonstrated end-to-end on live infra)
- `role=admin` does NOT bleed into elevated user-facing privileges
- Login error responses uniform across wrong-password vs nonexistent-email (no account-existence leak)
- GDPR pseudonymize cuts off access on re-login
- Cross-user UUID + most pathological inputs handled cleanly (path-traversal flagged as P1 above)

## Framework fragility — known to fix in next iteration

- `power_user` crashed on an Anthropic API 500 mid-run. The runner's `try/finally` block correctly finalized the partial report (11 findings persisted), so no data lost — but the run was cut short by ~5 min. **Next iteration**: add retry-with-backoff (3 attempts) to `messages.create()` on `anthropic.InternalServerError` and `anthropic.RateLimitError`. ~30 min, out of scope for the Day-3 commit.

## Sweep operational stats

- Total wall-clock: ~40 min including iteration
- Total Anthropic spend: **~$11.50** (initial $5.83 + iteration $5.67)
- Total api calls: ~200+ across all persona runs
- Test accounts touched: 8 (`gtb-*`), cleanly reseedable

## Day-4 priority list derived from this sweep

1. **`grupr-test-bots` GitHub repo** — make public, also serves as the launch demo for third-party Agent Protocol integration.
2. **P1: `RequireEmailVerified` on `/api/auth/2fa/enroll/begin`** — same shape as today's `a4ff4c7` fix. 10 min.
3. **P1: UUID-validate `grupr_id` before route dispatch** — closes the path-traversal info-leak. 20 min.
4. **P1: clarify `grup_type` semantics** — either preserve type for private gruprs or document the visibility-dependent mapping. 30 min.
5. **SDK README clarifications** for the 4 DX traps catalogued above — no code change. ~30 min of writing.
6. **Re-run persona sweep** against the Day-4 build to confirm everything reports `pass` or `obs`/`pass`-only.
