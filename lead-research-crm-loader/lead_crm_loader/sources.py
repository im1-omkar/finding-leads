from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from html import unescape
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from .config import Settings
from .call_budget import current_budget
from .models import ContactAccess, Evidence, SearchResult, WebsiteSnapshot
from .usage import current_usage


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:14]}"


def canonical_url(raw_url: str) -> str:
    """Normalize a URL without fetching it. Keep paths because source pages matter."""
    parsed = urlparse(raw_url.strip())
    scheme = parsed.scheme.lower() or "https"
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname.startswith("www."):
        hostname = hostname[4:]
    netloc = hostname
    if parsed.port and parsed.port not in (80, 443):
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def normalized_domain(raw_url: str) -> str:
    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def site_root(raw_url: str) -> str:
    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    return urlunparse((parsed.scheme.lower() or "https", parsed.netloc.lower(), "/", "", "", ""))


DIRECTORY_DOMAIN_HINTS = (
    "crunchbase.com", "linkedin.com", "wellfound.com", "clutch.co", "yelp.com", "yellowpages.",
    "justdial.com", "indiamart.com", "g2.com", "zoominfo.com", "goodfirms.co", "techbehemoths.com",
    "cbinsights.com", "tracxn.com", "owler.com", "themanifest.com", "ambitionbox.com", "glassdoor.",
    "sulekha.com", "tradeindia.com", "startupindia.gov.in", "f6s.com", "producthunt.com", "apollo.io",
)
BLOCKED_OFFICIAL_LINK_HOSTS = (
    "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com", "tiktok.com", "wa.me",
    "whatsapp.com", "google.com", "maps.google.com", *DIRECTORY_DOMAIN_HINTS,
)
EDITORIAL_PATH_HINTS = ("/blog/", "/insights/", "/news/", "/articles/", "/resources/")
LISTICLE_TITLE_RE = re.compile(r"^(?:top\s+\d+|best\s+\d+|list\s+of\b|\d+\s+(?:best|top)\b)", re.I)


def is_editorial_listicle(url: str, title: str, snippet: str = "") -> bool:
    """Identify listicle search hits that describe a market, not the company itself."""
    path = urlparse(url).path.lower()
    if not any(hint in f"{path.rstrip('/')}/" for hint in EDITORIAL_PATH_HINTS):
        return False
    headline = " ".join([title, snippet]).strip()
    return bool(LISTICLE_TITLE_RE.search(headline))


def is_business_directory(url: str, title: str = "", snippet: str = "", page_text: str = "") -> bool:
    """Conservatively identify likely directory/profile pages, including lesser-known hosts.

    False positives are less harmful than importing a directory's domain as if it were a
    company's official website, so generic listing/profile signals intentionally count.
    """
    host = normalized_domain(url)
    if any(hint in host for hint in DIRECTORY_DOMAIN_HINTS):
        return True

    parsed = urlparse(url)
    path = parsed.path.lower().replace("_", "-")
    explicit_path_signals = (
        "/business-directory", "/company-directory", "/business-listing", "/company-profile",
        "/company-profiles", "/directory/", "/listings/", "/vendor-profile", "/vendor-listing",
        "/startup-profile", "/business-profile", "/reviews/",
    )
    if any(signal in path for signal in explicit_path_signals):
        return True

    text = " ".join([title, snippet, page_text[:1800]]).lower()
    directory_phrases = (
        "business directory", "company directory", "company profile", "business listing",
        "startup directory", "vendor directory", "company reviews", "business reviews",
        "top companies in", "companies in this directory", "find companies", "listed companies",
        "compare companies", "reviews and ratings", "company listings",
    )
    return any(phrase in text for phrase in directory_phrases)


