"""Smoke: Perplexity provider is registered + DB constraint allows it.

Doesn't invoke the Perplexity API (would require a real PERPLEXITY_API_KEY
in BYOK or platform keys, neither of which is wired by default — paid users
BYOK Perplexity per the open-model strategy). Instead verifies:
  - The new provider passes the ai_agents.provider CHECK (migration 034).
  - The registry recognizes "perplexity" (POST agent succeeds and the agent
    is queryable). Adapter wiring would otherwise 500 on agent dispatch,
    but that's exercised by paid-user smokes when a real key exists.

Usage:
  source .venv/Scripts/activate
  python scripts/smoke-perplexity-provider.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from lib.user_client import UserClient, UserClientError


def main() -> int:
    load_dotenv()
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL", "bret.babcock+gtb-newuser@gmail.com")
    password = os.environ.get("GRUPR_TEST_PASSWORD", "test-bot-password-2026")

    print(f"Perplexity provider smoke: {email} -> {api_base}")

    with UserClient(base_url=api_base) as client:
        try:
            client.login(email, password)
            print("  login OK")
        except UserClientError as e:
            print(f"  login FAILED: {e}", file=sys.stderr)
            return 1

        # Migration 034 must allow provider='perplexity' OR this 500s with
        # "ai_agents_provider_check" constraint violation.
        try:
            agent_id = client.create_agent(
                display_name="PerplexitySmokeBot",
                provider="perplexity",
                model_id="sonar-pro",
                system_prompt="Reply with one sentence including a citation.",
            )
            print(f"  agent created: {agent_id}")
            print(f"  provider=perplexity accepted by migration 034 OK")
        except UserClientError as e:
            print(f"  agent create FAILED: {e}", file=sys.stderr)
            return 1

        # Confirm it round-trips.
        try:
            agents = client.my_agents()
            match = [a for a in agents if a.get("agent_id") == agent_id]
            if not match:
                print(f"  agent not found in my_agents()", file=sys.stderr)
                return 1
            a = match[0]
            print(f"  agent round-trip: provider={a.get('provider')} model_id={a.get('model_id')}")
        except UserClientError as e:
            print(f"  agent list FAILED: {e}", file=sys.stderr)
            return 1

    print("\nPerplexity provider smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
