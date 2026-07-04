# Deployment Guide

Production target: **Ubuntu 22.04/24.04 DigitalOcean droplet** (2 vCPU, 8 GB RAM) with **PostgreSQL**.

## One-command bootstrap (fresh droplet)

```bash
# On your laptop — copy project to droplet
scp -r . root@YOUR_DROPLET_IP:/root/ppt-dataset-6m

# On the droplet
cd /root/ppt-dataset-6m
sudo bash deploy/bootstrap.sh
sudo systemctl start ppt-discovery
```

`bootstrap.sh` will:

1. Install Python 3, PostgreSQL, git
2. Create system user `pptdiscovery`
3. Deploy app to `/opt/ppt-dataset-6m`
4. Create PostgreSQL database `ppt_urls` + user `ppt_urls`
5. Write `.env` from `deploy/env.production.example`
6. Run `init-db` and `preflight`
7. Install and enable `ppt-discovery` systemd service

## Manual setup

```bash
sudo apt update
sudo apt install -y python3 python3-venv postgresql postgresql-contrib

sudo -u postgres createuser ppt_urls -P
sudo -u postgres createdb ppt_urls -O ppt_urls

cd /opt/ppt-dataset-6m
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-prod.txt

cp deploy/env.production.example .env
# Edit .env — set DATABASE_URL password

python cli.py init-db
python cli.py preflight
python cli.py run
```

## Environment variables

| Variable | Production value |
|----------|------------------|
| `DATABASE_URL` | `postgresql://ppt_urls:PASSWORD@localhost:5432/ppt_urls` |
| `CONFIG_PATH` | `config/config.prod.yaml` |
| `DATA_DIR` | `data` |
| `TARGET_COUNT` | `6000000` |
| `LOG_LEVEL` | `INFO` |
| `LOG_FILE` | `/var/log/ppt-discovery/pipeline.log` |

## Operations

```bash
# Service control
sudo systemctl status ppt-discovery
sudo systemctl restart ppt-discovery
sudo systemctl stop ppt-discovery
sudo journalctl -u ppt-discovery -f

# Pipeline status (as app user)
sudo -u pptdiscovery bash -c 'cd /opt/ppt-dataset-6m && .venv/bin/python cli.py status'
sudo -u pptdiscovery bash -c 'cd /opt/ppt-dataset-6m && .venv/bin/python cli.py report'

# Manifests and progress
ls -lh /opt/ppt-dataset-6m/data/manifests/
cat /opt/ppt-dataset-6m/data/reports/progress_latest.json
```

## PostgreSQL tuning (8 GB RAM)

Add to `/etc/postgresql/*/main/postgresql.conf`:

```ini
shared_buffers = 1GB
effective_cache_size = 3GB
maintenance_work_mem = 256MB
max_connections = 100
```

Then: `sudo systemctl restart postgresql`

## Production config

`config/config.prod.yaml` lowers concurrency for a 2 vCPU droplet:

| Setting | Dev (`config.yaml`) | Prod (`config.prod.yaml`) |
|---------|---------------------|---------------------------|
| discovery.concurrency | 300 | 200 |
| discovery.crawl_concurrency | 80 | 60 |
| phase2.concurrency | 400 | 120 |

Phase 2 also caps DB writes at 32 concurrent connections to stay within PostgreSQL limits.

## Backups

```bash
# Database
sudo -u postgres pg_dump ppt_urls | gzip > ppt_urls_$(date +%F).sql.gz

# Manifests + audit trail
tar czf ppt_data_$(date +%F).tar.gz -C /opt/ppt-dataset-6m data/manifests data/audit data/reports
```

## Updating the app

```bash
cd /root/ppt-dataset-6m   # or your checkout
git pull                  # if using git
sudo systemctl stop ppt-discovery
sudo rsync -a --exclude '.venv' --exclude '.env' --exclude 'data' . /opt/ppt-dataset-6m/
sudo -u pptdiscovery bash -c 'cd /opt/ppt-dataset-6m && .venv/bin/pip install -r requirements-prod.txt -q'
sudo -u pptdiscovery bash -c 'cd /opt/ppt-dataset-6m && .venv/bin/python cli.py preflight'
sudo systemctl start ppt-discovery
```
