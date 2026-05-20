# Multi-LLM disagreement study — 20260520T013836Z

## Headline stats (PRE-COMMITTED methodology, NOT tuned to inflate)

**Verdict disagreement**: In **6 of 30** valid cases (**20%**), the single-model Claude Opus review reached a different verdict than the multi-Skill Grupr panel.

**Safety-directional disagreement**: In **5 of 30** valid cases (**17%**), the single model would have **shipped code the panel flagged as needing changes or blocked outright** — the single model's verdict was strictly less severe than the panel's consensus.

(Sample of 30 real recently-merged public-OSS PRs; 0 excluded for incomplete data — see Excluded below.)

## Methodology

**Sample**: 30 real, recently-merged PRs (≤30 days old at run time) sampled deterministically (seed=42) from 10 active OSS repos across Python, TypeScript, Go, and JavaScript. Diff size filter: 20–200 LoC. No manual cherry-pick. Repo list + sample selection lives in `prs.json` alongside this file; the full diffs are in `diffs/`.

**Panel under test**: 3-Skill Grupr panel — Architect (Claude Opus) + Security (GPT-4o) + Synthesizer (Claude Opus). Performance + Maintainability Skills (both Groq llama-3.3) were excluded from the study to avoid Groq's 100K-TPD daily rate-limit wall mid-run; the production launch product still ships the full 5-Skill panel. **The panel verdict is the Synthesizer's consensus output**, parsed to one of {ship, ship-with-changes, block}.

**Single-model baseline**: One Claude Opus call (`claude-opus-4-20250514`) with a generic *'review this code'* prompt that asks for a verdict tag and top-3 findings — designed to mirror what a developer would realistically paste into Claude when asking for a code review. Intentionally NOT tuned to the Synthesizer's prompt; that would be a strawman.

**Disagreement metric**: Verdict-level only. Panel verdict vs single-model verdict, both parsed to one of {ship, ship-with-changes, block}. Cases where either verdict failed to parse are excluded.

## Cross-tabulation: panel × single-model

| | single: ship | single: ship-with-changes | single: block |
|---|---|---|---|
| **panel: ship** | 14 ✓ | 1  | 0  |
| **panel: ship-with-changes** | 5  | 10 ✓ | 0  |
| **panel: block** | 0  | 0  | 0 ✓ |

Diagonal cells (✓) = agreement. Off-diagonal = disagreement.

## Verdict distributions

| Verdict | Panel | Single-model |
|---|---|---|
| ship | 15 | 19 |
| ship-with-changes | 15 | 11 |
| block | 0 | 0 |

## Sample composition

| Repo | Valid runs |
|---|---|
| denoland/deno | 5 |
| django/django | 5 |
| fastapi/fastapi | 6 |
| gohugoio/hugo | 4 |
| remix-run/react-router | 1 |
| spf13/cobra | 1 |
| vercel/next.js | 8 |

## Performance + cost

- Total panel wall-clock: 737s (avg 25s per panel run)
- Total single-model wall-clock: 482s (avg 16.1s per single run)
- Single-model tokens: 74,685 in / 8,680 out
- Single-model cost (claude-opus-4 list price: $15/$75 per Mtok): ~$1.77

Panel cost not directly broken out here (runs through the platform-key trial fallback, so it's bundled into the API container's overall Anthropic + OpenAI usage; rough estimate ~$0.30 per panel run × 30 = ~$9.00).

## Raw data

Per-PR results in `results/`. Each `<repo>__<num>.json` file contains the parsed verdicts, full Skill bodies, full single-model response body, wall-clock timings, and the user-rotation slot used.