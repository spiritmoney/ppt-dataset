-- PostgreSQL schema for 6M-scale collection
-- Run: psql -U user -d ppt_pipeline -f deploy/postgres_schema.sql

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    qualified_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS url_queue (
    id SERIAL PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    domain TEXT,
    status TEXT DEFAULT 'pending',
    discovered_at TIMESTAMPTZ NOT NULL,
    batch_id TEXT,
    attempts INTEGER DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_url_queue_status ON url_queue(status);

CREATE TABLE IF NOT EXISTS files (
    id SERIAL PRIMARY KEY,
    unique_filename TEXT UNIQUE,
    file_type TEXT,
    source_url TEXT NOT NULL,
    download_url TEXT,
    domain TEXT,
    batch_id TEXT NOT NULL,
    original_filename TEXT,
    status TEXT NOT NULL,
    rejection_reason TEXT,
    slide_count INTEGER,
    quality_score REAL,
    image_slide_ratio REAL,
    file_size_bytes INTEGER,
    document_title TEXT,
    author TEXT,
    organization TEXT,
    download_timestamp TIMESTAMPTZ,
    collection_timestamp TIMESTAMPTZ,
    audit_id TEXT UNIQUE NOT NULL,
    content_hash TEXT UNIQUE,
    local_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_batch ON files(batch_id);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash);
