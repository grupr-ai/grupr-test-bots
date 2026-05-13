"""Thin wrapper over the @grupr PyPI SDK for the external_dev persona.

The SDK already covers everything an agent-hub participant needs
(register, poll_messages, send_message, stream_events). This module
exists for two reasons:

  1. Dogfooding — we exercise our own published SDK against
     api.grupr.ai as part of the persona sweep. If the SDK has bugs,
     we surface them here before third-party developers do.
  2. Tool-friendly signatures — the persona runner wants simple
     `func(name: str, ...)` callables. This layer flattens the SDK's
     class instance into module-level helpers that bind to a single
     agent token, so it slots cleanly into the runner's ToolDef list.

The external_dev persona is the only persona that needs this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

try:
    from grupr import Grupr  # type: ignore
except ImportError:  # pragma: no cover — only matters at import time
    Grupr = None  # type: ignore


@dataclass
class AgentHubSession:
    """Single agent-hub session bound to one agent token."""

    client: Any  # grupr.Grupr — typed as Any to avoid import-time crash if SDK absent

    @classmethod
    def register(
        cls, user_jwt: str, agent_id: str, base_url: str = "https://api.grupr.ai"
    ) -> tuple["AgentHubSession", str]:
        """Mint a fresh agent token. Returns (session, token).

        Takes the api root (e.g. https://api.grupr.ai) and converts to
        the agent-hub root the SDK expects. The published SDK's
        `Grupr.register(base_url=...)` parameter is documented to be
        the agent-hub root, not the api root — a real DX trap if you
        misread the parameter. Day-3 persona run surfaced this.

        The token is shown only once by api side; persist it if you'd
        rather run the persona again without re-minting.
        """
        if Grupr is None:
            raise RuntimeError(
                "The `grupr` PyPI package isn't installed. Run `pip install grupr` "
                "or install grupr-test-bots from pyproject.toml."
            )
        agent_hub_url = base_url.rstrip("/") + "/api/v1/agent-hub"
        client, token = Grupr.register(jwt=user_jwt, agent_id=agent_id, base_url=agent_hub_url)
        return cls(client=client), token.token

    @classmethod
    def from_token(cls, agent_token: str, base_url: str = "https://api.grupr.ai") -> "AgentHubSession":
        if Grupr is None:
            raise RuntimeError("`grupr` SDK not installed")
        agent_hub_url = base_url.rstrip("/") + "/api/v1/agent-hub"
        client = Grupr(agent_token=agent_token, base_url=agent_hub_url)
        return cls(client=client)

    # ── operations the persona uses ────────────────────────────────

    def poll_messages(self, grupr_id: str, limit: int = 50) -> list[dict[str, Any]]:
        result = self.client.poll_messages(grupr_id, limit=limit)
        # Return a list of plain dicts so the runner can JSON it.
        # SDK's Message dataclass exposes the author as `sender_id` —
        # the api returns this field as `sender_id` too. There is no
        # `user_id` on the SDK Message; an early version of this
        # wrapper accessed .user_id and crashed once an agent had
        # posted, surfaced by Day-3 persona run.
        return [
            {
                "message_id": m.message_id,
                "grupr_id": m.grupr_id,
                "content": m.content,
                "agent_id": m.agent_id,
                "sender_id": m.sender_id,
                "created_at": str(m.created_at),
            }
            for m in (result.messages or [])
        ]

    def send_message(self, grupr_id: str, content: str) -> dict[str, Any]:
        msg = self.client.send_message(grupr_id, content)
        return {"message_id": getattr(msg, "message_id", ""), "content": content}

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
