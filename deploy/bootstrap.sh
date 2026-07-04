#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 22.04/24.04 droplet for the 6M URL discovery pipeline.
# Run as root: sudo bash deploy/bootstrap.sh
set -euo pipefail

APP_USER="${APP_USER:-pptdiscovery}"
APP_DIR="${APP_DIR:-/opt/ppt-dataset-6m}"
DB_NAME="${DB_NAME:-ppt_urls}"
DB_USER="${DB_USER:-ppt_urls}"
DB_PASS="${DB_PASS:-}"
LOG_DIR="/var/log/ppt-discovery"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/bootstrap.sh"
  exit 1
fi

if [[ -z "$DB_PASS" ]]; then
  DB_PASS="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)"
  echo "Generated DB password for ${DB_USER}"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  postgresql postgresql-contrib \
  git rsync curl

# App user
if ! id "$APP_USER" &>/dev/null; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR" "$LOG_DIR"
chown "$APP_USER:$APP_USER" "$LOG_DIR"

# PostgreSQL role + database
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  ELSE
    ALTER ROLE ${DB_USER} PASSWORD '${DB_PASS}';
  END IF;
END
\$\$;
SQL

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
fi

# Copy project (assumes script run from repo checkout)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
rsync -a --delete \
  --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '*.db' \
  --exclude 'data/audit' --exclude 'data/manifests' --exclude 'data/reports' \
  "$REPO_ROOT/" "$APP_DIR/"

mkdir -p "$APP_DIR/data/audit" "$APP_DIR/data/manifests" "$APP_DIR/data/reports"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# Python venv
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-prod.txt" -q

# Environment
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/deploy/env.production.example" "$APP_DIR/.env"
  sed -i "s/CHANGE_ME/${DB_PASS}/" "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

# Init schema
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && .venv/bin/python cli.py init-db"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && .venv/bin/python cli.py preflight"

# systemd
cp "$APP_DIR/deploy/ppt-discovery.service" /etc/systemd/system/ppt-discovery.service
systemctl daemon-reload
systemctl enable ppt-discovery.service

cat <<EOF

Bootstrap complete.

  App directory : $APP_DIR
  Database      : postgresql://${DB_USER}@localhost:5432/${DB_NAME}
  DB password   : $DB_PASS   (also in $APP_DIR/.env)
  Logs          : $LOG_DIR/pipeline.log (when LOG_FILE set)
                  $LOG_DIR/service.log   (systemd stdout)

Commands:
  sudo systemctl start ppt-discovery     # start pipeline
  sudo systemctl status ppt-discovery    # check service
  sudo journalctl -u ppt-discovery -f    # follow logs

  cd $APP_DIR && sudo -u $APP_USER .venv/bin/python cli.py status
  cd $APP_DIR && sudo -u $APP_USER .venv/bin/python cli.py report

Save the DB password now — it is not shown again unless you read $APP_DIR/.env
EOF
