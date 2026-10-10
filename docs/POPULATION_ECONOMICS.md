# Alliance and coalition Economics

Population → Economics appears after Flows when imported regional MER data can be allocated to at least one sovereignty region held by the entity during the available history. It is not shown on corporation pages. No schema changes, writes to the database, external API calls or jobs are required.

## Allocation

For each reported month, reconstruct player sovereignty at the end of every UTC day from `sovereignty.reconciled_map`. For each region, sum the entity's held systems across the days and divide by the sum of all held systems across those days. This is mean held systems / mean regional sovereignty systems. Multiply each regional monthly NPC-bounties, mining and production amount by that share; sum allocated amounts across regions and months. Unclaimed systems are not part of the denominator. These estimates include economic activity by other groups in the territory.

Coalition rules, nested membership, validity dates and exclusions are evaluated daily. Including both an alliance and one of its corporations does not count a system twice. Historical ownership is applied forward through changes; current ownership is never applied backwards. Months preceding the first recorded sovereignty event are excluded. Missing economic measurements return a dash, rather than zero or a misleading partial total.

The regional breakdown exposes mean systems held, mean regional held systems and the allocation percentage for each month. The main table also shows monthly and daily average allocated amounts.

## Population and PvP

The population denominator is the arithmetic mean of the recorded population over all selected MER-covered days, carrying the last known official daily population forward as in Global metrics. If any covered day has no known population, member ratios are unavailable. Coalition population uses the existing historical population aggregation, including its handling of overlap and missing member histories.

The configurable PvP window ends on the final covered MER day and intersects the selected MER months. Days in missing reports and beyond the final report are excluded. The default is 90 days, copied from Global metrics when first opening Economics. The page shows the effective coverage and all denominators.

Only known PvP killmails with a player victim and a player attacker count. Active PvP means a distinct attacking character, irrespective of later departures. Alliance/corporation IDs on the killmail and coalition rules at combat time establish membership. Kills, losses and attacking characters are deduplicated across coalition members and temporal segments. NPC-only losses and pilotless victims are excluded. Hidden MER rows cannot identify active pilots and are not added to these denominators.

Ratios divide the selected economic totals by average population, active pilots, losses and inflicted kills. A zero denominator yields a dash. Economic coverage and the configured PvP coverage are displayed separately, so shorter activity windows remain visible to the user.

## Validation

Run `python -m unittest scripts.test_population_economics` and `python -m scripts.test_population_economics_browser` in the application's Python environment. Browser checks render the actual alliance and coalition population templates and test tab visibility, navigation, deep links, filters, means and error handling. The SQL aggregation was also executed against a rollback-only local PostgreSQL fixture, covering multiple attackers, NPC-only losses and excluded dates.

A read-only live check for Goonswarm Federation in August 2026 covered 31 MER days: mean population 71,082.516129, 6,259 attacking PvP pilots, 26,950 inflicted kills and 26,266 PvP losses. Regional allocation covers ten regions. The catalog and monthly result each completed in under one second during the check. Economic attribution is a territorial estimate, not an alliance wallet accounting report.
