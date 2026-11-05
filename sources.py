"""Dual filing sources: SEC EDGAR (Form D XML) and SEDAR+ (Form 45-106F1 PDF)."""

from __future__ import annotations

import json
import re
import time
from typing import Any
import pymupdf as fitz  # PyMuPDF
import httpx
from lxml import etree

from auditor import AuditSession
from config import settings
from integrations import OpenRouterClient, TypeSafeClient
from models import ExecutiveDirector, NormalizedFiling
from storage import StateStore


def _clean_xml_bytes(raw: bytes) -> etree._Element:
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    return etree.fromstring(raw, parser=parser)


def _xp_text(node: etree._Element, xpath_expr: str, default: str = "") -> str:
    res = node.xpath(xpath_expr)
    if not res:
        return default
    first = res[0]
    if isinstance(first, etree._Element):
        return (first.text or "").strip()
    return str(first).strip()


class SecEdgarSource:
    """
    Polls SEC EDGAR's Form D Atom feed, parses `primary_doc.xml`,
    and sums historical Form D offerings via SEC's Submissions API.
    """

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.headers = {
            "User-Agent": settings.SEC_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        }

    async def fetch_latest_candidates(
        self, max_unseen: int = 5
    ) -> list[tuple[NormalizedFiling, AuditSession]]:
        results: list[tuple[NormalizedFiling, AuditSession]] = []
        async with httpx.AsyncClient(
            headers=self.headers, timeout=30.0, follow_redirects=True
        ) as client:
            resp = await client.get(settings.SEC_EDGAR_ATOM_URL)
            resp.raise_for_status()
            root = _clean_xml_bytes(resp.content)

            entries = root.xpath("//*[local-name()='entry']")
            for entry in entries:
                # Ensure the Atom entry is actually Form D or D/A (SEC prefix search can include DEF 14A)
                cat_term = _xp_text(entry, "./*[local-name()='category']/@term")
                if cat_term and cat_term not in ("D", "D/A"):
                    continue

                link_href = _xp_text(entry, "./*[local-name()='link']/@href")
                entry_id = _xp_text(entry, "./*[local-name()='id']")
                # Extract accession number (e.g., 0001234567-26-000001) and CIK
                acc_match = re.search(r"(\d{10}-\d{2}-\d{6})", link_href or entry_id)
                cik_match = re.search(r"/data/(\d+)/", link_href)
                if not acc_match or not cik_match:
                    continue

                accession = acc_match.group(1)
                cik = cik_match.group(1)
                if self.store.is_filing_seen(accession):
                    continue

                audit = AuditSession(
                    run_type="filing_eval",
                    target_label=f"SEC EDGAR {accession}",
                )
                filing = await self._parse_single_edgar_filing(
                    client, cik=cik, accession=accession, index_url=link_href, audit=audit
                )
                if filing:
                    audit.run.target_label = f"{filing.company_name} ({accession})"
                    results.append((filing, audit))
                    if len(results) >= max_unseen:
                        break
        return results

    async def _parse_single_edgar_filing(
        self,
        client: httpx.AsyncClient,
        *,
        cik: str,
        accession: str,
        index_url: str,
        audit: AuditSession,
    ) -> NormalizedFiling | None:
        acc_clean = accession.replace("-", "")
        xml_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/primary_doc.xml"

        with audit.step("Fetch SEC EDGAR Form D XML", provider="sec_edgar") as step:
            resp = await client.get(xml_url)
            if resp.status_code != 200:
                # Fallback: inspect the -index.htm page to locate the raw XML document
                idx_resp = await client.get(index_url)
                if idx_resp.status_code != 200:
                    step.record(
                        status="ERROR",
                        detail=f"Could not fetch {xml_url} (HTTP {resp.status_code})",
                    )
                    return None
                m_xml = re.search(
                    r'href="(/Archives/edgar/data/[^"]+\.xml)"', idx_resp.text
                )
                if not m_xml:
                    step.record(status="ERROR", detail="No XML link found in index page")
                    return None
                xml_url = f"https://www.sec.gov{m_xml.group(1)}"
                resp = await client.get(xml_url)

            root = _clean_xml_bytes(resp.content)
            company_name = (
                _xp_text(
                    root,
                    "//*[local-name()='primaryIssuer']/*[local-name()='entityName']",
                )
                or "Unknown Issuer"
            )
            industry = (
                _xp_text(
                    root,
                    "//*[local-name()='industryGroup']/*[local-name()='industryGroupType']",
                )
                or "Unknown"
            )
            pooled_flag = _xp_text(
                root,
                "//*[local-name()='typesOfSecuritiesOffered']/*[local-name()='isPooledInvestmentFundType']",
            ).lower()
            is_pooled = (
                pooled_flag == "true" or "pooled investment" in industry.lower()
            )

            # Security / Filing types
            sec_type_map = {
                "isEquityType": "Equity",
                "isDebtType": "Debt",
                "isOptionToAcquireType": "Option/Warrant",
                "isSecurityToBeAcquiredType": "Convertible/SAFE",
                "isPooledInvestmentFundType": "Pooled Investment Fund",
                "isTenantInCommonType": "Tenant-in-Common",
                "isMineralPropertyType": "Mineral Property",
                "isOtherType": "Other",
            }
            filing_types: list[str] = []
            for xml_tag, label in sec_type_map.items():
                val = _xp_text(
                    root,
                    f"//*[local-name()='typesOfSecuritiesOffered']/*[local-name()='{xml_tag}']",
                ).lower()
                if val in ("true", "1"):
                    filing_types.append(label)

            # Amount invested on this round
            sold_str = _xp_text(
                root,
                "//*[local-name()='offeringSalesAmounts']/*[local-name()='totalAmountSold']",
                "0",
            )
            try:
                amount_this_round = float(re.sub(r"[^\d.]", "", sold_str) or "0")
            except ValueError:
                amount_this_round = 0.0

            # Executive Directors (anyone who is an Executive Officer OR a Director)
            executives: list[ExecutiveDirector] = []
            person_nodes = root.xpath(
                "//*[local-name()='relatedPersonsList']/*[local-name()='relatedPersonInfo']"
            )
            for p_node in person_nodes:
                rels = [
                    (r.text or "").strip()
                    for r in p_node.xpath(".//*[local-name()='relationship']")
                    if (r.text or "").strip()
                ]
                matching_roles = [
                    r
                    for r in rels
                    if r.lower() in ("executive officer", "director")
                ]
                if not matching_roles:
                    continue

                first_n = _xp_text(
                    p_node, ".//*[local-name()='relatedPersonName']/*[local-name()='firstName']"
                )
                middle_n = _xp_text(
                    p_node, ".//*[local-name()='relatedPersonName']/*[local-name()='middleName']"
                )
                last_n = _xp_text(
                    p_node, ".//*[local-name()='relatedPersonName']/*[local-name()='lastName']"
                )
                full_name = " ".join(
                    part for part in (first_n, middle_n, last_n) if part
                ).strip()
                if full_name:
                    executives.append(
                        ExecutiveDirector(
                            first_name=first_n,
                            last_name=last_n,
                            full_name=full_name,
                            roles=matching_roles,
                            title="/".join(matching_roles),
                        )
                    )

            step.record(
                status="PASS",
                detail=(
                    f"Parsed {company_name}: ${amount_this_round:,.0f} this round, "
                    f"{len(executives)} exec/director(s), types={filing_types}"
                ),
            )

        # Total amount invested across all historical Form D rounds (Option 2A)
        total_all_rounds = await self._compute_historical_edgar_total(
            client,
            cik=cik,
            current_accession=accession,
            current_round_usd=amount_this_round,
            audit=audit,
        )

        return NormalizedFiling(
            source="SEC_EDGAR",
            filing_id=accession,
            cik_or_profile=cik,
            company_name=company_name,
            industry=industry,
            is_pooled_investment_fund=is_pooled,
            filing_types=filing_types or ["Unspecified"],
            amount_this_round_usd=amount_this_round,
            total_amount_all_rounds_usd=total_all_rounds,
            executives=executives,
            filing_url=index_url or xml_url,
        )

    async def _compute_historical_edgar_total(
        self,
        client: httpx.AsyncClient,
        *,
        cik: str,
        current_accession: str,
        current_round_usd: float,
        audit: AuditSession,
    ) -> float:
        """Queries SEC's free CIK Submissions API to sum prior Form D offerings."""
        with audit.step(
            "Sum Historical Form D Rounds (SEC Submissions API)", provider="sec_edgar"
        ) as step:
            sub_url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
            try:
                resp = await client.get(sub_url)
                if resp.status_code != 200:
                    step.record(
                        status="INFO",
                        detail="No historical submissions JSON found; using current round amount.",
                    )
                    return current_round_usd

                recent = resp.json().get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                accessions = recent.get("accessionNumber", [])
                prior_d_accessions = [
                    acc
                    for form, acc in zip(forms, accessions)
                    if form == "D" and acc != current_accession
                ][:5]  # Cap to 5 prior distinct Form D originations for fast polite polling

                historical_sum = current_round_usd
                for acc in prior_d_accessions:
                    acc_clean = acc.replace("-", "")
                    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/primary_doc.xml"
                    r = await client.get(url)
                    if r.status_code == 200:
                        root = _clean_xml_bytes(r.content)
                        sold = _xp_text(
                            root,
                            "//*[local-name()='offeringSalesAmounts']/*[local-name()='totalAmountSold']",
                            "0",
                        )
                        val = float(re.sub(r"[^\d.]", "", sold) or "0")
                        historical_sum += val

                step.record(
                    status="PASS",
                    detail=(
                        f"Found {len(prior_d_accessions)} prior Form D filing(s); "
                        f"cumulative total = ${historical_sum:,.0f}"
                    ),
                )
                return historical_sum
            except Exception as exc:
                step.record(
                    status="INFO",
                    detail=f"Historical lookup fallback ({exc}); using current round.",
                )
                return current_round_usd