def find_official_website_link(snapshot: WebsiteSnapshot, source_url: str, company_name: str) -> str | None:
    """Extract a likely official-site link from a directory page; never guess a URL from a name alone."""
    source_domain = normalized_domain(source_url)
    name_tokens = {t.lower() for t in re.findall(r"[a-zA-Z0-9]+", company_name) if len(t) > 2}
    choices: list[tuple[int, str]] = []
    for link in snapshot.links:
        url = link.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        domain = normalized_domain(url)
        if not domain or domain == source_domain:
            continue
        if any(blocked in domain for blocked in BLOCKED_OFFICIAL_LINK_HOSTS):
            continue
        text = link.get("text", "").lower()
        path = parsed.path.lower()
        if any(word in f"{text} {path}" for word in ["login", "signin", "sign-in", "contact", "privacy", "terms", "linkedin", "facebook"]):
            continue
        score = 0
        if any(phrase in text for phrase in ["official website", "company website", "visit website", "visit site", "website", "official site"]):
            score += 5
        if any(word in text for word in ["web site", "homepage"]):
            score += 3
        label_and_domain = (text + " " + domain).lower()
        if name_tokens and sum(1 for token in name_tokens if token in label_and_domain) >= min(2, len(name_tokens)):
            score += 2
        # Keep an explicit link only; don't synthesize domains from company names.
        if score > 0:
            choices.append((score, url))
    if not choices:
        return None
    choices.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return choices[0][1]


def evidence_from_text(source_url: str, source_type: str, excerpt: str) -> Evidence:
    compact = " ".join(unescape(excerpt).split())[:3000]
    fingerprint = f"{canonical_url(source_url)}\n{compact}"
    return Evidence(
        evidence_id=stable_id("ev", fingerprint),
        source_url=source_url,
        source_type=source_type,
        retrieved_at=datetime.now(timezone.utc),
        excerpt=compact,
        content_hash=hashlib.sha256(compact.encode("utf-8")).hexdigest(),
    )


class SearchProvider(Protocol):
    async def search(self, query: str, max_results: int = 20) -> list[SearchResult]: ...


class WebsiteReader(Protocol):
    async def fetch(self, url: str) -> WebsiteSnapshot: ...

    async def verify(self, url: str) -> WebsiteSnapshot: ...


class TestModeSearchProvider:
    def __init__(self, fallback: SearchProvider, live: SearchProvider) -> None:
        self.fallback = fallback
        self.live = live

    async def search(self, query: str, max_results: int = 20) -> list[SearchResult]:
        provider = self.live if current_budget() is not None else self.fallback
        return await provider.search(query, max_results)


class TestModeWebsiteReader:
    def __init__(self, fallback: WebsiteReader, live: WebsiteReader) -> None:
        self.fallback = fallback
        self.live = live

    async def fetch(self, url: str) -> WebsiteSnapshot:
        reader = self.live if current_budget() is not None else self.fallback
        return await reader.fetch(url)

    async def verify(self, url: str) -> WebsiteSnapshot:
        reader = self.live if current_budget() is not None else self.fallback
        return await reader.verify(url)


class BrowserbaseSearchProvider:
    """Search with Browserbase's structured Web Search REST API."""

    API_URL = "https://api.browserbase.com/v1/search"

    def __init__(
        self,
        api_key: str,
        timeout: float = 30.0,
        mode: str = "fast",
        max_concurrency: int = 1,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.mode = mode if mode in {"fast"} else "fast"
        self._slots = asyncio.Semaphore(max(1, max_concurrency))

    async def search(self, query: str, max_results: int = 20) -> list[SearchResult]:
        budget = current_budget()
        usage = current_usage()
        async with self._slots:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                body: dict[str, Any] | None = None
                for attempt in range(3):
                    if budget is not None and not budget.reserve_browserbase():
                        return []
                    if usage is not None:
                        usage.increment("browserbase_search_requests")
                    try:
                        response = await client.post(
                            self.API_URL,
                            headers={"X-BB-API-Key": self.api_key},
                            json={"provider": "parallel", "query": query[:200], "mode": self.mode, "numResults": min(max_results, 25)},
                        )
                        response.raise_for_status()
                        body = response.json()
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout):
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.4 * (2 ** attempt))
                if body is None:
                    return []
        output: list[SearchResult] = []
        for item in body.get("results", []):
            url = item.get("url")
            if not url:
                continue
            description = next(
                (
                    item.get(key)
                    for key in ("snippet", "description", "summary", "text", "content")
                    if isinstance(item.get(key), str) and item.get(key).strip()
                ),
                "",
            )
            extra = " ".join(filter(None, [description, item.get("author"), item.get("publishedDate")]))
            output.append(
                SearchResult(
                    title=(item.get("title") or normalized_domain(url))[:240],
                    url=url,
                    snippet=extra[:1000],
                    source_type="web_search_result",
                )
            )
        return output


