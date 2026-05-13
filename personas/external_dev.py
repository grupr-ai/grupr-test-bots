"""external_dev — third-party agent developer integrating via @grupr SDK.

The end-to-end agent-hub journey:
  1. Log in as a regular user (the developer)
  2. Create an agent via the user-side API (POST /api/agents)
  3. Mint an agent token via the @grupr SDK's Grupr.register(jwt, agent_id)
  4. Use the SDK to poll messages, send messages — as a third-party agent

This is the persona that exercises our published SDK end-to-end and
surfaces any gaps between docs and reality. Findings here are
disproportionately valuable because every third-party developer who
ever builds on Grupr will hit the same friction points.

Note: this persona has access to BOTH user-side tools (UserClient via
the runner's default tool set) AND the SDK-side tools (extra_tools
exposed below). Most personas only get UserClient.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from dotenv import load_dotenv

from lib.persona_runner import PersonaRunner, ToolDef
from lib.reporter import Reporter
from lib.user_client import UserClient

try:
    from grupr import Grupr  # @grupr SDK from PyPI
except ImportError:
    Grupr = None  # type: ignore


SYSTEM_PROMPT = """\
You are ExternalDev, an autonomous test persona evaluating Grupr's third-party agent integration story.

YOUR ATTITUDE
- You are a developer building an AI agent that participates in Grupr conversations.
- You've read the @grupr Python SDK README and you're trying to wire your agent up against the live api.
- You care about the developer experience: clear errors, good docs, sane defaults, no foot-guns.
- You expect the SDK to work the way the README says it works. Deviations are findings.

YOUR JOB
- Walk through the full third-party-agent lifecycle: log in as user → create agent → mint token → use SDK to participate.
- Report ANY gap between SDK behavior and the README. Even small ones — a sloppy error message, an undocumented requirement, a method that doesn't behave as you'd expect from its name.
- Severity rubric: P0 = SDK is fundamentally broken or docs misleading enough to block adoption, P1 = significant friction, P2 = polish, P3 = nit, obs = neutral, pass = positive.

OPERATING RULES
- Use only the tools listed. Don't fabricate api paths.
- 15–25 tool calls is the right ballpark.
- If you can't complete the full lifecycle for some reason, report exactly why — that's important data.
"""


INITIAL_GOAL = """\
External-dev journey. Credentials:
  email:    {email}
  password: {password}

Walk this end-to-end:

1. Log in as the developer.

2. Check your existing agents (`my_agents()`). Probably none.

3. Create a new agent (`create_agent`). Give it:
   - name: "ExternalDev Test Bot"
   - handle: "external_dev_bot_v1" (or similar lowercase URL-safe)
   - description: "Auto-created by external_dev persona test"
   Note the returned agent_id.

4. Mint an agent token using the SDK helper (`mint_agent_token(agent_id)`). This calls `Grupr.register(...)` from the @grupr SDK. The token is shown ONCE — capture it.

5. Create a grupr the agent can participate in (`create_grupr(name=..., is_public=True)`). Note the new grupr_id — you (as owner) will assign the agent to it.

6. Assign your agent to the grupr (`add_agent_to_grupr(grupr_id, agent_id)`). Agents need explicit assignment by an owner/admin before they can read or post in a grupr — surfacing this requirement in the SDK README is a real DX consideration to flag.

7. Use the SDK to poll messages in the grupr (`sdk_poll_messages(grupr_id, limit=N)`). Should now succeed because the agent is assigned.

8. Use the SDK to send a message AS THE AGENT (`sdk_send_message(grupr_id, content="Hello from external_dev test bot.")`). Confirm via the user-side `get_messages` that the message appears with the agent's identity attached.

9. Report ANY of these:
   - SDK methods that don't match the README
   - Error messages that aren't useful
   - Endpoints that 404 when they should 200 (or vice versa)
   - Authorization patterns that surprise you
   - Missing helper methods or undocumented prereq steps
   - Anything you'd want fixed before you'd recommend the SDK to a colleague

10. Call `finish(reason)`.

