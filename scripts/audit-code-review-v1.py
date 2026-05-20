"""End-to-end audit of Code Review v1.

Replicates what the grupr-web BFF (app/api/code-review/create/route.ts)
does — creates an ai_workshop grupr with category=code, attaches the
6-reviewer roster, posts a seed code snippet, then polls until every
reviewer + the synthesizer has weighed in. Reports timing, model
identity, and whether the synthesizer references the other reviewers.

Run with the gtb-newuser seeded account. No LLM in the test framework
itself — this exercises only the api's existing orchestrator path.

Usage:
  source .venv/Scripts/activate
  python scripts/audit-code-review-v1.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any

from dotenv import load_dotenv

from lib.user_client import UserClient, UserClientError


# 6 reviewer roster matches grupr-web/app/code-review/lib/reviewer-prompts.ts
REVIEWERS = [
    {
        "role": "architect", "display_name": "Architect",
        "provider": "anthropic", "model_id": "claude-opus-4-7",
        "system_prompt": (
            "You are the Architecture reviewer in a multi-LLM code review. "
            "Focus on structure, abstractions, coupling, naming. "
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
        "provider": "anthropic", "model_id": "claude-sonnet-4-6",
        "system_prompt": (
            "You are the Performance reviewer in a multi-LLM code review. "
            "Focus on algorithmic complexity, hot paths, N+1, caching, blocking ops. "
            "Respond with: **Verdict**: ship|ship-with-changes|block, then top 3 findings with line refs + big-O."
        ),
    },
    {
        "role": "maintainability", "display_name": "Maintainability",
        "provider": "google", "model_id": "gemini-2.0-flash-exp",
        "system_prompt": (
            "You are the Maintainability reviewer in a multi-LLM code review. "
            "Focus on readability, dead/duplicated code, comments, test coverage gaps. "
            "Respond with: **Verdict**: ship|ship-with-changes|block, then top 3 findings."
        ),
    },
    {
        "role": "synthesizer", "display_name": "Synthesizer",
        "provider": "anthropic", "model_id": "claude-opus-4-7",
        "system_prompt": (
            "You are the Synthesizer in a multi-LLM code review. "
            "Other specialists have posted reviews above. Read them and produce ONE unified verdict. "
            "If they disagree, surface it explicitly. Identify the single most important issue. "
            "Respond with: **Overall verdict**: ship|ship-with-changes|block, then must-fix / should-fix / nice-to-have."
        ),
    },
]

# A real Python snippet with three deliberate bugs (one per reviewer lens).
# Architect: God-class + tight coupling.
# Security: SQL string interpolation (injection).
# Performance: O(n^2) nested loop with no need.
# Maintainability: cryptic var names, no docstrings, dead `_legacy` flag.
TEST_CODE = '''\
import sqlite3

class UserService:
    def __init__(self, db_path):
        self.c = sqlite3.connect(db_path)
        self._legacy = False  # unused, kept "just in case"

    def f(self, e):
        # find user by email then count their orders
        q = "SELECT id FROM users WHERE email = '" + e + "'"
        r = self.c.execute(q).fetchone()
        if not r:
            return 0
        uid = r[0]
        orders = self.c.execute("SELECT id FROM orders").fetchall()
        n = 0
        for o in orders:
            for u in self.c.execute("SELECT user_id FROM orders WHERE id = ?", (o[0],)).fetchall():
                if u[0] == uid:
                    n += 1
        return n
'''


def main() -> int:
    load_dotenv(dotenv_path=".env", override=True)
    api_base = os.environ.get("GRUPR_API_BASE", "https://api.grupr.ai")
    email = os.environ.get("GRUPR_TEST_NEW_USER_EMAIL")
    password = os.environ.get("GRUPR_TEST_PASSWORD")
    if not email or not password:
        print("ERROR: GRUPR_TEST_NEW_USER_EMAIL + GRUPR_TEST_PASSWORD required", file=sys.stderr)
        return 2

    with UserClient(base_url=api_base) as c:
        c.login(email, password)
        print(f"audit: logged in as {c.me()['username']}")

        # 1. Create the code-review grupr
        print("\n[1/4] Creating ai_workshop grupr (category=code)")
        t0 = time.monotonic()
        # We can't pass category via UserClient.create_grupr — it hardcodes
        # category='general'. Hit the api directly so we match the BFF.
        _, body = c._request("POST", "/api/gruprs", json={
            "name": f"Code Review Audit {time.strftime('%Y-%m-%d %H:%M')}",
            "description": "v1 audit — script-driven, replicates BFF flow exactly",
            "category": "code",
            "type": "ai_workshop",
            "is_public": False,
            "max_members": 20,
        })
        grupr_id = body.get("data", {}).get("grupr_id") or body.get("data", {}).get("id")
        print(f"  grupr_id = {grupr_id}")

        # 2. Create + attach reviewers in order. Synthesizer LAST so it sees
        #    other agents' messages when its turn comes.
        print(f"\n[2/4] Creating + attaching {len(REVIEWERS)} reviewers in order")
        agent_ids = []
        for r in REVIEWERS:
            agent_id = c.create_agent(
                display_name=r["display_name"],
                provider=r["provider"],
                model_id=r["model_id"],
                system_prompt=r["system_prompt"],
                is_public=False,
            )
            c.add_agent_to_grupr(grupr_id, agent_id)
            agent_ids.append(agent_id)
            print(f"  + {r['display_name']:18} ({r['provider']}/{r['model_id']}) -> {agent_id[:8]}")

        # 3. Post the seed code message — this triggers the api's orchestrator
        #    to invoke each attached agent in turn.
        print("\n[3/4] Posting seed code message")
        seed = f"**Code under review**:\n```python\n{TEST_CODE}\n```"
        c.post_message(grupr_id, seed)

        # 4. Poll for completion. Reviewers + synthesizer = 6 expected agent
        #    messages on top of the seed. Wait up to 2 minutes.
        print(f"\n[4/4] Polling for agent responses (expecting {len(REVIEWERS)}; timeout 120s)")
        deadline = time.monotonic() + 120
        last_count = 0
        while time.monotonic() < deadline:
            msgs = c.get_messages(grupr_id, limit=50)
            agent_msgs = [m for m in msgs if m.get("agent_id") or m.get("ai_agent_id")]
            if len(agent_msgs) != last_count:
                print(f"  +{len(agent_msgs) - last_count} (running total: {len(agent_msgs)}/{len(REVIEWERS)})")
                last_count = len(agent_msgs)
            if len(agent_msgs) >= len(REVIEWERS):
                break
            time.sleep(4)

        elapsed = int(time.monotonic() - t0)
        print(f"\nWall-clock: {elapsed}s. Final agent msg count: {last_count}/{len(REVIEWERS)}")

        # ── Audit findings
        print("\n" + "=" * 60)
        print("AUDIT FINDINGS")
        print("=" * 60)

        msgs = c.get_messages(grupr_id, limit=50)
        agent_msgs = sorted(
            [m for m in msgs if m.get("agent_id") or m.get("ai_agent_id")],
            key=lambda m: m.get("created_at", ""),
        )

        # A: All 6 reviewers responded?
        if len(agent_msgs) == len(REVIEWERS):
            print(f"A. PASS — all {len(REVIEWERS)} reviewers responded")
        else:
            print(f"A. FAIL — only {len(agent_msgs)}/{len(REVIEWERS)} reviewers responded")
            seen = {m.get("sender_name", "?") for m in agent_msgs}
            expected = {r["display_name"] for r in REVIEWERS}
            print(f"   missing: {sorted(expected - seen)}")

        # B: Order — synthesizer should be LAST
        if agent_msgs:
            last_sender = agent_msgs[-1].get("sender_name", "")
            if "Synthesizer" in last_sender:
                print(f"B. PASS — synthesizer ran last (sender: {last_sender})")
            else:
                print(f"B. FAIL — last responder was {last_sender}, not Synthesizer")

        # C: Did synthesizer's message reference the other reviewers?
        syn_msgs = [m for m in agent_msgs if "Synthesizer" in m.get("sender_name", "")]
        if syn_msgs:
            content = syn_msgs[0].get("content", "")
            references = sum(
                1 for r in REVIEWERS
                if r["role"] != "synthesizer" and r["display_name"].lower() in content.lower()
            )
            if references >= 2:
                print(f"C. PASS — synthesizer references {references}/{len(REVIEWERS)-1} reviewers by name")
            else:
                print(f"C. WEAK — synthesizer references only {references} reviewers by name (may still be implicitly chaining)")
            print(f"   synthesizer content (first 400 chars):\n   {content[:400]}...")

        # D: Did each model identify itself / are we sure GPT-5 vs 4o?
        print(f"\nD. Model identity audit (check sender model_id vs requested):")
        for m in agent_msgs:
            name = m.get("sender_name", "?")
            # message metadata may not include model_id; we asked for specific ones
            content_preview = m.get("content", "")[:80].replace("\n", " ")
            print(f"   {name:20} -> {content_preview!r}")

        # E: Total latency
        print(f"\nE. Wall-clock to complete: {elapsed}s for {len(REVIEWERS)} reviewers")
        print(f"   Average per reviewer: {elapsed / max(len(agent_msgs), 1):.1f}s")

        # F: Bugs caught? Did anyone mention SQL injection / N+1 / dead code?
        print(f"\nF. Bug-detection check (3 seeded bugs):")
        all_content = "\n".join(m.get("content", "") for m in agent_msgs).lower()
        bugs = {
            "SQL injection (security)": "sql injection" in all_content or "string interpolation" in all_content or "injection" in all_content,
            "Quadratic O(n^2) loop (performance)": "n^2" in all_content or "o(n2)" in all_content or "nested loop" in all_content or "quadratic" in all_content,
            "Dead code _legacy (maintainability)": "_legacy" in all_content or "unused" in all_content or "dead" in all_content,
        }
        for b, caught in bugs.items():
            print(f"   {'PASS' if caught else 'MISS'} — {b}")

        print(f"\nGrupr URL: https://app.grupr.ai/g/{grupr_id}")
        print(f"Audit complete. Total api calls: {len(c.call_log)}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
