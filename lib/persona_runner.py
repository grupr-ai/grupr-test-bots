"""Shared agent loop for LLM-driven personas.

Each persona is a Sonnet-4.5 agent (the primary model in the project
memory) that's given:
  * a role (system prompt)
  * a goal (first user message)
  * a set of tools (UserClient methods + report_finding + finish)
  * a UserClient bound to api.grupr.ai

The runner enforces caps (USD + turn count), funnels every tool call
through the UserClient (which journals it for the reporter), and
finalizes the reporter at the end — including on hard failure, so a
crashed run still produces partial findings.

This module is intentionally NOT abstract over the model SDK — we
use Anthropic's SDK directly. If we ever want to drive personas with
a different model (e.g. for cost A/B testing), copy this file and
swap the SDK; the persona code itself stays portable as long as it
sticks to tool calls.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import anthropic

from lib.reporter import Reporter, Severity
from lib.user_client import UserClient, UserClientError


log = logging.getLogger(__name__)


# Sonnet 4.5 / claude-sonnet-4-5-20250929 pricing (per 1M tokens).
# Updated when Anthropic publishes new tiers; if you swap models,
# bump these too or cost reports will lie.
_PRICE_PER_MTOK_INPUT = 3.0
_PRICE_PER_MTOK_OUTPUT = 15.0


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class PersonaFinished(Exception):
    """Raised inside a tool handler to terminate the run cleanly."""

    def __init__(self, reason: str):
        self.reason = reason


@dataclass
class PersonaRunner:
    persona_name: str
    system_prompt: str
    initial_goal: str
    client: UserClient
    reporter: Reporter
    extra_tools: list[ToolDef] = field(default_factory=list)
    model: str = "claude-sonnet-4-5-20250929"
    max_cost_usd: float = 5.0
    max_turns: int = 40

    def _build_default_tools(self) -> list[ToolDef]:
        """Wraps UserClient methods + report/finish controls as Anthropic tools."""
        c = self.client
        r = self.reporter
        tools: list[ToolDef] = []

        tools.append(ToolDef(
            name="report_finding",
            description=(
                "Record a finding from this run. Use this for ANY observation worth a human's "
                "attention: bugs, friction, surprising UX, missing features, security concerns, "
                "or positive confirmations that an important flow works. Use severity 'P0' for "
                "launch blockers, 'P1' high, 'P2' medium, 'P3' polish, 'obs' for neutral notes, "
                "'pass' to confirm a critical flow succeeded."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3", "obs", "pass"]},
                    "title": {"type": "string", "description": "One-line summary of the finding."},
                    "detail": {"type": "string", "description": "Multi-line detail. Reproduction steps, observations, what you expected vs got."},
                },
                "required": ["severity", "title"],
            },
            handler=lambda severity, title, detail="": (r.add(severity, title, detail), {"recorded": True})[1],
        ))

        tools.append(ToolDef(
            name="finish",
            description="Call when you are done with the task. Provide a one-line summary of what you accomplished or why you're stopping early.",
            input_schema={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
            handler=self._finish,
        ))

        tools.append(ToolDef(
            name="login",
            description="Log in. Stashes the access/refresh tokens in the client for subsequent calls.",
            input_schema={
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "password": {"type": "string"},
                },
                "required": ["email", "password"],
            },
            handler=lambda email, password: _serialize(c.login(email, password)),
        ))

        tools.append(ToolDef(
            name="me",
            description="GET /api/users/me — current authenticated user, includes email_verified flag.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: c.me(),
        ))

        tools.append(ToolDef(
            name="my_gruprs",
            description="List the gruprs the current user is a member of.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: [_serialize(g) for g in c.my_gruprs()],
        ))

        tools.append(ToolDef(
            name="trending_gruprs",
            description="List public gruprs by trending score.",
            input_schema={"type": "object", "properties": {"limit": {"type": "integer", "default": 12}}},
            handler=lambda limit=12: [_serialize(g) for g in c.trending_gruprs(limit)],
        ))

        tools.append(ToolDef(
            name="create_grupr",
            description="Create a new grupr. Returns the new grupr_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "grupr_type": {"type": "string", "enum": ["workshop", "arena", "groupchat"], "default": "workshop"},
                    "description": {"type": "string", "default": ""},
                    "is_public": {"type": "boolean", "default": False},
                    "category": {"type": "string", "default": "general"},
                },
                "required": ["name"],
            },
            handler=lambda name, grupr_type="workshop", description="", is_public=False, category="general":
                {"grupr_id": c.create_grupr(name, grupr_type, description, is_public, category)},
        ))

        tools.append(ToolDef(
            name="get_grupr",
            description="Get a single grupr by id.",
            input_schema={"type": "object", "properties": {"grupr_id": {"type": "string"}}, "required": ["grupr_id"]},
            handler=lambda grupr_id: c.get_grupr(grupr_id),
        ))

        tools.append(ToolDef(
            name="join_grupr",
            description="Join a public grupr as a member.",
            input_schema={"type": "object", "properties": {"grupr_id": {"type": "string"}}, "required": ["grupr_id"]},
            handler=lambda grupr_id: (c.join_grupr(grupr_id), {"joined": True})[1],
        ))

        tools.append(ToolDef(
            name="post_message",
            description="Post a text message to a grupr. Returns the new message_id.",
            input_schema={
                "type": "object",
                "properties": {"grupr_id": {"type": "string"}, "content": {"type": "string"}},
                "required": ["grupr_id", "content"],
            },
            handler=lambda grupr_id, content: {"message_id": c.post_message(grupr_id, content)},
        ))

        tools.append(ToolDef(
            name="get_messages",
            description="Get recent messages in a grupr (default last 50).",
            input_schema={
                "type": "object",
                "properties": {"grupr_id": {"type": "string"}, "limit": {"type": "integer", "default": 50}},
                "required": ["grupr_id"],
            },
            handler=lambda grupr_id, limit=50: c.get_messages(grupr_id, limit),
        ))

        tools.append(ToolDef(
            name="two_factor_status",
            description="Check whether 2FA is enabled for the current user.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: c.two_factor_status(),
        ))

        tools.append(ToolDef(
            name="two_factor_enroll_begin",
            description="Start 2FA enrollment. Returns the otpauth URL the user would scan into their authenticator app.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: c.two_factor_enroll_begin(),
        ))

        tools.append(ToolDef(
            name="subscription",
            description="Get the current user's subscription state (free/pro/team, trialing/active/cancelled, etc.).",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: c.subscription(),
        ))

        tools.append(ToolDef(
            name="start_checkout",
            description="Create a Stripe Checkout session URL for the given tier. Does NOT complete checkout — that would require a real card. Use this to confirm the api creates a session successfully and returns a checkout.stripe.com url.",
            input_schema={
                "type": "object",
                "properties": {"tier": {"type": "string", "enum": ["pro_user", "pro_agent", "team"]}},
                "required": ["tier"],
            },
            handler=lambda tier: {"checkout_url": c.start_checkout(tier)},
        ))

        tools.append(ToolDef(
            name="create_agent",
            description="Create an AI agent owned by the current user. Returns agent_id. Required: display_name. Optional: provider ('openai'/'anthropic'/'google'), model_id (e.g. 'gpt-4o-mini'), system_prompt, is_public.",
            input_schema={
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "provider": {"type": "string", "default": "openai"},
                    "model_id": {"type": "string", "default": "gpt-4o-mini"},
                    "system_prompt": {"type": "string", "default": "You are a helpful assistant."},
                    "is_public": {"type": "boolean", "default": False},
                },
                "required": ["display_name"],
            },
            handler=lambda display_name, provider="openai", model_id="gpt-4o-mini", system_prompt="You are a helpful assistant.", is_public=False:
                {"agent_id": c.create_agent(display_name, provider, model_id, system_prompt, is_public)},
        ))

        tools.append(ToolDef(
            name="my_agents",
            description="List agents owned by the current user.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: c.my_agents(),
        ))

        tools.append(ToolDef(
            name="add_agent_to_grupr",
            description="Assign one of your agents to a grupr so it can poll + post via the @grupr SDK. Owner/admin role required on the grupr. Without this step, agent tokens get 'Agent is not assigned to this grupr' errors on every call.",
            input_schema={
                "type": "object",
                "properties": {
                    "grupr_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                },
                "required": ["grupr_id", "agent_id"],
            },
            handler=lambda grupr_id, agent_id: (c.add_agent_to_grupr(grupr_id, agent_id), {"assigned": True})[1],
        ))

        tools.append(ToolDef(
            name="export_my_data",
            description="GDPR data export. Returns a JSON dump of the user's data.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: c.export_my_data(),
        ))

        return tools

    # ── lifecycle ─────────────────────────────────────────────────

    def _finish(self, reason: str) -> dict[str, Any]:
        raise PersonaFinished(reason)

    def run(self) -> None:
        """Drives the agent loop until finish() or a cap is hit. Always finalizes."""
        anth = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

        all_tools = self._build_default_tools() + list(self.extra_tools)
        tool_specs = [t.to_anthropic() for t in all_tools]
        handlers = {t.name: t.handler for t in all_tools}

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self.initial_goal}
        ]
        finished_reason: Optional[str] = None

        try:
            while True:
                if self.reporter.cost_usd >= self.max_cost_usd:
                    self.reporter.p2(
                        "Run aborted — cost cap hit",
                        f"Stopped after ${self.reporter.cost_usd:.3f} (cap ${self.max_cost_usd:.2f}). "
                        f"Persona may not have completed all goals."
                    )
                    break
                if self.reporter.turns >= self.max_turns:
                    self.reporter.p2(
                        "Run aborted — turn cap hit",
                        f"Stopped after {self.reporter.turns} turns (cap {self.max_turns}). "
                        f"Persona may have been stuck in a loop."
                    )
                    break

                response = anth.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    system=self.system_prompt,
                    tools=tool_specs,
                    messages=messages,
                )

                # Accounting
                in_tok = response.usage.input_tokens
                out_tok = response.usage.output_tokens
                cost = (in_tok / 1_000_000) * _PRICE_PER_MTOK_INPUT + (out_tok / 1_000_000) * _PRICE_PER_MTOK_OUTPUT
                self.reporter.record_usage(in_tok, out_tok, cost)

                # Append the assistant turn for the next call
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    # Model decided it's done without calling finish — treat as graceful exit.
                    finished_reason = "ended without explicit finish() call"
                    break

                # Otherwise, expect at least one tool_use block — dispatch each
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    tool_name = block.name
                    tool_input = block.input or {}
                    handler = handlers.get(tool_name)
                    if handler is None:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps({"error": f"unknown tool {tool_name}"}),
                            "is_error": True,
                        })
                        continue
                    try:
                        result = handler(**tool_input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(_jsonable(result), default=str),
                        })
                    except PersonaFinished as pf:
                        finished_reason = pf.reason
                        # Don't actually send results — break the outer loop.
                        tool_results = []
                        break
                    except UserClientError as ue:
                        # Surface the api error to the model so it can decide what to do.
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps({
                                "error": True,
                                "status": ue.status,
                                "code": ue.code,
                                "message": ue.message,
                                "path": ue.path,
                            }),
                            "is_error": True,
                        })
                    except Exception as e:
                        # Don't crash the run on a tool implementation bug — surface to model.
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps({"error": True, "exception": type(e).__name__, "message": str(e)[:300]}),
                            "is_error": True,
                        })
                        self.reporter.p1(
                            f"Test framework bug in tool {tool_name}",
                            f"Exception: {type(e).__name__}: {e}",
                            tool=tool_name,
                            input=tool_input,
                        )

                if finished_reason is not None:
                    break

                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
        finally:
            # Drain the UserClient's call log into the reporter regardless of how we exited.
            for c in self.client.call_log:
                self.reporter.record_network(c.method, c.path, c.status, c.latency_ms, c.error)

            if finished_reason:
                self.reporter.obs("Persona finished", finished_reason)

            self.reporter.finalize()


# ── helpers ────────────────────────────────────────────────────────


def _serialize(obj: Any) -> Any:
    """Convert dataclass instances into JSON-safe dicts for tool returns."""
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return _jsonable(obj)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)
