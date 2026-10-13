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

The public zKill daily endpoint contains market history, not a complete set of final unit valuations. The local `Pricing` engine implements the public [Price](https://github.com/zKillboard/zKillboard/blob/master/classes/Price.php) and [Build](https://github.com/zKillboard/zKillboard/blob/master/classes/Build.php) algorithms using the downloaded prices and supplied SDE recipes. No network request or additional permanent price table is needed. A missing price with no usable manufacturing recipe remains 0.01 ISK and is reported as an unresolved fallback. Progress identifies this formula as `zkill-like-v3-cache`.

Price caching is part of the calculation, before summing a killmail. Quotes are keyed by item type and market date and survive market-history refreshes. Writing a new quote extends the expiry of the whole date bucket, matching zKill's Redis hash; reading it does not. Construction has a separate item/date cache lasting 24 hours. Expired entries are recomputed from the available history. In-memory caches are bounded and add no permanent database storage.

`Pricing(..., clock=..., rules_at=...)` can replay a specified cache-rule era; the base market formula remains the current one. The engine defaults to the current source rules and real UTC time. The era must refer to the valuation/source version, **not the loss date**: an old kill can be revalued years later. The source history establishes these changes:

- market-cache returns were disabled on January 13, 2019, and [restored on October 23, 2023](https://github.com/zKillboard/zKillboard/commit/4d9d48d46205bf37bdbcc3d6db70d4dd75133856);
- before [November 21, 2025](https://github.com/zKillboard/zKillboard/commit/3dade541384b815def95cfcb2c3bd7c09318f175), market date buckets expired after 24 hours; the later code keeps that lifetime for recent dates and uses one hour for older ones;
- construction started honoring the forced-recalculation flag on [March 2, 2024](https://github.com/zKillboard/zKillboard/commit/6b56f5526ab7), which affects structure-module pricing.

These are source revision dates; deployment timestamps are not established. The current cache starts empty. Its rules do not reveal the unit quotes in zKill's old Redis instance. Reconstructing a recorded historical valuation also requires its recipe version, history available at quote creation and initial cache state. The job does not choose between daily prices by examining the expected zKill total, nor apply hand-selected per-item day offsets.

Original blueprints in a ship's hold retain their price. Explicit copies (`singleton=2`) are worth 0.01 ISK. For kills before ID 21112472, the importer marks ambiguous blueprints **inside containers** as copies, matching zKill; it does not apply that assumption to every blueprint in a hold. Existing flattened historical items do not preserve container depth: previously imported ambiguous copies require re-import from their original archive to recover this distinction.

Research covered 53 distinct killmails from 2009, 2012, 2016, 2021 and 2026, including mining ships, freighters, capitals, supercapitals, structures, implant losses and blueprint cargo. The original current-recipe calculation matched 30 recorded totals. Archived 2022–2023 recipes and historical floating-point sums allowed exact reconstructions of all 39 older cases. Cache-history investigations also found matching configurations for the remaining 14 recent cases; the last seven matched all five zKill valuation fields. Those configurations were identified with knowledge of the recorded totals. They establish reproducibility, **not 53 independent predictions or an automatic rule for unseen kills**. Current page item sums are not accepted substitutes for recorded totals.

A subsequent independent check calculated eight new Hel/Erebus values before fetching their zKill reference totals. Current recipes with an empty current-rule cache matched **1/8**. A predeclared hypothesis using May 2022 recipes/cache rules for pre-2023 losses, and current inputs afterward, matched **5/8**: all five earlier cases were exact. The three later cases still differed by -1.489295%, -0.618725% and -4.941680%. This does not validate an automatic historical recipe/initial-cache selection rule. The production job still loads the current SDE and does not apply that historical-input hypothesis.

Adding the nullable column is a metadata-only schema change on supported PostgreSQL versions. Filling existing rows creates new PostgreSQL row versions and WAL. The final logical payload is eight bytes per valued killmail, while temporary and physical table growth can be larger until PostgreSQL vacuums or compacts old row versions.

New and re-imported killmails have a null value. Re-importing a known killmail explicitly clears its old value because its item list may have changed. The daily update pipeline first imports every absent price day, regardless of killmail activity, then fills every empty killmail value. The same valuation job remains manually launchable for the first full calculation or an immediate catch-up.
