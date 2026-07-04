"""Phase 2: lightweight GET probe — first bytes only, no full downloads."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yaml

from src.blocklist import BlocklistFilter
from src.database import Database
from src.settings import Settings
from src.utils import file_type_from_url, make_record_id

log = logging.getLogger(__name__)

PPTX_SIGNATURE = b"PK\x03\x04"
PPT_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

VALID_CONTENT_TYPES = {
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/octet-stream",
    "application/x-mspowerpoint",
    "binary/octet-stream",
}


class Phase2Prefilter:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.concurrency = int(settings.get("phase2", "concurrency", default=400))
        self.timeout = int(settings.get("phase2", "timeout_sec", default=10))
        self.retries = int(settings.get("phase2", "retries", default=1))
        self.min_size = int(settings.get("phase2", "min_content_length", default=10240))
        self.max_size = int(settings.get("phase2", "max_content_length", default=209715200))
        self.require_keyword = bool(settings.get("phase2", "require_category_keyword", default=False))
        self.user_agent = settings.get(
            "phase2",
            "user_agent",
            default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        self.probe_bytes = int(settings.get("phase2", "probe_bytes", default=512))
        self.verify_signature = bool(settings.get("phase2", "verify_signature", default=True))
        self.keywords = self._load_keywords()
        self.blocklist = self._load_blocklist()
        self._seq = 0

    def _load_keywords(self) -> list[str]:
        path_cfg = self.settings.get("category_keywords", default="config/category_keywords.yaml")
        path = Path(path_cfg)
        if not path.is_absolute():
            path = self.settings.config_path.parent.parent / path
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [k.lower() for k in data.get("keywords", [])]

    def _load_blocklist(self) -> BlocklistFilter:
        root = self.settings.config_path.parent.parent
        paths = []
        for p in self.settings.get("blocklists", default=[]):
            path = Path(p)
            if not path.is_absolute():
                path = root / path
            paths.append(path)
        return BlocklistFilter(paths)

    def _match_category(self, text: str) -> str | None:
        lower = text.lower()
        for kw in self.keywords:
            if kw in lower:
                return kw
        return None

    def _next_record_id(self, batch_id: str) -> str:
        self._seq += 1
        return make_record_id(batch_id, self._seq)

    async def _access_check(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> tuple[int | None, str, int | None, str, bytes]:
        http_status = None
        content_type = ""
        content_length = None
        note = ""
        body = b""

        headers = {"Range": f"bytes=0-{max(self.probe_bytes - 1, 0)}"}
        for attempt in range(self.retries + 1):
            try:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    allow_redirects=True,
                ) as resp:
                    http_status = resp.status
                    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        content_length = int(cl)
                    elif resp.status == 206:
                        cr = resp.headers.get("Content-Range", "")
                        if "/" in cr:
                            total = cr.split("/")[-1]
                            if total.isdigit():
                                content_length = int(total)
                    if resp.status >= 400:
                        note = f"HTTP_{resp.status}"
                    elif resp.status in (200, 206):
                        body = await resp.content.read(self.probe_bytes)
                    else:
                        note = f"HTTP_{resp.status}"
                break
            except Exception as exc:
                note = str(exc)[:120]
                if attempt < self.retries:
                    await asyncio.sleep(0.2)

        return http_status, content_type, content_length, note, body

    async def _validate_one(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        row: dict,
        batch_id: str,
    ) -> dict:
        url = row["url"]
        source_url = row.get("source_url") or url
        context = " ".join(filter(None, [
            url, row.get("snippet", ""), row.get("page_title", ""), row.get("parent_page_url", ""),
        ]))

        async with sem:
            http_status, content_type, content_length, note, body = await self._access_check(session, url)

        blocked, block_reason = self.blocklist.check(
            source_url,
            organization=row.get("organization", ""),
            title=row.get("page_title", ""),
            filename=urlparse(url).path.split("/")[-1],
        )
        category = self._match_category(context)
        ftype = file_type_from_url(url)

        rejection = None
        if not is_presentation_url_ext(url):
            rejection = "BAD_EXTENSION"
        elif blocked:
            rejection = block_reason or "BLOCKLIST"
        elif http_status is None or http_status >= 400:
            rejection = note or "NOT_ACCESSIBLE"
        elif http_status not in (200, 206) or not body:
            rejection = note or "NOT_ACCESSIBLE"
        elif _is_html_type(content_type) or _looks_like_html(body):
            rejection = "HTML_NOT_FILE"
        elif content_type and not _content_type_ok(content_type, ftype):
            rejection = "BAD_CONTENT_TYPE"
        elif self.verify_signature and not _detect_signature(body, ftype):
            rejection = "BAD_SIGNATURE"
        elif content_length is not None:
            if content_length < self.min_size:
                rejection = "TOO_SMALL"
            elif content_length > self.max_size:
                rejection = "TOO_LARGE"

        if not rejection and self.require_keyword and not category:
            rejection = "NO_CATEGORY_MATCH"

        now = datetime.now(timezone.utc).isoformat()
        audit_id = Database.new_audit_id()
        signature = _detect_signature(body, ftype) if body else ""
        base = {
            "file_type": ftype,
            "content_type": content_type,
            "content_length": content_length,
            "http_status": http_status,
            "url_accessible": "PASS" if not rejection else "FAIL",
            "category_match": category or "",
            "validated_at": now,
            "audit_id": audit_id,
            "batch_id": batch_id,
            "source_url": source_url,
            "file_verified": 0 if rejection else 1,
            "file_signature": signature,
        }

        if rejection:
            return {"id": row["id"], "status": "rejected", "rejection_reason": rejection, **base}

        return {
            "id": row["id"],
            "status": "qualified",
            "rejection_reason": None,
            "record_id": self._next_record_id(batch_id),
            **base,
        }

    async def run_batch(self, batch_id: str, limit: int = 5000) -> dict[str, int]:
        rows = self.db.claim_pending(limit)
        if not rows:
            return {"qualified": 0, "rejected": 0}

        self.db.create_batch(batch_id)
        self._seq = self.db.max_record_seq(batch_id)
        sem = asyncio.Semaphore(self.concurrency)
        db_sem = asyncio.Semaphore(min(32, self.concurrency))
        headers = {"User-Agent": self.user_agent}
        counts = {"qualified": 0, "rejected": 0}
        audit_path = self.settings.data_dir / "audit" / f"{batch_id}.jsonl"

        async with aiohttp.ClientSession(headers=headers) as session:
            tasks = [self._validate_one(session, sem, row, batch_id) for row in rows]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                cid = result.pop("id")
                status = result.pop("status")
                result["status"] = status
                async with db_sem:
                    self.db.update_candidate(cid, result)
                    counts[status] += 1
                    self.db.write_audit(audit_path, {
                        "audit_id": result.get("audit_id"),
                        "action": status,
                        "record_id": result.get("record_id"),
                        "source_url": result.get("source_url"),
                        "reason": result.get("rejection_reason") or "PASS",
                        "timestamp": result.get("validated_at"),
                    })

        return counts

    def run(self, batch_id: str, limit: int = 5000) -> dict[str, int]:
        return asyncio.run(self.run_batch(batch_id, limit))


def is_presentation_url_ext(url: str) -> bool:
    return urlparse(url).path.lower().endswith((".ppt", ".pptx"))


def _is_html_type(content_type: str) -> bool:
    ct = content_type.lower().split(";")[0].strip()
    return ct.startswith("text/html") or ct in ("text/plain", "application/xhtml+xml")


def _content_type_ok(content_type: str, file_type: str) -> bool:
    if content_type in VALID_CONTENT_TYPES:
        return True
    if file_type in ("ppt", "pptx") and ("powerpoint" in content_type or "presentation" in content_type):
        return True
    return not content_type or content_type == "application/octet-stream"


def _looks_like_html(body: bytes) -> bool:
    if not body:
        return False
    start = body.lstrip()[:32].lower()
    return start.startswith((b"<!doctype", b"<html", b"<head", b"<?xml"))


def _detect_signature(body: bytes, file_type: str) -> str:
    if file_type == "pptx" and body.startswith(PPTX_SIGNATURE):
        return "PK/ZIP"
    if file_type == "ppt" and body.startswith(PPT_SIGNATURE):
        return "OLE/PPT"
    return ""
