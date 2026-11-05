"""Pydantic data models for normalized filings, executives, filter rules, and audits."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field


class ExecutiveDirector(BaseModel):
    """Represents an individual listed as an Executive Officer or Director."""

    first_name: str = ""
    last_name: str = ""
    full_name: str
    roles: list[str] = Field(default_factory=list)  # e.g., ["Executive Officer", "Director"]
    title: str = ""


class NormalizedFiling(BaseModel):
    """Unified filing structure across SEC EDGAR (Form D) and SEDAR+ (Form 45-106F1)."""

    source: Literal["SEC_EDGAR", "SEDAR_PLUS"]
    filing_id: str  # Accession number (EDGAR) or Document ID (SEDAR+)
    cik_or_profile: str = ""
    company_name: str
    industry: str = "Unknown"
    is_pooled_investment_fund: bool = False
    filing_types: list[str] = Field(default_factory=list)  # e.g., ["Equity", "Debt"]
    amount_this_round_usd: float = 0.0
    total_amount_all_rounds_usd: float = 0.0
    executives: list[ExecutiveDirector] = Field(default_factory=list)
    company_website: str | None = None
    website_confidence: float | None = None
    filing_url: str = ""
    filed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def num_executive_directors(self) -> int:
        return len(self.executives)

    @property
    def executive_names_formatted(self) -> str:
        if not self.executives:
            return "None listed"
        return ", ".join(
            f"{e.full_name} ({'/'.join(e.roles)})" if e.roles else e.full_name
            for e in self.executives
        )


class EnrichedExecutive(BaseModel):
    """Deep-dive contact and social enrichment output for a chosen Executive Director."""

    full_name: str
    company_name: str
    linkedin_url: str | None = None
    linkedin_confidence: float | None = None
    social_handles: list[str] = Field(default_factory=list)
    blogs: list[str] = Field(default_factory=list)
    personal_websites: list[str] = Field(default_factory=list)
    valid_email: str | None = None
    email_verification_status: str = "Not found"
    enrichment_provider: str = "apify"


class FilterRule(BaseModel):
    """Dynamic discriminating criterion added by the user via Discord DM."""

    id: int | None = None
    raw_text: str
    stage: Literal["deterministic", "semantic"]
    field: Literal[
        "filing_type",
        "industry",
        "company_name",
        "min_round_usd",
        "max_round_usd",
        "min_total_usd",
        "max_total_usd",
        "pooled_fund",
        "typesafe_noul",
    ]
    operator: Literal["exclude_contains", "require_contains", "gte", "lte", "noul_pass"]
    value: str
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class AuditStep(BaseModel):
    """A single auditable step in a filing evaluation or executive deep-dive."""

    step_number: int
    step_name: str
    provider: str  # "local", "sec_edgar", "sedar_plus", "exa", "typesafe", "openrouter", "apify", "apollo"
    status: Literal["PASS", "FAIL", "INFO", "ERROR"] = "INFO"
    detail: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


class AuditRun(BaseModel):
    """Complete audit log for one filing pipeline execution or deep-dive request."""

    run_id: str
    run_type: Literal["filing_eval", "executive_deep_dive", "rule_parse"]
    target_label: str
    outcome: str = "IN_PROGRESS"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    steps: list[AuditStep] = Field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.steps)

    @property
    def total_tokens(self) -> int:
        return sum(s.input_tokens + s.output_tokens for s in self.steps)
