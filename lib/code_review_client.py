"""Quick + Deep tier Code Review orchestration for the bot-build tests.

Quick tier:
  Uses the legacy AI-Workshop dispatch path because the api's
  /api/code-review handler explicitly defers Quick + Smart back to
  the workshop flow (see internal/handlers/code_review.go header
  comment). We create an ai_workshop grupr with category=code, attach
  the 5-reviewer roster + Synthesizer in order, post the code, and
  poll messages until each reviewer has weighed in.

Deep tier:
  Uses the dedicated /api/code-review endpoint with tier=deep. The
  orchestrator runs sandboxed Claude Code + verification (tests +
  lint + typecheck) and returns a verified diff alongside a verdict.

Both functions are blocking until terminal/timeout — the bot-build
scripts call them serially per env.

The 5-reviewer roster matches scripts/audit-code-review-v1.py and
grupr-web/app/code-review/lib/reviewer-prompts.ts. Keep it in sync
manually for now; we'll extract a shared constant if a third caller
appears.
"""

from __future__ import annotations

import time
from typing import Any

from lib.user_client import UserClient, UserClientError


# Aligned with internal/services/codereview/reviewers.go LaunchReviewers as of
# 2026-05-18. Provider list (anthropic, openai, groq) matches the platform-key
# map populated in cmd/server/main.go so trial users can run Quick reviews
# without BYOK keys. Synthesizer LAST so it sees other verdicts.
REVIEWERS: list[dict[str, str]] = [
    {
        "role": "architect", "display_name": "Architect",
        "provider": "anthropic", "model_id": "claude-opus-4-20250514",
        "system_prompt": (
            "You are the Architecture reviewer in a multi-LLM code review. "
            "Focus exclusively on code structure, abstractions, coupling, naming, and design shape. "
            "Don't comment on security, performance, or style. "
            "Respond with: **Verdict**: ship|ship-with-changes|block, then top 3 findings with line refs."
        ),
    },
    {
        "role": "security", "display_name": "Security",
        "provider": "openai", "model_id": "gpt-4o",
        "system_prompt": (
            "You are the Security reviewer in a multi-LLM code review. "
            "Focus on injection vectors, secrets, auth, trust boundaries, OWASP Top 10. "
            "Respond with: **Verdict**: ship|ship-with-changes|block, then top 3 findings with line refs + OWASP category."
        ),
    },
    {
        "role": "performance", "display_name": "Performance",
        "provider": "groq", "model_id": "llama-3.3-70b-versatile",
        "system_prompt": (
            "You are the Performance reviewer in a multi-LLM code review. "
            "Focus on algorithmic complexity, hot paths, N+1, caching, blocking ops. "
            "Respond with: **Verdict**: ship|ship-with-changes|block, then top 3 findings with line refs + big-O."
        ),
    },
    {
        "role": "maintainability", "display_name": "Maintainability",
        "provider": "groq", "model_id": "llama-3.3-70b-versatile",
        "system_prompt": (
            "You are the Maintainability reviewer in a multi-LLM code review. "
            "Focus on readability, dead/duplicated code, comments, test coverage gaps. "
            "Respond with: **Verdict**: ship|ship-with-changes|block, then top 3 findings."
        ),
    },
    {
        "role": "synthesizer", "display_name": "Synthesizer",
        "provider": "anthropic", "model_id": "claude-opus-4-20250514",
        "system_prompt": (
            "You are the Synthesizer in a multi-LLM code review. "
            "Other specialists have posted reviews above. Read them and produce ONE unified verdict. "
            "If they disagree, surface it explicitly. Identify the single most important issue. "
            "Respond with: **Overall verdict**: ship|ship-with-changes|block, then must-fix / should-fix / nice-to-have."
        ),
    },
]


