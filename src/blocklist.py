"""Blocklist filter for Fortune 500, elite universities, think tanks."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import yaml


def _normalize_host(host: str) -> str:
    host = host.lower().strip()
    return host[4:] if host.startswith("www.") else host


def _domain_matches(netloc: str, blocked: str) -> bool:
    host = _normalize_host(netloc)
    blocked = _normalize_host(blocked)
    if not host or not blocked:
        return False
    return host == blocked or host.endswith(f".{blocked}")


def _name_matches(text: str, name: str) -> bool:
    if not name or len(name) < 3:
        return False
    return re.search(rf"\b{re.escape(name.lower())}\b", text.lower()) is not None


class BlocklistFilter:
    def __init__(self, paths: list[Path]):
        self.entries: list[dict] = []
        for path in paths:
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    self.entries.append(yaml.safe_load(f) or {})

    def check(
        self,
        source_url: str,
        organization: str = "",
        title: str = "",
        filename: str = "",
    ) -> tuple[bool, str | None]:
        netloc = urlparse(source_url).netloc
        combined = f"{source_url} {organization} {title} {filename}"

        for entry in self.entries:
            orgs = (
                entry.get("companies")
                or entry.get("institutions")
                or entry.get("organizations")
                or []
            )
            reason = (
                "BLOCKLIST_F500"
                if "companies" in entry
                else "BLOCKLIST_UNIVERSITY"
                if "institutions" in entry
                else "BLOCKLIST_THINKTANK"
            )
            for org in orgs:
                names = [org.get("name", "")] + org.get("aliases", [])
                domains = org.get("domains", [])
                for name in names:
                    if _name_matches(combined, name):
                        return True, reason
                for domain in domains:
                    if _domain_matches(netloc, domain):
                        return True, reason
        return False, None
