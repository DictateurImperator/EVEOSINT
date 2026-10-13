# Killmail ISK values

`scripts/calculate_killmail_values.py` writes one nullable `BIGINT` named `estimated_value_cents` directly on every `rawkm.killmails` row. The value is stored in hundredths of ISK so no separate 95-million-row result table is needed.

The job requires the daily snapshots imported by `scripts/import_zkill_prices.py`. It sends no network request. Its only work queue is `rawkm.killmails WHERE estimated_value_cents IS NULL`; existing values are never recalculated. It processes and commits each affected raw killmail month separately.

The calculation follows the current public zKill pricing source:

- the market pricing day is the kill time minus 3,601 seconds, including the exact 01:00:00 UTC boundary;
- up to 34 positive daily prices are used, within zKill's calendar search limit (the number of dates in the item's complete history);
- two prices at each end are removed when 34 exist, or the two highest prices when 7–33 exist;
- the exact daily price caps a higher rolling average;
- known fixed prices, capsules, special ships, SKINs, blueprint copies, golden-pod implants and frigate-bay ships use the corresponding zKill rules;
- titans, supercarriers and structure modules use construction costs from the SDE; ordinary items also use construction when market history is missing;
- blueprint requirements use `ceil(0.9 * material quantity)`, divided by the recipe's output quantity, with recursive material pricing and zKill's distinct construction/market date offsets;
- construction prices retain fractional cents; the hull, destroyed items and dropped items are summed, then the final total is rounded once.

The public zKill daily endpoint contains market history, not a complete set of final unit valuations. The local `Pricing` engine reproduces the public [Price](https://github.com/zKillboard/zKillboard/blob/master/classes/Price.php) and [Build](https://github.com/zKillboard/zKillboard/blob/master/classes/Build.php) algorithms using the downloaded prices and current SDE recipes. No network request or additional permanent price table is needed. A missing price with no usable manufacturing recipe remains 0.01 ISK and is reported as an unresolved fallback. Progress identifies this formula as `zkill-like-v2`.

Original blueprints in a ship's hold retain their price. Explicit copies (`singleton=2`) are worth 0.01 ISK. For kills before ID 21112472, the importer marks ambiguous blueprints **inside containers** as copies, matching zKill; it does not apply that assumption to every blueprint in a hold. Existing flattened historical items do not preserve container depth: previously imported ambiguous copies require re-import from their original archive to recover this distinction.

Validation covered 53 distinct killmails from 2009, 2012, 2016, 2021 and 2026, including mining ships, freighters, capitals, supercapitals, structures, implant losses and blueprint cargo. Thirty reproduced the stored zKill total; the other 23 reproduced the sum of the item values currently displayed by zKill. A stored zKill total can differ from its current item-price display, so validation keeps these references separate. This is a category-spanning check, not a guarantee for every historical killmail.

Adding the nullable column is a metadata-only schema change on supported PostgreSQL versions. Filling existing rows creates new PostgreSQL row versions and WAL. The final logical payload is eight bytes per valued killmail, while temporary and physical table growth can be larger until PostgreSQL vacuums or compacts old row versions.

New and re-imported killmails have a null value. Re-importing a known killmail explicitly clears its old value because its item list may have changed. The daily update pipeline first imports every absent price day, regardless of killmail activity, then fills every empty killmail value. The same valuation job remains manually launchable for the first full calculation or an immediate catch-up.