def run_quick(
    client: UserClient,
    code: str,
    review_name: str | None = None,
    poll_timeout_s: int = 240,
    poll_interval_s: float = 4.0,
) -> dict[str, Any]:
    """Run a Quick-tier code review using `client`'s logged-in user.

    Creates a fresh ai_workshop+code grupr, attaches the reviewer
    roster, posts the code, polls for the 5 agent responses.

    Returns:
      {
        "grupr_id": str,
        "elapsed_s": int,
        "reviewer_count_returned": int,
        "expected_count": int,
        "verdicts": [{"reviewer": str, "content": str, "created_at": str}, ...],
        "synthesizer_verdict": str,   # empty if Synthesizer didn't post
        "timed_out": bool,
      }
    """
    t0 = time.monotonic()
    name = review_name or f"Code Review {time.strftime('%Y-%m-%d %H:%M:%S')}"

    # The api hardcodes category='general' inside UserClient.create_grupr,
    # so we reach past the SDK helper to set category=code. The handler
    # accepts grup_type=ai_workshop.
    _, body = client._request("POST", "/api/gruprs", json={
        "name": name,
        "description": "Bot-built artifact — Quick tier review",
        "category": "code",
        "grup_type": "ai_workshop",
        "is_public": False,
        "max_members": 20,
    })
    grupr_id = (body.get("data") or {}).get("grupr_id") or ""
    if not grupr_id:
        raise RuntimeError(f"Quick review: grupr creation returned no id (body={body})")

    # Attach reviewers in order — Synthesizer must be last so it sees the
    # other specialists' messages when its turn fires.
    for r in REVIEWERS:
        agent_id = client.create_agent(
            display_name=r["display_name"],
            provider=r["provider"],
            model_id=r["model_id"],
            system_prompt=r["system_prompt"],
            is_public=False,
        )
        client.add_agent_to_grupr(grupr_id, agent_id)

    # Seed message kicks off the orchestrator.
    seed = f"**Code under review**:\n```python\n{code}\n```"
    client.post_message(grupr_id, seed)

    # Poll for completion. Important: measure the deadline from the moment
    # we START polling — NOT from t0 — because the create-grupr +
    # create-5-agents + attach-5-times setup can consume 30-180s on a busy
    # api (env D in suite 1 hit ~163s setup before polling could start,
    # leaving only ~20s of real poll time within a 180s t0-based budget).
    expected = len(REVIEWERS)
    deadline = time.monotonic() + poll_timeout_s
    agent_msgs: list[dict] = []
    while time.monotonic() < deadline:
        msgs = client.get_messages(grupr_id, limit=50)
        agent_msgs = [m for m in msgs if m.get("agent_id") or m.get("ai_agent_id")]
        if len(agent_msgs) >= expected:
            break
        time.sleep(poll_interval_s)

    # Streaming-settle: when poll exits at `>= expected` the synthesizer
    # row may still be mid-stream (`is_streaming=true`, empty content).
    # Poll a short additional window until every agent message has
    # is_streaming=false. Hard-cap at 30s of additional wait so a stuck
    # stream doesn't block forever.
    if len(agent_msgs) >= expected:
        settle_deadline = time.monotonic() + 30.0
        while time.monotonic() < settle_deadline:
            still_streaming = any(m.get("is_streaming") for m in agent_msgs)
            last_empty = bool(agent_msgs) and not (agent_msgs[-1].get("content") or "").strip()
            if not still_streaming and not last_empty:
                break
            time.sleep(2.0)
            msgs = client.get_messages(grupr_id, limit=50)
            agent_msgs = [m for m in msgs if m.get("agent_id") or m.get("ai_agent_id")]
            agent_msgs.sort(key=lambda m: m.get("created_at", ""))

    # Sort by created_at so verdicts are in dispatch order.
    agent_msgs.sort(key=lambda m: m.get("created_at", ""))

    # Map agent_id -> display_name from the REVIEWERS roster by attach order.
    # The /api/messages endpoint returns sender_name=user's display_name
    # even for agent-authored messages (api-side bug worth filing), so we
    # backfill by dispatch position. Synthesizer is dispatched LAST.
    role_by_index = [r["display_name"] for r in REVIEWERS]
    verdicts = []
    for i, m in enumerate(agent_msgs):
        reviewer_name = role_by_index[i] if i < len(role_by_index) else m.get("sender_name", "?")
        verdicts.append({
            "reviewer": reviewer_name,
            "agent_id": m.get("agent_id") or m.get("ai_agent_id", ""),
            "content": m.get("content", "") or "",
            "created_at": m.get("created_at", ""),
        })

    # Synthesizer = last attached agent = last in dispatch order. Use the
    # last verdict's content rather than name-matching (which is fragile
    # given the sender_name bug above).
    synth = verdicts[-1]["content"] if verdicts else ""
    return {
        "grupr_id": grupr_id,
        "elapsed_s": int(time.monotonic() - t0),
        "reviewer_count_returned": len(agent_msgs),
        "expected_count": expected,
        "verdicts": verdicts,
        "synthesizer_verdict": synth,
        "timed_out": len(agent_msgs) < expected,
    }


