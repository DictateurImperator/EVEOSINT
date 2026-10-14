# Killmail ISK values

`scripts/calculate_killmail_values.py` stores one nullable `BIGINT`, `estimated_value_cents`, directly on each `rawkm.killmails` row. Its only queue is **`WHERE estimated_value_cents IS NULL`**, regardless of the kill's age. Existing values are not overwritten. New and re-imported killmails enter that same queue; re-import clears the value because the item list may have changed.

The manual Admin Jobs action and daily pipeline run the same command without special arguments. The pipeline imports all absent daily zKill price snapshots first, then calculates empty killmail values. The calculator itself sends no network requests and creates no permanent result table.

## Default: value at the date of the loss

The default is `--valuation-mode loss-day --recipe-version auto`:

- The reference date is the killmail's **UTC calendar day**, including kills before 01:00 UTC. Hull, fitted items, cargo and recursive construction materials all use that same date.
- Each date selects the latest archived CCP recipe set effective on or before it. The complete manufacturing chain uses that set, including output quantities and component recipes. Current recipes are never mixed into an old hull's construction.
- Market prices follow this strict order: **the exact day's positive price; otherwise the latest earlier positive price; otherwise the first later positive price**. An earlier quote always takes priority over a closer later one. There is no lookback limit and no averaging in this mode. Zero, negative and null observations do not supply a price.
- Existing fixed-price and item rules are retained: capsules, special ships, SKINs, blueprint copies, golden-pod implants and frigate-bay ships. Titans, supercarriers and structure modules use construction costs; other items also use construction when market history is missing. If an old construction recipe is unavailable, a market quote selected by the same date rule can still value the item.
- Manufacturing uses `ceil(0.9 × raw material quantity)`, divided by recipe output quantity. Fractional cents survive recursive construction. Hull, destroyed items and dropped items are summed before rounding the final total to cents.
- In-memory quotes are cleared when the imported price day or recipe set changes. A quote made from partial data cannot freeze an older value for the completed loss day.

Today's unfinished trading day and days beyond the imported price history stay empty until a later daily run. Price coverage must be continuous from January 2003 through the completed valuation horizon. The job does not silently skip missing historical price files.

The first positive quote for every type is loaded once from all completed imported price days, including days after the latest target kill. The normal chronological stream supplies earlier quotes as the job advances. This uses a bounded aggregate and small in-memory dictionaries, without a database query for each item, another price table or network requests. For example, a December 1, 2007 Raven with no earlier quote can use its first available December 17 quote. This is a dated substitution, not proof that the price was observed on December 1.

If construction is required before the earliest archived recipe and no market quote exists anywhere in the imported history, the whole killmail stays **NULL**, with an unavailable-recipe count in progress. Market-priced items and known fixed prices can still be valued on those earlier dates. The existing 0.01 ISK fallback for an unpriced item with no recipe in an otherwise available set remains counted separately as a price fallback; it is not a verified market value.

## Historical CCP manufacturing recipes

The bundled catalogue contains **157 snapshots, July 2014–October 2026**, representing 95 distinct complete manufacturing recipe sets, in about 132 KiB compressed. Raw material quantities, ordering and output yields are preserved as a base plus deltas. Each manifest entry records provenance and checksums; reconstructed sets are verified on loading.

