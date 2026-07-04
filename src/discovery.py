"""Phase 1: search-driven URL discovery — no file downloads, no seed files."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import aiohttp
import yaml

from src.blocklist import BlocklistFilter
from src.database import Database
from src.settings import Settings
from src.utils import domain_from_url, extract_file_links, canonicalize_url, is_presentation_url

log = logging.getLogger(__name__)

BING_LINK_RE = re.compile(r'<li class="b_algo".*?<a href="([^"]+)"', re.I | re.S)
GOOGLE_LINK_RE = re.compile(r'href="/url\?q=([^&"]+)', re.I)
DDG_LITE_LINK_RE = re.compile(r"uddg=([^&\"]+)", re.I)
DDG_LINK_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"', re.I)
UDDG_RE = re.compile(r"uddg=([^&\"']+)", re.I)
GENERIC_FILE_RE = re.compile(r'href="(https?://[^"]+\.pptx?(?:\?[^"]*)?)"', re.I)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class Discovery:
    """Search engines + light same-domain crawl. Zero seed files."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.concurrency = int(settings.get("discovery", "concurrency", default=300))
        self.crawl_concurrency = int(settings.get("discovery", "crawl_concurrency", default=80))
        self.timeout = int(settings.get("discovery", "timeout_sec", default=12))
        self.max_depth = int(settings.get("discovery", "max_depth", default=1))
        self.max_pages = int(settings.get("discovery", "max_pages_per_domain", default=25))
        self.search_delay = float(settings.get("discovery", "search_delay_sec", default=1.0))
        self.results_per_query = int(settings.get("discovery", "results_per_query", default=30))
        self.engines = list(settings.get("discovery", "engines", default=["duckduckgo", "bing"]))
        self.queries_per_run = int(settings.get("discovery", "queries_per_run", default=40))
        self.user_agent = settings.get("discovery", "user_agent", default=BROWSER_UA)
        self.keywords = self._load_keywords()
        self.blocklist = self._load_blocklist()
        self._query_offset = 0

    def _load_keywords(self) -> list[str]:
        path_cfg = self.settings.get("category_keywords", default="config/category_keywords.yaml")
        path = Path(path_cfg)
        if not path.is_absolute():
            path = self.settings.config_path.parent.parent / path
        if not path.exists():
            return ["presentation", "dashboard", "infographic", "data visualization"]
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return list(data.get("keywords", []))

    def _load_blocklist(self) -> BlocklistFilter:
        root = self.settings.config_path.parent.parent
        paths = []
        for p in self.settings.get("blocklists", default=[]):
            path = Path(p)
            if not path.is_absolute():
                path = root / path
            paths.append(path)
        return BlocklistFilter(paths)

    def _build_queries(self, extensions: tuple[str, ...]) -> list[str]:
        queries = [f"filetype:{ext} {kw}" for kw in self.keywords for ext in extensions]
        start = self._query_offset
        end = start + self.queries_per_run
        batch = queries[start:end] if start < len(queries) else queries[: self.queries_per_run]
        self._query_offset = end if end < len(queries) else 0
        return batch

    def _candidate_row(self, url: str, batch_id: str | None, method: str, **extra) -> dict:
        canonical = canonicalize_url(url)
        return {
            "url": canonical,
            "source_url": url,
            "domain": domain_from_url(canonical),
            "batch_id": batch_id,
            "discovery_method": method,
            "parent_page_url": extra.get("parent_page_url", ""),
            "snippet": extra.get("snippet", ""),
            "page_title": extra.get("page_title", ""),
        }

    def _accept(self, url: str, seen: set[str]) -> bool:
        canonical = canonicalize_url(url)
        if canonical in seen or not is_presentation_url(url):
            return False
        if self.blocklist.check(url)[0]:
            return False
        seen.add(canonical)
        return True

    async def _fetch_html(self, session: aiohttp.ClientSession, url: str) -> str | None:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return None
                ct = resp.headers.get("Content-Type", "").lower()
                if "html" not in ct and not url.endswith((".html", ".htm", "/")):
                    return None
                return await resp.text(errors="ignore")
        except Exception as exc:
            log.debug("fetch %s: %s", url, exc)
            return None

    async def _search_one(
        self,
        session: aiohttp.ClientSession,
        engine: str,
        query: str,
    ) -> tuple[list[str], list[str]]:
        """Return (file_urls, html_page_urls) from SERP."""
        if engine == "bing":
            url = f"https://www.bing.com/search?q={quote_plus(query)}&count={self.results_per_query}"
        elif engine == "duckduckgo":
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        elif engine == "duckduckgo-lite":
            url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
        else:
            url = f"https://www.google.com/search?q={quote_plus(query)}&num={self.results_per_query}"

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return [], []
                html = await resp.text(errors="ignore")
        except Exception as exc:
            log.debug("search %s: %s", engine, exc)
            return [], []

        patterns = {
            "bing": BING_LINK_RE,
            "google": GOOGLE_LINK_RE,
            "duckduckgo": DDG_LINK_RE,
            "duckduckgo-lite": DDG_LITE_LINK_RE,
        }
        pattern = patterns.get(engine, GENERIC_FILE_RE)
        files: list[str] = []
        pages: list[str] = []
        seen_raw: set[str] = set()

        def _collect(raw: str) -> None:
            if raw in seen_raw:
                return
            seen_raw.add(raw)
            resolved = _resolve_serp_link(raw)
            if not resolved:
                return
            if is_presentation_url(resolved):
                files.append(resolved)
            else:
                pages.append(resolved)

        for match in pattern.finditer(html):
            _collect(match.group(1))
        for match in UDDG_RE.finditer(html):
            _collect(f"https://duckduckgo.com/?uddg={match.group(1)}")
        for match in GENERIC_FILE_RE.finditer(html):
            _collect(match.group(1))
        return files, pages

    async def _light_crawl(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        start_pages: list[str],
        batch_id: str | None,
        query: str,
        seen_files: set[str],
        rows: list[dict],
    ) -> None:
        """Light same-domain BFS from search result pages."""
        by_domain: dict[str, list[tuple[str, int]]] = {}
        for page in start_pages:
            if is_presentation_url(page):
                continue
            dom = domain_from_url(page)
            by_domain.setdefault(dom, []).append((page, 0))

        visited: set[str] = set()
        for domain, queue in by_domain.items():
            domain_pages = 0
            while queue and domain_pages < self.max_pages:
                url, depth = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                domain_pages += 1

                async with sem:
                    html = await self._fetch_html(session, url)
                if not html:
                    continue

                title = _page_title(html)
                for link in extract_file_links(html, url):
                    if self._accept(link, seen_files):
                        rows.append(self._candidate_row(
                            link, batch_id, "crawl",
                            parent_page_url=url, page_title=title, snippet=query,
                        ))

                if depth >= self.max_depth:
                    continue
                for match in HREF_RE.finditer(html):
                    href = match.group(1).strip()
                    if href.startswith(("mailto:", "javascript:", "#")):
                        continue
                    next_url = urljoin(url, href)
                    if domain_from_url(next_url) != domain or is_presentation_url(next_url):
                        continue
                    if next_url not in visited:
                        queue.append((next_url, depth + 1))

    async def run(
        self,
        batch_id: str | None = None,
        extensions: tuple[str, ...] | None = None,
    ) -> int:
        if not bool(self.settings.get("discovery", "enabled", default=True)):
            return 0

        exts = extensions or ("pptx", "ppt")
        queries = self._build_queries(exts)
        if not queries:
            return 0

        headers = {"User-Agent": self.user_agent}
        rows: list[dict] = []
        seen_files: set[str] = set(self.db.existing_urls())
        crawl_sem = asyncio.Semaphore(self.crawl_concurrency)

        async with aiohttp.ClientSession(headers=headers) as session:
            for query in queries:
                page_batch: list[str] = []
                for engine in self.engines:
                    files, pages = await self._search_one(session, engine, query)
                    for f in files:
                        if self._accept(f, seen_files):
                            rows.append(self._candidate_row(
                                f, batch_id, engine, snippet=query,
                            ))
                    page_batch.extend(pages)
                if page_batch:
                    await self._light_crawl(
                        session, crawl_sem, page_batch[:30], batch_id, query, seen_files, rows,
                    )
                await asyncio.sleep(self.search_delay)

        return self.db.insert_candidates(rows)

    def run_sync(self, batch_id: str | None = None, extensions: tuple[str, ...] | None = None) -> int:
        return asyncio.run(self.run(batch_id, extensions))


def _page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    return m.group(1).strip()[:300] if m else ""


def _resolve_serp_link(raw: str) -> str | None:
    """Normalize search-engine redirect links to target URLs."""
    raw = unquote(raw.strip())
    if raw.startswith("//"):
        raw = "https:" + raw
    if "uddg=" in raw:
        qs = parse_qs(urlparse(raw).query)
        if "uddg" in qs:
            raw = unquote(qs["uddg"][0])
    elif "/url?q=" in raw or "q=" in raw and "google" in raw:
        qs = parse_qs(urlparse(raw).query)
        if "q" in qs:
            raw = unquote(qs["q"][0])
    if raw.startswith("http"):
        return raw
    return None
