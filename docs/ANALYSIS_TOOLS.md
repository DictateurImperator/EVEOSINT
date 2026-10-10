# Analysis tools

The top-bar **Tools** entry opens `/tools`. Its cards and contextual menu reuse
`entities.view` and `superintel.view`; no database menu migration is required.
Super Evolution, Ship Analysis and the global maps use this navigation context. Existing URLs
and entity-page tools remain available.

## Comparing entities

`/tools/population` and `/tools/economics` allow up to six alliances or coalitions
on one graph, with a shared period, indicator and activity window. Computation
starts only on **Compare**. Requests run sequentially; progress is shown and
**Stop** aborts loading while retaining completed values. An entity failure does
not discard other series. Changing settings clears obsolete results.

Population comparisons use the existing population-intelligence series APIs.
Official snapshot indicators use one request per entity. PvP series are fetched
in batches of seven days with a shared participation rule; they retain the
existing membership and activity semantics. Economic comparisons use the
existing monthly evolution API, including its bounded batches, territorial
estimates, monthly average population and MER-day-restricted attacker activity.
All PPA curves share the selected reference month. Entities without sufficient
economic data have no plotted values.

Graphs support wheel zoom, drag pan, reset, exact-value hover and CSV export of
the visible dates, raw returned values, entity IDs and shared parameters.
Unknown values remain empty. The summary compares the first and last available
point separately for each entity and labels those dates explicitly.

The map accepts `?mode=influence`, `?mode=heat` and `?mode=economy` to open the
requested tool directly; unrecognized modes retain the normal systems view.

Offline validation:

```
python -m unittest scripts.test_tools scripts.test_population_economics
python scripts/test_tools_browser.py
```