Sources are the [Fuzzwork conversions of older CCP exports](https://www.fuzzwork.co.uk/dump/old/), [archived CCP SDEs at EVE Ref](https://data.everef.net/ccp/sde/), and [CCP's versioned SDE service](https://developers.eveonline.com/docs/services/static-data/). Snapshot dates are distinguished from actual patch dates: pre-release exports are not applied before the expansion, and confirmed recipe revisions have explicit effective dates. Archive coverage is finite; selection uses the last known set between snapshots, not an assertion that every historical hotfix was archived. On a patch day, selection is by UTC date rather than the precise downtime hour.

Notable revisions are documented in CCP's [April 2021 industry refactor](https://www.eveonline.com/news/view/patch-notes-version-19-03), [May 2022 Siege Green changes](https://www.eveonline.com/news/view/patch-notes-version-20-05), [June 2023 Viridian changes](https://www.eveonline.com/news/view/viridian-expansion-notes), [September 2024 changes](https://www.eveonline.com/news/view/patch-notes-version-22-01), and [March 2025 changes](https://www.eveonline.com/news/view/patch-notes-version-22-02). There is no extrapolation of these recipes back before July 22, 2014.

Normal SDE synchronization preserves the imported recipe set **before replacing it**, then retains the new set using CCP's release timestamp. These small compressed files live under `data/market/recipe_history`; identical recipe sets share one content-addressed file. They extend the bundled history without adding a database table or downloading another SDE archive. An already-imported build may need one small CCP change-journal request to recover its release timestamp on first retention.

```bash
# No database connection or network request.
python scripts/calculate_killmail_values.py --list-recipe-versions

# The same defaults used by Admin Jobs and the daily pipeline.
python scripts/calculate_killmail_values.py
```

To extend the bundled catalogue, supply an ordered manifest (`version`, `snapshot_date`, `source`, optional `date_kind`, `effective_from`, `effective_source`) and extracted `blueprints-<version>.json` arrays to `scripts/build_killmail_recipe_catalog.py --manifest ... --source-dir ...`. This packer is offline. Preserve provenance: a mirror's later modification time is not the release date of its contents.

## Optional zKill source replay

The previous source-replay engine remains available for explicit research. It retains zKill's trimmed rolling average and daily downward cap, kill time minus 3,601 seconds for the market date, separate recursive component date offsets, and versioned quote-cache rules. Replay requires an explicit recipe set; it does not choose an era from the loss date:

```bash
python scripts/calculate_killmail_values.py --valuation-mode zkill \
  --recipe-version 2022-05-10 --cache-rules-at 2022-08-01
```

These arguments affect the entire NULL-value queue. They are not needed for the normal loss-day calculation. An exact `--recipe-version current` or archived version can also override automatic selection; such an override intentionally stops following each kill's historical recipe date.

Price caching is implemented before summing the killmail. Market quotes are keyed by type/date, date buckets are extended by writes but not reads, and construction has its own 24-hour cache. Historical market-cache enablement, TTL changes and forced construction refreshes follow the researched zKill source revisions. All caches are bounded, in memory, and initially empty.

The daily endpoint contains market history, not all final quotes once stored in zKill's Redis. Recorded zKill totals can therefore differ from a loss-day valuation because they were calculated later, with later recipes or earlier cached prices. Matching such frozen totals is not the default objective. Regression fixtures for two old Hel losses still reproduce their recorded totals using the explicit 2022 replay profile; they test compatibility, not automatic profile selection for unseen kills. See the public [Price](https://github.com/zKillboard/zKillboard/blob/master/classes/Price.php) and [Build](https://github.com/zKillboard/zKillboard/blob/master/classes/Build.php) implementations.

## Progress, storage and verification

Stored estimates appear by default under the time in global and derived killboards (character, corporation, alliance, coalition, ship and location), and in the killmail detail header. Amounts use K/M/B/T; hovering shows the full amount to the cent. Hidden MER rows retain their MER loss value. A missing calculated value displays an em dash rather than zero. Display reads the existing row during normal page hydration and neither calculates nor changes values. Reading the optional field through `to_jsonb(km)` also lets pages load before the valuation job has added its column.

Admin Jobs shows the current partition, price-reading date, completed months, values written, remaining unvalued kills, fallback counts and active historical recipe. Completed months commit separately; stopping retains them, and resuming selects only remaining NULLs. The progress file identifies `loss-day-v6-price-dates` and the selected valuation mode. Existing values from earlier formula versions remain untouched by this update.

The final value payload is eight bytes per valued killmail. PostgreSQL row updates also generate WAL and obsolete row versions, so temporary and physical growth can exceed that logical payload. The job checks a free-disk reserve between months and uses an advisory lock against concurrent valuation runs.

Offline tests cover every bundled snapshot checksum, recipe changes and output yields, recursive same-day prices, cache invalidation and retention during SDE synchronization. PostgreSQL tests use an isolated disposable database and check NULL-only writes, UTC boundaries, unavailable prices/recipes and continuation after new daily price data arrives. No production valuation is started by these checks.
