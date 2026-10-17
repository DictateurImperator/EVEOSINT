# Monthly ISK rankings (Admin Debug)

`/admin/debug` now opens `/admin/debug/rankings`. The previous experimental
sovereignty dashboard is no longer the Debug landing page; no sovereignty data is deleted.
Access uses existing `admin.jobs.view` / `admin.jobs.run` permissions.

## Rules

- Full-value mode: each distinct character, corporation and alliance appearing
  among attackers receives the full killmail value **once**. Multiple members do
  not multiply their corporation/alliance credit.
- Damage mode: value × the entity's combined damage / all recorded attacker
  damage. NPC/object damage stays in the denominator; NPC corporations recorded
  on killmails can also appear in corporation rankings. Negative/NULL damage is
  treated as zero. Without positive total damage the weighted credit is zero.
- Losses always count in full, including NPC losses and player-owned structures.
  Character rows require a character ID; structure losses still credit their
  corporation/alliance. Every affiliation comes from the killmail itself.
- Unvalued killmails contribute counts but no ISK. The page shows price coverage
  and incomplete months explicitly. Efficiency is destroyed / (destroyed + lost).
- UTC calendar months; one sparse row per active entity/month. A row exists only
  when the entity has a kill or loss. Both ISK modes share that row. **No cumulative
  totals and no per-kill ranking copies are stored.**

## Build / refresh

Deploy, open Admin Debug and click **Build / refresh rankings**. The same controls
are linked from Admin Jobs. The job creates its own tables from
`scripts/killmail_rankings_schema.sql`. Progress reports the current month, stage,
completed months and saved monthly rows; a stop button and job log are available.
Completed months are committed separately. Stopping/failure rolls back the active
month; retry resumes missing/changed months. No source killmail, item, attacker,
hash or stored killmail valuation is modified by this builder.

Daily orchestration refreshes rankings **after** price recovery and valuation,
but skips until a manual initialization has produced at least one month. Normal
imports, changed archive imports and valuation mark affected months in the same
transaction as their changes. A rebuild replaces that month's ranking rows using
a repeatable-read source snapshot. Concurrent changes increment a revision and
remain pending for the next run; they cannot be silently marked up to date.
An out-of-band SQL change can be handled with an explicit forced rebuild:

```sh
python scripts/build_killmail_rankings.py --force --from-month 2026-01 --through-month 2026-03
```

Do not start production initialization as a deployment side effect. The first
all-history build is intentionally initiated by the administrator. Existing raw
partitions are discovered from PostgreSQL's catalog rather than scanned by the
web page. The builder uses at most two parallel query workers, 32 MB work_mem
per SQL operation and a 20 GiB free-space reserve checked before each month.
Temp files may spill to disk; time and final size depend on the actual history.

## Period queries and caching

The page opens directly on the all-time top 1,000 entities (50/page), from the
earliest built month to the latest. Month/period selection is optional; leaving
both date fields empty restores all time. Use the same month in both fields for
a single-month ranking. Builder controls and coverage details are collapsed below
the results after initialization. Single-month queries sort the
existing facts directly. Longer periods sum the selected months and sort the
result, then fetch names only for the top rows. Query time is limited to 25s;
longer selections show a clear timeout message. Ranking queries allow up to two
concurrent calculations per web process. Repeated requests for a calculation
already running ask the user to retry, rather than duplicate the database work.

A bounded in-memory cache retains at most 16 top-1,000 results for ten minutes
**per web process**. Pagination reuses it. The committed build timestamps invalidate
cached periods after a rebuild. It contains no permanent cumulative facts; a
worker restart clears it, and another web worker has its own cache.

Synthetic disposable PostgreSQL 16 benchmark on 2026-10-17:

| Dataset / query | Result |
| --- | --- |
| 3,000,000 distinct pilots, two months | 6,000,000 monthly rows |
| Monthly facts + primary index | 748,158,976 bytes (~713.5 MiB) |
| Top 1,000, one month | 0.82 s |
| Top 1,000, two months | 7.61 s |
| Same period from in-process cache | <1 ms (excluding HTTP/catalog query) |

This measures synthetic data, **not the live all-time ranking**, and does not
predict the cost of summing every historical month. No production build was run.
Tests cover per-entity deduplication, NPC damage, zero damage, old affiliations,
NULL values, sparse rows, monthly boundaries, idempotent replacement, rollback,
concurrent invalidation, CLI resume, permissions and browser rendering.
