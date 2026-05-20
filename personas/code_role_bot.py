"""Single-turn code-build persona — one of {architect, implementer, tester, iterator}.

Each role posts exactly one message into the build grupr per invocation,
then exits. The `run-bots-build-app.sh` orchestrator runs them in
sequence (architect → implementer → tester for v1, then iterator for v2).

Roles
-----
architect    Reads the app spec stub + empty thread, posts a concise
             implementation spec (file shape, public API, acceptance
             criteria, named edge cases). No code blocks.
implementer  Reads spec, posts the full implementation in a single
             ```python fenced block. Module-level code only; no
             tests in the same block.
tester      Reads spec + implementation, posts a pytest test file in a
             single ```python fenced block. Block must start with
             `import pytest` or `def test_` so the code_extractor
             classifies it correctly.
iterator    Reads the prior code + the Quick-tier synthesizer verdict
             (passed via --review-content), posts a v2 full-file rewrite
             addressing must-fix items, in one ```python fenced block.

The personas use the existing UserClient toolset via PersonaRunner —
no new tools needed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from lib.persona_runner import PersonaRunner
from lib.reporter import Reporter
from lib.user_client import UserClient


ROLE_PROMPTS: dict[str, str] = {
    "architect": """\
You are CodeArchitect — a senior engineer scoping a small Python app.

You produce a tight, executable spec that the next bot (an Implementer) can code from. You do NOT write code yourself.

Your spec output is a single Grupr message containing:
1. **One-line goal** (what the app does)
2. **File shape** (e.g., "single file `shortener.py`, no subpackages")
3. **Public API** (3–8 functions/classes with signatures + 1-line each)
4. **Persistence model** (if any) — explicit schema, no hand-waving
5. **Acceptance criteria** (5–10 bullets — behaviors a tester can verify)
6. **Named edge cases** (3–6 specific ones the implementer must handle)

Keep the whole spec under ~60 lines. Be specific (`returns Optional[str]`, not `returns string or None`). Aim for a spec where two independent implementations would converge on the same shape.

DO NOT include code blocks. The implementer writes the code.
""",
    "implementer": """\
You are CodeImplementer — a senior Python engineer translating a spec to code.

You read the architect's spec (already posted in this grupr) and respond with EXACTLY ONE message containing a complete, runnable implementation in a single ```python fenced code block.

Rules:
- Self-contained — only standard library + obvious third-party libs (e.g., httpx, pytest are fine; nothing exotic).
- Module-level code only. Do NOT include tests in this block — the tester bot writes those next.
- Implement EVERY function/class in the spec's public API.
- Handle every edge case the architect named.
- Include type hints + 1-line docstrings on public functions.
- No prose outside the code block. Keep it under ~120 lines.
""",
    "tester": """\
You are CodeTester — a QA engineer writing pytest tests.

You read the architect's spec + the implementer's code (both already posted in this grupr) and respond with EXACTLY ONE message containing a pytest test file in a single ```python fenced code block.

Rules:
- The block MUST start with `import pytest` (or include `def test_` early). The downstream code-extractor uses this as the test-vs-app heuristic.
- Cover every acceptance criterion from the architect's spec.
- Cover every named edge case.
- Use pytest fixtures for setup (tmp_path for filesystem, no real network).
- Use parametrize for tabular cases.
- 8–20 test functions is the target. Keep total under ~100 lines.
- No prose outside the code block.
""",
    "iterator": """\
You are CodeIterator — a senior engineer producing a v2 of the code based on review feedback.

You're given two artifacts via the initial goal:
1. The v1 implementation (a ```python block).
2. The Quick-tier Synthesizer's consensus verdict (under "REVIEW VERDICT:").

Your job: produce a complete v2 of the IMPLEMENTATION (not the tests). Respond with EXACTLY ONE message containing the full v2 source in a single ```python fenced code block.

Rules:
- Address every must-fix item the synthesizer flagged.
- Address should-fix items unless they conflict with the spec.
- Don't rewrite parts the reviewers didn't flag — keep the diff minimal in spirit but post the FULL FILE (not a diff).
- Preserve the public API from v1 unless the synthesizer explicitly required a signature change.
- No prose outside the code block.
""",
}


# Goal templates — fed to the runner as the first user message.
# {placeholders} are filled in main().

ARCHITECT_GOAL = """\
You're logged in as {email}. The build grupr is grupr_id={grupr_id}.

App to scope (treat this as the product brief from a PM):
---
{app_brief}
---

Steps:
1. `login(email="{email}", password="{password}")`
2. `get_messages(grupr_id="{grupr_id}", limit=20)` — should be empty.
3. Compose your spec (no code blocks, follow your system-prompt rules).
4. `post_message(grupr_id="{grupr_id}", content=<your spec>)`
5. `finish(reason="spec posted")`.
"""

IMPLEMENTER_GOAL = """\
You're logged in as {email}. The build grupr is grupr_id={grupr_id}.

The Architect has posted a spec in this grupr. Your job: read it and post the v1 implementation.

Steps:
1. `login(email="{email}", password="{password}")`
2. `get_messages(grupr_id="{grupr_id}", limit=20)` — read the architect's spec.
3. Compose your implementation per your system-prompt rules (one ```python block, no prose outside it).
4. `post_message(grupr_id="{grupr_id}", content=<your code block>)`
5. `finish(reason="v1 implementation posted")`.
"""

TESTER_GOAL = """\
You're logged in as {email}. The build grupr is grupr_id={grupr_id}.