class MockSearchProvider:
    """Deterministic fixtures for development and official-scenario testing."""

    def __init__(self, target_leads: int = 50) -> None:
        self.results = [
            SearchResult(
                title="Acme AI Private Limited",
                url="https://acme-ai.example/about",
                snippet="Acme AI Private Limited is a B2B AI software company based in Bengaluru. It builds machine-learning automation products for enterprises.",
                source_type="business_directory",
            ),
            SearchResult(
                title="Acme AI Private Limited - Product Site",
                url="https://acme-automation.example/company",
                snippet="Official product site for Acme AI Private Limited, an enterprise AI automation software company in Bengaluru.",
                source_type="business_directory",
            ),
            SearchResult(
                title="Deadstack AI Private Limited",
                url="https://deadstack-ai.invalid/",
                snippet="Deadstack AI Private Limited was listed as a B2B AI software business in Bengaluru.",
                source_type="business_directory",
            ),
            SearchResult(
                title="Gated Labs Private Limited",
                url="https://gated-labs.example/",
                snippet="Gated Labs Private Limited develops B2B AI software in Bengaluru.",
                source_type="business_directory",
            ),
            SearchResult(
                title="Nova Retail Private Limited",
                url="https://nova-retail.example/",
                snippet="Nova Retail Private Limited is a clothing retailer in Bengaluru.",
                source_type="business_directory",
            ),
        ]
        # Generate enough synthetic fixture leads to exercise the configured target
        # while retaining the four named edge-case fixtures above.
        synthetic_count = max(0, target_leads - 2)
        for index in range(1, synthetic_count + 1):
            name = f"Bengaluru AI Systems {index:03d} Private Limited"
            domain = f"bengaluru-ai-systems-{index:03d}.example"
            self.results.append(SearchResult(
                title=name,
                url=f"https://{domain}/",
                snippet=f"{name} is a B2B AI software company based in Bengaluru. It builds enterprise machine-learning automation products. 25 employees.",
                source_type="synthetic_mock",
            ))
        self._next_result = 0
        self._lock = asyncio.Lock()

    async def search(self, query: str, max_results: int = 20) -> list[SearchResult]:
        # Partition results across concurrent queries so the mock can satisfy the
        # configured target without repeatedly returning only the first page.
        async with self._lock:
            slot = self._next_result
            self._next_result += 1
        start = slot * max_results
        return self.results[start : start + max_results]


