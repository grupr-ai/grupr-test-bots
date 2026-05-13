"""new_user — first-time Grupr user.

Logs in with the seeded account, then explores what a real first-time
user would: viewing their (empty) home, browsing trending, creating
their first grupr in each of the three social contracts, posting an
opening message, checking how the email-not-verified banner behaves,
and looking at their subscription state.

The persona is given freedom inside its role: the system prompt sets
the *attitude* ("you're a curious first-timer; report friction"); the
initial goal lists the high-level checkpoints; the persona is free
to deviate, retry, or report whatever it notices.

Signup is INTENTIONALLY out of scope — Cloudflare Turnstile (Day-2
follow-up #1) now gates the register page and can't be passed
without a real browser. The seed script pre-creates this account.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from lib.persona_runner import PersonaRunner
from lib.reporter import Reporter
from lib.user_client import UserClient


SYSTEM_PROMPT = """\
You are NewUser, an autonomous test persona evaluating the Grupr web product (https://app.grupr.ai).

YOUR ATTITUDE
- You are a curious first-time user. You've just signed up and verified your email.
- You are NOT a developer or a power user — you're poking at the product to see if it makes sense.
- You speak to yourself in plain English. You think out loud, then act.

YOUR JOB
- Walk through the first-time-user journey using the tools provided.
- For ANYTHING worth a human's attention — bugs, confusing UX, surprising errors, broken endpoints, missing features, or positive confirmations that critical flows work — call `report_finding(severity, title, detail)`.
- Severity rubric: P0 = launch-blocking, P1 = high-priority bug, P2 = medium bug, P3 = polish/UX, obs = neutral observation, pass = positive confirmation that an important flow works.
- When you're done with the agenda or have hit a dead end, call `finish(reason)`.

OPERATING RULES
- Don't call `finish` after one or two tool calls. Cover the journey.
- If a tool errors out, report it (severity depending on impact) AND try a different path. Don't get stuck retrying the same thing.
- DO NOT call tools you weren't told you have. Use only the listed tools.
- Network costs are real. Don't spam tools — be deliberate.
- Aim for 15–30 tool calls in a productive run.
"""


INITIAL_GOAL = """\
You're about to log in for the first time. Here's your journey — explore freely within it and report findings as you go:

1. Log in as the new-user persona. The credentials are:
     email:    {email}
     password: {password}

2. Check `me()` — confirm you're authed and your account state makes sense. Report anything odd.

3. List your gruprs with `my_gruprs()`. As a brand-new user you should have none — that's expected.

4. Check trending public gruprs (`trending_gruprs(limit=12)`). See what's there.

5. Create your first grupr. Pick a Workshop ("workshop"), give it a real-sounding name and short description, keep it private. Then create one more grupr as an Arena ("arena") with a different name. And one as a Group Chat ("groupchat"). Report friction on any of these.

6. For at least one of the gruprs you created, post a short opening message ("Hi, just getting set up — anyone here?").

7. Try `get_messages(grupr_id)` on that grupr to confirm the message landed.

8. Check `subscription()` — you should be on the free tier. If anything looks confusing, report it.

9. Try `start_checkout(tier="pro_user")` — this should return a checkout.stripe.com URL but NOT actually charge anything. Confirm it does. (Don't visit the URL.)

10. Pick anything that surprised you along the way and report it as obs/pass/PN.

11. Call `finish(reason)` summarizing what you accomplished and any blockers you saw.

Don't be timid — surface anything that feels broken or wrong, even if you're not sure. False positives are fine; misses are not.
"""


def main() -> int:
    load_dotenv(override=True)

    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL")
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    model = os.environ.get("GRUPR_TEST_MODEL", "claude-sonnet-4-5-20250929")
    max_cost = float(os.environ.get("GRUPR_TEST_MAX_COST_USD", "5.0"))
    max_turns = int(os.environ.get("GRUPR_TEST_MAX_TURNS", "40"))

    if not email or not password:
        print("ERROR: GRUPR_TEST_NEW_USER_EMAIL + GRUPR_TEST_PASSWORD must be set (see .env.example)", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY must be set", file=sys.stderr)
        return 2

    reporter = Reporter.for_run("new_user")
    print(f"new_user run -> {reporter.out_dir}")

    with UserClient(base_url=api_base) as client:
        runner = PersonaRunner(
            persona_name="new_user",
            system_prompt=SYSTEM_PROMPT,
            initial_goal=INITIAL_GOAL.format(email=email, password=password),
            client=client,
            reporter=reporter,
            model=model,
            max_cost_usd=max_cost,
            max_turns=max_turns,
        )
        runner.run()

    print(f"Done. Cost ${reporter.cost_usd:.3f}, {reporter.turns} turns, {len(reporter.findings)} findings.")
    print(f"Report: {reporter.out_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
