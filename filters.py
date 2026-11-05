"""Two-stage short-circuit filter engine and natural-language DM criteria parser."""

from __future__ import annotations

import re
from auditor import AuditSession
from config import settings
from integrations import OpenRouterClient, TypeSafeClient
from models import FilterRule, NormalizedFiling
from storage import StateStore


class FilterEngine:
    """
    Evaluates filings in two stages for maximum cost efficiency:
    - Stage 1 (Zero Cost): Deduplication + deterministic XML/structured rules.
    - Stage 2 (Paid QC): TypeSafe.ai `noul` evaluation for semantic rules.
    """

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.typesafe = TypeSafeClient()
        self.openrouter = OpenRouterClient()

    def evaluate_stage1_free(
        self, filing: NormalizedFiling, audit: AuditSession
    ) -> tuple[bool, str]:
        """Runs $0.00 deterministic checks before calling any paid API."""
        with audit.step("Stage 1: Deterministic Filter Rules", provider="local") as step:
            if self.store.is_filing_seen(filing.filing_id):
                reason = f"Already seen filing {filing.filing_id}"
                step.record(status="FAIL", detail=reason)
                return False, reason

            rules = [r for r in self.store.list_rules() if r.stage == "deterministic"]
            for rule in rules:
                val_lower = rule.value.lower().strip()
                if rule.field == "min_round_usd":
                    limit = float(rule.value)
                    if filing.amount_this_round_usd <= limit:
                        reason = (
                            f"Rule #{rule.id} ('{rule.raw_text}'): "
                            f"Round amount ${filing.amount_this_round_usd:,.0f} <= ${limit:,.0f}"
                        )
                        step.record(status="FAIL", detail=reason)
                        return False, reason

                elif rule.field == "max_round_usd":
                    limit = float(rule.value)
                    if filing.amount_this_round_usd > limit:
                        reason = (
                            f"Rule #{rule.id} ('{rule.raw_text}'): "
                            f"Round amount ${filing.amount_this_round_usd:,.0f} > ${limit:,.0f}"
                        )
                        step.record(status="FAIL", detail=reason)
                        return False, reason

                elif rule.field == "min_total_usd":
                    limit = float(rule.value)
                    if filing.total_amount_all_rounds_usd <= limit:
                        reason = (
                            f"Rule #{rule.id} ('{rule.raw_text}'): "
                            f"Total invested ${filing.total_amount_all_rounds_usd:,.0f} <= ${limit:,.0f}"
                        )
                        step.record(status="FAIL", detail=reason)
                        return False, reason

                elif rule.field == "max_total_usd":
                    limit = float(rule.value)
                    if filing.total_amount_all_rounds_usd > limit:
                        reason = (
                            f"Rule #{rule.id} ('{rule.raw_text}'): "
                            f"Total invested ${filing.total_amount_all_rounds_usd:,.0f} > ${limit:,.0f}"
                        )
                        step.record(status="FAIL", detail=reason)
                        return False, reason

                elif rule.field == "pooled_fund":
                    if val_lower == "true" and filing.is_pooled_investment_fund:
                        reason = f"Rule #{rule.id} ('{rule.raw_text}'): Excluded Pooled Investment Fund"
                        step.record(status="FAIL", detail=reason)
                        return False, reason

                elif rule.field == "filing_type":
                    types_joined = " | ".join(t.lower() for t in filing.filing_types)
                    matched = val_lower in types_joined
                    if rule.operator == "exclude_contains" and matched:
                        reason = (
                            f"Rule #{rule.id} ('{rule.raw_text}'): "
                            f"Filing types {filing.filing_types} matched excluded '{rule.value}'"
                        )
                        step.record(status="FAIL", detail=reason)
                        return False, reason
                    if rule.operator == "require_contains" and not matched:
                        reason = (
                            f"Rule #{rule.id} ('{rule.raw_text}'): "
                            f"Filing types {filing.filing_types} missing required '{rule.value}'"
                        )
                        step.record(status="FAIL", detail=reason)
                        return False, reason

                elif rule.field == "industry":
                    ind_lower = filing.industry.lower()
                    matched = val_lower in ind_lower
                    if rule.operator == "exclude_contains" and matched:
                        reason = (
                            f"Rule #{rule.id} ('{rule.raw_text}'): "
                            f"Industry '{filing.industry}' matched excluded '{rule.value}'"
                        )
                        step.record(status="FAIL", detail=reason)
                        return False, reason
                    if rule.operator == "require_contains" and not matched:
                        reason = (
                            f"Rule #{rule.id} ('{rule.raw_text}'): "
                            f"Industry '{filing.industry}' missing required '{rule.value}'"
                        )
                        step.record(status="FAIL", detail=reason)
                        return False, reason

                elif rule.field == "company_name":
                    comp_lower = filing.company_name.lower()
                    if rule.operator == "exclude_contains" and val_lower in comp_lower:
                        reason = f"Rule #{rule.id}: Company name matched excluded '{rule.value}'"
                        step.record(status="FAIL", detail=reason)
                        return False, reason

            step.record(
                status="PASS",
                detail=f"Passed {len(rules)} deterministic rule(s) at $0.00 cost.",
            )
            return True, "Passed Stage 1"

    async def evaluate_stage2_semantic(
        self, filing: NormalizedFiling, audit: AuditSession
    ) -> tuple[bool, str]:
        """Evaluates custom semantic rules in parallel via TypeSafe.ai `noul`."""
        semantic_rules = [
            r for r in self.store.list_rules() if r.stage == "semantic"
        ]
        if not semantic_rules:
            return True, "No semantic rules configured"

        questions = {f"rule_{r.id}": r.value for r in semantic_rules}
        scores = await self.typesafe.evaluate_noul_batch(
            state=filing.model_dump(),
            questions=questions,
            audit=audit,
            step_name="Stage 2: TypeSafe Semantic Rules",
        )
        for r in semantic_rules:
            prob = scores.get(f"rule_{r.id}", 0.0)
            if prob < settings.TYPESAFE_CONFIDENCE_THRESHOLD:
                return (
                    False,
                    f"Failed semantic Rule #{r.id} ('{r.raw_text}'): TypeSafe noul={prob:.2f}",
                )
        return True, "Passed Stage 2 semantic rules"

    async def parse_and_add_user_rule(
        self, raw_text: str, audit: AuditSession
    ) -> FilterRule:
        """
        Parses natural-language criteria from Discord DMs (such as 'exclude filing type: debt')
        into a safe, non-eval FilterRule. Uses regex first ($0.00) and falls back to OpenRouter.
        """
        cleaned = raw_text.strip()

        # Fast $0 deterministic pattern matchers for common DM phrases
        m_type = re.match(
            r"^(?:exclude|no|block)\s+filing\s+type\s*:\s*(.+)$", cleaned, re.I
        )
        if m_type:
            rule = FilterRule(
                raw_text=cleaned,
                stage="deterministic",
                field="filing_type",
                operator="exclude_contains",
                value=m_type.group(1).strip(),
            )
            return self.store.add_rule(rule)

        m_ind = re.match(
            r"^(?:exclude|no|block)\s+industry\s*:\s*(.+)$", cleaned, re.I
        )
        if m_ind:
            rule = FilterRule(
                raw_text=cleaned,
                stage="deterministic",
                field="industry",
                operator="exclude_contains",
                value=m_ind.group(1).strip(),
            )
            return self.store.add_rule(rule)

        m_min = re.match(
            r"^(?:raised\s+more\s+than|min\s+raise|minimum\s+raise)\s*[:$]?\s*([\d,]+(?:\.\d+)?)$",
            cleaned,
            re.I,
        )
        if m_min:
            amt = m_min.group(1).replace(",", "")
            rule = FilterRule(
                raw_text=cleaned,
                stage="deterministic",
                field="min_round_usd",
                operator="gte",
                value=amt,
            )
            return self.store.add_rule(rule)

        m_max = re.match(
            r"^(?:raised\s+less\s+than|max\s+raise|maximum\s+raise)\s*[:$]?\s*([\d,]+(?:\.\d+)?)$",
            cleaned,
            re.I,
        )
        if m_max:
            amt = m_max.group(1).replace(",", "")
            rule = FilterRule(
                raw_text=cleaned,
                stage="deterministic",
                field="max_round_usd",
                operator="lte",
                value=amt,
            )
            return self.store.add_rule(rule)

        # Fallback: Use OpenRouter to convert any free-form criterion into either a deterministic or TypeSafe noul rule
        parsed = await self.openrouter.chat_json(
            system_prompt=(
                "Convert the user's filing filter criterion into strict JSON matching this schema:\n"
                '{"stage": "deterministic" | "semantic", '
                '"field": "filing_type" | "industry" | "company_name" | "min_round_usd" | "max_round_usd" | "min_total_usd" | "max_total_usd" | "pooled_fund" | "typesafe_noul", '
                '"operator": "exclude_contains" | "require_contains" | "gte" | "lte" | "noul_pass", '
                '"value": "string"}\n'
                "If stage is 'semantic', set field='typesafe_noul', operator='noul_pass', and set 'value' "
                "to a clear affirmative statement that MUST be true for a filing to PASS."
            ),
            user_prompt=cleaned,
            audit=audit,
            step_name="Parse New DM Criterion via OpenRouter",
        )
        if parsed and "stage" in parsed and "field" in parsed:
            rule = FilterRule(
                raw_text=cleaned,
                stage=parsed["stage"],
                field=parsed["field"],
                operator=parsed.get("operator", "noul_pass"),
                value=str(parsed.get("value", cleaned)),
            )
        else:
            # Safe fallback to TypeSafe semantic statement
            rule = FilterRule(
                raw_text=cleaned,
                stage="semantic",
                field="typesafe_noul",
                operator="noul_pass",
                value=f"This filing satisfies the user criterion: {cleaned}",
            )
        return self.store.add_rule(rule)
