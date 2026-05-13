"""adversary — abuse-pattern + boundary probes.

Targets:
  * Rate limits — does the api shed traffic when a single user posts very fast?
  * The email_verified hard-gate — adversary is seeded UNVERIFIED, so any POST
    creating gruprs / messages / agents must 403 with code email_unverified.
  * Subscription endpoints with bad inputs (invalid tier name, malformed body)
  * Cross-user access attempts (try to read another user's grupr by id)
  * Login with wrong password (does the api leak whether the email exists?)
  * GDPR delete-then-relogin (verifies pseudonymize cuts off access)

This is NOT a real pen-test — that's Day 8. This is the fast launch
sanity check that the obvious failure modes are properly handled.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from lib.persona_runner import PersonaRunner
from lib.reporter import Reporter
from lib.user_client import UserClient


SYSTEM_PROMPT = """\
You are Adversary, an autonomous test persona poking at Grupr's failure modes.

YOUR ATTITUDE
- You are a methodical adversary. You're not a malicious attacker — you're verifying that the obvious wrong-thing-happens-gracefully cases all behave correctly.
- You catalog failure modes and check whether each fails CORRECTLY (clean error, no leak, no crash) vs INCORRECTLY (server error, info leak, accidental success).

YOUR JOB
- Probe the boundaries: rate limits, the email_verified gate, bad inputs, cross-user access, login error paths.
- For each probe: state the hypothesis ("this should X"), do it via a tool, report whether X happened.
- Severity rubric: P0 = security gap that lets through forbidden action, P1 = information disclosure (e.g. login response that reveals account existence), P2 = noisy/confusing error, P3 = polish, obs = expected behavior worth noting, pass = correctly handled.

OPERATING RULES
- Don't actually rate-limit-saturate at scale — 8–10 rapid posts is enough signal.
- Don't try to brute-force credentials. Stick to documented inputs.
- 20–30 tool calls. Be specific in findings.
"""


INITIAL_GOAL = """\
Adversary sweep. The credentials seed an UNVERIFIED account on purpose:
  email:    {email}
  password: {password}

Walk these probes:

A. **Login error paths**
   1. Try to log in with the WRONG password ("definitely-not-the-password"). Should fail. Capture the exact error message — does it say "email exists but wrong password" (info leak) or just "invalid credentials" (good)? Report.
   2. Try to log in with a clearly NON-EXISTENT email ("no-such-user-just-testing@example.com" + a random password). Should fail. Capture the error — same message as #1? (Same message = good; different = info leak.)

B. **Log in for real, then check the email_verified gate**
   3. Log in with the real credentials. `me()` should show email_verified=false.
   4. Try `create_grupr(name="should-fail-no-verify")`. The api should 403 with code `email_unverified` (or similar). If it SUCCEEDS, that's P0 — the email-verified gate is broken.
   5. Try `post_message` on any grupr_id (even a fake one) — should ALSO 403 with email_unverified before any other validation. Report.

C. **Bad inputs to subscription**
   6. Try `start_checkout(tier="not_a_real_tier")`. Should be 400 with clear error, not 500 or pass-through.
   7. Try `start_checkout(tier="")`. Should also fail cleanly.

D. **Cross-user attempts**
   8. Try `get_grupr(grupr_id="00000000-0000-0000-0000-000000000000")` — a UUID that won't exist. Should 404, not 500.
   9. Try `get_grupr(grupr_id="../../../etc/passwd")` — pathological input. Should reject cleanly.

E. **Rate-limit probe**
   10. After succeeding to log in, post 8 messages back-to-back into any grupr (you may need to create one first IF email_unverified gate allows you to — likely it doesn't). If the gate blocks message posting, that's expected; report it as pass. Otherwise count how many succeed before the api shed traffic.

F. **GDPR delete-then-relogin**
   11. Call `export_my_data()` first to capture state.
   12. Call `delete_my_account(password=...)`. Should succeed (pseudonymizes the row).
   13. Try to `login()` again with the same credentials. Should fail — pseudonymized email no longer matches. Report.

End with `finish(reason)`.

For EACH probe: state expected vs actual in your finding detail. That makes triage instant.
"""


def main() -> int:
    load_dotenv(override=True)

    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_ADVERSARY_EMAIL")
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    model = os.environ.get("GRUPR_TEST_MODEL", "claude-sonnet-4-5-20250929")
    max_cost = float(os.environ.get("GRUPR_TEST_MAX_COST_USD", "5.0"))
    max_turns = int(os.environ.get("GRUPR_TEST_MAX_TURNS", "40"))

    if not email or not password or not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: see .env.example", file=sys.stderr)
        return 2

    reporter = Reporter.for_run("adversary")
    print(f"adversary run -> {reporter.out_dir}")

    with UserClient(base_url=api_base) as client:
        runner = PersonaRunner(
            persona_name="adversary",
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
