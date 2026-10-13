# Daily zKill item valuation history

**Admin → Item Prices** launches a manual, resumable import. It is also linked
from **Admin → Jobs**, where the same start/stop controls and live dated progress
are displayed directly on the job card. Viewing requires `admin.jobs.view`; starting and stopping
requires `admin.jobs.run`. Visits do not start downloads. No daily schedule is added.

Leave both dates empty to import the documented history from **2003-01-01**
through **yesterday UTC**. This includes every inventory type returned by zKill
for each day, including unpublished/obsolete types if present in that response.
Today is allowed when selected explicitly, but that day's snapshot may be incomplete.
The API is `/api/prices/date/YYYY-MM-DD/`, documented at
https://zkillboard.com/api/docs/.

These are **zKill valuation values**, not independently verified historic market
transactions or regional prices. Early snapshots include fixed valuations even
before EVE's launch. Missing inventory IDs are unknown; zero is stored only when
zKill explicitly returns zero. An empty successful response is recorded as an
empty snapshot, distinct from a failed request. API errors, malformed JSON and
invalid prices are never recorded as a successful day. Numbers and numeric strings
are normalized using Decimal and saved as JSON numbers without float conversion.

## Storage and queries

The job creates `market.zkill_daily_prices` on its first run. Each row contains:

- `price_date`: UTC date, primary key.
- `prices`: inventory type ID → unit price in ISK, in JSONB.
- `item_count`, `status` (`imported` or `empty`).
- `source_url`, SHA-256 of the decoded HTTP body, and `fetched_at`.

PostgreSQL compresses large snapshots using its default TOAST storage. There is
no per-item GIN index and no duplicate raw download cache on disk. The database
itself is the local response cache. Single-date lookups are inexpensive; querying
one item across the full history decompresses those daily snapshots and should
be done in a background analysis, not repeatedly from interactive pages.

```sql
SELECT price_date, (prices ->> '34')::numeric AS tritanium_unit_isk
FROM market.zkill_daily_prices
WHERE price_date = DATE '2026-10-11';
```

An absent key returns SQL NULL, not zero. These snapshots do not overwrite
existing killmail values and are not yet added as predictors to the first-loss study.

## Resume, limits and interruption

Each complete day is committed atomically. Relaunch skips saved days, including
saved empty responses, except the last seven calendar days which are refreshed.
Refreshing replaces the row for that date without accumulating archive copies.
Failed dates remain missing and are retried on the next launch. Previous good
snapshots remain intact if their refresh fails.

Requests are sequential, with at least two seconds **after a response** before
the next request. User-Agent identifies EVEOSINT and links its repository; gzip
is enabled. Network/server failures have bounded retries and exponential delay.
HTTP 420/429 honor Retry-After with at least a minute's wait. Persistent rate
limits or five consecutive failures stop the import. Other individual failures
are logged, and a partially successful run ends with a failed state so it can
be resumed. No assumption is made that a published fixed quota exists.

Stop uses SIGTERM and interrupts request pacing/backoff immediately. An ongoing
HTTP request can finish first (10-second connection / 60-second read timeout);
that day's successful commit is retained. The PostgreSQL advisory lock prevents
a second importer, including direct CLI launches. A rejected duplicate does not
overwrite the running import's progress file.

Progress includes a progress bar, date range, current date (or last saved date when finished), total/processed/downloaded/reused/
empty days, item prices read, requests and errors. Saved coverage and actual
PostgreSQL table/index/TOAST size are also shown. The job checks free space on
the progress-file filesystem before each new day, preserving 2 GiB by default;
if PostgreSQL is hosted on another volume, monitor that volume separately.

The entire documented history is about 8,700 daily requests as of October 2026.
Two-second pacing alone is almost five hours; response times, retries and database
commits add time. This is a server background process and needs no open browser.
It needs psycopg2 and requests in the job interpreter; no scientific packages.

```sh
python scripts/import_zkill_prices.py
python scripts/import_zkill_prices.py --from 2026-10-01 --through 2026-10-12
python scripts/import_zkill_prices.py --schema-only
```

Configuration defaults to `~/eveosint/config/db.json`, status to
`~/eveosint/data/market/zkill_prices_progress.json`, and job log to
`~/eveosint/data/logs/zkill_prices_import.log`. CLI options also include
`--interval`, `--refresh-days` and `--min-free-gb`.
