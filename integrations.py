"""External API integrations: OpenRouter, Exa.ai, TypeSafe.ai, Apify, and Apollo.io."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol
import httpx
from apify_client import ApifyClientAsync

from auditor import AuditSession
from config import settings
from models import EnrichedExecutive, ExecutiveDirector, NormalizedFiling


def _safe_get(obj: Any, *keys: str) -> Any:
    """Safely retrieves a value whether obj is a dict or an object attribute."""
    if obj is None:
        return None
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            return obj[k]
        if hasattr(obj, k):
            return getattr(obj, k)
        # Check snake_case fallback
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower()
        if isinstance(obj, dict) and snake in obj:
            return obj[snake]
        if hasattr(obj, snake):
            return getattr(obj, snake)
    return None


def clean_brand_name(name: str) -> str:
    """Strips legal entity suffixes (Inc, LLC, Corp, etc.) to get the core brand name for clean search queries."""
    pattern = r"(?i)\b(?:inc(?:orporated)?|corp(?:oration)?|llc|l\.l\.c\.|lp|l\.p\.|ltd|limited|holdings?|co(?:mpany)?|group|partners|fund|series\s+of\s+[^,]+)\b\.?"
    cleaned = re.sub(pattern, "", name)
    cleaned = re.sub(r"[\s,]+", " ", cleaned).strip()
    return cleaned or name


class OpenRouterClient:
    """Calls OpenRouter (default: google/gemini-2.5-flash) with strict JSON outputs and cost auditing."""

    async def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        audit: AuditSession,
        step_name: str,
    ) -> dict[str, Any]:
        with audit.step(step_name, provider="openrouter") as step:
            if not settings.OPENROUTER_API_KEY:
                step.record(
                    status="INFO",
                    detail="OPENROUTER_API_KEY not set; skipping LLM call.",
                )
                return {}

            headers = {
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": settings.OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(
                    settings.OPENROUTER_BASE_URL, headers=headers, json=payload
                )
                resp.raise_for_status()
                data = resp.json()

            usage = data.get("usage", {})
            in_tok = int(usage.get("prompt_tokens", 0))
            out_tok = int(usage.get("completion_tokens", 0))
            # OpenRouter returns native USD cost in `usage.cost` or `usage.total_cost`; fallback to Gemini Flash rate
            cost_usd = float(
                usage.get("cost")
                or usage.get("total_cost")
                or (in_tok * 0.00000015 + out_tok * 0.00000060)
            )
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "{}")
            )
            # Strip optional markdown code fences if present
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            parsed = json.loads(cleaned) if cleaned else {}
            step.record(
                status="PASS",
                detail=f"Model {settings.OPENROUTER_MODEL} returned JSON ({in_tok}+{out_tok} tokens)",
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost_usd,
            )
            return parsed


class TypeSafeClient:
    """
    Calls TypeSafe.ai System One (`POST /v1/systemone`, model `jev-latest`)
    using the `noul` binary probability primitive (0.0 to 1.0) for Quality Control.
    """

    async def evaluate_noul_batch(
        self,
        *,
        state: dict[str, Any] | str,
        questions: dict[str, str],
        audit: AuditSession,
        step_name: str,
    ) -> dict[str, float]:
        """
        Evaluates a batch of yes/no instructions in a single parallel pass.
        `questions` maps `{question_key: instruction_statement}`.
        Returns `{question_key: probability_between_0_and_1}`.
        """
        if not questions:
            return {}

        with audit.step(step_name, provider="typesafe") as step:
            if not settings.TYPESAFE_API_KEY:
                step.record(
                    status="INFO",
                    detail="TYPESAFE_API_KEY missing; defaulting noul scores to 1.0 (unverified).",
                )
                return {k: 1.0 for k in questions}

            formatted_questions = {
                k: {"type": "noul", "instructions": instr}
                for k, instr in questions.items()
            }
            payload = {
                "model": settings.TYPESAFE_MODEL,
                "state": state if isinstance(state, str) else json.dumps(state),
                "questions": formatted_questions,
            }
            headers = {
                "Authorization": f"Bearer {settings.TYPESAFE_API_KEY}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    settings.TYPESAFE_URL, headers=headers, json=payload
                )
                resp.raise_for_status()
                data = resp.json()

            answers_raw = data.get("answers", data)
            scores: dict[str, float] = {}
            for k in questions:
                ans = answers_raw.get(k, {})
                scores[k] = float(ans.get("noul", 0.0)) if isinstance(ans, dict) else 0.0

            usage = data.get("usage", {})
            in_tok = int(usage.get("input_tokens", 0))
            out_tok = int(usage.get("output_tokens", 0))
            cost_usd = float(
                usage.get("cost_usd")
                or (
                    in_tok * settings.TYPESAFE_INPUT_TOKEN_USD
                    + out_tok * settings.TYPESAFE_OUTPUT_TOKEN_USD
                )
            )
            summary = ", ".join(f"{k}={v:.2f}" for k, v in scores.items())
            step.record(
                status="PASS",
                detail=f"TypeSafe noul scores: {summary}",
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost_usd,
            )
            return scores


class ExaClient:
    """Uses Exa.ai as the neural search engine for company websites and executive profiles."""

    async def search(
        self,
        query: str,
        *,
        num_results: int = 5,
        include_domains: list[str] | None = None,
        audit: AuditSession,
        step_name: str,
    ) -> list[dict[str, Any]]:
        with audit.step(step_name, provider="exa") as step:
            if not settings.EXA_API_KEY:
                step.record(
                    status="INFO",
                    detail="EXA_API_KEY not configured; returning empty search results.",
                )
                return []

            payload: dict[str, Any] = {
                "query": query,
                "numResults": num_results,
                "useAutoprompt": True,
                "contents": {"text": {"maxCharacters": 600}},
            }
            if include_domains:
                payload["includeDomains"] = include_domains

            headers = {
                "x-api-key": settings.EXA_API_KEY,
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    settings.EXA_SEARCH_URL, headers=headers, json=payload
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            cost_usd = float(
                data.get("costDollars", {}).get("total", settings.EXA_COST_PER_SEARCH_USD)
                if isinstance(data.get("costDollars"), dict)
                else settings.EXA_COST_PER_SEARCH_USD
            )
            step.record(
                status="PASS" if results else "INFO",
                detail=f"Exa returned {len(results)} result(s) for '{query[:60]}'",
                cost_usd=cost_usd,
            )
            return results


class ContactEnricher(Protocol):
    """Protocol allowing seamless switching or chaining between Apify and Apollo.io."""

    async def find_and_verify_email(
        self,
        exec_info: ExecutiveDirector,
        company_name: str,
        company_website: str | None,
        linkedin_url: str | None,
        audit: AuditSession,
    ) -> tuple[str | None, str]:
        ...


class ApifyContactEnricher:
    """
    Finds an executive's email from their LinkedIn URL via `vulnv/linkedin-email-finder`
    and validates deliverability via `michael.g/email-verifier-validator`.
    """

    async def find_and_verify_email(
        self,
        exec_info: ExecutiveDirector,
        company_name: str,
        company_website: str | None,
        linkedin_url: str | None,
        audit: AuditSession,
    ) -> tuple[str | None, str]:
        if not linkedin_url:
            return None, "Skipped (No verified LinkedIn profile found)"

        if not settings.APIFY_API_TOKEN:
            with audit.step("Apify Email Lookup", provider="apify") as step:
                step.record(status="INFO", detail="APIFY_API_TOKEN not set.")
            return None, "Skipped (APIFY_API_TOKEN missing)"

        candidate_email: str | None = None

        # Step 1: vulnv/linkedin-email-finder
        with audit.step(
            f"Apify Email Finder ({settings.APIFY_LINKEDIN_EMAIL_ACTOR})",
            provider="apify",
        ) as step1:
            actor_slug = settings.APIFY_LINKEDIN_EMAIL_ACTOR.replace("/", "~")
            run_url = (
                f"https://api.apify.com/v2/acts/{actor_slug}/run-sync-get-dataset-items"
                f"?token={settings.APIFY_API_TOKEN}&timeout=120"
            )
            payload1 = {
                "linkedinUrl": linkedin_url,
                "url": linkedin_url,
                "urls": [linkedin_url],
            }
            items: list[dict[str, Any]] = []
            cost1 = 0.005  # Standard actor credit estimate
            async with httpx.AsyncClient(timeout=130.0) as client:
                try:
                    resp1 = await client.post(run_url, json=payload1)
                    if resp1.status_code in (200, 201):
                        data1 = resp1.json()
                        items = data1 if isinstance(data1, list) else [data1]
                    else:
                        step1.record(status="INFO", detail=f"Apify HTTP {resp1.status_code}")
                except Exception as exc:
                    step1.record(status="ERROR", detail=f"Apify call failed: {exc}")

            for item in items:
                for key in ("email", "emailAddress", "work_email", "personal_email"):
                    val = _safe_get(item, key)
                    if isinstance(val, str) and "@" in val:
                        candidate_email = val.strip()
                        break
                if candidate_email:
                    break

            step1.record(
                status="PASS" if candidate_email else "INFO",
                detail=(
                    f"Found candidate email: {candidate_email}"
                    if candidate_email
                    else "No email found for LinkedIn URL"
                ),
                cost_usd=cost1,
            )

        if not candidate_email:
            return None, "No email associated with LinkedIn profile"

        # Step 2: michael.g/email-verifier-validator
        with audit.step(
            f"Apify Email Verifier ({settings.APIFY_EMAIL_VERIFIER_ACTOR})",
            provider="apify",
        ) as step2:
            actor_slug2 = settings.APIFY_EMAIL_VERIFIER_ACTOR.replace("/", "~")
            run_url2 = (
                f"https://api.apify.com/v2/acts/{actor_slug2}/run-sync-get-dataset-items"
                f"?token={settings.APIFY_API_TOKEN}&timeout=120"
            )
            payload2 = {
                "emails": [candidate_email],
                "email": candidate_email,
            }
            items2: list[dict[str, Any]] = []
            cost2 = 0.005
            async with httpx.AsyncClient(timeout=130.0) as client:
                try:
                    resp2 = await client.post(run_url2, json=payload2)
                    if resp2.status_code in (200, 201):
                        data2 = resp2.json()
                        items2 = data2 if isinstance(data2, list) else [data2]
                    else:
                        step2.record(status="INFO", detail=f"Apify HTTP {resp2.status_code}")
                except Exception as exc:
                    step2.record(status="ERROR", detail=f"Apify call failed: {exc}")

            is_valid = False
            status_str = "Unverified"
            if items2:
                first = items2[0]
                raw_status = str(
                    _safe_get(first, "status", "result", "deliverable", "isValid", "is_valid") or ""
                ).lower()
                is_valid = raw_status in ("valid", "deliverable", "true", "ok", "safe")
                status_str = f"Verified ({raw_status})" if is_valid else f"Invalid ({raw_status})"

            step2.record(
                status="PASS" if is_valid else "FAIL",
                detail=f"{candidate_email} -> {status_str}",
                cost_usd=cost2,
            )
            return (candidate_email if is_valid else None), status_str


class ApolloContactEnricher:
    """
    Ready-to-activate Apollo.io People Match enricher (`ENRICHER_PROVIDER=apollo` or `both`).
    Not enabled in v1 by default, per specification.
    """

    async def find_and_verify_email(
        self,
        exec_info: ExecutiveDirector,
        company_name: str,
        company_website: str | None,
        linkedin_url: str | None,
        audit: AuditSession,
    ) -> tuple[str | None, str]:
        with audit.step("Apollo.io People Match", provider="apollo") as step:
            if not settings.APOLLO_API_KEY:
                step.record(status="INFO", detail="APOLLO_API_KEY not configured.")
                return None, "Apollo API key not configured"

            headers = {
                "Cache-Control": "no-cache",
                "Content-Type": "application/json",
                "X-Api-Key": settings.APOLLO_API_KEY,
            }
            payload = {
                "first_name": exec_info.first_name,
                "last_name": exec_info.last_name,
                "name": exec_info.full_name,
                "organization_name": company_name,
                "domain": company_website,
                "linkedin_url": linkedin_url,
                "reveal_personal_emails": True,
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.apollo.io/api/v1/people/match",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                person = resp.json().get("person") or {}
                email = person.get("email")
                email_status = person.get("email_status", "unknown")
                step.record(
                    status="PASS" if email else "INFO",
                    detail=f"Apollo returned email={email} ({email_status})",
                    cost_usd=0.015 if email else 0.0,
                )
                return email, f"Apollo ({email_status})"


class EnrichmentPipeline:
    """Orchestrates Exa + TypeSafe QC + OpenRouter + Apify/Apollo for companies and executives."""

    def __init__(self) -> None:
        self.openrouter = OpenRouterClient()
        self.exa = ExaClient()
        self.typesafe = TypeSafeClient()
        self.apify = ApifyContactEnricher()
        self.apollo = ApolloContactEnricher()

    async def find_verified_company_website(
        self, filing: NormalizedFiling, audit: AuditSession
    ) -> None:
        """Finds the company website via Exa (if not already present) and scores it with TypeSafe AI."""
        candidates: list[dict[str, str]] = []
        if filing.company_website:
            candidates.append(
                {"url": filing.company_website, "title": filing.company_name, "snippet": "Listed on filing"}
            )
        else:
            brand = clean_brand_name(filing.company_name)
            results = await self.exa.search(
                f"{brand} website",
                num_results=3,
                audit=audit,
                step_name="Exa Company Website Search",
            )
            for r in results:
                url = r.get("url", "")
                if url and not any(
                    bad in url.lower()
                    for bad in ("sec.gov", "edgar", "linkedin.com", "facebook.com", "bizapedia")
                ):
                    candidates.append(
                        {
                            "url": url,
                            "title": r.get("title", ""),
                            "snippet": (r.get("text") or "")[:300],
                        }
                    )

        if not candidates:
            return

        questions = {
            f"cand_{i}": (
                f"Candidate URL '{c['url']}' (title: '{c['title']}') is the official website "
                f"of the company '{filing.company_name}' in the '{filing.industry}' industry."
            )
            for i, c in enumerate(candidates)
        }
        scores = await self.typesafe.evaluate_noul_batch(
            state={
                "company_name": filing.company_name,
                "industry": filing.industry,
                "executives": [e.full_name for e in filing.executives],
                "candidates": candidates,
            },
            questions=questions,
            audit=audit,
            step_name="TypeSafe QC: Verify Company Website",
        )
        best_idx = max(range(len(candidates)), key=lambda i: scores.get(f"cand_{i}", 0.0))
        best_score = scores.get(f"cand_{best_idx}", 0.0)
        if best_score >= settings.TYPESAFE_CONFIDENCE_THRESHOLD:
            filing.company_website = candidates[best_idx]["url"]
            filing.website_confidence = best_score

    async def deep_dive_executive(
        self,
        exec_info: ExecutiveDirector,
        filing: NormalizedFiling,
        audit: AuditSession,
    ) -> EnrichedExecutive:
        """
        On-demand deep-dive for a chosen Executive Director:
        1. Exa searches for LinkedIn profile, social media handles, blogs, and personal websites.
        2. OpenRouter structures candidate links from Exa results.
        3. TypeSafe AI (`noul`) verifies each candidate belongs to this exact Executive Director.
        4. Apify (`vulnv/linkedin-email-finder` + `michael.g/email-verifier-validator`) finds & validates email.
        """
        result = EnrichedExecutive(
            full_name=exec_info.full_name,
            company_name=filing.company_name,
            enrichment_provider=settings.ENRICHER_PROVIDER,
        )

        brand = clean_brand_name(filing.company_name)

        # 1. Exa Search for LinkedIn
        li_results = await self.exa.search(
            f'"{exec_info.full_name}" "{brand}" site:linkedin.com/in/',
            num_results=3,
            include_domains=["linkedin.com"],
            audit=audit,
            step_name=f"Exa LinkedIn Search ({exec_info.full_name})",
        )

        # 2. Exa Search for Socials, Blogs, and Personal Websites
        web_results = await self.exa.search(
            f'"{exec_info.full_name}" "{brand}" (twitter OR x.com OR github OR substack OR medium OR blog OR personal website)',
            num_results=6,
            audit=audit,
            step_name=f"Exa Socials/Blog/Website Search ({exec_info.full_name})",
        )

        # 3. Structure candidates via OpenRouter
        structured = await self.openrouter.chat_json(
            system_prompt=(
                "Extract candidate profile links for the specified executive from the search results. "
                "Return strict JSON: "
                '{"linkedin_urls": [str], "social_handles": [str], "blogs": [str], "personal_websites": [str]}'
            ),
            user_prompt=json.dumps(
                {
                    "executive": exec_info.full_name,
                    "roles": exec_info.roles,
                    "company": filing.company_name,
                    "linkedin_search": [
                        {"url": r.get("url"), "title": r.get("title"), "text": r.get("text")}
                        for r in li_results
                    ],
                    "web_search": [
                        {"url": r.get("url"), "title": r.get("title"), "text": r.get("text")}
                        for r in web_results
                    ],
                }
            ),
            audit=audit,
            step_name=f"OpenRouter Candidate Extraction ({exec_info.full_name})",
        )

        li_cands = [u for u in structured.get("linkedin_urls", []) if isinstance(u, str)]
        if not li_cands and li_results:
            li_cands = [r["url"] for r in li_results if r.get("url")]

        social_cands = [u for u in structured.get("social_handles", []) if isinstance(u, str)]
        blog_cands = [u for u in structured.get("blogs", []) if isinstance(u, str)]
        site_cands = [u for u in structured.get("personal_websites", []) if isinstance(u, str)]

        # 4. TypeSafe AI Parallel QC over all extracted candidates
        qc_questions: dict[str, str] = {}
        for i, u in enumerate(li_cands[:3]):
            qc_questions[f"li_{i}"] = (
                f"The LinkedIn profile '{u}' belongs to {exec_info.full_name}, "
                f"who is associated with {filing.company_name}."
            )
        for i, u in enumerate(social_cands[:4]):
            qc_questions[f"soc_{i}"] = (
                f"The social media handle/profile '{u}' belongs to {exec_info.full_name} of {filing.company_name}."
            )
        for i, u in enumerate(blog_cands[:3]):
            qc_questions[f"blog_{i}"] = (
                f"The blog '{u}' is authored by {exec_info.full_name} of {filing.company_name}."
            )
        for i, u in enumerate(site_cands[:3]):
            qc_questions[f"site_{i}"] = (
                f"The website '{u}' is the personal website of {exec_info.full_name} of {filing.company_name}."
            )

        scores = await self.typesafe.evaluate_noul_batch(
            state={
                "executive": exec_info.model_dump(),
                "company": filing.company_name,
                "industry": filing.industry,
                "linkedin_results": li_results,
                "web_results": web_results,
            },
            questions=qc_questions,
            audit=audit,
            step_name=f"TypeSafe QC: Verify Profiles ({exec_info.full_name})",
        )

        threshold = settings.TYPESAFE_CONFIDENCE_THRESHOLD
        for i, u in enumerate(li_cands[:3]):
            sc = scores.get(f"li_{i}", 0.0)
            if sc >= threshold and (
                result.linkedin_confidence is None or sc > result.linkedin_confidence
            ):
                result.linkedin_url = u
                result.linkedin_confidence = sc

        result.social_handles = [
            f"{u} (QC: {scores.get(f'soc_{i}', 0.0):.0%})"
            for i, u in enumerate(social_cands[:4])
            if scores.get(f"soc_{i}", 0.0) >= threshold
        ]
        result.blogs = [
            f"{u} (QC: {scores.get(f'blog_{i}', 0.0):.0%})"
            for i, u in enumerate(blog_cands[:3])
            if scores.get(f"blog_{i}", 0.0) >= threshold
        ]
        result.personal_websites = [
            f"{u} (QC: {scores.get(f'site_{i}', 0.0):.0%})"
            for i, u in enumerate(site_cands[:3])
            if scores.get(f"site_{i}", 0.0) >= threshold
        ]

        # 5. Email Lookup & Validation via Apify (and/or Apollo.io when enabled)
        email, status_msg = await self.apify.find_and_verify_email(
            exec_info=exec_info,
            company_name=filing.company_name,
            company_website=filing.company_website,
            linkedin_url=result.linkedin_url,
            audit=audit,
        )
        if not email and settings.ENRICHER_PROVIDER in ("apollo", "both"):
            email, status_msg = await self.apollo.find_and_verify_email(
                exec_info=exec_info,
                company_name=filing.company_name,
                company_website=filing.company_website,
                linkedin_url=result.linkedin_url,
                audit=audit,
            )

        result.valid_email = email
        result.email_verification_status = status_msg
        return result
