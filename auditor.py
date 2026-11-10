"""Step-by-step process and cost auditor for full pipeline transparency."""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from models import AuditRun, AuditStep


class StepRecorder:
    """Mutable handle yielded inside an `AuditSession.step(...)` block."""

    def __init__(self, step_number: int, step_name: str, provider: str) -> None:
        self.step_number = step_number
        self.step_name = step_name
        self.provider = provider
        self.status: str = "INFO"
        self.detail: str = ""
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cost_usd: float = 0.0
        self.duration_ms: int = 0

    def record(
        self,
        *,
        status: str = "INFO",
        detail: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.status = status
        if detail:
            self.detail = detail
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost_usd


class AuditSession:
    """Records all steps, tokens, and USD costs for a single pipeline execution."""

    def __init__(self, run_type: str, target_label: str) -> None:
        self.run = AuditRun(
            run_id=uuid.uuid4().hex[:10],
            run_type=run_type,  # type: ignore[arg-type]
            target_label=target_label,
        )

    @contextmanager
    def step(self, step_name: str, provider: str = "local") -> Iterator[StepRecorder]:
        idx = len(self.run.steps) + 1
        recorder = StepRecorder(idx, step_name, provider)
        t0 = time.perf_counter()
        try:
            yield recorder
        except Exception as exc:
            recorder.status = "ERROR"
            recorder.detail = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            recorder.duration_ms = int((time.perf_counter() - t0) * 1000)
            self.run.steps.append(
                AuditStep(
                    step_number=recorder.step_number,
                    step_name=recorder.step_name,
                    provider=recorder.provider,
                    status=recorder.status,  # type: ignore[arg-type]
                    detail=recorder.detail[:800],
                    input_tokens=recorder.input_tokens,
                    output_tokens=recorder.output_tokens,
                    cost_usd=round(recorder.cost_usd, 7),
                    duration_ms=recorder.duration_ms,
                )
            )

    def finish(self, outcome: str) -> AuditRun:
        self.run.outcome = outcome
        return self.run

    def format_discord_report(self) -> str:
        """Renders a concise, human-readable step & cost breakdown (including full Exa queries per hop) for Discord DMs."""
        lines = [
            f"### 🧾 Audit Report (`{self.run.run_id}`) — **{self.run.target_label}**",
            f"**Outcome:** `{self.run.outcome}` | "
            f"**Total Tokens:** `{self.run.total_tokens:,}` | "
            f"**Total Cost:** `${self.run.total_cost_usd:.6f}`\n",
        ]
        exa_steps = [s for s in self.run.steps if s.provider == "exa"]
        if exa_steps:
            lines.append("**🔎 Exa Queries by Hop:**")
            for es in exa_steps:
                lines.append(f"• **{es.step_name}:** {es.detail}")
            lines.append("")

        for s in self.run.steps:
            icon = {"PASS": "✅", "FAIL": "❌", "INFO": "🔹", "ERROR": "⚠️"}.get(
                s.status, "🔹"
            )
            tok_str = (
                f"{s.input_tokens + s.output_tokens:,} tok"
                if (s.input_tokens + s.output_tokens) > 0
                else "0 tok"
            )
            lines.append(
                f"{icon} **Step {s.step_number}: {s.step_name}** (`{s.provider}`)\n"
                f"   ↳ Cost: `${s.cost_usd:.6f}` | Tokens: `{tok_str}` | Time: `{s.duration_ms}ms`\n"
                f"   ↳ {s.detail or s.status}"
            )
        return "\n".join(lines)
