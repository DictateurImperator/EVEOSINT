# EVEOSINT — Installation Guide

This document describes how to install EVEOSINT on a fresh Linux server.

The current codebase has been developed and deployed on:

- Ubuntu 24.04
- Python 3.12
- PostgreSQL
- FastAPI / Uvicorn
- Nginx as the public reverse proxy
- systemd for the web service and scheduled update pipeline

Other Linux distributions may work, but the instructions below target Ubuntu/Debian.

---

## 1. Important path requirement

**EVEOSINT currently expects the repository root to be installed as:**

```text
~/eveosint
```

Several runtime components explicitly resolve files from:

```text
$HOME/eveosint/config
$HOME/eveosint/data
$HOME/eveosint/scripts
$HOME/eveosint/venv
$HOME/eveosint/web
```

Do not install the current version under `/opt/eveosint`, `/srv/eveosint`, or another directory unless you also adapt those paths in the code.

For example, for Linux user `eveosint`:

```text
/home/eveosint/eveosint
```

For Linux user `ubuntu`:

```text
/home/ubuntu/eveosint
```

The systemd services must run as the same Linux user that owns this `$HOME/eveosint` installation.

---

## 2. Expected repository layout

The deployed repository is expected to contain at least:

```text
~/eveosint/
├── LICENSE
├── README.md
├── HOW_TO_INSTALL.md
├── config/
│   ├── db.json
│   └── jobs.json
├── data/
├── scripts/
│   ├── init_db.sql
│   ├── sync_sde.py
│   ├── sync_killmails.py
│   ├── build_superintel_events.py
│   ├── build_monthly_super_reports.py
│   ├── esi_affiliation_refresh.py
│   ├── infer_character_skills_from_killmails.py
│   ├── run_recent_kill_pilots_affiliation_refresh.py
│   ├── esi_entities_backfill_audit.py
│   ├── build_pilotable_ships.py
│   ├── update_pipeline.py
│   └── ...
├── venv/
└── web/
    ├── main.py
    ├── setup_acl.py
    ├── create_user.py
    ├── app/
    └── templates/
```

Runtime directories under `data/` are created by the application and maintenance scripts as needed.

---

## 3. System packages

Install the base packages:

```bash
sudo apt update
sudo apt install -y \
  git \
  python3 \
  python3-venv \
  python3-pip \
  postgresql \
  postgresql-client \
  nginx \
  ca-certificates
```

For HTTPS with Let's Encrypt / Certbot:

```bash
sudo apt install -y certbot python3-certbot-nginx
```

Check the versions:

```bash
python3 --version
psql --version
nginx -v
```

Python 3.12 is the currently deployed/tested version.

---

## 4. Clone EVEOSINT

Run the clone as the Linux user that will run the application.

```bash
cd ~
git clone https://github.com/DictateurImperator/EVEOSINT.git eveosint
cd ~/eveosint
```

The repository **must remain named `eveosint` in the current codebase**.

---

## 5. Create the Python virtual environment

From the repository root:

```bash
cd ~/eveosint
python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip
```

The current source imports or requires the following third-party Python packages:

```bash
./venv/bin/pip install \
  fastapi \
  uvicorn \
  jinja2 \
  python-multipart \
  psycopg2-binary \
  requests \
  ijson \
  "passlib[bcrypt]"
```

Notes:

- `python-multipart` is required by FastAPI `Form`, `File`, and `UploadFile`.
- `jinja2` is required by the HTML templates.
- `passlib[bcrypt]` is required for admin/user password hashing.
- `psycopg2-binary` provides the `psycopg2` module used throughout the project.
- The final public release should preferably include a pinned `requirements.txt` generated from the validated server environment.

Quick import test:

```bash
./venv/bin/python - <<'PY'
import fastapi
import ijson
import jinja2
import passlib
import psycopg2
import requests
import uvicorn
print("Python dependencies OK")
PY
```

---

## 6. PostgreSQL database

### 6.1 Create the database role and database

Example using a dedicated PostgreSQL role named `eveosint`:

```bash
sudo -u postgres psql
```

Then:

```sql
CREATE ROLE eveosint LOGIN PASSWORD 'CHANGE_THIS_PASSWORD';
CREATE DATABASE eveosint OWNER eveosint;
\q
```

The database user must be able to create and alter the schemas/tables used by EVEOSINT. Using the database owner for a dedicated EVEOSINT database is the simplest supported layout.

### 6.2 Create the private DB configuration

Create the config directory:

```bash
mkdir -p ~/eveosint/config
```

Create:

```text
~/eveosint/config/db.json
```

Example:

```json
{
  "db_name": "eveosint",
  "db_user": "eveosint",
  "db_password": "CHANGE_THIS_PASSWORD",
  "db_host": "127.0.0.1",
  "db_port": 5432
}
```

Protect it:

```bash
chmod 600 ~/eveosint/config/db.json
```

**Never commit `config/db.json`. It contains the database password.**

Test the Python connection:

```bash
cd ~/eveosint
./venv/bin/python - <<'PY'
import json
from pathlib import Path
import psycopg2

cfg = json.loads((Path.home() / "eveosint" / "config" / "db.json").read_text())
conn = psycopg2.connect(
    dbname=cfg["db_name"],
    user=cfg["db_user"],
    password=cfg["db_password"],
    host=cfg["db_host"],
    port=cfg["db_port"],
)
print(conn.get_dsn_parameters()["dbname"], "OK")
conn.close()
PY
```

---

## 7. Initialize the database

EVEOSINT has two initialization layers:

1. the base data schema from `scripts/init_db.sql`;
2. the web/auth/ACL schema initialized by the Python web code.

### 7.1 Base SQL schema

Run:

```bash
cd ~/eveosint
psql -h 127.0.0.1 -U eveosint -d eveosint -f scripts/init_db.sql
```

This initializes the base PostgreSQL objects used by the SDE and raw killmail layers.

### 7.2 Web tables

Before ACL initialization, create the core web tables:

```bash
cd ~/eveosint/web
../venv/bin/python -c "from app.schema import ensure_tables; ensure_tables()"
```

### 7.3 ACL and menus

Run:

```bash
cd ~/eveosint/web
../venv/bin/python setup_acl.py
```

This creates/populates the roles, permissions and menu tables.

It can be run again after an application update when permissions/menu definitions change.

### 7.4 Create the first administrator

Run:

```bash
cd ~/eveosint/web
../venv/bin/python app/create_user.py
```

Choose:

```text
Role: admin
```

Passwords must be at least 12 characters and bcrypt-compatible (maximum 72 bytes).

---

## 8. Optional Admin Jobs configuration

The Admin Jobs screen reads:

```text
~/eveosint/config/jobs.json
```

The file must contain a JSON object with a `jobs` array.

A minimal valid configuration is:

```json
{
  "jobs": []
}
```

With an empty list, the application automatically adds the built-in DOTLAN alliance population jobs.

For the other whitelisted Admin Jobs, use deployment-specific absolute command paths because `subprocess.Popen()` does not shell-expand `$HOME` or `~` in command arguments.

Example for Linux user `eveosint`:

```json
{
  "jobs": [
    {
      "key": "sync_sde",
      "label": "SDE sync",
      "type": "sde",
      "command": [
        "/home/eveosint/eveosint/venv/bin/python",
        "/home/eveosint/eveosint/scripts/sync_sde.py"
      ],
      "log_path": "~/eveosint/data/logs/sync_sde.log",
      "enabled": true
    },
    {
      "key": "sync_killmails",
      "label": "Killmails sync",
      "type": "killmails",
      "command": [
        "/home/eveosint/eveosint/venv/bin/python",
        "/home/eveosint/eveosint/scripts/sync_killmails.py"
      ],
      "log_path": "~/eveosint/data/logs/killmail_import.log",
      "enabled": true
    },
    {
      "key": "sync_recent_kill_pilots_affiliation",
      "label": "Recent kill pilots affiliation refresh",
      "type": "recent_kill_pilots_affiliation",
      "command": [
        "/home/eveosint/eveosint/venv/bin/python",
        "/home/eveosint/eveosint/scripts/run_recent_kill_pilots_affiliation_refresh.py"
      ],
      "log_path": "~/eveosint/data/logs/recent_kill_pilots_affiliation.log",
      "enabled": true
    },
    {
      "key": "sync_character_skill_inference",
      "label": "Character skill inference",
      "type": "character_skill_inference",
      "command": [
        "/home/eveosint/eveosint/venv/bin/python",
        "/home/eveosint/eveosint/scripts/infer_character_skills_from_killmails.py"
      ],
      "log_path": "~/eveosint/data/logs/character_skill_inference.log",
      "enabled": true
    }
  ]
}
```

Replace `/home/eveosint` with the actual home directory of the Linux service user.

Protect the configuration if it contains local/private information:

