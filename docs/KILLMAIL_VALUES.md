# Killmail ISK values

`scripts/calculate_killmail_values.py` writes one nullable `BIGINT` named `estimated_value_cents` directly on every `rawkm.killmails` row. The value is stored in hundredths of ISK so no separate 95-million-row result table is needed.

The job requires the daily snapshots imported by `scripts/import_zkill_prices.py`. It sends no network request. Its only work queue is `rawkm.killmails WHERE estimated_value_cents IS NULL`; existing values are never recalculated. It processes and commits each affected raw killmail month separately.

The calculation follows the current public zKill pricing source:

- the pricing day is the kill time minus one hour;
- up to 34 earlier non-null daily prices are retained per item;
- two prices at each end are removed when 34 exist, or the two highest prices when 7–33 exist;
- the exact daily price caps a higher rolling average;
- known fixed prices, capsules, special ships, SKINs, blueprint copies, golden-pod implants and frigate-bay ships use the corresponding zKill rules;
- the hull, destroyed items and dropped items are summed into the one stored total.

zKill can fall back to its private build-price calculation for a market price at or below 0.01 ISK. That private derived value is not present for every item in the public daily endpoint. EVEOSINT uses 0.01 ISK in that case and reports the number of affected date/type pairs in Admin Jobs. The stored figure is therefore an estimate identified as `zkill-like-v1` in job progress.

Adding the nullable column is a metadata-only schema change on supported PostgreSQL versions. Filling existing rows creates new PostgreSQL row versions and WAL. The final logical payload is eight bytes per valued killmail, while temporary and physical table growth can be larger until PostgreSQL vacuums or compacts old row versions.

New and re-imported killmails have a null value. Re-importing a known killmail explicitly clears its old value because its item list may have changed. The daily update pipeline first imports every absent price day, regardless of killmail activity, then fills every empty killmail value. The same valuation job remains manually launchable for the first full calculation or an immediate catch-up.
