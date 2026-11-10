"""External API integrations: OpenRouter, Exa.ai, TypeSafe.ai, Apify, and Apollo.io."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol
from urllib.parse import urlparse
import dns.resolver
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


_RESERVED_GH_PATHS = {
    "features", "orgs", "topics", "pricing", "login", "join", "about",
    "settings", "site", "security", "sponsors", "marketplace", "explore",
    "notifications", "pulls", "issues", "codespaces", "enterprise", "readme",
}


def _is_safe_public_url(url: str) -> bool:
    """SSRF defense: allows only http/https URLs on public internet hostnames."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(
            (".local", ".internal", ".lan")
        ):
            return False
        if re.match(r"^(?:10\.|127\.|169\.254\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)", host):
            return False
        return "." in host
    except Exception:
        return False


def _extract_emails_from_text(text: str) -> list[str]:
    """Extracts clean email addresses from HTML/text while ignoring image filenames and noreply addresses."""
    raw = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,12}", text)
    cleaned: list[str] = []
    bad_Fragments = (
        "noreply", "no-reply", "example.com", "sentry.io", "wixpress.com",
        "github.com", "2x.png", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js",
    )
    for e in raw:
        el = e.lower().strip(".")
        if not any(b in el for b in bad_Fragments) and el not in cleaned:
            cleaned.append(el)
    return cleaned


async def _scrape_public_page_signals(
    client: httpx.AsyncClient, url: str
) -> dict[str, list[str]]:
    """Fetches up to 250KB from a public URL and extracts GitHub repos/users, LinkedIn, socials, and emails."""
    signals: dict[str, list[str]] = {
        "github_repos": [],
        "github_users": [],
        "linkedin_urls": [],
        "emails": [],
    }
    if not _is_safe_public_url(url):
        return signals
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FormDScoutBot/1.0)"},
            timeout=6.0,
        )
        if resp.status_code != 200:
            return signals
        html = resp.text[:250_000]

        for m in re.finditer(
            r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?",
            html,
        ):
            owner = m.group(1).strip("/")
            repo = (m.group(2) or "").strip("/")
            if owner.lower() in _RESERVED_GH_PATHS:
                continue
            if repo and repo.lower() not in _RESERVED_GH_PATHS:
                slug = f"{owner}/{repo}"
                if slug not in signals["github_repos"]:
                    signals["github_repos"].append(slug)
            elif owner not in signals["github_users"]:
                signals["github_users"].append(owner)

        for m_li in re.finditer(
            r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%-]+/?", html
        ):
            li_url = m_li.group(0).rstrip("/")
            if li_url not in signals["linkedin_urls"]:
                signals["linkedin_urls"].append(li_url)

        for email in _extract_emails_from_text(html):
            if email not in signals["emails"]:
                signals["emails"].append(email)
    except Exception:
        pass
    return signals


def _extract_candidate_handles(items: list[str]) -> list[str]:
    """Extracts clean username handles from '@handle' strings or profile URLs."""
    handles: list[str] = []
    for item in items:
        s = item.strip()
        if s.startswith("@") and len(s) > 2:
            h = re.sub(r"[^A-Za-z0-9_.-]", "", s[1:])
            if h and h.lower() not in handles:
                handles.append(h.lower())
            continue
        m = re.search(
            r"(?:github\.com|x\.com|twitter\.com|teapawt\.org|bsky\.app/profile|medium\.com/@|dev\.to)/([A-Za-z0-9_.-]+)",
            s,
            re.I,
        )
        if m:
            h = m.group(1).lstrip("@")
            if h.lower() not in _RESERVED_GH_PATHS and h.lower() not in handles:
                handles.append(h.lower())
    return handles