def run_deep(
    client: UserClient,
    code: str,
    poll_timeout_s: int = 900,
    poll_interval_s: float = 5.0,
    reviewer_roles: list[str] | None = None,
    auto_approve_patch: bool = True,
) -> dict[str, Any]:
    """Run a Deep-tier code review.

    Flow: POST /api/code-review tier=deep → orchestrator runs reviewers
    + synthesizer → state lands at `awaiting_patch` (user-approval gate)
    → if auto_approve_patch=True, POST .../approve action=generate_patch
    → patcher runs in E2B sandbox → state advances through `patching`
    → `verifying` → `completed`. Fetch patch via GET .../patch.

    For the bot-build test we auto-approve since the bot is both author
    and approver. Real users see the gate as a UI button after the
    Synthesizer verdict is presented.

    Returns:
      {
        "review_id": str,
        "status": str,              # final terminal status (completed|failed|cancelled)
        "elapsed_s": int,
        "timed_out": bool,
        "approved_at_state": str,   # state when auto-approve fired, or "" if not approved
        "synthesizer_verdict": str, # empty on failure
        "patch_diff": str,          # empty if no verified patch produced
        "patch_status": str,        # verified|unverified|none|""
        "verification_report": str, # empty unless patch produced
        "raw_review": dict,         # last polled review row, for debug
        "raw_patch": dict | None,   # /patch response if status=completed
        "error_code": str,          # empty on success
        "error_message": str,       # empty on success
      }
    """
    t0 = time.monotonic()
    review_id = client.create_code_review_deep(code=code, reviewer_roles=reviewer_roles)
    if not review_id:
        raise RuntimeError("Deep review: POST /api/code-review returned no review_id")

    terminal_states = {"completed", "failed", "cancelled"}
    deadline = time.monotonic() + poll_timeout_s
    review: dict[str, Any] = {}
    approved_at_state = ""
    while time.monotonic() < deadline:
        try:
            review = client.get_code_review(review_id)
        except UserClientError as e:
            # Treat transient errors as "still running" but bail on 404.
            if e.status == 404:
                raise
            time.sleep(poll_interval_s)
            continue
        status = (review.get("status") or "").lower()
        if status in terminal_states:
            break
        # User-approval gate: orchestrator parks at `awaiting_patch` after
        # synthesis and waits for an explicit POST /approve. Auto-approve
        # if the caller opted in (bot tests do; real-user flows don't).
        if status == "awaiting_patch" and auto_approve_patch and not approved_at_state:
            try:
                client.approve_code_review(review_id, action="generate_patch",
                                            note="auto-approved by bot-build test")
                approved_at_state = status
            except UserClientError as e:
                # Surface the approval error in the result; treat as
                # terminal-failure-equivalent so we don't poll forever.
                return {
                    "review_id": review_id,
                    "status": status,
                    "elapsed_s": int(time.monotonic() - t0),
                    "timed_out": False,
                    "approved_at_state": "",
                    "synthesizer_verdict": review.get("synthesizer_verdict", "") or "",
                    "patch_diff": "",
                    "patch_status": "",
                    "verification_report": "",
                    "raw_review": review,
                    "raw_patch": None,
                    "error_code": "approve_failed",
                    "error_message": f"approve returned {e.status} {e.code}: {e.message}",
                }
        time.sleep(poll_interval_s)

    final_status = (review.get("status") or "").lower()
    timed_out = final_status not in terminal_states

    patch_diff = ""
    patch_status = ""
    verification_report = ""
    raw_patch: dict[str, Any] | None = None
    error_code = ""
    error_message = ""

    if final_status == "completed":
        try:
            raw_patch = client.get_code_review_patch(review_id)
            patch_diff = raw_patch.get("diff", "") or raw_patch.get("patch", "") or ""
            # /patch response uses a boolean `verified` field (no string
            # patch_status). Map to a string for caller convenience.
            verified = raw_patch.get("verified")
            if verified is True:
                patch_status = "verified"
            elif verified is False:
                patch_status = "unverified"
            else:
                patch_status = raw_patch.get("patch_status", "") or ""
            vr = raw_patch.get("verification_report") or raw_patch.get("verification")
            if isinstance(vr, dict):
                import json as _json
                verification_report = _json.dumps(vr, indent=2)[:8000]
            elif isinstance(vr, str):
                verification_report = vr[:8000]
        except UserClientError as e:
            error_code = e.code
            error_message = f"patch fetch failed: {e.message}"
    else:
        error_code = (review.get("error_code") or "")
        error_message = (review.get("error_message") or "")

    return {
        "review_id": review_id,
        "status": final_status,
        "elapsed_s": int(time.monotonic() - t0),
        "timed_out": timed_out,
        "approved_at_state": approved_at_state,
        "synthesizer_verdict": review.get("synthesizer_verdict", "") or review.get("verdict", ""),
        "patch_diff": patch_diff,
        "patch_status": patch_status,
        "verification_report": verification_report,
        "raw_review": review,
        "raw_patch": raw_patch,
        "error_code": error_code,
        "error_message": error_message,
    }
