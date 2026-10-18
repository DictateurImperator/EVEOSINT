# Personal groups

Access: account username menu → **My groups**. Accounts with `entities.view` can
own three groups. Definitions combine characters, corporations, alliances and
coalitions, with include/exclude rules, individual inclusive UTC dates, optional
group dates, combat areas and ship/model-group filters. Overlapping rules never
multiply a pilot, kill or damage credit. Membership follows the studied date;
killmail affiliations take precedence over historical fallback, never today's
membership. Coalition history is read when the requested result is calculated.
The editor uses the same grouped search renderer as the top bar. A separate
**Avoid** list accepts up to 60 pilots, corporations, alliances or coalitions.
A victim matching Avoid and killed by the group is removed from all group combat results
(killboard, stats, top kills and local ranking). This is independent of membership
exclusions: the pilotable-ship roster remains intact. Losses caused by opponents
remain visible; internal losses against an Avoid target are hidden. Coalition
Avoid rules follow historical composition. MER Hidden/TOTAL can match whole
corporations/alliances/coalitions; individual Avoid pilots are matched only when
a unique resolved raw killmail identifies them, never guessed on Hidden rows.

## Calculate only on request

There is no preparation button, preparation endpoint, scheduled group job,
precomputed monthly group table, persistent result or saved member roster.
Private group pages are management only, including legacy private result URLs.
They do not load result data or query source-period metadata; private data endpoints
return 404. Opening the bare public link is also idle. Clicking an allowed result
page fetches its data endpoint, which calculates only the requested view and period. Available all/year/month choices are derived from
imported partition metadata and clipped to the group dates and current UTC day.
Public visitors cannot supply arbitrary dates or change the saved scope.

- Combat stats scan the selected months and display provisional totals and
  monthly history as each month completes. Kills/losses include unvalued entries;
  incomplete ISK totals are identified. True ISK divides matching group damage
  by damage from all attackers, including NPCs. Losses count in full. A victim
  belonging to the group makes the event a loss only: friendly fire never adds
  kill credit, Raw/True ISK destroyed, top kills or local-ranking attack credit.
- Top kills are a separate explicit Combat stats request. The scan keeps only
  the best 20 raw and True candidates per month and merges these bounded lists,
  using the standard top-kills renderer. Opening monthly history does not run it.
- Pilotable ships calculate only the requested interval's distinct known members,
  including inactive members, and read existing latest inferred skills. Combat
  areas and ship filters restrict killmails, not this roster. Historical skill
  levels and uncollected inactive members cannot be reconstructed. Coverage is
  shown. The ship tree uses the same component as standard entity pages.
  Supercapital visibility follows the owner's current permission.
- Killboards use the standard shared renderer and styles, KILL / VICTIM / ALL
  selection and fixed-scope source data in pages of 100, checking at most three
  months per API request, with continuation cursor and visible progress.
  API / TOTAL / Hidden use the same semantics as standard corporation/alliance
  killboards. TOTAL and Hidden reuse the standard MER scanner and hydration.
  Whole corporations, alliances and historically dated/nested coalitions can
  match MER; character IDs and damage shares cannot be invented from MER.
  Saved area, ship and individual membership date/exclusion rules still apply.
  Pagination uses the standard MER timestamp/month/source-row cursor.
- Local rankings scan the requested period in the background. Characters,
  corporations and alliances get credit only for group members' contributions.
  A corporation/alliance gets full value once per kill, regardless of how many
  matching pilots participate. True credit uses their combined matching damage.
  A month is ranked without lifetime cumulative scores. Top 1,000, paged by 50.
  Results use the same ranking table and styles as the standard leaderboard.
  Exact running scores use a connection-local PostgreSQL temporary table that
  disappears on connection close; no permanent source table is changed.

Stats/ship results use a bounded RAM cache of **eight entries for two minutes**.
Local rankings use at most **eight top lists for five minutes**. Rendered
killboard fragments use at most **32 entries for two minutes**. Expired entries
are removed by a periodic reaper even if no later request occurs. Revisions and
actual range dates are in cache identities, so edits/custom periods cannot reuse
old results. Permission checks happen before all cache lookups.

One stats/ship scan and one local-ranking scan can run at a time, each guarded by
a PostgreSQL session advisory lock. Source statements have a 120-second limit.
Each browser tab carries a private random scan identity and an increasing
selection generation. Changing page, period or ranking selection detaches the
previous request immediately, cancels its active PostgreSQL statement and allows
the replacement scan to start. Late responses/polls cannot revive an older
selection. Shared work continues if another visitor still needs that result.
Completed cached results survive navigation. Fetches and poll timers also stop
on navigation; a beacon detaches the departing tab. Background scans additionally
stop at their next checkpoint when no page has polled for 60 seconds. Closing the source connection releases the lock and temporary scores.
All permanent source tables are read-only during calculations.

## Publication and delegation

Publishing requires no prior calculation. The owner or a delegated manager can
choose Killboard, Combat stats, Pilotable ships and Local ranking independently.
Blocked pages are denied on HTML and data endpoints. The anonymous public link
accepts only saved-scope all/year/month periods, fixed ranking enums and bounded
pagination, with no arbitrary member/date/area/ship parameters. There is no public
directory. Anonymous visitors have no links into account-only entity pages;
signed-in visitors can follow the normal entity, killmail, ship and location
links, with each destination enforcing its own access permissions. Rendered
killboard caches distinguish anonymous and signed-in responses, and login state
is checked on every data request. Public pages display a read-only summary of configured members, membership
dates, Avoid entries, areas and ship filters, derived from the saved definition
without scanning killmails or expanding rosters. Owner metadata, delegation and
management controls remain private. Anonymous pages
show an EVEOSINT link to login; signed-in viewers get their usual app navigation. Responses are
no-store/noindex, with no-referrer.

**Editing a group preserves its public token and URL.** Its revision changes,
so subsequent requests calculate against the edited definition. Turning sharing
off revokes the token; republishing then creates a new token. Deletion revokes
access and cascades only to its definition/access data.

The owner can appoint up to 20 active site accounts as managers. They can edit
and manage publication; only the owner may delete the group or change delegates.
Delegated groups appear separately in the manager's **My groups**, with direct
private and public-page links, a visible public URL with a Copy button, and do not use that manager's owned-group quota.
All private operations require login, entity-read access and current ownership
or delegation. Every mutation is covered by the application CSRF guard.

## Storage and upgrade

Only `web.personal_groups` (definitions/publication settings) and
`web.personal_group_managers` (delegation) persist. Normal schema initialization
removes the obsolete `web.personal_group_periods` derived table and former
preparation columns. It preserves group definitions, public tokens, managers and
all source killmails. No production calculation is started by installation.

Tests: `python -m unittest scripts.test_personal_groups`; optional SQL integration
requires `KILLMAIL_VALUES_TEST_DSN` pointing specifically to disposable database
`killmail_values_test`. Browser: `python scripts/test_personal_groups_browser.py`
with Playwright/Chromium; it uses only local mocked fixtures.
