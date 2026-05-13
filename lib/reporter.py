"""Structured markdown reporter for persona runs.

Each persona run accumulates findings here and finalizes to a single
.md file. The markdown is designed to be human-skimmable for triage
AND grep-friendly for cross-persona summary roll-ups (see
`scripts/run-all.sh` which catenates these into a top-level summary).

Severity convention (matches CLAUDE-internal triage labels):

  P0 — blocker, launch-blocking
  P1 — high, ship-with-mitigation
  P2 — medium, fix in next sprint
  P3 — low, polish
  obs — observation, neither bug nor pass
  pass — positive confirmation a flow works

Personas don't read these back — they only write. The runner persists
on shutdown, including on hard failure (so we still get the partial
findings on a crash).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


Severity = Literal["P0", "P1", "P2", "P3", "obs", "pass"]


@dataclass
class Finding:
    severity: Severity
    title: str
    detail: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


@dataclass
class Reporter:
    persona: str
    run_id: str
    out_dir: Path
    started_at: float = field(default_factory=time.time)
    findings: list[Finding] = field(default_factory=list)
    network_calls: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    turns: int = 0

    @classmethod
    def for_run(
        cls, persona: str, runs_root: Path | str = "runs"
    ) -> "Reporter":
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{persona}-{ts}"
        out_dir = Path(runs_root) / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        return cls(persona=persona, run_id=run_id, out_dir=out_dir)

    def add(self, severity: Severity, title: str, detail: str = "", **context: Any) -> None:
        self.findings.append(Finding(severity=severity, title=title, detail=detail, context=context))

    # Convenience aliases — make the persona code self-documenting.
    def p0(self, title: str, detail: str = "", **context: Any) -> None: self.add("P0", title, detail, **context)
    def p1(self, title: str, detail: str = "", **context: Any) -> None: self.add("P1", title, detail, **context)
    def p2(self, title: str, detail: str = "", **context: Any) -> None: self.add("P2", title, detail, **context)
    def p3(self, title: str, detail: str = "", **context: Any) -> None: self.add("P3", title, detail, **context)
    def obs(self, title: str, detail: str = "", **context: Any) -> None: self.add("obs", title, detail, **context)
    def pass_(self, title: str, detail: str = "", **context: Any) -> None: self.add("pass", title, detail, **context)

    def record_network(self, method: str, path: str, status: int, latency_ms: int, error: str | None = None) -> None:
        self.network_calls.append(
            {"method": method, "path": path, "status": status, "latency_ms": latency_ms, "error": error}
        )

    def record_usage(self, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost_usd
        self.turns += 1

    # ── output ─────────────────────────────────────────────────────

    def finalize(self) -> Path:
        """Write summary.md + raw findings.json + network.json. Idempotent."""
        duration_s = int(time.time() - self.started_at)
        summary_md = self._render_markdown(duration_s)

        (self.out_dir / "summary.md").write_text(summary_md, encoding="utf-8")
        (self.out_dir / "findings.json").write_text(
            json.dumps([asdict(f) for f in self.findings], indent=2), encoding="utf-8"
        )
        (self.out_dir / "network.json").write_text(
            json.dumps(self.network_calls, indent=2), encoding="utf-8"
        )
        return self.out_dir / "summary.md"

    def _render_markdown(self, duration_s: int) -> str:
        bucket: dict[Severity, list[Finding]] = {"P0": [], "P1": [], "P2": [], "P3": [], "obs": [], "pass": []}
        for f in self.findings:
            bucket[f.severity].append(f)

        out: list[str] = []
        out.append(f"# {self.persona} — persona run\n")
        out.append(f"**Run ID**: `{self.run_id}`")
        out.append(f"**Duration**: {duration_s}s")
        out.append(f"**Turns**: {self.turns}")
        out.append(f"**LLM cost**: ${self.cost_usd:.3f} ({self.tokens_in} in / {self.tokens_out} out)")
        out.append(f"**Network calls**: {len(self.network_calls)}")
        out.append("")

        # Top-of-doc counts so a reader can triage at a glance.
        counts = " · ".join(
            f"**{label}**: {len(bucket[label])}"
            for label in ("P0", "P1", "P2", "P3", "obs", "pass")
            if bucket[label]
        ) or "**no findings**"
        out.append(counts)
        out.append("")

        for label, heading in [
            ("P0", "🚨 P0 — blockers"),
            ("P1", "🔴 P1 — high"),
            ("P2", "🟠 P2 — medium"),
            ("P3", "🟡 P3 — low / polish"),
            ("obs", "📝 Observations"),
            ("pass", "✅ Passing flows"),
        ]:
            items = bucket[label]
            if not items:
                continue
            out.append(f"## {heading}\n")
            for f in items:
                out.append(f"### {f.title}\n")
                if f.detail:
                    out.append(f.detail.strip())
                    out.append("")
                if f.context:
                    out.append("```json")
                    out.append(json.dumps(f.context, indent=2, default=str))
                    out.append("```")
                out.append("")

        # Network journal — folded as a details block. Useful for forensics
        # but not the headline; the findings above are.
        if self.network_calls:
            out.append("<details>")
            out.append("<summary>Network journal (every api call)</summary>\n")
            out.append("| # | method | path | status | latency (ms) | error |")
            out.append("|---|---|---|---|---|---|")
            for i, c in enumerate(self.network_calls, 1):
                err = (c.get("error") or "").replace("|", "/")[:60]
                out.append(f"| {i} | {c['method']} | `{c['path']}` | {c['status']} | {c['latency_ms']} | {err} |")
            out.append("\n</details>")

        return "\n".join(out)