class SedarPlusSource:
    """
    Scrapes SEDAR+ (`sedarplus.ca`) for Canadian Form 45-106F1 (Report of Exempt Distribution) PDFs
    using Playwright + PyMuPDF + OpenRouter (`google/gemini-2.5-flash`) + TypeSafe AI QC.
    Includes an independent WAF-safe cooldown so 1-minute debug loops never trigger IP blocks.
    """

    SEARCH_URL = (
        "https://www.sedarplus.ca/csa-party/service/create.html"
        "?targetAppCode=csa-party&service=searchDocuments&_locale=en"
    )

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.openrouter = OpenRouterClient()
        self.typesafe = TypeSafeClient()
        self._last_polled_ts: float = 0.0

    def is_cooldown_active(self, force: bool = False) -> bool:
        if force:
            return False
        elapsed_min = (time.time() - self._last_polled_ts) / 60.0
        return elapsed_min < settings.SEDAR_MIN_COOLDOWN_MINUTES

    async def fetch_latest_candidates(
        self, max_unseen: int = 2, force: bool = False
    ) -> list[tuple[NormalizedFiling, AuditSession]]:
        if not settings.SEDAR_ENABLED or self.is_cooldown_active(force=force):
            return []

        self._last_polled_ts = time.time()
        results: list[tuple[NormalizedFiling, AuditSession]] = []

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()
            try:
                await page.goto(self.SEARCH_URL, wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(2500)

                # Attempt to filter by 'Report of exempt distribution' if search input is available
                search_inputs = await page.locator("input[type='text']").all()
                for inp in search_inputs:
                    placeholder = (await inp.get_attribute("placeholder") or "").lower()
                    if "document" in placeholder or "type" in placeholder or "category" in placeholder:
                        await inp.fill("Report of exempt distribution")
                        break

                submit_btn = page.locator("button:has-text('Search')").first
                if await submit_btn.count() > 0:
                    await submit_btn.click()
                    await page.wait_for_timeout(3000)

                rows = await page.locator("table tbody tr").all()
                for row in rows[:15]:
                    row_text = (await row.inner_text()).strip()
                    if not row_text or "exempt distribution" not in row_text.lower():
                        continue

                    doc_id = "SEDAR-" + re.sub(r"\W+", "", row_text[:45])
                    if self.store.is_filing_seen(doc_id):
                        continue

                    audit = AuditSession(
                        run_type="filing_eval",
                        target_label=f"SEDAR+ {doc_id}",
                    )
                    pdf_text = row_text

                    # Attempt PDF download if a document link exists in the row
                    doc_link = row.locator("a").first
                    if await doc_link.count() > 0:
                        try:
                            async with page.expect_download(timeout=12000) as dl_info:
                                await doc_link.click()
                            dl = await dl_info.value
                            pdf_path = await dl.path()
                            if pdf_path:
                                with fitz.open(pdf_path) as doc:
                                    extracted = "\n".join(
                                        pg.get_text("text") for pg in doc[:12]
                                    )
                                    # Strip control characters to prevent PDF prompt injection
                                    pdf_text = re.sub(
                                        r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", extracted
                                    )[:15000]
                        except Exception:
                            pass

                    filing = await self._extract_sedar_filing(
                        doc_id=doc_id, raw_text=pdf_text, audit=audit
                    )
                    if filing:
                        audit.run.target_label = f"{filing.company_name} ({doc_id})"
                        results.append((filing, audit))
                        if len(results) >= max_unseen:
                            break
            except Exception:
                pass
            finally:
                await browser.close()

        return results

    async def _extract_sedar_filing(
        self, *, doc_id: str, raw_text: str, audit: AuditSession
    ) -> NormalizedFiling | None:
        parsed: dict[str, Any] = await self.openrouter.chat_json(
            system_prompt=(
                "Extract structured Canadian Form 45-106F1 (Report of Exempt Distribution) metadata "
                "from the provided filing text. Return strict JSON:\n"
                "{\n"
                '  "company_name": str,\n'
                '  "industry": str,\n'
                '  "is_pooled_investment_fund": bool,\n'
                '  "filing_types": [str],\n'
                '  "amount_this_round_cad": float,\n'
                '  "total_amount_all_rounds_cad": float,\n'
                '  "company_website": str | null,\n'
                '  "executives": [{"first_name": str, "last_name": str, "full_name": str, "roles": [str]}]\n'
                "}\n"
                "Include anyone who is an Executive Officer or a Director in `executives`."
            ),
            user_prompt=raw_text,
            audit=audit,
            step_name="Extract SEDAR+ Form 45-106F1 via OpenRouter",
        )
        if not parsed or not parsed.get("company_name"):
            return None

        # Verify extraction quality via TypeSafe AI
        scores = await self.typesafe.evaluate_noul_batch(
            state={"raw_snippet": raw_text[:3000], "extracted": parsed},
            questions={
                "extraction_valid": (
                    "The extracted JSON accurately reflects the issuer name, offering amount, "
                    "and executive officers/directors listed in the Form 45-106F1 text."
                )
            },
            audit=audit,
            step_name="TypeSafe QC: Verify SEDAR+ Extraction",
        )
        if scores.get("extraction_valid", 0.0) < settings.TYPESAFE_CONFIDENCE_THRESHOLD:
            return None

        round_cad = float(parsed.get("amount_this_round_cad") or 0.0)
        total_cad = float(parsed.get("total_amount_all_rounds_cad") or round_cad)
        execs = [
            ExecutiveDirector(
                first_name=e.get("first_name", ""),
                last_name=e.get("last_name", ""),
                full_name=e.get("full_name", "Unknown"),
                roles=e.get("roles", ["Executive Officer/Director"]),
            )
            for e in parsed.get("executives", [])
            if isinstance(e, dict) and e.get("full_name")
        ]

        return NormalizedFiling(
            source="SEDAR_PLUS",
            filing_id=doc_id,
            company_name=str(parsed["company_name"]),
            industry=str(parsed.get("industry") or "Unknown"),
            is_pooled_investment_fund=bool(parsed.get("is_pooled_investment_fund", False)),
            filing_types=[str(t) for t in parsed.get("filing_types", ["Equity"])],
            amount_this_round_usd=round(round_cad * settings.CAD_TO_USD_RATE, 2),
            total_amount_all_rounds_usd=round(total_cad * settings.CAD_TO_USD_RATE, 2),
            company_website=parsed.get("company_website"),
            executives=execs,
            filing_url=self.SEARCH_URL,
        )
