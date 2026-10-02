# EVEOSINT — Sovereignty collection

These are **two separate ingestion stages**. Neither changes the existing map,
coalition matching, public routes, or historical affiliation calculations.

Both can now be launched from **Admin → Debug → Sovereignty collectors**
(`admin.jobs.run` required; read-only status/log access uses `admin.jobs.view`).
The ESI button runs the same current snapshot job as the independent timer.
The DOTLAN form exposes scope, one optional system, batch limit (0 = all pending)
and refreshing completed systems. Both cards show their own live logs, completion
status and Stop button. Stopping one job does not stop the other or the existing
affiliation benchmark. This Debug access does **not** make the collectors dry runs:
they populate the real `sovereignty.*` tables.

## 1. Current ownership from CCP ESI

File: `scripts/sync_sovereignty_esi.py`

- One public GET: `https://esi.evetech.net/latest/sovereignty/map/?datasource=tranquility`. CCP does not expose a server-side `nullsec only` filter, so EVEOSINT performs the scope filter locally after this single response.
- No OAuth, API key, scraping or pagination.
- Identifiable User-Agent (app, version and public source URL) and pinned
  `X-Compatibility-Date: 2026-09-28`.
- Obeys the `Expires` HTTP cache header (never requests early); sends the
  previous full `ETag` as `If-None-Match` after expiry and handles 304 without
  reloading the map.
- Honors CCP's `Retry-After`, old error-budget headers and transient 5xx
  backoff. A short/broken payload, transport failure or HTTP error does **not**
  delete or replace the existing map.
- EVEOSINT stores only **conquerable 0.0**: `securityStatus <= 0.0`, known-space regions, with NPC faction ownership excluded at system/constellation/region level. This removes HS/LS, Pochven, wormholes, NPC nullsec regions and NPC pockets.
- Atomic complete current snapshot inside that scope; absence of a formerly owned claimable system from a valid ESI response means it is no longer part of the current SOV map.
- Persistent advisory lock prevents overlapping ESI collector executions.

**First run on the EVEOSINT host**, once code is deployed:

```bash
cd /home/ubuntu/eveosint
./venv/bin/python scripts/sync_sovereignty_esi.py
```

The script creates `sovereignty.current_map` (system_id, alliance_id,
corporation_id, faction_id, observed_at, source) and
`sovereignty.esi_map_state` (cache state and last HTTP status) if missing.