If `mint_agent_token` fails because the api `/api/v1/agent-hub/register` endpoint doesn't accept your JWT, report it as P0 — that's the critical first step for every third-party developer.
"""


def main() -> int:
    load_dotenv(override=True)

    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_EXTERNAL_DEV_EMAIL")
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    model = os.environ.get("GRUPR_TEST_MODEL", "claude-sonnet-4-5-20250929")
    max_cost = float(os.environ.get("GRUPR_TEST_MAX_COST_USD", "5.0"))
    max_turns = int(os.environ.get("GRUPR_TEST_MAX_TURNS", "40"))

    if not email or not password or not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: see .env.example", file=sys.stderr)
        return 2
    if Grupr is None:
        print("ERROR: @grupr SDK not importable. pip install grupr.", file=sys.stderr)
        return 2

    reporter = Reporter.for_run("external_dev")
    print(f"external_dev run -> {reporter.out_dir}")

    # Agent-hub state captured across SDK tool calls
    state: dict[str, Any] = {"agent_token": None, "agent_client": None}

    with UserClient(base_url=api_base) as client:

        # The SDK's base_url param is documented as the agent-hub root,
        # not the api root. api_base from .env is the api root, so we
        # construct the agent-hub URL here. Easy DX trap — Day-3 sweep
        # surfaced this when the persona's mint call silently 404'd
        # against the user-signup /register endpoint at the api root.
        agent_hub_url = api_base.rstrip("/") + "/api/v1/agent-hub"

        def _mint(agent_id: str) -> dict[str, Any]:
            if not client.access_token:
                return {"error": "must log in first via the login tool"}
            try:
                sdk_client, token_info = Grupr.register(
                    jwt=client.access_token, agent_id=agent_id, base_url=agent_hub_url
                )
            except Exception as e:
                return {"error": True, "exception": type(e).__name__, "message": str(e)[:300]}
            state["agent_token"] = token_info.token
            state["agent_client"] = sdk_client
            # Return a redacted view; never echo the full token in the LLM transcript.
            return {
                "ok": True,
                "agent_id": getattr(token_info, "agent_id", agent_id),
                "token_prefix": (token_info.token[:8] + "...") if token_info.token else "",
                "note": "Token captured. Use sdk_poll_messages / sdk_send_message — they bind to this token internally.",
            }

        def _sdk_poll(grupr_id: str, limit: int = 20) -> Any:
            ac = state["agent_client"]
            if ac is None:
                return {"error": "no agent token — call mint_agent_token first"}
            try:
                result = ac.poll_messages(grupr_id, limit=limit)
                # SDK Message exposes the author as `sender_id`. No `user_id`
                # field exists on the dataclass — Day-3 sweep surfaced an
                # AttributeError when this wrapper tried to read .user_id.
                return [
                    {
                        "message_id": m.message_id,
                        "content": m.content[:200],
                        "agent_id": m.agent_id,
                        "sender_id": m.sender_id,
                    }
                    for m in (result.messages or [])
                ]
            except Exception as e:
                return {"error": True, "exception": type(e).__name__, "message": str(e)[:300]}

        def _sdk_send(grupr_id: str, content: str) -> Any:
            ac = state["agent_client"]
            if ac is None:
                return {"error": "no agent token — call mint_agent_token first"}
            try:
                ac.send_message(grupr_id, content)
                return {"ok": True}
            except Exception as e:
                return {"error": True, "exception": type(e).__name__, "message": str(e)[:300]}

        sdk_tools = [
            ToolDef(
                name="mint_agent_token",
                description="Use the @grupr SDK's Grupr.register(jwt, agent_id) to mint a fresh agent token bound to one of your agents. Must be logged in first — the user JWT is read from the client's access_token automatically. Returns a redacted view; the real token is held internally and used by sdk_poll_messages / sdk_send_message.",
                input_schema={
                    "type": "object",
                    "properties": {"agent_id": {"type": "string"}},
                    "required": ["agent_id"],
                },
                handler=_mint,
            ),
            ToolDef(
                name="sdk_poll_messages",
                description="Use the @grupr SDK to poll messages in a grupr AS THE AGENT (not as the user). Requires mint_agent_token first.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "grupr_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["grupr_id"],
                },
                handler=_sdk_poll,
            ),
            ToolDef(
                name="sdk_send_message",
                description="Use the @grupr SDK to post a message in a grupr AS THE AGENT. Requires mint_agent_token first.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "grupr_id": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["grupr_id", "content"],
                },
                handler=_sdk_send,
            ),
        ]

        runner = PersonaRunner(
            persona_name="external_dev",
            system_prompt=SYSTEM_PROMPT,
            initial_goal=INITIAL_GOAL.format(email=email, password=password),
            client=client,
            reporter=reporter,
            extra_tools=sdk_tools,
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
