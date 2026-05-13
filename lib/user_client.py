"""User-facing api.grupr.ai client.

This is intentionally NOT the @grupr PyPI SDK — that one is scoped to
agent-hub flows (third-party agents participating in gruprs). User-
facing flows like login, creating gruprs, posting messages, viewing
subscription state, etc. don't have an official SDK yet. This module
fills that gap for the test framework. Some of these helpers are
candidates for promotion into a "user" namespace on @grupr eventually.

All methods return parsed JSON (or raise UserClientError on failures
worth halting on). No silent retries; the personas use the runner-
level error handling so failures surface in the report.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


log = logging.getLogger(__name__)


class UserClientError(Exception):
    """Raised on api errors a persona should treat as material findings."""

    def __init__(self, status: int, code: str, message: str, path: str):
        self.status = status
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"{path} -> {status} {code}: {message}")


@dataclass
class LoginResult:
    access_token: str
    refresh_token: str
    user_id: str
    email: str
    email_verified: bool
    has_2fa: bool
    challenge_token: Optional[str] = None  # set when 2fa is required


@dataclass
class GruprSummary:
    grupr_id: str
    name: str
    grup_type: str
    is_public: bool
    member_count: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "GruprSummary":
        return cls(
            grupr_id=raw["grupr_id"],
            name=raw["name"],
            grup_type=raw.get("grup_type", "unknown"),
            is_public=raw.get("is_public", False),
            member_count=raw.get("member_count", 0),
        )


@dataclass
class CallLog:
    """One row in the persona's network journal — emitted on every call.

    Personas don't get raw access to this; the runner persists it to
    the run output for forensics ("what did the LLM actually try?").
    """

    ts: float
    method: str
    path: str
    status: int
    latency_ms: int
    error: Optional[str] = None


class UserClient:
    """Sync client. One per persona run. Holds the access/refresh cookies."""

    def __init__(
        self,
        base_url: str = "https://api.grupr.ai",
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        # Cached at login() so password-reconfirm endpoints (GDPR export +
        # delete) can use it without the LLM having to remember it. Test-
        # bot context — these are seeded throwaway accounts.
        self._password: Optional[str] = None
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.call_log: list[CallLog] = []

    # ── lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "UserClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── internal request helper ─────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        require_auth: bool = True,
        raise_on_error: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        """All api calls funnel through here so the journal stays complete."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if require_auth and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"

        started = time.monotonic()
        error_msg: Optional[str] = None
        try:
            response = self._http.request(
                method, path, json=json, params=params, headers=headers
            )
            status = response.status_code
            try:
                body = response.json()
            except Exception:
                body = {"_raw": response.text[:500]}
        except httpx.HTTPError as e:
            status = 0
            body = {"_transport_error": str(e)}
            error_msg = str(e)
        latency_ms = int((time.monotonic() - started) * 1000)

        self.call_log.append(
            CallLog(
                ts=time.time(),
                method=method,
                path=path,
                status=status,
                latency_ms=latency_ms,
                error=error_msg,
            )
        )

        if raise_on_error and (status >= 400 or status == 0):
            errors = body.get("errors") if isinstance(body, dict) else None
            if errors and isinstance(errors, list):
                code = errors[0].get("code", "unknown")
                message = errors[0].get("message", "(no message)")
            else:
                code = body.get("error", "unknown") if isinstance(body, dict) else "unknown"
                message = error_msg or str(body)[:200]
            raise UserClientError(status, code, message, path)

        return status, body

    # ── auth ───────────────────────────────────────────────────────

    def login(self, email: str, password: str) -> LoginResult:
        """POST /api/auth/login. Stashes tokens in the client.

        Returns a LoginResult. If 2FA is required, `challenge_token`
        is set and `access_token` is empty — persona must follow up
        with verify_2fa(...).
        """
        status, body = self._request(
            "POST",
            "/api/auth/login",
            json={"email": email, "password": password},
            require_auth=False,
            raise_on_error=False,
        )
        if status >= 400:
            raise UserClientError(
                status,
                body.get("errors", [{}])[0].get("code", "login_failed") if isinstance(body, dict) else "login_failed",
                body.get("errors", [{}])[0].get("message", "Login failed") if isinstance(body, dict) else "Login failed",
                "/api/auth/login",
            )

        data = body.get("data", body)
        challenge = data.get("challenge_token") or data.get("two_factor_challenge")
        if challenge:
            return LoginResult(
                access_token="",
                refresh_token="",
                user_id=data.get("user_id", ""),
                email=email,
                email_verified=False,
                has_2fa=True,
                challenge_token=challenge,
            )

        self._access_token = data.get("access_token", "")
        self._refresh_token = data.get("refresh_token", "")
        self._password = password
        return LoginResult(
            access_token=self._access_token,
            refresh_token=self._refresh_token,
            user_id=data.get("user", {}).get("user_id", "") or data.get("user_id", ""),
            email=email,
            email_verified=data.get("user", {}).get("email_verified", True),
            has_2fa=False,
        )

    def logout(self) -> None:
        try:
            self._request("POST", "/api/auth/logout", raise_on_error=False)
        finally:
            self._access_token = None
            self._refresh_token = None

    def refresh_session(self) -> None:
        if not self._refresh_token:
            raise UserClientError(401, "no_refresh_token", "No refresh token available", "/api/auth/refresh")
        _, body = self._request(
            "POST",
            "/api/auth/refresh",
            json={"refresh_token": self._refresh_token},
            require_auth=False,
        )
        data = body.get("data", body)
        self._access_token = data.get("access_token", "")
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]

    # ── self ───────────────────────────────────────────────────────

    def me(self) -> dict[str, Any]:
        """GET /api/users/me. Includes email_verified post-Day-2 follow-up."""
        _, body = self._request("GET", "/api/users/me")
        return body.get("data", body)

    def export_my_data(self, password: str | None = None) -> dict[str, Any]:
        """POST /api/users/me/export — GDPR Art. 20. Requires password
        re-confirm in body. If password isn't passed explicitly, uses
        the one cached at login (test-bot convenience).
        """
        pw = password or self._password
        if not pw:
            raise UserClientError(0, "no_password", "No password — call login() first or pass password=", "/api/users/me/export")
        _, body = self._request(
            "POST",
            "/api/users/me/export",
            json={"current_password": pw},
        )
        return body.get("data", body)

    def delete_my_account(self, password: str | None = None) -> None:
        """DELETE /api/users/me — GDPR Art. 17. Pseudonymizes, not hard-deletes."""
        pw = password or self._password
        if not pw:
            raise UserClientError(0, "no_password", "No password — call login() first or pass password=", "/api/users/me")
        self._request(
            "DELETE",
            "/api/users/me",
            json={"current_password": pw},
        )

    # ── gruprs ─────────────────────────────────────────────────────

    def my_gruprs(self) -> list[GruprSummary]:
        _, body = self._request("GET", "/api/gruprs/my")
        return [GruprSummary.from_api(g) for g in body.get("data", []) or []]

    def trending_gruprs(self, limit: int = 12) -> list[GruprSummary]:
        _, body = self._request("GET", "/api/gruprs/trending", params={"limit": limit})
        return [GruprSummary.from_api(g) for g in body.get("data", []) or []]

    def create_grupr(
        self,
        name: str,
        grupr_type: str = "workshop",
        description: str = "",
        is_public: bool = False,
        category: str = "general",
    ) -> str:
        """POST /api/gruprs. Returns grupr_id."""
        _, body = self._request(
            "POST",
            "/api/gruprs",
            json={
                "name": name,
                "description": description,
                "type": grupr_type,
                "is_public": is_public,
                "category": category,
                "max_members": 50,
            },
        )
        data = body.get("data", body)
        return data.get("grupr_id", "")

    def get_grupr(self, grupr_id: str) -> dict[str, Any]:
        _, body = self._request("GET", f"/api/gruprs/{grupr_id}")
        return body.get("data", body)

    def join_grupr(self, grupr_id: str) -> None:
        self._request("POST", f"/api/gruprs/{grupr_id}/join")

    def post_message(self, grupr_id: str, content: str) -> str:
        _, body = self._request(
            "POST",
            f"/api/gruprs/{grupr_id}/messages",
            json={"content": content},
        )
        data = body.get("data", body)
        return data.get("message_id", "")

    def get_messages(self, grupr_id: str, limit: int = 50) -> list[dict[str, Any]]:
        _, body = self._request(
            "GET", f"/api/gruprs/{grupr_id}/messages", params={"limit": limit}
        )
        return body.get("data", []) or []

    # ── 2fa ────────────────────────────────────────────────────────

    def two_factor_status(self) -> dict[str, Any]:
        _, body = self._request("GET", "/api/auth/2fa/status")
        return body.get("data", body)

    def two_factor_enroll_begin(self) -> dict[str, Any]:
        """Returns the otpauth URL + QR-source data the user would scan."""
        _, body = self._request("POST", "/api/auth/2fa/enroll/begin")
        return body.get("data", body)

    def two_factor_enroll_finish(self, totp_code: str) -> list[str]:
        """Returns the 10 backup codes (shown once)."""
        _, body = self._request(
            "POST", "/api/auth/2fa/enroll/finish", json={"code": totp_code}
        )
        data = body.get("data", body)
        return data.get("backup_codes", []) or []

    def two_factor_verify(self, challenge_token: str, totp_code: str) -> LoginResult:
        """Completes a 2fa-required login. Stashes the tokens."""
        _, body = self._request(
            "POST",
            "/api/auth/2fa/verify",
            json={"challenge_token": challenge_token, "code": totp_code},
            require_auth=False,
        )
        data = body.get("data", body)
        self._access_token = data.get("access_token", "")
        self._refresh_token = data.get("refresh_token", "")
        return LoginResult(
            access_token=self._access_token,
            refresh_token=self._refresh_token,
            user_id=data.get("user", {}).get("user_id", ""),
            email=data.get("user", {}).get("email", ""),
            email_verified=data.get("user", {}).get("email_verified", True),
            has_2fa=True,
        )

    # ── subscription ───────────────────────────────────────────────

    def subscription(self) -> dict[str, Any]:
        _, body = self._request("GET", "/api/subscription")
        return body.get("data", body)

    def start_checkout(self, tier: str) -> str:
        """POST /api/subscription/checkout. Returns the Stripe checkout URL.

        Doesn't actually go through Stripe — we just want to confirm
        the api creates a Checkout session successfully. The url
        leads to live Stripe and would require a real card to complete.
        """
        _, body = self._request(
            "POST", "/api/subscription/checkout", json={"tier": tier}
        )
        data = body.get("data", body)
        return data.get("checkout_url", "")

    def open_portal(self) -> str:
        _, body = self._request("POST", "/api/subscription/portal", json={})
        data = body.get("data", body)
        return data.get("portal_url", "")

    # ── agents (user-side, not @grupr SDK side) ─────────────────────

    def create_agent(
        self,
        display_name: str,
        provider: str = "openai",
        model_id: str = "gpt-4o-mini",
        system_prompt: str = "You are a helpful assistant.",
        is_public: bool = False,
    ) -> str:
        """POST /api/agents. Fields per the api's createAgentRequest struct —
        display_name (required), provider, model_id, system_prompt, is_public.
        No `name` or `handle` field exists in the request shape.
        """
        _, body = self._request(
            "POST",
            "/api/agents",
            json={
                "display_name": display_name,
                "provider": provider,
                "model_id": model_id,
                "system_prompt": system_prompt,
                "is_public": is_public,
            },
        )
        data = body.get("data", body)
        return data.get("agent_id", "")

    def my_agents(self) -> list[dict[str, Any]]:
        _, body = self._request("GET", "/api/agents")
        return body.get("data", []) or []

    def add_agent_to_grupr(self, grupr_id: str, agent_id: str) -> None:
        """POST /api/gruprs/:id/agents. Owner/admin only — assigns one of
        your agents to a grupr so it can poll and post via the @grupr SDK.
        Without this step the agent token gets "Agent is not assigned to
        this grupr" on every call.
        """
        self._request(
            "POST",
            f"/api/gruprs/{grupr_id}/agents",
            json={"agent_id": agent_id},
        )

    # ── exposed accessor for the runner ──────────────────────────

    @property
    def access_token(self) -> Optional[str]:
        """The runner needs this to hand off to grupr_client for agent flows."""
        return self._access_token
