# ISK rankings: monthly activity and sparse cumulative snapshots

`/admin/debug` opens `/admin/debug/rankings`, under the existing
`admin.jobs.view` / `admin.jobs.run` permissions. It displays the current all-time
ranking immediately. An optional **Through** month selects a historical ranking;
the beginning is always the start of the available history.

## Credit rules

- Full-value mode: each distinct attacker character, corporation and alliance gets
  the full kill value once. Several members never multiply their entity's credit.
- Damage mode: value × combined entity damage / damage from all recorded attackers.
  NPC/object damage stays in the denominator. NPC corporations can appear in the
  corporation ranking. NULL/negative damage is zero; without positive total damage,
  weighted credit is zero.
- Losses count in full, including NPC losses and owned structures. Affiliations
  come from each killmail, rather than current memberships.
- Unvalued killmails contribute counts but no ISK; coverage is shown explicitly.
  Efficiency = destroyed / (destroyed + lost).

## Stored facts

`analytics.killmail_rankings_monthly` contains one row per entity and **active UTC
month**. Each row contains both the month's activity and its cumulative total from
the beginning. Both full-value and damage-weighted amounts are stored together.
A month is active if the entity has at least one kill **or loss**. No zero-activity
months are inserted; no dense entity × calendar-month matrix is generated.

Example: January has monthly 100 / cumulative 100; February and March have no rows;
April has monthly 50 / cumulative 150. A request through March uses January's
snapshot. Loss-only entities are retained.

`analytics.killmail_rankings_current` keeps only the latest total and last active
month per entity. Indexed destroyed/weighted/lost amounts make the current top
1,000 a small indexed read. An inactive entity receives no new monthly row and no
current-total update. The historical query reuses a current total when its last
activity predates the requested cutoff; otherwise it reads the most recent active
snapshot on or before that cutoff. It does not sum the full history per visitor.

An old changed month is rebuilt from source KM. The difference between old and new
monthly facts updates the current total and **only later existing snapshots** for
the affected entities. Everything commits atomically with that month's replacement.
A snapshot is removed if its month becomes inactive; entities with no remaining
activity are removed from the current totals. Repeated identical rebuilds do not
accumulate credits, including fractional damage shares.

## Deployment and one-time upgrade

Deploy the branch, open Admin Debug, then click **Build / refresh rankings** once.
The job creates/updates its tables using `scripts/killmail_rankings_schema.sql`.
It fills cumulative columns and current totals from the **existing monthly facts**;
it does not reread all source killmails for this upgrade. Clean monthly source
calculations are skipped. The page explains when this initialization is needed.
Until it completes, the page does not trigger the previous expensive all-time sum.

The initial upgrade writes the existing monthly ranking rows and creates indexes,
so it needs additional disk space and time. It runs as one transaction and can be
stopped/rolled back. The normal build remains one transaction per changed month,
with previous completed months preserved. Progress includes the cumulative
initialization phase, current month, saved rows and completed months; stop and log
controls remain available. No source KM, attacker, item, hash or KM valuation is
changed by this job. No production upgrade is started automatically by this patch.

Daily orchestration runs it after price recovery and valuation. Before the first
manual initialization, the daily step skips. Import and valuation revisions mark
changed months in the same source transaction; concurrent imports remain pending
for the next build. Out-of-band SQL corrections can be handled explicitly:

```sh
python scripts/build_killmail_rankings.py --force --from-month 2026-01 --through-month 2026-03
```

Those CLI bounds select **months to rebuild**, not a ranking's start date.

## Result cache and bounds

Only the top 1,000 rows of requested rankings are cached (50/page): at most 16
in-memory results per process and 64 atomic JSON result files under
`~/eveosint/data/cache/killmail_rankings`. Shared file locks prevent separate web
processes from recalculating the same period simultaneously. Files are keyed by
source database, filters, query version and committed monthly build timestamps;
they remain valid until data changes, instead of expiring every ten minutes.
A web restart can reuse them. These small cached results are separate from the
complete current facts and sparse monthly snapshots. No full entity histories are
serialized into the cache. Cache failures fall back to database reads.

SQL uses at most two parallel workers and 32 MiB work_mem per operation; web queries
have a 25-second statement limit. Very old dates can still need many snapshot
lookups and a sort; repeated views reuse the shared cache. The builder checks a
20 GiB free-space reserve before initialization and each changed month.

Tests use disposable PostgreSQL and cover sparse carry-forward, monthly versus
cumulative totals, old corrections, removal of empty entities, all ranking modes
and sorts against independently summed monthly facts, exact fractional-credit
rebuilds, cache invalidation/restart, CLI resume and transaction rollback.

Synthetic disposable PostgreSQL 16 benchmark (2026-10-17): current top 1,000
from 3,000,000 entity totals / 6,000,000 monthly facts: **0.032 s without cache**.
The previous two-month grouped query took 7.61 s on the same synthetic population.
This does not measure live server latency or historical snapshot lookup cost.

## Search, scope and affiliations

The inline **Find in ranking** controls accept a name (with suggestions) or a rank
directly on the ranking page; there is no separate search tab. It returns five
neighbours before/after the target where available and highlights the requested
entity/rank. It works beyond the cached top 1,000; no whole-history KM scan is
needed. Equal scores are ordered by entity ID for deterministic positions.

Membership filters include/exclude coalitions, alliances or corporations. Several
includes are ORed; exclusions win. Coalition rules support nested coalitions,
dated memberships and their include/exclude rules. Positions and neighbours are
recomputed **inside the filtered ranking**. An alliance ranking's picker offers
coalition/alliance filters; corporation membership applies to characters.

The scope is membership **today** for the default current view, or at the end of
the requested month for historical views. Filtering selects members; their score
remains their full career score through the cutoff, not only kills made while in
that group. Historical gaps are not filled with current affiliations.

Visible rows are enriched in a batch: corporation/alliance names and logos replace
raw entity IDs under names. Membership metadata is read when displaying the rows,
so a cached score does not freeze current memberships. Known character/corporation
history is used for historical month-end affiliations. Where unavailable, the page
says so. Existing earned/lost credit still uses affiliations recorded on killmails;
this display/filter metadata does not redistribute that credit.

The membership picker shares the standard killboard/tool filter styling: rich
suggestions with logos, entity types and tickers, and selected entity rows with
per-item +/− toggles and removal. No separate include/exclude dropdown is needed.
The common stylesheet is included by both the original filter and rankings.
