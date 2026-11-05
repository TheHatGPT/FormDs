"""Offline verification suite for SEC EDGAR XML parsing, dynamic rules, and cost auditing."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from auditor import AuditSession
from filters import FilterEngine
from models import ExecutiveDirector, NormalizedFiling
from sources import SecEdgarSource
from storage import StateStore


async def run_smoke_tests() -> None:
    test_db = Path(__file__).parent / "test_state.db"
    if test_db.exists():
        test_db.unlink()

    store = StateStore(db_path=test_db)
    engine = FilterEngine(store)

    filing = NormalizedFiling(
        source="SEC_EDGAR",
        filing_id="0001999999-26-000001",
        cik_or_profile="0001999999",
        company_name="Acme Quantum Widgets Inc.",
        industry="Technology",
        is_pooled_investment_fund=False,
        filing_types=["Equity"],
        amount_this_round_usd=150000.0,
        total_amount_all_rounds_usd=450000.0,
        executives=[
            ExecutiveDirector(
                first_name="Ada",
                last_name="Lovelace",
                full_name="Ada Lovelace",
                roles=["Executive Officer", "Director"],
            ),
            ExecutiveDirector(
                first_name="Alan",
                last_name="Turing",
                full_name="Alan Turing",
                roles=["Director"],
            ),
        ],
    )

    audit = AuditSession("filing_eval", filing.company_name)
    passed, reason = engine.evaluate_stage1_free(filing, audit)
    assert passed, f"Expected filing to pass baseline rules, got: {reason}"
    assert filing.num_executive_directors == 2

    # Test adding "exclude filing type: debt" via DM parser
    rule_audit = AuditSession("rule_parse", "exclude filing type: debt")
    rule = await engine.parse_and_add_user_rule("exclude filing type: debt", rule_audit)
    assert rule.field == "filing_type" and rule.value.lower() == "debt"

    debt_filing = filing.model_copy(
        update={"filing_id": "0001999999-26-000002", "filing_types": ["Debt"]}
    )
    audit2 = AuditSession("filing_eval", "Debt Filing Check")
    passed2, reason2 = engine.evaluate_stage1_free(debt_filing, audit2)
    assert not passed2, "Expected Debt filing to be filtered out by new DM rule"

    print(audit.format_discord_report())
    print("\n--- Fetching Live SEC EDGAR Form D Filing (Dry Run) ---")
    edgar = SecEdgarSource(store)
    live_candidates = await edgar.fetch_latest_candidates(max_unseen=5)
    for live_filing, live_audit in live_candidates:
        ok, why = engine.evaluate_stage1_free(live_filing, live_audit)
        live_audit.finish("PASSED_STAGE1" if ok else f"FILTERED ({why})")
        print(f"\n[Live Filing ID: {live_filing.filing_id}]")
        print(f"1. Number of Executive Directors: {live_filing.num_executive_directors}")
        print(f"2. Name of Executive Director(s): {live_filing.executive_names_formatted}")
        print(f"3. Name of Company:               {live_filing.company_name}")
        print(f"4. Industry:                      {live_filing.industry}")
        print(f"5. Total Invested (All Rounds):   ${live_filing.total_amount_all_rounds_usd:,.2f} USD")
        print(f"6. Amount Invested (This Round):  ${live_filing.amount_this_round_usd:,.2f} USD")
        print(f"7. Filing Types / Stage 1 Status: {live_filing.filing_types} -> {live_audit.run.outcome}")
        print(live_audit.format_discord_report())

    print("\nAll smoke & live SEC EDGAR tests passed!")
    if test_db.exists():
        test_db.unlink()


if __name__ == "__main__":
    asyncio.run(run_smoke_tests())
