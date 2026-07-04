"""Shared helpers."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
SRC_RE = re.compile(r'\bsrc=["\']([^"\']+)["\']', re.I)

TRACKING_QUERY_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_eid", "ref", "source", "spm",
})


def canonicalize_url(url: str) -> str:
    """Normalize a URL for deduplication (scheme/host/path/query)."""
    raw = url.strip()
    if not raw:
        return raw

    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.lower()

    scheme = parsed.scheme.lower()
    if scheme == "http":
        scheme = "https"
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if scheme == "http" and host.endswith(":80"):
        host = host[:-3]
    elif scheme == "https" and host.endswith(":443"):
        host = host[:-4]

    path = unquote(parsed.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    path = path.lower()

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_PARAMS
    ]
    query = urlencode(sorted(query_pairs)) if query_pairs else ""

    return urlunparse((scheme, host, path, "", query, ""))


def domain_from_url(url: str) -> str:
    host = urlparse(canonicalize_url(url)).netloc.lower().strip()
    return host[4:] if host.startswith("www.") else host


def is_presentation_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".ppt", ".pptx"))


def file_type_from_url(url: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    return ext.lstrip(".") if ext else ""


def extract_file_links(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for pattern in (HREF_RE, SRC_RE):
        for match in pattern.finditer(html):
            href = match.group(1).strip()
            if href.startswith(("mailto:", "javascript:", "#", "data:")):
                continue
            absolute = urljoin(base_url, href)
            if not is_presentation_url(absolute):
                continue
            key = canonicalize_url(absolute)
            if key not in seen:
                seen.add(key)
                links.append(absolute)
    return links


def make_batch_id(seq: int) -> str:
    date = datetime.now().strftime("%Y%m%d")
    return f"BATCH-{date}-{seq:03d}"


def make_record_id(batch_id: str, seq: int) -> str:
    return f"{batch_id}_{seq:08d}"