class PublicWebsiteReader:
    """Bounded HTML reader with conservative SSRF checks and no login bypass."""

    def __init__(self, timeout: float = 12.0, max_bytes: int = 1_000_000, max_redirects: int = 5) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.headers = {"User-Agent": "LeadResearchAgent/1.0 (+public-company-research; respectful rate limits)"}

    def _validate_public_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only HTTP(S) websites are permitted")
        host = parsed.hostname
        if not host or host.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("Local hostnames are not permitted")
        try:
            literal = ipaddress.ip_address(host)
            if not literal.is_global:
                raise ValueError("Non-public IP addresses are not permitted")
            return
        except ValueError as exc:
            # `ip_address` raises ValueError for ordinary hostnames too.
            if "not permitted" in str(exc):
                raise
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise RuntimeError(f"DNS resolution failed: {exc}") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("Host did not resolve exclusively to public IP addresses")

    async def _request(self, url: str) -> tuple[str, int | None, str, str | None]:
        current = url
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            headers=self.headers,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        ) as client:
            for _ in range(self.max_redirects + 1):
                await asyncio.to_thread(self._validate_public_url, current)
                try:
                    async with client.stream("GET", current) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                return current, response.status_code, "", "Redirect had no location"
                            current = urljoin(current, location)
                            continue
                        content = b""
                        async for chunk in response.aiter_bytes():
                            content += chunk
                            if len(content) > self.max_bytes:
                                content = content[: self.max_bytes]
                                break
                        encoding = response.encoding or "utf-8"
                        try:
                            html = content.decode(encoding, errors="replace")
                        except LookupError:
                            html = content.decode("utf-8", errors="replace")
                        return str(response.url), response.status_code, html, None
                except httpx.TimeoutException as exc:
                    return current, None, "", f"timeout: {exc}"
                except httpx.HTTPError as exc:
                    # A transient transport error is not proof that a company is closed.
                    return current, None, "", f"transport_error: {exc}"
            return current, None, "", "Too many redirects"

    @staticmethod
    def _parse(url: str, final_url: str | None, status: int | None, html: str, error: str | None) -> WebsiteSnapshot:
        now = datetime.now(timezone.utc)
        if status in {404, 410}:
            outcome = "inactive"
        elif status is not None and 200 <= status < 300:
            outcome = "reachable"
        elif error and "DNS resolution failed" in error and any(
            phrase in error.lower() for phrase in ["name or service not known", "nodename nor servname", "no address associated", "nxdomain"]
        ):
            outcome = "inactive"
        else:
            outcome = "unknown"

        soup = BeautifulSoup(html or "", "html.parser")
        script_app = bool(soup.find("div", id=re.compile(r"^(root|app|__next)$")))
        raw_text = " ".join(soup.get_text(" ", strip=True).split())
        render_candidate = len(raw_text) < 180 and (script_app or bool(soup.find("script")))
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        h1_tag = soup.find("h1")
        h1 = h1_tag.get_text(" ", strip=True) if h1_tag else ""
        description_tag = soup.find("meta", attrs={"name": "description"})
        meta_description = (description_tag.get("content") or "") if description_tag else ""
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ", strip=True).split())[:12000]
        links: list[dict[str, str]] = []
        for anchor in soup.find_all("a", href=True)[:250]:
            href = urljoin(final_url or url, str(anchor.get("href")))
            label = " ".join(anchor.get_text(" ", strip=True).split())[:180]
            links.append({"url": href, "text": label})
        contact_access = ContactAccess.SKIPPED_REQUIRES_LOGIN if status in {401, 403} else ContactAccess.UNKNOWN
        if status not in {401, 403}:
            for link in links:
                combined = f"{link['text']} {link['url']}".lower()
                if "contact" in combined:
                    if any(token in combined for token in ["login", "log-in", "signin", "sign-in", "auth"]):
                        contact_access = ContactAccess.SKIPPED_REQUIRES_LOGIN
                    else:
                        contact_access = ContactAccess.PUBLIC
                    break
        lowered = (text + " " + (html or "")[:5000]).lower()
        if soup.find("input", attrs={"type": "password"}) is not None:
            contact_access = ContactAccess.SKIPPED_REQUIRES_LOGIN
        login_indicators = ["sign in to continue", "log in to view", "authentication required", "please log in", "login required"]
        if any(marker in lowered for marker in login_indicators) and contact_access != ContactAccess.PUBLIC:
            contact_access = ContactAccess.SKIPPED_REQUIRES_LOGIN
        challenge_indicators = [
            "captcha", "verify you are human", "verify that you are human", "checking your browser",
            "security check", "unusual traffic", "access denied", "robot check",
        ]
        needs_browser = (
            render_candidate
            and outcome == "reachable"
            and contact_access != ContactAccess.SKIPPED_REQUIRES_LOGIN
            and not any(marker in lowered for marker in challenge_indicators)
        )
        return WebsiteSnapshot(
            requested_url=url,
            final_url=final_url,
            status_code=status,
            title=title[:240],
            h1=h1[:240],
            meta_description=meta_description[:1000],
            text=text,
            links=links,
            fetched_at=now,
            outcome=outcome,
            error=error,
            contact_access=contact_access,
            needs_browser=needs_browser,
        )

    async def fetch(self, url: str) -> WebsiteSnapshot:
        usage = current_usage()
        if usage is not None:
            usage.increment("direct_fetch_requests")
        try:
            final_url, status, html, error = await self._request(url)
            return self._parse(url, final_url, status, html, error)
        except (ValueError, RuntimeError) as exc:
            status = None
            outcome = "inactive" if "DNS resolution failed" in str(exc) and any(
                phrase in str(exc).lower() for phrase in ["name or service not known", "nodename nor servname", "no address associated", "nxdomain"]
            ) else "unknown"
            return WebsiteSnapshot(requested_url=url, status_code=status, outcome=outcome, error=str(exc))

    async def verify(self, url: str) -> WebsiteSnapshot:
        # Kept as a separate method so production implementations can use a separate
        # verification schedule/client and do not accidentally trust discovery-time data.
        return await self.fetch(url)


