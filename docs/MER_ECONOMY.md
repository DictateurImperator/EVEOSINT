# MER economic import

`scripts/import_mer_economy.py` is a manual importer for the ZIP archives already
downloaded by EVEOSINT. It does not download anything, extract archives, read kill
CSVs, schedule work or change existing killmail tables.

Use the application's Python environment, under the server account that owns
the application and its database configuration. The default configuration is
`~/eveosint/config/db.json`; an account with schema/table creation and insert/update
permissions is required. `codex_ro` is not used for imports.

```bash
# Validate existing archives without connecting to PostgreSQL.
python scripts/import_mer_economy.py --dry-run

# Create the tables/views without importing records.
python scripts/import_mer_economy.py --schema-only

# Create missing tables and import all local reports, newest first.
python scripts/import_mer_economy.py

# Restrict the report months, inclusively.
python scripts/import_mer_economy.py --from 2025-01 --to 2026-08

# Explicit file/month for an unfamiliar archive filename.
python scripts/import_mer_economy.py --archive /path/report.zip --month 2026-08

# Override paths without changing application configuration.
python scripts/import_mer_economy.py --archive-dir /path/archives --db-config /path/db.json
```

The matching DDL is also available separately in `scripts/mer_economy_schema.sql`.
Repeated executions are safe. `--force` reprocesses an unchanged economic payload.

## Storage

| Table | Contents |
| --- | --- |
| `mer.region_economy_monthly` | Regional ISK production, mining, destruction, trade, imports/exports, NPC bounties and LP; mining/waste volumes; lunar values by source; WH trade by class/category. |
| `mer.global_economy_history` | Published dated money balances, ISK velocity, trade volume, mining/production/destruction, mining volumes by security band, lunar quantities by material class, price-index components and published index levels. |
| `mer.isk_flow_history` | Signed ISK sinks/faucets, commodity flows and historical net-flow series, retaining the categories actually published by CCP. |
| `mer.economy_imports` | Report month, archive path, economic fingerprint, parser version, source-member manifest, counters, status and error. |
| `mer.economy_unmapped_rows` | Original rows containing new/unrecognized columns or datasets, flagged for review instead of being discarded. |

Facts use one numeric metric per row, with units and JSON dimensions. This allows
older/sparser MER formats and additional measurements without adding columns to
every table. Values use exact `NUMERIC`, not binary float for CSV values. Empty
and unavailable values stay absent; an explicit zero remains zero.

Regional scope distinguishes real SDE region IDs, unresolved region names,
published space aggregates, and wormhole classes. Name-only rows are resolved
using `public.sde_mapregions`; unknown historical/future names are retained as
`region_name`, counted in the log, and never silently assigned to a different
region. WH-class totals do not become per-system measurements.

Aliases cover historical CamelCase/dotted and current snake_case CSV headers,
numeric prefixes, old filenames and leading Pandas row-number columns. `CPI`
normalizes to `Consumer Price Index`; historical `metanox_mining` normalizes to
`metenox_mining`. Regional lunar `quantity` is **market value in ISK**, as confirmed
by CCP's regional graph. Lunar class-history `quantity` is material quantity,
not an ISK amount. Refinery quantities are CCP's estimates using their published
refining assumptions; the importer does not recompute them.

Published index levels are read from the supported self-contained Plotly HTML
charts, including encoded numeric arrays. Annotation markers are excluded. The
full-history economic indices and ship/module indices retain separate chart
dimensions because their baselines differ. Where older reports only contain
index-component CSVs, their price-change factors/weights are stored; an index
level is not invented from an incomplete basket.

Static reference files (index baskets, ore/type/system mappings, markers and
pricing references) are outside this economic-fact import. Unknown economic CSVs
are retained in the unmapped table, not treated as successfully normalized metrics.

## Reimports and load

The fingerprint covers selected economic member names, CRCs and sizes, not the
kill dump or entire archive. Economic payloads with an unchanged fingerprint and
parser version are skipped. Newer report months win when histories overlap;
older archives fill missing facts and cannot overwrite newer corrections.
Latest reports are processed first to reduce unnecessary updates.

CSV parsing is streamed and database writes use batches of 1,000 facts. Each
archive's facts are committed in one transaction. A failed reimport rolls back
its replacements and retains previously imported data, while saving a retryable
failure status separately. Other archives continue; any failed archive makes the
command exit with status 1. A PostgreSQL advisory lock prevents concurrent runs.

The MER catalog shows `imported` or `imported_with_unmapped` after a successful
archive commit. Counters report parsed rows/facts, not a claim that every fact was
new: historical overlaps are deduplicated in the database.

## Calculated views

- `mer.region_economy_evolution`: absolute monthly change and monthly/yearly
  percentage change, using matching calendar months and dimensions.
- `mer.global_economy_monthly`: first/last **published observation** and their
  dates, mean, monthly/yearly change, trailing three-calendar-month mean of last
  values, and sample volatility of consecutive-day percentage changes.
- `mer.isk_flow_monthly`: monthly sums of published flows, keeping daily and
  monthly-grain sources separate to prevent double counting.

Missing comparison periods and division by zero yield NULL. Missing daily dates
are not treated as consecutive returns. First/last observations are not necessarily
balances at exactly midnight on the first/last day of the month; coverage is
explicit. A partial month remains partial. A three-month mean can contain fewer
observations if months are absent.

For inflation, use `price_index_levels/index_level` with its series/chart
dimensions. Component factors and weighted contributions are separate measures;
their generic month-to-month changes are not the headline inflation rate.
Signed sinks are preserved. Do not add commodity-flow detail to main sink/faucet
totals: these datasets can describe overlapping activity. Likewise, an exchange
between players is not automatically an ISK faucet/sink, and published flow
totals are not assumed to reconcile perfectly with balance changes.

## Validation

```bash
python -m unittest scripts.test_mer_economy -v
```

The PostgreSQL tests run only with an explicit
`EVEOSINT_FORENSICS_TEST_SOCKET` for the existing local test lab on port 55444.
Never point them at a production database. Tests cover precision, missing values,
regional/WH scopes, signed daily versus monthly flows, historical aliases,
encoded chart series, ignored annotations, preserved unknown data, no kill-file
reads, deduplication, newer-report precedence, transactional rollback, calendar
comparisons and daily volatility.
