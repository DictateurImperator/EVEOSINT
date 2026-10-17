#!/usr/bin/env bash
# Run with sudo on the EVEOSINT server. Only generated ranking data is removed.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo 'Run: sudo bash /tmp/eveosint-purge-rankings.sh' >&2
    exit 1
fi
cd /tmp
printf 'Disk space before purge:\n'
df -h /home/ubuntu/eveosint
# Match the exact script argument, never a broad process-name pattern.
python3 - <<'PY'
import os
from pathlib import Path
import signal
import time
expected = b'/home/ubuntu/eveosint/scripts/build_killmail_rankings.py'
def workers():
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            if expected in argv[1:]:
                yield int(proc.name)
        except (OSError, ProcessLookupError):
            pass
pids = list(workers())
for pid in pids:
    print(f'Stopping ranking worker PID {pid}', flush=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
if pids:
    time.sleep(5)
for pid in workers():
    if pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
PY
# An orphaned database query can survive its Python worker; terminate ranking
# sessions only, then hold the same advisory lock throughout the reset.
runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d eveosint <<'SQL'
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname=current_database() AND application_name='killmail_rankings'
  AND pid<>pg_backend_pid();
SET lock_timeout='15s';
SELECT pg_advisory_lock(79130425);
BEGIN;
TRUNCATE TABLE
    analytics.killmail_rankings_monthly,
    analytics.killmail_rankings_current,
    analytics.killmail_ranking_months,
    analytics.killmail_ranking_versions,
    analytics.killmail_ranking_current_state;
INSERT INTO analytics.killmail_ranking_current_state(singleton,ready) VALUES(true,false);
COMMIT;
SELECT pg_advisory_unlock(79130425);
SQL
# Clear only this job's UI state and cached ranking pages; retain its log.
python3 - <<'PY'
import json
import os
from pathlib import Path
import pwd
from datetime import datetime, timezone
root = Path('/home/ubuntu/eveosint/data')
owner = pwd.getpwnam('ubuntu')
cache = root / 'cache/killmail_rankings'
if cache.is_dir():
    for path in cache.iterdir():
        if path.is_file() and path.suffix == '.json':
            path.unlink()
for name in ('build_killmail_rankings.pid', 'build_killmail_rankings.json'):
    (root / 'run' / name).unlink(missing_ok=True)
progress = root / 'market/killmail_rankings_progress.json'
progress.parent.mkdir(parents=True, exist_ok=True)
temp = progress.with_suffix('.purge.tmp')
temp.write_text(json.dumps(dict(phase='purged', processed=0, months=0, entity_rows=0,
                                updated_at=datetime.now(timezone.utc).isoformat())))
os.chown(temp, owner.pw_uid, owner.pw_gid)
os.replace(temp, progress)
PY
printf '\nRankings cleared. Killmails and prices preserved. Restart using Build / refresh rankings.\n'
printf '\nDisk space after purge:\n'
df -h /home/ubuntu/eveosint
