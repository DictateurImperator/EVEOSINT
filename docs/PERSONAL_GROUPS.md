# Personal groups

Access: account username menu → **My groups**, not the entity Profiles menu.
An account with `entities.view` can keep three groups. Each group combines up to
60 character, corporation, alliance or coalition rules. Include rules form a
union; exclusions win. Rules may have inclusive UTC dates. Optional group dates
and up to 20 system, constellation, region or security-space rules restrict
combat history. Up to 20 model or ship-group rules filter ships on the attacking,
victim or either side, using the standard killboard include/exclude semantics.
These rules constrain combat results and local rankings, not the roster.
Overlapping rules do not multiply kill or damage credits.

## Historical membership and results

Killmail affiliations are authoritative at the kill's date. Missing affiliations
fall back to recorded character/corporation and corporation/alliance history,
never today's affiliation. Coalition definitions and their dated memberships are
snapshotted when the owner prepares the group, including nested coalitions.
Refreshing updates that snapshot.

The owner or an appointed manager starts preparation. Progress identifies the current phase, month,
completed months and matching killmails. Completed months can be viewed while
preparation continues. A stop takes effect between database operations; each
source statement has a 120-second limit. One preparation runs across the site at
a time using a PostgreSQL advisory lock. A server restart releases the lock and
progress polling recovers stale Running controls. Refresh rebuilds the summaries.
No scheduled refresh or production calculation is started by installation.

Prepared results include all history, individual years and individual months.
The owner can also compute one custom private date range; a new custom range
replaces the previous one. Combat stats show kills, losses, full ISK, True ISK
and monthly history. True ISK sums matching attacker damage once and divides by
all attacker damage, including NPCs. Unvalued kills remain in counts and are
identified as incomplete ISK totals.

Killboards reread source killmails within the saved period and geographical scope.
Requests return up to 50 rows, scan at most three monthly partitions and provide
a continuation cursor and visible progress. Source-query timeouts return an
explicit error. A bounded two-minute in-process cache holds rendered pages.

Pilotable ships count distinct **known members at any point in the period**,
including inactive members. Combat locations do not restrict this roster. The
site cannot know every inactive character if its membership history was never
collected. Capabilities use the existing latest inferred skills, not historical
skill levels; coverage is displayed. Supercapital capabilities are removed during
preparation when the owner lacks `superintel.view`.

## Publication and access control

Groups are private by default. Once preparation completes, the owner can enable
an anonymous opaque public link and independently allow Killboard, Combat stats
and Pilotable ships and Local ranking. Blocked pages are denied on both HTML and data endpoints.
Visitors select only prepared all/year/month periods, an allowed page and a
killboard pagination cursor. They cannot change rules, create a custom period,
start group-preparation jobs, or retrieve the private definition. Fixed-result
killboard and ranking reads can scan source data, as described below. Killboard
pagination reads existing raw data but does not persist additional analysis.
There is no group directory. The public page exposes result names/images but no
links into account-only entity or killmail pages.

Disabling sharing revokes the token. Republishing creates a new token. Editing
the definition clears results and revokes the link. Full refresh temporarily hides
public results until it completes; calculating a private custom period does not.
Private endpoints require login, normal entity-read access, and either ownership
or a current management delegation. The application CSRF guard
covers every group mutation. Authorization precedes cache access. Private/public
render caches are separate. Responses are no-store and noindex, with no-referrer.

## Storage and deployment

Normal application schema initialization creates `web.personal_groups` and
`web.personal_group_periods` from `web/app/personal_groups_schema.sql`. They store
only definitions, publication settings, progress, coalition-rule snapshots and
small aggregate period results with ship counts. Source killmails and individual
member rosters are never copied. The three-group quota uses account-row locking
and a unique owner/slot constraint, including concurrent submissions. Deleting a
group cascades to its summaries; deleting an account cascades to its groups.

Tests: `python -m unittest scripts.test_personal_groups`. Database integration
tests require `KILLMAIL_VALUES_TEST_DSN` pointing specifically to the disposable
`killmail_values_test` database; never use a production DSN. They cover historical
coalitions, inactive rosters, deduplication, damage attribution, pagination, quota,
concurrent creation, public access blocks, revocation and preparation persistence.

Browser workflow: `python scripts/test_personal_groups_browser.py` (Playwright /
Chromium). It serves only mocked local fixtures and blocks external requests.

## Local rankings and delegated management

The Local ranking page is separately publishable. It ranks characters,
corporations or alliances by full or damage-weighted ISK destroyed, losses,
net ISK, efficiency, kills or losses, within the prepared period and saved
membership/date/area/ship scope. Only contributions by group members count.
Corporations and alliances deduplicate a kill across their matching pilots;
True ISK sums their matching damage over **all** attacker damage. NPC characters
are excluded. Affiliations use the individual killmail and historical fallback.
A YYYY-MM period uses that month's activity only, with no lifetime score.

Visitors may select only a prepared period and fixed ranking enums/page (no
free membership, ship, spatial or date query). Scans run in a background thread
and return provisional rankings after each month. One local ranking scan at a
time is enforced with a session advisory lock. Exact numeric totals accumulate
in a connection-local PostgreSQL temporary table, removed on connection close.
Permanent source tables remain read-only. There are no permanent per-member or
per-kill group copies. The bounded in-process cache keeps at most eight top-1,000
lists for five minutes; pages contain 50 rows. Changing the prepared result or
revision invalidates its cache identity. Access/tab checks run before cache lookup.

The private Management tab lets the owner search active **site accounts**, add
up to 20 managers, or revoke them. Managers can edit definitions, prepare/stop
calculations and change publication settings. Only the owner can delete a group
or manage its delegates. Management has no public equivalent. Delegated groups
are listed separately and do not consume the manager's three owned-group slots.
Mutations recheck delegation and the existing CSRF guard covers these paths.
Delegated preparations cannot increase the owner's supercapital visibility.

The standard Leaderboard also offers **Selected month only**. Its existing
monthly facts are used directly, including filtered/name/rank searches. The
cumulative mode remains the default; no additional score storage is introduced.
