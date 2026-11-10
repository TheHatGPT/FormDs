"""Live verification of the 3-Hop Handle & GitHub Pivot on Simon Le Marchant (Shader Effects Inc)."""

from __future__ import annotations

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from auditor import AuditSession
from integrations import EnrichmentPipeline
from models import ExecutiveDirector, NormalizedFiling


async def main() -> None:
    pipeline = EnrichmentPipeline()
    filing = NormalizedFiling(
        source="SEC_EDGAR",
        filing_id="0002162989-26-000001",
        company_name="Shader Effects Inc",
        industry="Other Technology",
        company_website="https://shaders.com/",
        website_confidence=0.85,
        amount_this_round_usd=29000.0,
        total_amount_all_rounds_usd=29000.0,
        executives=[
            ExecutiveDirector(
                first_name="Simon",
                last_name="Le Marchant",
                full_name="Simon Le Marchant",
                roles=["Executive Officer", "Director"],
            )
        ],
    )

    audit = AuditSession(
        run_type="executive_deep_dive",
        target_label="Simon Le Marchant (Shader Effects Inc)",
    )
    enriched = await pipeline.deep_dive_executive(filing.executives[0], filing, audit)
    audit.finish("COMPLETED")

    print("=" * 65)
    print(f"EXECUTIVE DEEP-DIVE: {enriched.full_name} ({enriched.company_name})")
    print("=" * 65)
    print(f"1. LinkedIn Profile:    {enriched.linkedin_url or 'None verified'}")
    print(f"2. Social Media Handles: {enriched.social_handles}")
    print(f"3. Blogs:               {enriched.blogs}")
    print(f"4. Personal Websites:   {enriched.personal_websites}")
    print(f"5. Valid Email Address: {enriched.valid_email} — {enriched.email_verification_status}")
    print("\n" + audit.format_discord_report())


if __name__ == "__main__":
    asyncio.run(main())