def _has_valid_mx_record(email: str) -> bool:
    """Verifies via DNS that the email domain has active mail exchange (MX) servers."""
    if "@" not in email:
        return False
    domain = email.split("@", 1)[1].strip()
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=4.0)
        return len(answers) > 0
    except Exception:
        return False


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
            domains_note = f" [domains: {', '.join(include_domains)}]" if include_domains else ""
            top_urls = ", ".join(r.get("url", "") for r in results[:3] if r.get("url"))
            urls_note = f" → Top hits: {top_urls}" if top_urls else ""
            step.record(
                status="PASS" if results else "INFO",
                detail=f"Query: `{query}`{domains_note} ({len(results)} result(s)){urls_note}",
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
    Finds an executive's email (via pre-discovered GitHub/web emails or `vulnv/linkedin-email-finder`)
    and validates deliverability via `michael.g/email-verifier-validator` + DNS MX verification.
    """

    async def find_and_verify_email(
        self,
        exec_info: ExecutiveDirector,
        company_name: str,
        company_website: str | None,
        linkedin_url: str | None,
        audit: AuditSession,
        discovered_emails: list[str] | None = None,
    ) -> tuple[str | None, str]:
        candidate_email: str | None = (
            discovered_emails[0].strip() if discovered_emails else None
        )
        source_label = "GitHub/Web Pivot" if candidate_email else "LinkedIn"

        # Step 1: Only call vulnv/linkedin-email-finder if we don't already have a pivoted email
        if not candidate_email:
            if not linkedin_url:
                return None, "Skipped (No LinkedIn profile or GitHub/Web email found)"

            if not settings.APIFY_API_TOKEN:
                with audit.step("Apify Email Lookup", provider="apify") as step:
                    step.record(status="INFO", detail="APIFY_API_TOKEN not set.")
                return None, "Skipped (APIFY_API_TOKEN missing)"

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
                cost1 = 0.005
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

        # Step 2: michael.g/email-verifier-validator (+ DNS MX fallback verification)
        with audit.step(
            f"Verify Email ({candidate_email} via {settings.APIFY_EMAIL_VERIFIER_ACTOR})",
            provider="apify",
        ) as step2:
            is_valid = False
            status_str = "Unverified"
            cost2 = 0.0
            if settings.APIFY_API_TOKEN:
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
                async with httpx.AsyncClient(timeout=130.0) as client:
                    try:
                        resp2 = await client.post(run_url2, json=payload2)
                        if resp2.status_code in (200, 201):
                            cost2 = 0.005
                            data2 = resp2.json()
                            items2 = data2 if isinstance(data2, list) else [data2]
                    except Exception:
                        pass

                if items2:
                    first = items2[0]
                    raw_status = str(
                        _safe_get(first, "status", "result", "deliverable", "isValid", "is_valid") or ""
                    ).lower()
                    if raw_status in (
                        "valid",
                        "deliverable",
                        "true",
                        "ok",
                        "safe",
                        "risky",
                        "catch-all",
                        "catch_all",
                        "accept_all",
                        "accept-all",
                    ):
                        is_valid = True
                        status_str = f"Verified via Apify ({raw_status}, source: {source_label})"
                    elif raw_status:
                        status_str = f"Invalid ({raw_status})"

            # Fallback / Secondary Check: DNS MX verification if Apify verifier returned no items
            if not is_valid and status_str == "Unverified":
                if _has_valid_mx_record(candidate_email):
                    is_valid = True
                    status_str = f"Verified ({source_label} + Active MX Domain)"
                else:
                    status_str = "Invalid (No MX record)"

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
        3-Hop Chained Investigation for a chosen Executive Director:
        - Hop 1: Domain-aware Exa search (LinkedIn, socials, blogs, websites) + OpenRouter structuring.
        - Hop 2: Handle & GitHub API Pivot (scrapes company website & social pages for GitHub repos/handles,
                 queries GitHub API for user profile, personal website `blog`, and commit author emails).
        - Hop 3: TypeSafe AI (`noul`) parallel verification + Apify/MX email validation.
        """
        result = EnrichedExecutive(
            full_name=exec_info.full_name,
            company_name=filing.company_name,
            enrichment_provider=settings.ENRICHER_PROVIDER,
        )

        brand = clean_brand_name(filing.company_name)
        domain = ""
        if filing.company_website:
            domain = (urlparse(filing.company_website).hostname or "").lower().removeprefix("www.")
        brand_or_domain = f'("{brand}" OR "{domain}")' if domain else f'"{brand}"'

        # --- HOP 1: Exa Search + OpenRouter Candidate Extraction ---
        li_results = await self.exa.search(
            f'"{exec_info.full_name}" {brand_or_domain} site:linkedin.com/in/',
            num_results=3,
            include_domains=["linkedin.com"],
            audit=audit,
            step_name=f"Hop 1a: Exa LinkedIn Search ({exec_info.full_name})",
        )

        web_results = await self.exa.search(
            f'"{exec_info.full_name}" {brand_or_domain} (github OR twitter OR x.com OR substack OR medium OR blog OR personal website)',
            num_results=6,
            audit=audit,
            step_name=f"Hop 1b: Exa Socials/Blog/Website Search ({exec_info.full_name})",
        )

        structured = await self.openrouter.chat_json(
            system_prompt=(
                "Extract candidate profile links, handles, and emails for the specified executive from the search results. "
                "Return strict JSON: "
                '{"linkedin_urls": [str], "social_handles": [str], "blogs": [str], "personal_websites": [str], "emails": [str]}'
            ),
            user_prompt=json.dumps(
                {
                    "executive": exec_info.full_name,
                    "roles": exec_info.roles,
                    "company": filing.company_name,
                    "company_website": filing.company_website,
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
            step_name=f"Hop 1c: OpenRouter Candidate Extraction ({exec_info.full_name})",
        )

        li_cands = [u for u in structured.get("linkedin_urls", []) if isinstance(u, str)]
        if not li_cands and li_results:
            li_cands = [r["url"] for r in li_results if r.get("url")]

        social_cands = [u for u in structured.get("social_handles", []) if isinstance(u, str)]
        blog_cands = [u for u in structured.get("blogs", []) if isinstance(u, str)]
        site_cands = [u for u in structured.get("personal_websites", []) if isinstance(u, str)]
        email_cands = [e for e in structured.get("emails", []) if isinstance(e, str) and "@" in e]

        # Also harvest raw URLs from Exa web_results for handle extraction
        raw_web_urls = [r.get("url", "") for r in web_results if r.get("url")]

        # --- HOP 2: Multi-Hop Handle, Website & GitHub API Pivot ($0.00 Free Pivot) ---
        gh_profile_evidence: list[dict[str, Any]] = []
        with audit.step(
            f"Hop 2: Handle & GitHub API Pivot ({exec_info.full_name})", provider="local"
        ) as pivot_step:
            discovered_repos: list[str] = []
            candidate_handles = _extract_candidate_handles(
                social_cands + blog_cands + site_cands + raw_web_urls
            )

            gh_headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "FormDScoutBot/1.0",
            }
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                # 2a. Scrape company website & top 2 discovered social URLs (e.g. teapawt.org/marchantweb)
                urls_to_scrape: list[str] = []
                if filing.company_website:
                    urls_to_scrape.append(filing.company_website)
                for u in (social_cands + raw_web_urls)[:3]:
                    if u.startswith("http") and u not in urls_to_scrape and "linkedin.com" not in u:
                        urls_to_scrape.append(u)

                for page_url in urls_to_scrape[:3]:
                    sig = await _scrape_public_page_signals(client, page_url)
                    for r_slug in sig["github_repos"]:
                        if r_slug not in discovered_repos:
                            discovered_repos.append(r_slug)
                    for g_user in sig["github_users"]:
                        if g_user.lower() not in candidate_handles:
                            candidate_handles.append(g_user.lower())
                    for li_u in sig["linkedin_urls"]:
                        if li_u not in li_cands:
                            li_cands.append(li_u)
                    for em in sig["emails"]:
                        if em not in email_cands:
                            email_cands.append(em)

                # 2b. From discovered GitHub repos (e.g. shader-effects-inc/shaders), get top contributors
                for repo_slug in discovered_repos[:2]:
                    try:
                        c_resp = await client.get(
                            f"https://api.github.com/repos/{repo_slug}/contributors?per_page=5",
                            headers=gh_headers,
                        )
                        if c_resp.status_code == 200 and isinstance(c_resp.json(), list):
                            for contrib in c_resp.json():
                                login = (contrib.get("login") or "").strip().lower()
                                if login and not login.endswith("[bot]") and login not in candidate_handles:
                                    candidate_handles.append(login)
                    except Exception:
                        pass

                # 2c. Also search GitHub users by executive full name
                try:
                    q_name = exec_info.full_name.replace(" ", "+")
                    u_search = await client.get(
                        f"https://api.github.com/search/users?q={q_name}+in:fullname&per_page=3",
                        headers=gh_headers,
                    )
                    if u_search.status_code == 200:
                        for item in u_search.json().get("items", []):
                            login = (item.get("login") or "").strip().lower()
                            if login and login not in candidate_handles:
                                candidate_handles.append(login)
                except Exception:
                    pass

                # 2d. Inspect each candidate handle on GitHub API (Profile + Public Commits + Personal Website)
                exec_first = exec_info.first_name.lower() or exec_info.full_name.split()[0].lower()
                exec_last = exec_info.last_name.lower() or exec_info.full_name.split()[-1].lower()

                for handle in candidate_handles[:4]:
                    try:
                        u_resp = await client.get(
                            f"https://api.github.com/users/{handle}", headers=gh_headers
                        )
                        if u_resp.status_code != 200:
                            continue
                        gh_user = u_resp.json()
                        gh_name = (gh_user.get("name") or "").strip()
                        gh_bio = (gh_user.get("bio") or "").strip()
                        gh_blog = (gh_user.get("blog") or "").strip()
                        gh_email = (gh_user.get("email") or "").strip()
                        gh_twitter = (gh_user.get("twitter_username") or "").strip()
                        gh_html = gh_user.get("html_url") or f"https://github.com/{handle}"

                        # Check relevance to executive
                        combined_meta = f"{gh_name} {gh_bio} {gh_blog} {handle}".lower()
                        if not (
                            exec_last in combined_meta
                            or exec_first in combined_meta
                            or (domain and domain in combined_meta)
                            or brand.lower() in combined_meta
                        ):
                            continue

                        gh_profile_evidence.append(
                            {
                                "handle": handle,
                                "name": gh_name,
                                "bio": gh_bio,
                                "blog": gh_blog,
                                "email": gh_email,
                                "url": gh_html,
                            }
                        )
                        if gh_html not in social_cands:
                            social_cands.insert(0, gh_html)
                        if gh_twitter and f"@{gh_twitter}" not in social_cands:
                            social_cands.append(f"@{gh_twitter}")
                        if gh_email and "@" in gh_email and gh_email not in email_cands:
                            email_cands.insert(0, gh_email)

                        # Normalize and scrape personal website from GitHub `blog` field (e.g. https://marchantweb.com)
                        if gh_blog:
                            norm_blog = (
                                gh_blog
                                if gh_blog.startswith(("http://", "https://"))
                                else f"https://{gh_blog}"
                            )
                            if norm_blog not in site_cands:
                                site_cands.insert(0, norm_blog)
                            blog_sig = await _scrape_public_page_signals(client, norm_blog)
                            for em in blog_sig["emails"]:
                                if em not in email_cands:
                                    email_cands.insert(0, em)
                            for li_u in blog_sig["linkedin_urls"]:
                                if li_u not in li_cands:
                                    li_cands.insert(0, li_u)

                        # Extract author email from public commits (since unauthenticated /users/{handle} hides email)
                        commit_urls = [
                            f"https://api.github.com/users/{handle}/events/public?per_page=15",
                            f"https://api.github.com/search/commits?q=author:{handle}&sort=author-date&per_page=5",
                        ]
                        for r_slug in discovered_repos[:2]:
                            commit_urls.insert(
                                0,
                                f"https://api.github.com/repos/{r_slug}/commits?author={handle}&per_page=5",
                            )
                        for c_url in commit_urls:
                            if email_cands:
                                break
                            try:
                                c_r = await client.get(c_url, headers=gh_headers)
                                if c_r.status_code == 200:
                                    for em in _extract_emails_from_text(c_r.text):
                                        if em not in email_cands:
                                            email_cands.insert(0, em)
                            except Exception:
                                pass

                        # If personal website domain exists (e.g. marchantweb.com) and still no email, check common founder address
                        if gh_blog and not email_cands:
                            p_dom = (urlparse(norm_blog).hostname or "").lower().removeprefix("www.")
                            if p_dom and _has_valid_mx_record(f"{exec_first}@{p_dom}"):
                                email_cands.append(f"{exec_first}@{p_dom}")
                    except Exception:
                        pass

            pivot_step.record(
                status="PASS" if (gh_profile_evidence or site_cands or email_cands) else "INFO",
                detail=(
                    f"Scraped={urls_to_scrape[:3]} | Handles={candidate_handles[:4]} | Repos={discovered_repos[:2]} | "
                    f"GH Profiles={len(gh_profile_evidence)} | Websites={site_cands[:2]} | Emails={email_cands[:2]}"
                ),
            )

        # Hop 2b: Conditional Exa Handle Pivot Search (if a handle was found but personal website or LinkedIn is still missing)
        if candidate_handles and (not site_cands or not li_cands):
            primary_handle = candidate_handles[0]
            handle_results = await self.exa.search(
                f'"{primary_handle}" ("{exec_info.full_name}" OR "{brand}") (website OR blog OR linkedin OR github)',
                num_results=4,
                audit=audit,
                step_name=f"Hop 2b: Exa Handle Pivot Search (@{primary_handle})",
            )
            for hr in handle_results:
                h_url = (hr.get("url") or "").strip()
                h_text = hr.get("text") or ""
                if "linkedin.com/in/" in h_url and h_url not in li_cands:
                    li_cands.append(h_url)
                elif (
                    h_url.startswith("http")
                    and primary_handle.lower() in h_url.lower()
                    and not any(
                        s in h_url.lower()
                        for s in ("github.com", "twitter.com", "x.com", "teapawt.org", "dev.to", "linkedin.com")
                    )
                    and h_url not in site_cands
                ):
                    site_cands.append(h_url)
                for em in _extract_emails_from_text(h_text):
                    if em not in email_cands:
                        email_cands.append(em)
                web_results.append(hr)

        # --- HOP 3: TypeSafe AI Parallel QC over all discovered candidates ---
        qc_questions: dict[str, str] = {}
        for i, u in enumerate(li_cands[:3]):
            qc_questions[f"li_{i}"] = (
                f"The LinkedIn profile '{u}' belongs to {exec_info.full_name}, "
                f"who is associated with {filing.company_name}."
            )
        for i, u in enumerate(social_cands[:5]):
            qc_questions[f"soc_{i}"] = (
                f"The social media or GitHub profile '{u}' belongs to {exec_info.full_name} of {filing.company_name}."
            )
        for i, u in enumerate(blog_cands[:3]):
            qc_questions[f"blog_{i}"] = (
                f"The blog or article '{u}' is authored by or about {exec_info.full_name} / {filing.company_name}."
            )
        for i, u in enumerate(site_cands[:3]):
            qc_questions[f"site_{i}"] = (
                f"The website '{u}' is the personal or portfolio website of {exec_info.full_name} of {filing.company_name}."
            )
        for i, em in enumerate(email_cands[:3]):
            qc_questions[f"email_{i}"] = (
                f"The email address '{em}' belongs to {exec_info.full_name} of {filing.company_name}."
            )

        scores = await self.typesafe.evaluate_noul_batch(
            state={
                "executive": exec_info.model_dump(),
                "company": filing.company_name,
                "company_website": filing.company_website,
                "industry": filing.industry,
                "github_profiles_discovered": gh_profile_evidence,
                "discovered_personal_websites": site_cands[:3],
                "discovered_emails_from_github_and_websites": email_cands[:3],
                "linkedin_results": li_results,
                "web_results": web_results,
            },
            questions=qc_questions,
            audit=audit,
            step_name=f"Hop 3a: TypeSafe QC Verify Profiles & Emails ({exec_info.full_name})",
        )

        threshold = settings.TYPESAFE_CONFIDENCE_THRESHOLD
        for i, u in enumerate(li_cands[:3]):
            sc = scores.get(f"li_{i}", 0.0)
            if sc >= threshold and (
                result.linkedin_confidence is None or sc > result.linkedin_confidence
            ):
                result.linkedin_url = u if u.startswith(("http://", "https://")) else f"https://{u}"
                result.linkedin_confidence = sc

        def _fmt_link(val: str) -> str:
            v = val.strip()
            if not v or v.startswith(("@", "http://", "https://")):
                return v
            if "." in v and " " not in v:
                return f"https://{v}"
            return v

        result.social_handles = [
            f"{_fmt_link(u)} (QC: {scores.get(f'soc_{i}', 0.0):.0%})"
            for i, u in enumerate(social_cands[:5])
            if scores.get(f"soc_{i}", 0.0) >= threshold
        ]
        result.blogs = [
            f"{_fmt_link(u)} (QC: {scores.get(f'blog_{i}', 0.0):.0%})"
            for i, u in enumerate(blog_cands[:3])
            if scores.get(f"blog_{i}", 0.0) >= threshold
        ]
        result.personal_websites = [
            f"{_fmt_link(u)} (QC: {scores.get(f'site_{i}', 0.0):.0%})"
            for i, u in enumerate(site_cands[:3])
            if scores.get(f"site_{i}", 0.0) >= threshold
        ]

        verified_pivoted_emails = [
            em
            for i, em in enumerate(email_cands[:3])
            if scores.get(f"email_{i}", 0.0) >= threshold
        ]

        # 5. Email Lookup & Validation via Apify (supports pre-discovered GitHub/web emails OR LinkedIn lookup)
        email, status_msg = await self.apify.find_and_verify_email(
            exec_info=exec_info,
            company_name=filing.company_name,
            company_website=filing.company_website,
            linkedin_url=result.linkedin_url,
            audit=audit,
            discovered_emails=verified_pivoted_emails,
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