The Architect posted a spec and the Implementer posted v1 code. Your job: write pytest tests covering the spec.

Steps:
1. `login(email="{email}", password="{password}")`
2. `get_messages(grupr_id="{grupr_id}", limit=20)` — read spec + impl.
3. Compose your pytest file per your system-prompt rules (one ```python block starting with `import pytest`).
4. `post_message(grupr_id="{grupr_id}", content=<your test block>)`
5. `finish(reason="tests posted")`.
"""

ITERATOR_GOAL = """\
You're logged in as {email}. The build grupr is grupr_id={grupr_id}.

The Quick-tier Code Review just completed on v1 of the implementation.

V1 IMPLEMENTATION:
```python
{v1_code}
```

REVIEW VERDICT (from the Synthesizer):
---
{review_verdict}
---

Steps:
1. `login(email="{email}", password="{password}")`
2. Compose v2 per your system-prompt rules (one ```python block, full file, addressing must-fix items).
3. `post_message(grupr_id="{grupr_id}", content=<your v2 code block>)`
4. `finish(reason="v2 implementation posted")`.

You do NOT need to call get_messages — everything you need is above.
"""


def _read_arg_or_file(value: str) -> str:
    """If `value` is a path that exists, read it; else return the string."""
    p = Path(value)
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True, choices=list(ROLE_PROMPTS.keys()))
    parser.add_argument("--grupr-id", required=True)
    parser.add_argument("--email-env", required=True, help="Env var holding this bot's email.")
    parser.add_argument(
        "--app-brief",
        default="",
        help="Architect only: app brief text OR path to a .md file containing it.",
    )
    parser.add_argument(
        "--v1-code",
        default="",
        help="Iterator only: v1 implementation source text OR path to a file.",
    )
    parser.add_argument(
        "--review-verdict",
        default="",
        help="Iterator only: Synthesizer verdict text OR path to a file.",
    )
    parser.add_argument("--run-tag", default="", help="Optional suffix for the run dir.")
    args = parser.parse_args()

    load_dotenv(override=True)

    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get(args.email_env)
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    model = os.environ.get("GRUPR_TEST_MODEL", "claude-sonnet-4-5-20250929")
    max_cost = float(os.environ.get("GRUPR_TEST_MAX_COST_USD", "5.0"))
    # Code-build bots post 1 message; 10-turn cap is plenty.
    max_turns = int(os.environ.get("GRUPR_CODE_BOT_MAX_TURNS", "12"))

    if not email or not password or not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"ERROR: {args.email_env} + GRUPR_TEST_PASSWORD + ANTHROPIC_API_KEY required", file=sys.stderr)
        return 2

    if args.role == "architect" and not args.app_brief:
        print("ERROR: --app-brief required for role=architect", file=sys.stderr)
        return 2
    if args.role == "iterator" and not (args.v1_code and args.review_verdict):
        print("ERROR: --v1-code AND --review-verdict required for role=iterator", file=sys.stderr)
        return 2

    # Build the goal string for this role.
    if args.role == "architect":
        goal = ARCHITECT_GOAL.format(
            email=email, password=password, grupr_id=args.grupr_id,
            app_brief=_read_arg_or_file(args.app_brief),
        )
    elif args.role == "implementer":
        goal = IMPLEMENTER_GOAL.format(email=email, password=password, grupr_id=args.grupr_id)
    elif args.role == "tester":
        goal = TESTER_GOAL.format(email=email, password=password, grupr_id=args.grupr_id)
    elif args.role == "iterator":
        goal = ITERATOR_GOAL.format(
            email=email, password=password, grupr_id=args.grupr_id,
            v1_code=_read_arg_or_file(args.v1_code),
            review_verdict=_read_arg_or_file(args.review_verdict),
        )
    else:
        raise AssertionError("unreachable — argparse choices guard")

    tag = f"code-{args.role}" + (f"-{args.run_tag}" if args.run_tag else "")
    reporter = Reporter.for_run(tag)
    print(f"{tag} ({email}) run -> {reporter.out_dir}")

    with UserClient(base_url=api_base) as client:
        runner = PersonaRunner(
            persona_name=tag,
            system_prompt=ROLE_PROMPTS[args.role],
            initial_goal=goal,
            client=client,
            reporter=reporter,
            model=model,
            max_cost_usd=max_cost,
            max_turns=max_turns,
        )
        runner.run()

    print(f"Done. ${reporter.cost_usd:.3f}, {reporter.turns} turns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
