# Economic mode on the 2D EVE map

Open **2D EVE Map → Economy**. The page uses the economic tables populated by
**Admin MER → Import economic data**. It never starts an import or downloads data.
The existing `entities.view` permission allows reading this mode.

Select mining, production, destruction, trade, NPC bounties, imports or exports.
Availability depends on the published MER; recent reports do not contain all
historical indicators. These measures all use ISK. Detailed mining volumes, LP,
global balances and moon breakdowns are not added to regional ISK totals.

- **Fixed period** displays a month or sums a selected range of months.
- **Monthly evolution** loads the series once and provides Play/Pause, a month
  slider and playback speed. Changing the mode, filters or period stops playback.
- **Separate indicators** assigns each indicator its own light and legend color.
- **Combine selected indicators** sums their published amounts. This composite
  is the selected combination, not an estimate of GDP; activities may overlap.
- **Value / regional production** uses the region's production in the same
  period. Across several months it is `sum(values) / sum(production)`, not an
  average of ratios. The tooltip shows the actual ratio, which may exceed 100%.
- **Value in ISK** displays absolute published amounts, including where relative
  ratios cannot be calculated.

Full brightness is set from the maximum of the selected series, and remains
constant throughout the animation. A region is plotted once at the center of its
systems; its value is not multiplied by its number of systems. Hover to see the
period, production, individual/combined value and ratio; click to open its region
map. Both the static SVG and interactive canvas support this mode. SVG animation
updates only the economic layer, keeping the base map and zoom in place.

Missing values are never interpreted as zero. A range total requires the indicator
for every requested month, and a ratio additionally requires production for every
month and a positive total production. A combined value requires every selected
indicator. Unavailable values use gray outlined markers and explain why in the
tooltip; published zeros remain distinct. MER scope aggregates such as Wormhole
are listed as unplaced when CCP does not publish individual regional figures.
Genuine region-level WH data, if present, can use the Anoikis region positions.

The read-only APIs `/api/map/eve-2d/economy/options` and
`/api/map/eve-2d/economy?from=YYYY-MM&to=YYYY-MM&metric=mining_isk&evolution=true`
select only the `regional` dataset with empty dimensions and ISK units. This
avoids adding overlapping datasets or accidentally mixing units. Requests allow
up to 240 months; all dates and indicator keys are validated before querying.
Money values are serialized as decimal strings; ratios are computed with Decimal
before conversion for display. No new database migration is needed.

Validation: `python -m unittest scripts.test_map_economy -q` and
`python -m scripts.test_map_economy_browser`. The database integration test uses
only the explicit disposable PostgreSQL socket selected by
`EVEOSINT_FORENSICS_TEST_SOCKET`, port 55444, and checks the API queries in a
read-only database session after preparing its fixture.
