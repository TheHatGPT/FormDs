"""Live verification test suite for OpenRouter, Exa, TypeSafe, and Apify."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from auditor import AuditSession
from config import settings
from integrations import (
    ApifyContactEnricher,
    EnrichmentPipeline,
    ExaClient,
    OpenRouterClient,
    TypeSafeClient,
)
from models import ExecutiveDirector, NormalizedFiling


async def test_all_integrations() -> None:
    print("=" * 65)
    print("PHASE 2: EXTERNAL API CONNECTIVITY & COST AUDIT VERIFICATION")
    print("=" * 65)

    # Check key status
    key_statuses = {
        "OPENROUTER_API_KEY": bool(settings.OPENROUTER_API_KEY),
        "EXA_API_KEY": bool(settings.EXA_API_KEY),
        "TYPESAFE_API_KEY": bool(settings.TYPESAFE_API_KEY),
        "APIFY_API_TOKEN": bool(settings.APIFY_API_TOKEN),
        "DISCORD_BOT_TOKEN": bool(settings.DISCORD_BOT_TOKEN),
        "OWNER_DISCORD_ID": bool(settings.OWNER_DISCORD_ID != 0),
    }

    print("\n[Credential Status in .env]")
    for k, present in key_statuses.items():
        status_icon = "🟢 Configured" if present else "⚪ Missing / Empty"
        print(f"  • {k:22}: {status_icon}")

    audit = AuditSession(
        run_type="filing_eval",
        target_label="Integration Smoke Test (Psympl Inc. / Randall Mullins)",
    )

    pipeline = EnrichmentPipeline()

    sample_filing = NormalizedFiling(
        source="SEC_EDGAR",
        filing_id="0002055784-26-000005",
        cik_or_profile="0002055784",
        company_name="Psympl Inc.",
        industry="Other Technology",
        is_pooled_investment_fund=False,
        filing_types=["Option/Warrant", "Other"],
        amount_this_round_usd=250000.0,
        total_amount_all_rounds_usd=350000.0,
        executives=[
            ExecutiveDirector(
                first_name="Randall",
                last_name="Mullins",
                full_name="Randall Mullins",
                roles=["Executive Officer", "Director"],
            )
        ],
    )

    # 1. Test OpenRouter
    print("\n--- 1. Testing OpenRouter (Model: {}) ---".format(settings.OPENROUTER_MODEL))
    if settings.OPENROUTER_API_KEY:
        try:
            res = await pipeline.openrouter.chat_json(
                system_prompt="Return strict JSON: {\"status\": \"ok\", \"service\": \"OpenRouter\"}",
                user_prompt="Say hello.",
                audit=audit,
                step_name="OpenRouter Ping",
            )
            print(f"  ✅ OpenRouter Response: {res}")
        except Exception as exc:
            print(f"  ❌ OpenRouter Failed: {exc}")
    else:
        print("  ⚪ Skipped: OPENROUTER_API_KEY is not configured in .env.")

    # 2. Test Exa.ai
    print("\n--- 2. Testing Exa.ai Neural Search ---")
    if settings.EXA_API_KEY:
        try:
            results = await pipeline.exa.search(
                "Psympl Inc. official company website",
                num_results=2,
                audit=audit,
                step_name="Exa Website Search Ping",
            )
            print(f"  ✅ Exa Search Returned {len(results)} results:")
            for r in results:
                print(f"     ↳ {r.get('title')} ({r.get('url')})")
        except Exception as exc:
            print(f"  ❌ Exa Search Failed: {exc}")
    else:
        print("  ⚪ Skipped: EXA_API_KEY is not configured in .env.")

    # 3. Test TypeSafe.ai
    print("\n--- 3. Testing TypeSafe.ai System One (/v1/systemone, Model: {}) ---".format(settings.TYPESAFE_MODEL))
    if settings.TYPESAFE_API_KEY:
        try:
            scores = await pipeline.typesafe.evaluate_noul_batch(
                state={"company": "Psympl Inc.", "industry": "Technology"},
                questions={
                    "is_tech": "The company Psympl Inc. operates in the technology industry."
                },
                audit=audit,
                step_name="TypeSafe QC Ping",
            )
            print(f"  ✅ TypeSafe noul Score: {scores}")
        except Exception as exc:
            print(f"  ❌ TypeSafe Failed: {exc}")
    else:
        print("  ⚪ Skipped: TYPESAFE_API_KEY is not configured in .env.")

    # 4. Test Apify
    print("\n--- 4. Testing Apify Contact Enrichment ---")
    if settings.APIFY_API_TOKEN:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as http_client:
                r = await http_client.get(
                    f"https://api.apify.com/v2/users/me?token={settings.APIFY_API_TOKEN}"
                )
                r.raise_for_status()
                username = r.json().get("data", {}).get("username", "Authenticated User")
            print(f"  ✅ Apify Token Verified! Connected as: {username}")
        except Exception as exc:
            print(f"  ❌ Apify Verification Failed: {exc}")
    else:
        print("  ⚪ Skipped: APIFY_API_TOKEN is not configured in .env.")

    # 5. Full Pipeline Simulation (Website + Executive Deep-Dive)
    print("\n--- 5. Simulating Enrichment Pipeline (Clean Brand Prompts) ---")
    await pipeline.find_verified_company_website(sample_filing, audit)
    print(f"  • Company Website: {sample_filing.company_website or 'None found / QC pending'}")
    if sample_filing.website_confidence is not None:
        print(f"  • Website TypeSafe QC Confidence: {sample_filing.website_confidence:.1%}")

    print("\n--- 6. Simulating Executive Deep-Dive (Clean Brand Prompts) ---")
    exec_res = await pipeline.deep_dive_executive(
        sample_filing.executives[0], sample_filing, audit
    )
    print(f"  • Executive: {exec_res.full_name}")
    print(f"  • LinkedIn: {exec_res.linkedin_url or 'None found'} (QC: {exec_res.linkedin_confidence})")
    print(f"  • Socials: {exec_res.social_handles or 'None'}")
    print(f"  • Blogs: {exec_res.blogs or 'None'}")
    print(f"  • Personal Sites: {exec_res.personal_websites or 'None'}")
    print(f"  • Email: {exec_res.valid_email or 'None'} ({exec_res.email_verification_status})")

    run = audit.finish("PHASE_2_TEST_COMPLETE")

    print("\n" + "=" * 65)
    print("STEP & COST AUDIT REPORT")
    print("=" * 65)
    print(audit.format_discord_report())


if __name__ == "__main__":
    asyncio.run(test_all_integrations())
