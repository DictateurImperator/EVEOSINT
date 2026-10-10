# Alliance and coalition Economics

Population → Economics appears after Flows when imported regional MER data can be allocated to at least one sovereignty region held by the entity during the available history. It is not shown on corporation pages. No schema changes, writes to the database, external API calls or jobs are required.

## Allocation

For each reported month, reconstruct player sovereignty at the end of every UTC day from `sovereignty.reconciled_map`. For each region, sum the entity's held systems across the days and divide by the sum of all held systems across those days. This is mean held systems / mean regional sovereignty systems. Multiply each regional monthly NPC-bounties, mining and production amount by that share; sum allocated amounts across regions for each month. Months are never summed. The summary compares an observed month with a configurable base month, one row per indicator; the chart displays monthly evolution. Unclaimed systems are not part of the denominator. These estimates include economic activity by other groups in the territory.

Coalition rules, nested membership, validity dates and exclusions are evaluated daily. Including both an alliance and one of its corporations does not count a system twice. Historical ownership is applied forward through changes; current ownership is never applied backwards. Months preceding the first recorded sovereignty event are excluded. Missing economic measurements return a dash, rather than zero or a misleading partial total.

The regional breakdown exposes mean systems held, mean regional held systems and the allocation percentage for each month. The main table shows the observed monthly allocated amount and ratios, each followed by the absolute and percentage change against the base month. Green marks increases and red decreases. The default observed month is the latest available MER; the base is the previous calendar month. Missing baselines and zero bases have no percentage. ISK amounts use M/B/T abbreviations; hovering exposes the complete decimal value.

## Population and PvP

The population denominator is the arithmetic mean of the recorded population over the MER-covered days of each month, carrying the last known official daily population forward as in Global metrics. If any covered day has no known population, member ratios are unavailable. Coalition population uses the existing historical population aggregation, including its handling of overlap and missing member histories.

The configurable PvP window ends at each displayed month-end and intersects all available MER history, including earlier months when needed. The result for month X is independent of whether the display range starts at month X or earlier. Days in missing reports and beyond the final report are excluded. The default is 90 days, copied from Global metrics when first opening Economics. The page shows the effective coverage and all denominators.

Only known PvP killmails with a player victim and a player attacker establish active PvP participation. Active PvP means a distinct attacking character, irrespective of later departures. Alliance/corporation IDs on the killmail and coalition rules at combat time establish membership. Kills, losses and attacking characters are deduplicated across coalition members and temporal segments. NPC-only losses and pilotless victims are excluded. Hidden MER rows cannot identify active pilots and are not added to these denominators.

Ratios divide each monthly economic amount by average population, active PvP pilots. Loss and kill ratios have been removed. A zero denominator yields a dash. Mean population and PvP denominators are shown in a separate monthly table, with covered windows in the regional details. The chart provides monthly amounts and the same population/PvP ratios, with wheel zoom, drag pan, exact hover values, reset and CSV export of the visible range. Missing months interrupt the line. The comparison requests only its two months; the historical series endpoint remains batched six months at a time; chart requests twelve months at a time for economic/population views and one month at a time for PvP ratios, displaying each result immediately. PvP queries run only for the table or an explicitly selected PvP chart basis.

## Validation

Run `python -m unittest scripts.test_population_economics` and `python -m scripts.test_population_economics_browser` in the application's Python environment. Browser checks render the actual alliance and coalition population templates and test tab visibility, navigation, deep links, filters, means and error handling. The SQL aggregation was also executed against a rollback-only local PostgreSQL fixture, covering multiple attackers, NPC-only losses and excluded dates.

A read-only live check for Goonswarm Federation in August 2026 covered 31 MER days: mean population 71,082.516129, 6,259 attacking PvP pilots, 26,950 inflicted kills and 26,266 PvP losses. Regional allocation covers ten regions. The catalog and monthly result each completed in under one second during the check. Economic attribution is a territorial estimate, not an alliance wallet accounting report.


### Ratio loading

PvP activity now uses only the filtered attacker query; the loss query has been removed with the loss ratio. This avoids the previous OR across both sides of the attacker/victim join. Active character IDs are deduplicated across disjoint temporal membership segments. A bounded five-minute cache shares the resulting activity counts between table and chart views, and for the active-pilot basis. Its key includes the actual temporal coalition scopes.

The chart shows completed/total months and the month currently being calculated. Later failures leave previously received points visible. A read-only check of the March 2016 Goonswarm Mining / kill ratio over a March 2016–August 2026 display range returned the first point in 3.07 seconds; reusing those counts for the active-pilot ratio took 0.02 seconds. The split SQL queries also passed the rollback-only local PostgreSQL fixture.


### Monthly comparison

`/api/{kind}/{entity_id}/population-economics/comparison?base=YYYY-MM&observed=YYYY-MM&window=90` computes each month independently using the same allocation and PvP counters as the chart. Changes use exact Decimal subtraction and percentage arithmetic on the server. The base value, complete absolute change and percentage are exposed in hover text. A separate denominator table shows both monthly population means and PvP counts, with their changes. The graph has no daily or weekly measurement modes.


### Additional PPA indicators

Nominal NPC bounties, mining and production remain unchanged. Each has an additional PPA indicator, including monthly amounts, per-average-member and per-active-PvP-pilot measurements. The independent reference month defaults to the latest available MER and is selectable from months having a published CPI. The comparison base month is a separate control.

PPA amount = nominal amount × CPI(reference month) / CPI(amount month). Both compared months are independently converted before calculating absolute and percentage changes. Charts use the same conversion and reference; CSV exports include the reference month. The price index and ISK purchasing-power index are additional measurements with the reference set to 100, and can also be graphed.

All calculations use the global `Consumer Price Index` from the single `20_economy_indices` published chart series in `mer.global_economy_history` (`price_index_levels/index_level`). CPI values are positive and required at both the amount and reference dates; missing data yields unavailable PPA values rather than a nominal fallback. This measures purchasing power over time with a global price basket, not regional price differences or physical mining/production volume. No database migration or re-import is required when CPI levels are already imported.
