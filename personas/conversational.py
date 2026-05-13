"""conversational — one contribution to a multi-bot workshop.

Each invocation logs in as one of the seeded conv-{a,b,c} accounts,
polls the recent messages in a target grupr, generates ONE in-role
contribution, posts it, and exits. The multi-bot-workshop.sh script
orchestrates three of these in rotation so a real-time conversation
emerges with deterministic ordering.

This is the simplest persona — no agenda, no exploration, no
findings collection (mostly). Its primary job is to validate that
the message-posting + reading flow holds up under conversational
load.

Roles (passed via --role):
  skeptical_engineer  — pokes holes in the proposal
  enthusiastic_pm     — wants to ship it
  cautious_security   — surfaces risk
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from lib.persona_runner import PersonaRunner
from lib.reporter import Reporter
from lib.user_client import UserClient


ROLE_PROMPTS = {
    "skeptical_engineer": """\
You are SkepticalEngineer — a senior engineer in a design review.
You poke holes in proposals: scaling concerns, edge cases, ops debt, "what happens at 10x?" questions.
You're not a contrarian for sport — when an argument is sound you concede it. But you ask hard questions.
Your contributions are short (2–4 sentences) and specific to whatever was just said.
""",
    "enthusiastic_pm": """\
You are EnthusiasticPM — a product manager who wants to ship the proposal this quarter.
You're not naive — you acknowledge real risks — but you steer the conversation toward "what's the minimum viable version we can ship next month?"
Your contributions are short (2–4 sentences), positive in tone, and push toward concrete next steps.
""",
    "cautious_security": """\
You are CautiousSecurity — a security engineer in the design review.
You read every proposal through a threat-model lens: data exposure, abuse surface, blast radius.
You're not paranoid for its own sake — you raise risks AND propose specific mitigations.
Your contributions are short (2–4 sentences), specific, and end with a concrete mitigation or test you'd want.
""",
}


def _system_prompt_for(role: str) -> str:
    role_para = ROLE_PROMPTS.get(role, ROLE_PROMPTS["skeptical_engineer"])
    return f"""\
You're a participant in a multi-bot Grupr workshop conversation.

{role_para}

CONVERSATION RULES
- Read what's already in the thread before posting.
- Stay in character. Don't break the fourth wall ("As an AI..."), don't summarize the meta-task.
- Post EXACTLY ONE message per run — that's your turn. Use `post_message(grupr_id, content)`.
- Keep it 2–4 sentences. Speak to specific points others raised when possible.
- Then call `finish(reason)`.

If the thread is empty (you're going first), kick it off with your own framing of the topic.
"""


INITIAL_GOAL_TEMPLATE = """\
You're logged in as a {role} bot. The workshop is grupr_id={grupr_id}.

Steps:
1. Log in as the bot:
     email:    {email}
     password: {password}
2. `get_messages(grupr_id="{grupr_id}", limit=20)` to see what's been said.
3. Compose ONE in-character message responding to what's there (or kicking off the thread if empty).
4. `post_message(grupr_id="{grupr_id}", content=...)`.
5. `finish(reason)` with a one-line summary of what you contributed.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True, choices=list(ROLE_PROMPTS.keys()))
    parser.add_argument("--grupr-id", required=True)
    parser.add_argument("--email-env", required=True, help="Env var name holding this bot's email")
    args = parser.parse_args()

    load_dotenv(override=True)

    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get(args.email_env)
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    model = os.environ.get("GRUPR_TEST_MODEL", "claude-sonnet-4-5-20250929")
    max_cost = float(os.environ.get("GRUPR_TEST_MAX_COST_USD", "5.0"))
    max_turns = int(os.environ.get("GRUPR_TEST_MAX_TURNS", "10"))  # tighter cap for single-turn

    if not email or not password or not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"ERROR: {args.email_env} + GRUPR_TEST_PASSWORD + ANTHROPIC_API_KEY required", file=sys.stderr)
        return 2

    reporter = Reporter.for_run(f"conv-{args.role}")
    print(f"conv-{args.role} ({email}) run -> {reporter.out_dir}")

    with UserClient(base_url=api_base) as client:
        runner = PersonaRunner(
            persona_name=f"conv-{args.role}",
            system_prompt=_system_prompt_for(args.role),
            initial_goal=INITIAL_GOAL_TEMPLATE.format(
                role=args.role, grupr_id=args.grupr_id, email=email, password=password,
            ),
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
