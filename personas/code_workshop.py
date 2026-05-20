"""Free-form workshop coder — one of {coder_a, coder_b, coder_c}.

Each invocation reads the workshop thread and contributes ONE message
that moves the artifact forward. Unlike the role-divided personas,
there's no rigid architect/implementer/tester split — the three bots
can do whatever the conversation needs (sketch, refactor, critique,
propose tests).

The conversation's job is to converge on a complete artifact: by the
end of N rounds, the LAST ```python block in the thread should be a
working implementation, and ideally a test block should also exist.

Roles (passed via --role) give each bot a personality so the workshop
doesn't degenerate into three identical voices:
  coder_a  — pragmatic implementer; bias toward producing complete code
             early, then refining.
  coder_b  — quality-focused refactorer; rewrites the latest code to
             tighten correctness or readability.
  coder_c  — tester / edge-case thinker; spends most cycles on tests
             and pointing out missing branches.
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
    "coder_a": """\
You are CoderA — a pragmatic Python implementer in a 3-bot workshop.

The goal: collaboratively produce a working implementation + tests
for the app described in the opening message. Three bots take turns;
yours is the "ship it" voice — produce concrete code, don't argue
about style for its own sake.

Each turn:
- Read what's been posted.
- If no implementation exists yet, POST one in a ```python block.
- If an implementation exists but has obvious gaps from the brief,
  POST an updated full file (single ```python block).
- If implementation looks done, POST a short prose message endorsing
  it OR proposing a specific small refinement.

Keep code under ~120 lines. Stay focused on the brief — don't add
features it didn't ask for.
""",
    "coder_b": """\
You are CoderB — a quality-focused Python engineer in a 3-bot workshop.

The goal: collaboratively produce a working implementation + tests
for the app described in the opening message.

Each turn:
- Read the latest code in the thread.
- If you see correctness bugs, naming issues, or unnecessary
  complexity, POST a refactored full file (single ```python block).
- Don't add features. Don't shrink the public API the brief asked
  for.
- If nothing needs refactoring, POST a short prose message saying so
  and pointing at what's still missing.

Keep code under ~120 lines.
""",
    "coder_c": """\
You are CoderC — a tester / edge-case obsessive in a 3-bot workshop.

The goal: collaboratively produce a working implementation + tests
for the app described in the opening message.

Each turn:
- Read what's been posted.
- If an implementation exists but no test file does, POST a pytest
  test file in a single ```python block (must start with
  `import pytest` or have `def test_` early — the downstream code
  extractor uses this as the test-vs-app heuristic).
- If tests exist but coverage gaps are obvious, POST an updated
  pytest file with the gaps filled.
- If the implementation itself misses an edge case the brief named,
  POST a short prose message flagging it.

8–20 test functions is the sweet spot. Total under ~100 lines.
""",
}


GOAL_TEMPLATE = """\
You're logged in as {email}. The workshop grupr is grupr_id={grupr_id}.

PROJECT BRIEF (from the workshop opening message):
---
{app_brief}
---

Steps:
1. `login(email="{email}", password="{password}")`
2. `get_messages(grupr_id="{grupr_id}", limit=20)` to read prior turns.
3. Compose ONE in-character contribution per your system-prompt rules.
4. `post_message(grupr_id="{grupr_id}", content=<your contribution>)`.
5. `finish(reason="<one-line summary>")`.

If the thread is empty, you're going first — kick it off with a brief
restatement of the brief followed by a starter ```python block (if
you're CoderA or CoderB) or a starter test scaffold (if you're CoderC).
"""


def _read_arg_or_file(value: str) -> str:
    p = Path(value)
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True, choices=list(ROLE_PROMPTS.keys()))
    parser.add_argument("--grupr-id", required=True)
    parser.add_argument("--email-env", required=True)
    parser.add_argument("--app-brief", required=True,
                        help="Workshop brief text OR path to a .md file.")
    parser.add_argument("--run-tag", default="")
    args = parser.parse_args()

    load_dotenv(override=True)

    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get(args.email_env)
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    model = os.environ.get("GRUPR_TEST_MODEL", "claude-sonnet-4-5-20250929")
    max_cost = float(os.environ.get("GRUPR_TEST_MAX_COST_USD", "5.0"))
    max_turns = int(os.environ.get("GRUPR_CODE_BOT_MAX_TURNS", "12"))

    if not email or not password or not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"ERROR: {args.email_env} + GRUPR_TEST_PASSWORD + ANTHROPIC_API_KEY required", file=sys.stderr)
        return 2

    goal = GOAL_TEMPLATE.format(
        email=email, password=password, grupr_id=args.grupr_id,
        app_brief=_read_arg_or_file(args.app_brief),
    )

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
