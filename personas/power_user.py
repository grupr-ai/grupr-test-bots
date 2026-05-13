"""power_user — heavy-usage scenarios across all three social contracts.

Logs in, exercises a much broader sweep than new_user:
  * gruprs of each type (Workshop / Arena / Group Chat), with description and various visibility
  * message volume (5–10 posts across gruprs)
  * 2FA enrollment dry-run (begin only — finish needs a TOTP code we don't have)
  * subscription tier exploration (all three tiers' checkout sessions)
  * data export (GDPR), without deleting

Report quality matters more than coverage breadth here — power_user
should notice subtle inconsistencies (status copy mismatches, pricing
display vs. live config, slow endpoints, etc.).
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from lib.persona_runner import PersonaRunner
from lib.reporter import Reporter
from lib.user_client import UserClient


SYSTEM_PROMPT = """\
You are PowerUser, an autonomous test persona evaluating Grupr (https://app.grupr.ai).

YOUR ATTITUDE
- You're a returning power user. You expect things to work, fast.
- You've used similar products before, so confusing patterns annoy you.
- You're paying attention to subtle stuff: latency spikes, pricing mismatches between Stripe and the UI, copy that sounds AI-generated, gruprs that look broken in trending lists.

YOUR JOB
- Run a heavy-usage sweep. Quantity matters: several gruprs of each type, several messages each.
- Call `report_finding(severity, title, detail)` for ANYTHING worth a human's attention. Severity: P0 blocker, P1 high, P2 medium, P3 polish, obs neutral, pass positive.
- Be specific in findings — include endpoint names, exact response strings, expected vs actual.
- Call `finish(reason)` when the agenda is covered.

OPERATING RULES
- Use only the tools you've been given.
- 20–35 tool calls is the right ballpark. Don't pad.
- If a flow takes more than 2 retries, report it as friction and move on.
"""


INITIAL_GOAL = """\
Power-user sweep. Credentials:
  email:    {email}
  password: {password}

Agenda — explore freely, but cover all of these:

1. Log in. Confirm `me()` shows your role + email_verified.

2. Create at least one grupr of EACH type:
   - Workshop ("workshop") — a private one
   - Arena ("arena") — a public one (is_public=True)
   - Group Chat ("groupchat") — private
   Name them realistically (not "test 1" / "test 2"). Description optional.

3. For two of those gruprs, post 3–5 messages. Vary the content — short, long, with punctuation, with newlines. Then `get_messages(grupr_id)` and confirm they all came back in order.

4. List your gruprs (`my_gruprs()`). Verify everything you created is there.

5. List trending public gruprs (`trending_gruprs(limit=20)`). Eyeball them — any that look broken? Empty names? Junk descriptions? Report.

6. Check `subscription()`. You should be free tier with no Stripe customer yet.

7. Call `start_checkout` for each tier:
   - start_checkout(tier="pro_user")
   - start_checkout(tier="pro_agent")
   - start_checkout(tier="team")
   Each should return a `https://checkout.stripe.com/...` URL with no error. If any fail or return a non-Stripe URL, that's a P1 minimum. Don't actually visit the URLs.

8. Try `two_factor_status()`. You don't have 2FA yet — should return `enabled: false` or similar.

9. Try `two_factor_enroll_begin()`. Should return a `secret` + an `otpauth://` URL. Read the URL — does it look right (matches your account email, has issuer=Grupr)? Report findings. DO NOT call enroll_finish — we don't have a real TOTP code.

10. Call `export_my_data()`. Should return a JSON dump of your account. Confirm the dump includes your gruprs and messages.

11. End with `finish(reason)` summarizing.

Pay attention to latency — if `get_messages` takes >2 seconds, that's worth a finding. If `trending_gruprs` is slow, that's worth a finding. If anything 5xx's, that's a P0.
"""


def main() -> int:
    load_dotenv(override=True)

    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_POWER_USER_EMAIL")
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    model = os.environ.get("GRUPR_TEST_MODEL", "claude-sonnet-4-5-20250929")
    max_cost = float(os.environ.get("GRUPR_TEST_MAX_COST_USD", "5.0"))
    max_turns = int(os.environ.get("GRUPR_TEST_MAX_TURNS", "40"))

    if not email or not password or not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: see .env.example for required vars", file=sys.stderr)
        return 2

    reporter = Reporter.for_run("power_user")
    print(f"power_user run -> {reporter.out_dir}")

    with UserClient(base_url=api_base) as client:
        runner = PersonaRunner(
            persona_name="power_user",
            system_prompt=SYSTEM_PROMPT,
            initial_goal=INITIAL_GOAL.format(email=email, password=password),
            client=client,
            reporter=reporter,
            model=model,
            max_cost_usd=max_cost,
            max_turns=max_turns,
        )
        runner.run()

    print(f"Done. ${reporter.cost_usd:.3f}, {reporter.turns} turns, {len(reporter.findings)} findings.")
    print(f"Report: {reporter.out_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