class BrowserbaseWebsiteReader:
    """Fetches pages through Browserbase, using a short-lived browser for JS-only pages."""

    FETCH_URL = "https://api.browserbase.com/v1/fetch"
    SESSIONS_URL = "https://api.browserbase.com/v1/sessions"

    def __init__(self, api_key: str, timeout: float = 30.0, max_browser_concurrency: int = 1) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._browser_slots = asyncio.Semaphore(max_browser_concurrency)
        self._url_validator = PublicWebsiteReader(timeout=timeout)

    @staticmethod
    def _needs_browser(html: str) -> bool:
        soup = BeautifulSoup(html or "", "html.parser")
        text = " ".join(soup.get_text(" ", strip=True).split())
        script_app = bool(soup.find("div", id=re.compile(r"^(root|app|__next)$")))
        return len(text) < 180 and (script_app or bool(soup.find("script")))

    async def _fetch_raw(self, url: str) -> tuple[str, int | None, str, str | None]:
        current = url
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for _ in range(6):
                await asyncio.to_thread(self._url_validator._validate_public_url, current)
                budget = current_budget()
                if budget is not None and not budget.reserve_browserbase():
                    return current, None, "", "Test-mode Browserbase request budget exhausted"
                usage = current_usage()
                if usage is not None:
                    usage.increment("browserbase_fetch_requests")
                response = await client.post(
                    self.FETCH_URL,
                    headers={"X-BB-API-Key": self.api_key},
                    json={"url": current, "allowRedirects": False, "allowInsecureSsl": False, "proxies": False, "format": "raw"},
                )
                response.raise_for_status()
                body = response.json()
                status = body.get("statusCode")
                headers = {str(k).lower(): str(v) for k, v in (body.get("headers") or {}).items()}
                if status in {301, 302, 303, 307, 308}:
                    location = headers.get("location")
                    if not location:
                        return current, status, "", "Redirect did not include a location header"
                    current = urljoin(current, location)
                    continue
                content = body.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content)
                error = body.get("message") or body.get("error")
                return current, status, content[:1_000_000], str(error) if error else None
        return current, None, "", "Too many redirects"

    async def _fetch_in_browser(self, url: str) -> tuple[str | None, int | None, str | None]:
        from playwright.async_api import async_playwright

        async with self._browser_slots:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                budget = current_budget()
                if budget is not None and not budget.reserve_browserbase():
                    return None, None, None
                usage = current_usage()
                if usage is not None:
                    usage.increment("browserbase_session_requests")
                response = await client.post(
                    self.SESSIONS_URL,
                    headers={"X-BB-API-Key": self.api_key},
                    json={"timeout": 60, "keepAlive": False},
                )
                response.raise_for_status()
                session = response.json()

            async with async_playwright() as playwright:
                browser = await playwright.chromium.connect_over_cdp(session["connectUrl"])
                try:
                    context = browser.contexts[0]
                    page = context.pages[0] if context.pages else await context.new_page()

                    async def allow_public_request(route: Any) -> None:
                        request_url = route.request.url
                        try:
                            await asyncio.to_thread(self._url_validator._validate_public_url, request_url)
                        except (ValueError, RuntimeError):
                            await route.abort()
                        else:
                            await route.continue_()

                    await page.route("**/*", allow_public_request)
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
                    html = await page.content()
                    status = response.status if response else None
                    return page.url, status, html
                finally:
                    await browser.close()

    async def fetch(self, url: str) -> WebsiteSnapshot:
        try:
            final_url, status, html, error = await self._fetch_raw(url)
            if status is not None and 200 <= status < 300 and self._needs_browser(html):
                browser_url, browser_status, browser_html = await self._fetch_in_browser(final_url)
                if browser_html:
                    final_url, status, html = browser_url or final_url, browser_status or status, browser_html
            return PublicWebsiteReader._parse(url, final_url, status, html, error)
        except (ValueError, RuntimeError, httpx.HTTPError, KeyError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            outcome = "inactive" if "DNS resolution failed" in message and any(
                phrase in message.lower()
                for phrase in ["name or service not known", "nodename nor servname", "no address associated", "nxdomain"]
            ) else "unknown"
            return WebsiteSnapshot(requested_url=url, status_code=None, outcome=outcome, error=message)

    async def render(self, url: str) -> WebsiteSnapshot:
        """Render a URL already identified as a JS shell without a redundant raw fetch."""
        try:
            # Validate before asking Browserbase to open the URL. The page route below
            # validates redirects and subresource requests as well.
            await asyncio.to_thread(self._url_validator._validate_public_url, url)
            final_url, status, html = await self._fetch_in_browser(url)
            if not html:
                return WebsiteSnapshot(
                    requested_url=url,
                    final_url=final_url,
                    status_code=status,
                    outcome="unknown",
                    error="Browserbase returned no rendered page content",
                )
            return PublicWebsiteReader._parse(url, final_url, status, html, None)
        except (ValueError, RuntimeError, httpx.HTTPError, KeyError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            outcome = "inactive" if "DNS resolution failed" in message and any(
                phrase in message.lower()
                for phrase in ["name or service not known", "nodename nor servname", "no address associated", "nxdomain"]
            ) else "unknown"
            return WebsiteSnapshot(requested_url=url, status_code=None, outcome=outcome, error=message)

    async def verify(self, url: str) -> WebsiteSnapshot:
        return await self.fetch(url)


class HybridWebsiteReader:
    """Fetch directly first and render only reachable JavaScript shells with Browserbase."""

    def __init__(self, direct: WebsiteReader, browserbase: WebsiteReader) -> None:
        self.direct = direct
        self.browserbase = browserbase

    @staticmethod
    def _direct_network_failure(snapshot: WebsiteSnapshot) -> bool:
        if snapshot.outcome != "unknown":
            return False
        error = (snapshot.error or "").lower()
        return (
            error.startswith(("dns resolution failed", "transport_error:", "timeout:"))
            or snapshot.status_code in {500, 502, 503, 504}
        )

    async def fetch(self, url: str) -> WebsiteSnapshot:
        direct_snapshot = await self.direct.fetch(url)
        if self._direct_network_failure(direct_snapshot):
            try:
                return await self.browserbase.fetch(url)
            except Exception:
                return direct_snapshot
        if direct_snapshot.outcome != "reachable" or not direct_snapshot.needs_browser:
            return direct_snapshot
        try:
            render = getattr(self.browserbase, "render", None)
            rendered = await render(url) if callable(render) else await self.browserbase.fetch(url)
        except Exception:
            return direct_snapshot
        if rendered.outcome == "reachable" and len(rendered.text) > len(direct_snapshot.text):
            return rendered
        return direct_snapshot

    async def verify(self, url: str) -> WebsiteSnapshot:
        # Availability checks are independent of discovery. If direct HTTP is unavailable
        # at the network layer, use Browserbase Fetch as a fallback instead of marking a
        # live site unknown solely because this runtime cannot resolve or reach it.
        direct_snapshot = await self.direct.verify(url)
        if not self._direct_network_failure(direct_snapshot):
            return direct_snapshot
        try:
            return await self.browserbase.verify(url)
        except Exception:
            return direct_snapshot


class MockWebsiteReader:
    """Offline website fixtures; never makes network calls."""

    PAGES: dict[str, tuple[int, str]] = {
        "acme-ai.example": (
            200,
            """<html><head><title>Acme AI Private Limited | Enterprise AI</title><meta name='description' content='B2B AI software and machine-learning automation for enterprise teams in Bengaluru.'></head><body><h1>Acme AI Private Limited</h1><p>We build B2B AI software and machine-learning automation products for enterprise teams in Bengaluru, India.</p><a href='/about'>About</a><a href='/contact'>Contact</a></body></html>""",
        ),
        "acme-automation.example": (
            200,
            """<html><head><title>Acme AI Private Limited - Automation</title></head><body><h1>Acme AI Private Limited</h1><p>Enterprise B2B AI software and machine-learning automation company located in Bengaluru.</p><a href='/company'>Company</a></body></html>""",
        ),
        "gated-labs.example": (
            200,
            """<html><head><title>Gated Labs Private Limited</title></head><body><h1>Gated Labs Private Limited</h1><p>We develop B2B AI software products for companies in Bengaluru.</p><a href='/contact/login'>Contact us</a></body></html>""",
        ),
        "nova-retail.example": (
            200,
            """<html><head><title>Nova Retail Private Limited</title></head><body><h1>Nova Retail Private Limited</h1><p>Clothing retail chain and fashion shop in Bengaluru.</p><a href='/contact'>Contact</a></body></html>""",
        ),
        "deadstack-ai.invalid": (404, "<html><title>Not Found</title><body>404 not found</body></html>"),
    }

    def _resolve(self, url: str) -> WebsiteSnapshot:
        domain = normalized_domain(url)
        path = urlparse(url).path.lower()
        if domain == "gated-labs.example" and path.startswith("/contact"):
            html = "<html><title>Sign in required</title><body><form><input type='password'></form><p>Authentication required. Please log in.</p></body></html>"
            return PublicWebsiteReader._parse(url, url, 401, html, None)
        synthetic = re.fullmatch(r"bengaluru-ai-systems-(\d{3})\.example", domain)
        if synthetic:
            index = int(synthetic.group(1))
            name = f"Bengaluru AI Systems {index:03d} Private Limited"
            html = (
                f"<html><head><title>{name}</title>"
                "<meta name='description' content='B2B AI software company based in Bengaluru, building "
                "enterprise machine-learning automation products.'></head>"
                f"<body><h1>{name}</h1><p>B2B AI software company based in Bengaluru. We build enterprise "
                "machine-learning automation products. 25 employees.</p></body></html>"
            )
            return PublicWebsiteReader._parse(url, url, 200, html, None)
        status, html = self.PAGES.get(domain, (404, "<html><title>Not Found</title></html>"))
        return PublicWebsiteReader._parse(url, url, status, html, None)

    async def fetch(self, url: str) -> WebsiteSnapshot:
        await asyncio.sleep(0)
        return self._resolve(url)

    async def verify(self, url: str) -> WebsiteSnapshot:
        await asyncio.sleep(0)
        return self._resolve(url)


def build_source_adapters(settings: Settings) -> tuple[SearchProvider, WebsiteReader]:
    if settings.mock_mode and not settings.browserbase_api_key:
        return MockSearchProvider(settings.number_of_leads), MockWebsiteReader()
    if not settings.browserbase_api_key:
        raise RuntimeError("BROWSERBASE_API_KEY is required when MOCK_MODE=false")
    live_search = BrowserbaseSearchProvider(
        settings.browserbase_api_key,
        timeout=settings.request_timeout_seconds,
        mode=settings.browserbase_search_mode,
        max_concurrency=settings.browserbase_max_concurrency,
    )
    browserbase_reader = BrowserbaseWebsiteReader(
        settings.browserbase_api_key,
        timeout=settings.request_timeout_seconds,
        max_browser_concurrency=settings.browserbase_max_concurrency,
    )
    live_reader = HybridWebsiteReader(
        PublicWebsiteReader(timeout=settings.request_timeout_seconds),
        browserbase_reader,
    )
    if settings.mock_mode:
        return (
            TestModeSearchProvider(MockSearchProvider(settings.number_of_leads), live_search),
            TestModeWebsiteReader(MockWebsiteReader(), live_reader),
        )
    return live_search, live_reader