```bash
chmod 600 ~/eveosint/config/jobs.json
```

---

## 9. Initial data bootstrap

The web application can be started before the full historical dataset is populated, but many intelligence pages require SDE, killmail and derived tables.

**Historical imports can be very large and take a long time. Monitor disk space, PostgreSQL size, CPU and external API usage.**

### 9.1 Import the EVE Static Data Export (SDE)

Run:

```bash
cd ~/eveosint
./venv/bin/python scripts/sync_sde.py
```

This creates/imports the SDE tables and rebuilds the `sde_work` helper tables required by several site features.

### 9.2 Import killmails

Example for a limited period:

```bash
cd ~/eveosint
./venv/bin/python scripts/sync_killmails.py \
  --from 2026-01-01 \
  --to 2026-09-26 \
  --workers 4
```

The script default start date is `2007-12-05`. Running it without `--from` therefore requests the complete supported killmail history and is a major import.

Use an explicit range unless a full historical bootstrap is intentional.

### 9.3 Build SuperINTEL events

After SDE and killmails exist:

```bash
cd ~/eveosint
./venv/bin/python scripts/build_superintel_events.py --rebuild
```

### 9.4 Refresh public ESI affiliations for SuperINTEL pilots

```bash
cd ~/eveosint
./venv/bin/python scripts/esi_affiliation_refresh.py \
  --source-table superintel.super_pilots \
  --source-column character_id \
  --workers 4
```

The code enforces a global ESI request limit and defaults below 300 calls/minute.

### 9.5 Build a daily SuperINTEL report

Example:

```bash
cd ~/eveosint
./venv/bin/python scripts/build_monthly_super_reports.py \
  --schema report_super \
  --mode daily \
  --date 2026-09-26 \
  --replace
```

To backfill every available daily report, review the script options first:

```bash
./venv/bin/python scripts/build_monthly_super_reports.py --help
```

The script supports `--all-daily`, which can be a large operation.

### 9.6 Character skill inference

```bash
cd ~/eveosint
./venv/bin/python scripts/infer_character_skills_from_killmails.py
```

### 9.7 Recent-pilot affiliations

Example for the last 30 days:

```bash
cd ~/eveosint
./venv/bin/python scripts/run_recent_kill_pilots_affiliation_refresh.py \
  --days 30 \
  --workers 4
```

### 9.8 Entity backfill

```bash
cd ~/eveosint
./venv/bin/python scripts/esi_entities_backfill_audit.py
```

### 9.9 Pilotable ships

For an initial full build:

```bash
cd ~/eveosint
./venv/bin/python scripts/build_pilotable_ships.py --mode init
```

Ongoing updates are handled by the unified update pipeline.

### 9.10 Population / DOTLAN

Alliance population initialization can be launched from the Admin Jobs screen or directly:

```bash
cd ~/eveosint
./venv/bin/python web/app/population_alliances_init.py
```

The collector intentionally throttles DOTLAN requests globally. A complete first import is slow by design.

Corporation population history is initialized on demand by the application.

---

## 10. Test the web application manually

Before creating systemd units:

```bash
cd ~/eveosint/web
../venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

From another shell:

```bash
curl -sS -D - -o /dev/null http://127.0.0.1:8000/
```

Stop Uvicorn with `Ctrl+C`.

Uvicorn should only listen on localhost in the production deployment:

```text
127.0.0.1:8000
```

Do not expose port 8000 directly to the Internet.

---

## 11. systemd web service

The application itself expects the service to be named:

```text
eveosint-web.service
```

because the Admin System page checks that unit name.

Create:

```text
/etc/systemd/system/eveosint-web.service
```

Example for Linux user `eveosint`:

```ini
[Unit]
Description=EVEOSINT FastAPI web service
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=eveosint
Group=eveosint
WorkingDirectory=/home/eveosint/eveosint/web
ExecStart=/home/eveosint/eveosint/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Replace the Linux username/home path as required.

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now eveosint-web
sudo systemctl status eveosint-web --no-pager
```

Confirm that the application is not publicly bound:

```bash
sudo ss -ltnp | grep ':8000'
```

Expected address:

```text
127.0.0.1:8000
```

---

## 12. Scheduled update pipeline

The application expects the following unit names for Admin > Update Pipeline status:

```text
eveosint-update.service
eveosint-update.timer
```

The current unified pipeline is launched in `weekly` mode and the application displays a schedule of **15:30 Europe/Paris**.

Create:

```text
/etc/systemd/system/eveosint-update.service
```

```ini
[Unit]
Description=EVEOSINT unified update pipeline
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=eveosint
Group=eveosint
WorkingDirectory=/home/eveosint/eveosint
ExecStart=/home/eveosint/eveosint/venv/bin/python /home/eveosint/eveosint/scripts/update_pipeline.py --mode weekly --trigger timer
Environment=PYTHONUNBUFFERED=1
```

Create:

```text
/etc/systemd/system/eveosint-update.timer
```

```ini
[Unit]
Description=Run EVEOSINT update pipeline daily

