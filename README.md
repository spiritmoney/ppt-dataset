# 6M URL Discovery Pipeline

Search-driven pipeline to discover and qualify **6,000,000** PPT / PPTX file URLs from the public web.

**No full file downloads.** HTML search pages, light page crawls, and a tiny GET probe (first 512 bytes) per URL.

Tuned for a DigitalOcean droplet (2 vCPU, 8 GB RAM). PostgreSQL required at scale.

## How It Works

```
Search engines (filetype:pptx + category keywords)
        ↓
Direct file links from SERPs
        ↓
Light same-domain crawl of result pages (depth 1)
        ↓
Extract .ppt / .pptx links → pending candidates
        ↓
GET probe (first 512 bytes) + blocklist filter
        ↓
Qualified URL records → CSV/Excel manifest
```

**No seed files.** No Common Crawl. Light GET probe (first 512 bytes) per URL for verification.

## Requirements Met (URL stage)

| Requirement | Implementation |
|-------------|----------------|
| 6M file URLs | `target_count: 6000000`, continuous `run` loop |
| PPT / PPTX only | URL extension matching (`.pdf` excluded) |
| Mandatory source URL | Stored on every qualified record |
| Public accessibility | GET probe (first bytes) + file signature check → `url_accessible: PASS` |
| Blocklists | `config/blocklists/` — F500, elite universities, think tanks |
| Category targeting | `filetype:` queries from `category_keywords.yaml` |
| Rich metadata | url, domain, file_type, title, snippet, discovery_method, timestamps |
| Batch tracking | `BATCH-YYYYMMDD-NNN` |
| Unique records | `{batch_id}_{seq:08d}` + canonical URL dedup (`url` UNIQUE) |
| Audit trail | `data/audit/{batch_id}.jsonl` |
| CSV/Excel report | `data/manifests/` |

## Deployment (DigitalOcean)

See **[deploy/DEPLOY.md](deploy/DEPLOY.md)** for the full guide.

```bash
# Copy to droplet, then on the server:
sudo bash deploy/bootstrap.sh
sudo systemctl start ppt-discovery
```

Preflight check before going live:

```bash
python cli.py preflight
```

## Quick Start (local dev)

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

python cli.py init-db
python cli.py run
python cli.py status
python cli.py report
```

### Individual steps

```bash
python cli.py discover    # Phase 1: search + light crawl
python cli.py validate    # Phase 2: GET probe + signature check
```

## Concurrency (2 vCPU / 8 GB)

Production uses `config/config.prod.yaml` (set via `CONFIG_PATH` in `.env`).

```yaml
# config.prod.yaml (droplet)
discovery:
  concurrency: 200
  crawl_concurrency: 60
  queries_per_run: 40

phase2:
  concurrency: 120
```

Dev defaults in `config/config.yaml`:

```yaml
discovery:
  concurrency: 300        # search fetches
  crawl_concurrency: 80   # light page crawl
  queries_per_run: 40     # search queries per cycle

phase2:
  concurrency: 400        # parallel GET probes
```

## PostgreSQL Setup

```bash
sudo apt install postgresql
sudo -u postgres createdb ppt_urls
# .env: DATABASE_URL=postgresql://user:pass@localhost:5432/ppt_urls
```

SQLite works for local dev only.

## Deduplication

URLs are normalized via `canonicalize_url()` before storage:

- `http` / `https` and `www.` variants collapse to one key
- Paths lowercased, trailing slashes removed
- URL fragments dropped
- Tracking query params stripped (`utm_*`, `gclid`, etc.)
- `candidates.url` is **UNIQUE** (canonical form); `source_url` keeps the original

Dedup layers:

1. In-memory per discovery run (`seen_files`)
2. Preload of existing DB URLs (up to 500k rows)
3. Batch dedup before insert
4. Database `UNIQUE` constraint + `ON CONFLICT DO NOTHING`
5. Manifest export dedup (safety net)

One-time cleanup of legacy rows:

```bash
python cli.py dedupe
```

## Phase 2 Filters (lightweight GET probe)

1. URL ends with `.ppt` or `.pptx` only
2. HTTP status 200/206 with readable bytes
3. Content-Type is not `text/html` and body is not an HTML error page
4. File signature matches format (`PK/ZIP` for pptx, `OLE/PPT` for ppt)
5. Content-Length 10 KB – 200 MB (when present)
6. Domain not on blocklists
7. Optional category keyword match (`phase2.require_category_keyword`)

## Project Structure

```
cli.py
config/
  config.yaml           # dev settings
  config.prod.yaml      # droplet settings
  category_keywords.yaml
  blocklists/
deploy/
  bootstrap.sh          # one-shot droplet setup
  ppt-discovery.service # systemd unit
  DEPLOY.md
src/
  discovery.py      # Phase 1: search + light crawl
  prefilter.py      # Phase 2: GET probe + signature check
  database.py       # PostgreSQL / SQLite
  blocklist.py
  reporter.py
  utils.py
data/
  audit/
  manifests/
  reports/
```

## What This Does NOT Do

- No full file downloads (only first ~512 bytes per URL for verification)
- No seed files
- No slide counting or visual quality scoring

These require a separate download/validation pass if needed later.