To enable the **independent** refresh timer (not part of the current weekly
pipeline; it doesn't depend on users visiting the web UI):

```bash
sudo cp /home/ubuntu/eveosint/deploy/eveosint-sovereignty-esi.service /etc/systemd/system/
sudo cp /home/ubuntu/eveosint/deploy/eveosint-sovereignty-esi.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eveosint-sovereignty-esi.timer
sudo systemctl start eveosint-sovereignty-esi.service
sudo systemctl status eveosint-sovereignty-esi.service --no-pager
```

The timer starts after boot and 75 minutes after the previous execution
(including a small random delay). The script independently checks the **actual**
server-supplied Expires value and skips when still fresh; 75 minutes alone is
not treated as a guarantee the cache expired.

Check current data:

```sql
SELECT COUNT(*) AS claimed_systems, MAX(observed_at) AS source_observed_at
FROM sovereignty.current_map;

SELECT etag, expires_at, fetched_at, row_count, last_status
FROM sovereignty.esi_map_state
WHERE id = 1;
```

## 2. Historical DOTLAN event import (manual and resumable)

File: `scripts/import_sovereignty_dotlan.py`

This downloads each source system page at
`https://evemaps.dotlan.net/system/{system_name}` and extracts the
**Sovereignty Changes** table, but only for the same **conquerable 0.0** SDE scope as the ESI collector. Records include source URL, date/time as displayed
by DOTLAN, raw action, normalized action (GAIN/LOST/TRANSFER/LEVEL_CHANGE/OTHER),
optional displayed Alliance and Corporation names/links, raw source cells,
row index, fetch time and the EVEOSINT ownership convention active at that date.

**Important:** This step stores DOTLAN **source events**, not reconstructed
ownership intervals. A level-up/down event does not itself change the owner.
A long interval without a DOTLAN event is **not** treated as a data gap: when
periods are reconstructed later, the last known owner can continue until the
next ownership event. A name without a resolvable source link stays a raw name;
IDs and missing dates are never guessed. `event_at` intentionally stores the
page's clock value as an unzoned timestamp until the historical source timezone
is verified.

EVEOSINT also stores an `ownership_model` tag for later reconstruction:

- before **2015-07-14**: `legacy_sov`;
- **2015-07-14 → 2024-06-10**: `ihub_proxy` — by project convention, DOTLAN's
  territorial owner is interpreted as the effective IHub owner for analysis;
- **2024-06-11 → 2024-06-26**: `sovhub_legacy_ihub_proxy` — the old IHub has become a SovHub in legacy IHub mode;
- **2024-06-27 → 2024-10-28**: `ihub_sovhub_transition_proxy` — voluntary per-system conversion to SovHub mode is possible; the exact conversion instant is not invented when DOTLAN cannot prove it;
- from **2024-10-29**: `sovhub`.

This is an explicit EVEOSINT analytical convention, not a claim that DOTLAN
contains a complete historical IHub ownership feed. The public History pages
will need to display that convention when this reconstructed SOV history is
surfaced.

Use small batches first:

```bash
cd /home/ubuntu/eveosint
./venv/bin/python scripts/import_sovereignty_dotlan.py --system 1DQ1-A --limit 1
./venv/bin/python scripts/import_sovereignty_dotlan.py --scope current --limit 25
```

To include historical **conquerable nullsec** systems not currently present in ESI SOV,
use `--scope all-nullsec`. NPC nullsec and NPC pockets are still excluded. Resume by repeating the command; completed systems
are skipped, failed ones retried. `--limit 0` processes the entire remaining
selection (potentially **many hours**). `--refresh` explicitly re-fetches
already successful systems. There is no automatic DOTLAN bulk crawl on
deployment or in the weekly pipeline.

The importer reuses the existing global `app.dotlan_throttle` lock, enforcing
the same host-wide minimum interval between DOTLAN requests as existing
Population jobs, including across processes. It reacts to 429/Retry-After,
aborts on access denial and records failed systems so a later run can resume.
Writes replace **one successfully parsed system at a time, transactionally**;
a failed page never erases its previous events.

The importer creates:

- `sovereignty.dotlan_events` — source events (no owner IDs inferred), including `ownership_model`.
- `sovereignty.dotlan_system_sync` — import status, source hash, event counts,
  fetched timestamp, errors.

Check historical import:

```sql
SELECT last_status, COUNT(*) AS systems, SUM(event_count) AS events
FROM sovereignty.dotlan_system_sync
GROUP BY last_status;

SELECT system_id, event_at, action, action_raw, ownership_model, alliance_name, corporation_name, source_url
FROM sovereignty.dotlan_events
ORDER BY event_at DESC
LIMIT 30;
```

## Offline tests

```bash
cd /home/ubuntu/eveosint
./venv/bin/python -m unittest scripts.test_sovereignty_collectors -v
```

Tests do not call ESI, DOTLAN or PostgreSQL. A live first-run test and a
representative HTML layout check must still be performed on the deployed host
before a full historical crawl.

References:

- CCP ESI: https://developers.eveonline.com/docs/services/esi/best-practices/
- CCP limits: https://developers.eveonline.com/docs/services/esi/rate-limiting/
- CCP static-data ID ranges: https://developers.eveonline.com/docs/guides/id-ranges/
- CCP Equinox sovereignty transition: https://support.eveonline.com/hc/en-us/articles/14189361268636-Equinox-Sovereignty-Updates
- CCP 2024-10-29 forced legacy SovHub conversion: https://www.eveonline.com/news/view/patch-notes-version-22-01
- Example DOTLAN history: https://evemaps.dotlan.net/system/1DQ1-A