[Timer]
OnCalendar=*-*-* 15:30:00 Europe/Paris
Persistent=true
Unit=eveosint-update.service

[Install]
WantedBy=timers.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now eveosint-update.timer
sudo systemctl list-timers eveosint-update.timer
```

Manual test:

```bash
sudo systemctl start eveosint-update.service
sudo systemctl status eveosint-update.service --no-pager
```

Pipeline state/log files are written below:

```text
~/eveosint/data/run/update_pipeline/
~/eveosint/data/logs/update_pipeline/
```

---

## 13. Nginx reverse proxy

EVEOSINT relies on the reverse proxy for several production security properties.

Important requirements:

- proxy only to `127.0.0.1:8000`;
- forward the original `Host`;
- forward the HTTPS scheme;
- **overwrite** `X-Forwarded-For` with `$remote_addr`;
- do not use an untrusted client-provided X-Forwarded-For chain;
- rate-limit `/login`;
- send the security headers listed below.

The `Host` header is particularly important because the application's CSRF protection compares the request host with the browser `Origin` / `Referer`.

### 13.1 Login rate-limit zone

Create for example:

```text
/etc/nginx/conf.d/eveosint-rate-limit.conf
```

Example:

```nginx
limit_req_zone $binary_remote_addr zone=eveosint_login:10m rate=5r/m;
```

The exact rate is deployment policy and may be adjusted.

### 13.2 Site configuration

Create:

```text
/etc/nginx/sites-available/eveosint
```

Example HTTPS server block:

```nginx
server {
    listen 443 ssl http2;
    server_name <DOMAIN>;

    server_tokens off;

    ssl_certificate /etc/letsencrypt/live/<DOMAIN>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<DOMAIN>/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location = /login {
        limit_req zone=eveosint_login burst=3 nodelay;

        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/eveosint /etc/nginx/sites-enabled/eveosint
sudo nginx -t
sudo systemctl reload nginx
```

If replacing the default site:

```bash
sudo rm -f /etc/nginx/sites-enabled/default
```

Then re-test:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## 14. TLS certificate

Before enabling the HTTPS-only configuration, ensure the DNS record for the domain points to the server.

One common Certbot flow is:

```bash
sudo certbot --nginx -d <DOMAIN>
```

If a `www` hostname is also configured:

```bash
sudo certbot --nginx -d <DOMAIN> -d www.<DOMAIN>
```

After HTTPS is working, verify certificate renewal:

```bash
sudo certbot renew --dry-run
```

---

## 15. Production verification

### Web service

```bash
sudo systemctl status eveosint-web --no-pager
```

### Local Uvicorn binding

```bash
sudo ss -ltnp | grep ':8000'
```

It should show only localhost (`127.0.0.1`), not `0.0.0.0`.

### Nginx

```bash
sudo nginx -t
sudo systemctl status nginx --no-pager
```

### Public GET

Do not use only `curl -I` for application checks: some application routes do not accept `HEAD`.

Use a real GET and discard the body:

```bash
curl -sS -D - -o /dev/null https://<DOMAIN>/
```

### Security headers

```bash
curl -sS -D - -o /dev/null https://<DOMAIN>/ \
  | grep -Ei 'strict-transport-security|x-content-type-options|x-frame-options|referrer-policy'
```

Expected:

```text
Strict-Transport-Security: max-age=31536000
X-Content-Type-Options: nosniff
X-Frame-Options: SAMEORIGIN
Referrer-Policy: strict-origin-when-cross-origin
```

### PostgreSQL

```bash
psql -h 127.0.0.1 -U eveosint -d eveosint -c '\dn'
```

### Update timer

```bash
systemctl status eveosint-update.timer --no-pager
systemctl list-timers eveosint-update.timer
```

---

## 16. Runtime storage

EVEOSINT writes runtime data below:

```text
~/eveosint/data/
```

Examples include:

```text
data/killmails/
data/sde/
data/mer/
data/logs/
data/run/
```

A historical killmail archive, SDE data, derived PostgreSQL tables and MER archives can consume substantial disk space.

Monitor both filesystem and PostgreSQL usage:

```bash
df -h
du -sh ~/eveosint/data
sudo -u postgres psql -d eveosint -c "SELECT pg_size_pretty(pg_database_size('eveosint'));"
```

Do not place runtime `data/` content in Git.

---

## 17. External network dependencies

Depending on which modules are enabled, the server performs outbound HTTPS requests to EVE/OSINT data sources including:

```text
developers.eveonline.com
esi.evetech.net
data.everef.net
evemaps.dotlan.net
www.eveonline.com
content.eveonline.com
cdn1.eveonline.com
web.ccpgamescdn.com
```

The browser UI also references external assets/sites including:

```text
images.evetech.net
cdn.jsdelivr.net
zkillboard.com
fenriscreations.com
```

A firewall/proxy deployment must account for the services actually used.

---

## 18. Security-sensitive files

Never publish or commit:

```text
config/db.json
data/
*.log
runtime PID/status files
database backups/dumps containing private deployment data
TLS private keys
```

Recommended permissions:

```bash
chmod 700 ~/eveosint/config
chmod 600 ~/eveosint/config/db.json
chmod 600 ~/eveosint/config/jobs.json
```

The web and update services should run as the non-root EVEOSINT Linux user.

---

## 19. Git repository hygiene before a public release

The public repository should contain:

```text
LICENSE
README.md
HOW_TO_INSTALL.md
web/
scripts/
```

It should not contain:

```text
__pycache__/
*.pyc
venv/
config/db.json
data/
private database dumps
local runtime logs
private deployment configuration```

The repository already uses the GNU Affero General Public License v3 (AGPL-3.0).

**Important:** verify the repository `.gitignore` explicitly ignores at least:

```gitignore
/config/db.json
/config/jobs.json
/data/
```

before committing a real deployment tree.

---

## 20. Updating EVEOSINT

Before updating production:

1. back up the PostgreSQL database;
2. preserve `config/db.json` and deployment-specific `config/jobs.json`;
3. update the repository;
4. activate/use the existing `venv`;
5. install any new Python dependencies;
6. re-run ACL initialization if permissions/menu definitions changed;
7. restart the web service;
8. check the update timer and logs.

Typical application-side update:

```bash
cd ~/eveosint
git pull

./venv/bin/pip install -r requirements.txt   # once a requirements file is shipped

cd ~/eveosint/web
../venv/bin/python setup_acl.py

sudo systemctl restart eveosint-web
sudo systemctl status eveosint-web --no-pager
```

Do not blindly overwrite deployment secrets or runtime data during upgrades.

---

## 21. Troubleshooting

### `Missing DB config file`

Check:

```text
~/eveosint/config/db.json
```

and verify permissions/JSON syntax.

### Admin ACL/menu errors

Run:

```bash
cd ~/eveosint/web
../venv/bin/python -c "from app.schema import ensure_tables; ensure_tables()"
../venv/bin/python setup_acl.py
```

### Admin Jobs says configuration is missing/invalid

Check:

```text
~/eveosint/config/jobs.json
```

The top level must be:

```json
{
  "jobs": []
}
```

or a list of valid whitelisted jobs as described above.

### Admin Update Pipeline says Python/runner is missing

The code expects exactly:

```text
~/eveosint/venv/bin/python
~/eveosint/scripts/update_pipeline.py
```

### SuperINTEL reports are missing

Check in this order:

1. SDE was imported;
2. killmails were imported;
3. SuperINTEL events were built;
4. ESI affiliations were refreshed;
5. a `report_super.report_daily_*` snapshot was generated.

### Population import is slow

The DOTLAN collector is deliberately throttled. Do not disable the throttle to speed up first initialization.

### HTTP error page / `Erreur XXX`

Browser navigations intentionally use the common friendly error page.

Fetch/XHR requests intentionally return a short public error such as:

```text
Erreur 500
```

Detailed exceptions are kept server-side in logs rather than exposed to visitors.

---

## 22. License

EVEOSINT is distributed under the GNU Affero General Public License v3.

See:

```text
LICENSE
```

in the repository root.

---

## 23. Legal notice

EVEOSINT is an independent third-party EVE Online analytics/OSINT project.

The project repository currently states:

> All EVE related materials are property of Fenris Creation.

EVEOSINT is not affiliated with or endorsed by Fenris Creation.
