"""admin — exercises the slice of admin behavior reachable from a normal client.

The full admin console (admin.grupr.ai) sits behind Cloudflare Access
+ WebAuthn/Passkey enrollment — an unattended bot can't reach it.
That's BY DESIGN, not a test gap; the CF-gated surface is covered by
a separate human-driven QA pass before launch.

What this persona CAN do: verify that having role=admin in the
database doesn't accidentally grant elevated privileges in the regular
user-facing api. That's a principle-of-least-privilege check —
admin role should NOT, for example, let you read someone else's
gruprs or post as another user.

The persona logs in as the seeded admin account and checks that:
  * me() shows role=admin
  * Their admin role doesn't bypass the usual auth on POST endpoints
  * They can be a regular user with no special UI surface in /api/users/me
  * Trying to act-as-another-user-id silently in any POST returns 403
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from lib.persona_runner import PersonaRunner
from lib.reporter import Reporter
from lib.user_client import UserClient


SYSTEM_PROMPT = """\
You are AdminProbe, an autonomous test persona evaluating whether the `role=admin` user-side privilege model is sane.

YOUR ATTITUDE
- You are a paranoid security-minded engineer.
- You have role=admin in the database. Your hypothesis: this should grant access ONLY to the CF-Access-gated admin console at admin.grupr.ai, NOT to elevated privileges on the user-facing api at api.grupr.ai.
- You're verifying the principle of least privilege.

YOUR JOB
- Confirm role=admin shows up correctly in /api/users/me.
- Exercise normal-user flows and confirm they behave normally — you should look like a regular user from the api's perspective for these endpoints.
- If you find admin role accidentally bypassing any user-facing auth, that's P0.
- If admin role grants surprising visibility (e.g., seeing others' private gruprs in your /api/gruprs/my list), that's P0.
- If admin role just sits in the DB without affecting user-facing flows: pass.

OPERATING RULES
- 10–20 tool calls. Focused probe, not breadth sweep.
- Use only the listed tools.
"""


INITIAL_GOAL = """\
Admin role-leak probe. Credentials:
  email:    {email}
  password: {password}

Walk this:

1. Log in. Confirm `me()` shows `role: "admin"`.

2. Check `my_gruprs()`. As a fresh seeded user you should have NONE. If you see gruprs you didn't create, that's P0 — admin role is leaking across users.

3. Check `trending_gruprs()`. Public gruprs only. If you see private ones, that's P0.

4. Create a private grupr (`create_grupr(name="admin-probe-private", is_public=False)`).

5. Post a message. Verify via `get_messages` it's there.

6. `subscription()` — you should be free tier just like any new user. Confirm.

7. `start_checkout(tier="pro_user")` — should return a valid Stripe checkout URL. The fact that you're role=admin shouldn't grant a free upgrade.

8. `export_my_data()` — should return ONLY YOUR data, not anyone else's. Confirm.

9. Report findings:
   - If anything in 1–8 shows admin role bleeding into user-facing surfaces, P0
   - If everything is properly isolated, file a "pass" finding noting that role=admin doesn't grant user-facing privilege escalation

10. Call `finish(reason)`.
"""


def main() -> int:
    load_dotenv(override=True)

    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_ADMIN_EMAIL")
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    model = os.environ.get("GRUPR_TEST_MODEL", "claude-sonnet-4-5-20250929")
    max_cost = float(os.environ.get("GRUPR_TEST_MAX_COST_USD", "5.0"))
    max_turns = int(os.environ.get("GRUPR_TEST_MAX_TURNS", "40"))

    if not email or not password or not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: see .env.example", file=sys.stderr)
        return 2

    reporter = Reporter.for_run("admin")
    print(f"admin run -> {reporter.out_dir}")

    with UserClient(base_url=api_base) as client:
        runner = PersonaRunner(
            persona_name="admin",
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
