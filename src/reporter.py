"""Generate CSV/Excel URL manifests and progress reports."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.database import Database
from src.settings import Settings
from src.utils import canonicalize_url

_ILLEGAL_XLSX_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _excel_safe(value) -> object:
    if isinstance(value, str):
        return _ILLEGAL_XLSX_CHARS.sub("", value)
    return value


class Reporter:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.columns = settings.get("reporting", "manifest_columns", default=[])

    def _rows(self, batch_id: str | None = None) -> list[dict]:
        rows = []
        seen: set[str] = set()
        for record in self.db.iter_qualified(batch_id):
            key = canonicalize_url(record.get("url") or record.get("source_url", ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "record_id": record.get("record_id", ""),
                "source_url": record.get("source_url", record.get("url", "")),
                "file_type": record.get("file_type", ""),
                "domain": record.get("domain", ""),
                "batch_id": record.get("batch_id", ""),
                "content_type": record.get("content_type", ""),
                "content_length": record.get("content_length", ""),
                "http_status": record.get("http_status", ""),
                "url_accessible": record.get("url_accessible", ""),
                "page_title": record.get("page_title", ""),
                "snippet": record.get("snippet", ""),
                "organization": record.get("organization", ""),
                "category_match": record.get("category_match", ""),
                "discovery_method": record.get("discovery_method", ""),
                "parent_page_url": record.get("parent_page_url", ""),
                "discovered_at": record.get("discovered_at", ""),
                "validated_at": record.get("validated_at", ""),
                "audit_id": record.get("audit_id", ""),
                "file_verified": record.get("file_verified", ""),
                "file_signature": record.get("file_signature", ""),
                "rejection_reason": "",
            })
        return rows

    def write_batch_manifest(self, batch_id: str) -> tuple[Path, Path | None]:
        manifest_dir = self.settings.data_dir / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        rows = self._rows(batch_id)
        df = pd.DataFrame(rows, columns=self.columns if self.columns else None)
        df = df.map(_excel_safe)
        csv_path = manifest_dir / f"{batch_id}.csv"
        df.to_csv(csv_path, index=False)
        xlsx_path = None
        if len(rows) <= 1_000_000:
            xlsx_path = manifest_dir / f"{batch_id}.xlsx"
            df.to_excel(xlsx_path, index=False, engine="openpyxl")
        return csv_path, xlsx_path

    def write_master_manifest(self) -> tuple[Path, Path | None]:
        manifest_dir = self.settings.data_dir / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        rows = self._rows()
        df = pd.DataFrame(rows, columns=self.columns if self.columns else None)
        df = df.map(_excel_safe)
        csv_path = manifest_dir / "MASTER_MANIFEST.csv"
        df.to_csv(csv_path, index=False)
        xlsx_path = None
        if len(rows) <= 1_000_000:
            xlsx_path = manifest_dir / "MASTER_MANIFEST.xlsx"
            df.to_excel(xlsx_path, index=False, engine="openpyxl")
        return csv_path, xlsx_path

    def write_progress(self) -> Path:
        report_dir = self.settings.data_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        qualified = self.db.qualified_count()
        pending = self.db.pending_count()
        target = self.settings.target_count
        pct = round(100 * qualified / target, 4) if target else 0
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "qualified_url_count": qualified,
            "target_count": target,
            "progress_pct": pct,
            "pending_candidates": pending,
            "remaining": max(0, target - qualified),
        }
        path = report_dir / "progress_latest.json"
        path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return path
