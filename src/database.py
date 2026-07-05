"""PostgreSQL / SQLite store for discovered URL records."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from src.utils import canonicalize_url, domain_from_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    qualified_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    domain TEXT,
    batch_id TEXT,
    status TEXT DEFAULT 'pending',
    rejection_reason TEXT,
    file_type TEXT,
    content_type TEXT,
    content_length INTEGER,
    http_status INTEGER,
    url_accessible TEXT,
    page_title TEXT,
    snippet TEXT,
    organization TEXT,
    category_match TEXT,
    discovery_method TEXT,
    parent_page_url TEXT,
    record_id TEXT UNIQUE,
    discovered_at TEXT NOT NULL,
    validated_at TEXT,
    audit_id TEXT,
    file_verified INTEGER,
    file_signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_batch ON candidates(batch_id);
CREATE INDEX IF NOT EXISTS idx_candidates_domain ON candidates(domain);
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    qualified_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS candidates (
    id SERIAL PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    domain TEXT,
    batch_id TEXT,
    status TEXT DEFAULT 'pending',
    rejection_reason TEXT,
    file_type TEXT,
    content_type TEXT,
    content_length BIGINT,
    http_status INTEGER,
    url_accessible TEXT,
    page_title TEXT,
    snippet TEXT,
    organization TEXT,
    category_match TEXT,
    discovery_method TEXT,
    parent_page_url TEXT,
    record_id TEXT UNIQUE,
    discovered_at TIMESTAMPTZ NOT NULL,
    validated_at TIMESTAMPTZ,
    audit_id TEXT,
    file_verified BOOLEAN,
    file_signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_batch ON candidates(batch_id);
CREATE INDEX IF NOT EXISTS idx_candidates_domain ON candidates(domain);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def _as_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    return dict(row)


class Database:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.is_postgres = database_url.startswith("postgresql")

    def init(self) -> None:
        if self.is_postgres:
            import psycopg

            with psycopg.connect(self.database_url) as conn:
                conn.execute(PG_SCHEMA)
                self._migrate(conn)
                conn.commit()
        else:
            path = self.database_url.replace("sqlite:///", "")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with __import__("sqlite3").connect(path) as conn:
                conn.executescript(SCHEMA)
                self._migrate(conn)
                conn.commit()

    def _migrate(self, conn) -> None:
        for col, col_type in (("file_verified", "INTEGER"), ("file_signature", "TEXT")):
            existing = self._columns(conn, "candidates")
            if col not in existing:
                conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} {col_type}")

    def _columns(self, conn, table: str) -> set[str]:
        if self.is_postgres:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            ).fetchall()
            return {_first(r) if not isinstance(r, dict) else r["column_name"] for r in rows}
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}

    @contextmanager
    def connect(self):
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
                yield conn
                conn.commit()
        else:
            import sqlite3

            path = self.database_url.replace("sqlite:///", "")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def create_batch(self, batch_id: str) -> None:
        with self.connect() as conn:
            if self.is_postgres:
                conn.execute(
                    "INSERT INTO batches (batch_id, created_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (batch_id, _utcnow()),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO batches (batch_id, created_at) VALUES (?, ?)",
                    (batch_id, _utcnow()),
                )

    def insert_candidates(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0

        unique_rows: dict[str, dict[str, Any]] = {}
        for row in rows:
            original = row.get("source_url") or row["url"]
            canonical = canonicalize_url(row["url"])
            if canonical in unique_rows:
                continue
            unique_rows[canonical] = {
                **row,
                "url": canonical,
                "source_url": original,
                "domain": row.get("domain") or domain_from_url(canonical),
            }

        inserted = 0
        now = _utcnow()
        with self.connect() as conn:
            for row in unique_rows.values():
                try:
                    params = (
                        row["url"],
                        row["source_url"],
                        row.get("domain", ""),
                        row.get("batch_id"),
                        row.get("discovery_method", "crawl"),
                        row.get("parent_page_url", ""),
                        row.get("snippet", ""),
                        row.get("page_title", ""),
                        now,
                    )
                    if self.is_postgres:
                        cur = conn.execute(
                            """
                            INSERT INTO candidates
                            (url, source_url, domain, batch_id, discovery_method,
                             parent_page_url, snippet, page_title, discovered_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (url) DO NOTHING
                            """,
                            params,
                        )
                        inserted += cur.rowcount
                    else:
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO candidates
                            (url, source_url, domain, batch_id, discovery_method,
                             parent_page_url, snippet, page_title, discovered_at)
                            VALUES (?,?,?,?,?,?,?,?,?)
                            """,
                            params,
                        )
                        inserted += cur.rowcount
                except Exception:
                    continue
        return inserted

    def total_url_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()
            return int(_first(row))

    def existing_urls(self, max_urls: int = 500_000) -> set[str]:
        """Canonical URLs already stored. Skips preload when table exceeds max_urls."""
        if self.total_url_count() > max_urls:
            return set()
        with self.connect() as conn:
            rows = conn.execute("SELECT url FROM candidates").fetchall()
        return {r["url"] for r in rows}

    def dedupe_existing(self) -> dict[str, int]:
        """Normalize stored URLs and remove duplicate canonical rows."""
        status_rank = {"qualified": 4, "checking": 3, "pending": 2, "rejected": 1}
        deleted = 0
        normalized = 0
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, url, status, discovered_at FROM candidates ORDER BY id"
            ).fetchall()

            groups: dict[str, list[tuple]] = {}
            for row in rows:
                rid = row["id"]
                url = row["url"]
                status = row["status"]
                discovered_at = row["discovered_at"]
                groups.setdefault(canonicalize_url(url), []).append(
                    (rid, url, status, discovered_at)
                )

            for canonical, items in groups.items():
                items.sort(
                    key=lambda item: (
                        -status_rank.get(item[2] or "", 0),
                        item[3] or "",
                        item[0],
                    )
                )
                keep_id, keep_url, _, _ = items[0]
                ph = "%s" if self.is_postgres else "?"
                for dup_id, _, _, _ in items[1:]:
                    conn.execute(f"DELETE FROM candidates WHERE id = {ph}", (dup_id,))
                    deleted += 1
                if keep_url != canonical:
                    conn.execute(
                        f"UPDATE candidates SET url = {ph} WHERE id = {ph}",
                        (canonical, keep_id),
                    )
                    normalized += 1

        return {"normalized": normalized, "deleted": deleted}

    def claim_pending(self, limit: int, file_types: list[str] | None = None) -> list[dict]:
        with self.connect() as conn:
            if file_types:
                ph = ",".join("%s" if self.is_postgres else "?" for _ in file_types)
                ext_filter = " AND (LOWER(url) LIKE '%.pptx' OR LOWER(url) LIKE '%.ppt')"
                if file_types == ["ppt", "pptx"]:
                    ext_filter = " AND (LOWER(url) LIKE '%.pptx' OR LOWER(url) LIKE '%.ppt')"

            if self.is_postgres:
                if file_types:
                    rows = conn.execute(
                        f"""
                        UPDATE candidates SET status = 'checking'
                        WHERE id IN (
                            SELECT id FROM candidates WHERE status = 'pending' {ext_filter}
                            ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
                        )
                        RETURNING *
                        """,
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        UPDATE candidates SET status = 'checking'
                        WHERE id IN (
                            SELECT id FROM candidates WHERE status = 'pending'
                            ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
                        )
                        RETURNING *
                        """,
                        (limit,),
                    ).fetchall()
            else:
                if file_types:
                    rows = conn.execute(
                        f"SELECT * FROM candidates WHERE status = 'pending' {ext_filter} ORDER BY id LIMIT ?",
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM candidates WHERE status = 'pending' ORDER BY id LIMIT ?",
                        (limit,),
                    ).fetchall()
                ids = [r["id"] for r in rows]
                if ids:
                    ph = ",".join("?" * len(ids))
                    conn.execute(
                        f"UPDATE candidates SET status = 'checking' WHERE id IN ({ph})",
                        ids,
                    )
            return [_as_dict(r) for r in rows]

    def pending_ppt_count(self) -> int:
        return self.pending_file_count(["ppt", "pptx"])

    def pending_file_count(self, file_types: list[str] | None = None) -> int:
        with self.connect() as conn:
            if not file_types:
                row = conn.execute(
                    "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
                ).fetchone()
                return int(_first(row))
            clauses: list[str] = []
            if "ppt" in file_types or "pptx" in file_types:
                clauses.append("(LOWER(url) LIKE '%.pptx' OR LOWER(url) LIKE '%.ppt')")
            if not clauses:
                return 0
            filter_sql = " AND (" + " OR ".join(clauses) + ")"
            row = conn.execute(
                f"SELECT COUNT(*) FROM candidates WHERE status = 'pending'{filter_sql}"
            ).fetchone()
            return int(_first(row))

    def reclaim_checking(self) -> int:
        """Reset stuck 'checking' rows back to 'pending' (e.g. after interrupted phase2)."""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE candidates SET status = 'pending' WHERE status = 'checking'"
            )
            return cur.rowcount

    def max_record_seq(self, batch_id: str) -> int:
        """Highest numeric suffix already assigned for this batch."""
        ph = "%s" if self.is_postgres else "?"
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT record_id FROM candidates WHERE batch_id = {ph} AND record_id IS NOT NULL",
                (batch_id,),
            ).fetchall()
        max_seq = 0
        prefix = f"{batch_id}_"
        for row in rows:
            rid = row["record_id"]
            if rid and rid.startswith(prefix):
                try:
                    max_seq = max(max_seq, int(rid[len(prefix):]))
                except ValueError:
                    continue
        return max_seq

    def update_candidate(self, candidate_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        fields = self._adapt_fields(fields)
        cols = ", ".join(f"{k} = %s" if self.is_postgres else f"{k} = ?" for k in fields)
        sql = f"UPDATE candidates SET {cols} WHERE id = {'%s' if self.is_postgres else '?'}"
        with self.connect() as conn:
            conn.execute(sql, (*fields.values(), candidate_id))

    def _adapt_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        if not self.is_postgres:
            return fields
        adapted = dict(fields)
        if "file_verified" in adapted and isinstance(adapted["file_verified"], int):
            adapted["file_verified"] = bool(adapted["file_verified"])
        return adapted

    def qualified_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM candidates WHERE status = 'qualified'"
            ).fetchone()
            return int(_first(row))

    def qualified_ppt_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM candidates
                WHERE status = 'qualified' AND file_type IN ('ppt', 'pptx')
                """
            ).fetchone()
            return int(_first(row))

    def pending_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
            ).fetchone()
            return int(_first(row))

    def next_batch_seq(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM batches").fetchone()
            return int(_first(row)) + 1

    def iter_qualified(self, batch_id: str | None = None) -> Iterator[dict]:
        with self.connect() as conn:
            base = "SELECT * FROM candidates WHERE status = 'qualified' AND url_accessible = 'PASS'"
            if batch_id:
                sql = base + " AND batch_id = %s ORDER BY id"
                params = (batch_id,)
                if not self.is_postgres:
                    sql = sql.replace("%s", "?")
                rows = conn.execute(sql, params).fetchall()
            else:
                rows = conn.execute(base + " ORDER BY id").fetchall()
            for row in rows:
                yield _as_dict(row)

    def write_audit(self, audit_path: Path, entry: dict) -> None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def new_audit_id() -> str:
        return str(uuid4())
