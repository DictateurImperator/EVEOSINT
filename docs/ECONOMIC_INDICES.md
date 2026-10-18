# Debug economic indices

`/admin/debug` now contains an experimental comparison of fixed baskets.
Access uses the existing `admin.jobs.view` permission. Viewing the page loads
only SDE metadata and available price dates. A calculation starts only after
**Calculate indices**; a new selection or **Stop** cancels the owner's previous
scan without cancelling another user's scan.

## Measure

For fixed quantities `q_i`, reference date `b`, and comparison date `t`:

    cost(t) = sum(q_i * price_i(t))
    index(t) = 100 * cost(t) / cost(b)
    price change (%) = index(t) - 100

Built-in baskets use one unit of each eligible object, not equal expenditure
per type or market trading volumes. Custom baskets accept positive quantities
and save their names and contents in browser local storage, not the database.
Consequently, a high reference-cost object contributes more to the index.
Built-in families and their sub-indices overlap and must not be added together.

## Source and coverage

The only price source is `market.zkill_daily_prices`. These are zKill valuation
quotes, not guaranteed historical market transactions or CCP's published index.
No new external API calls, market import, killmail scan or persistent analysis
facts are created. Recovered-price and artificial fallback tables are not used.

The scan replays sparse positive quotes chronologically. An object's price on a
date is its last positive quote on or before that date. Future quotes never fill
earlier dates. The UI exposes actual reference/end quote dates and warns about
carried prices, which may be stale.

Objects without a positive reference quote are explicitly excluded from the
entire series; counts, exclusions and contents are shown. Earlier points missing
any eligible object are gaps, not partial indices. New hulls do not enter midway.
The chart includes month-end points plus exact start, reference and end dates.

## SDE classification

Ship classes aggregate specialized SDE groups into their hull size, including
industrial transports, barges/exhumers, and separate Orca/Porpoise baskets.
Capital groups include freighters, jump freighters, capital industrial ships,
lancer dreadnoughts and command carriers. Supercarriers and titans belong only
to Supercapital. Triglavian faction/race takes precedence, including its T2 hulls
and capital hull. Sub-indices split T1/T2/T3, Navy/Pirate market ancestry, and
other faction variants. Capsule and Special Edition Ships market families are
excluded from built-in ship baskets. Abyssal objects are excluded.

Module and material baskets use SDE groups, with module meta-group variants.
Custom baskets can include any available published market object except abyssals.

## Resource bounds

Scans read one month's price observations at a time in a read-only repeatable
snapshot with a 60-second statement timeout. Two workers maximum; six built-in
curves plus one custom basket (100 items maximum), with 6,000 distinct objects
maximum. Results are short-lived in RAM with bounded retention; no result tables
or daily precomputation. Work stops after progress has not been requested for
60 seconds (checked between source queries).

## Verification

    python -m unittest scripts.test_economic_indices
    python -m scripts.test_economic_indices_browser

SQL tests require an explicit disposable PostgreSQL socket under `/tmp`, port
55444, supplied through `EVEOSINT_FORENSICS_TEST_SOCKET`. Never use production.
