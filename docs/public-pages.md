# Public pages and tab publication

The anonymous landing page is `/public` (the shared Home/changelog, without
session information). `/` and `/home` send anonymous visitors there. Login is
at the top right; authenticated visitors keep their normal application menu.
The existing `/public/groups/{token}` feature is independent and unchanged.

## Admin workflow

Open **Admin → Public pages** (`admin.system.view` permission).

1. Prepare an exact local URL, such as `/leaderboard`,
   `/leaderboard?kind=alliance&mode=weighted&through=2025-12`, or
   `/character/94036773?tab=stats`. Preparation uses the admin's real session.
   Only one preparation may run at a time across web workers. Status shows
   running, complete, failed or interrupted, with elapsed time.
2. Click the cached URL for a **private preview**. Preparation/preview never
   changes publication settings.
3. Enable the page family and the desired tabs, then save. Both switches must
   be on. Every configurable page/tab starts off.
4. Public URLs use `/public/leaderboard`, `/public/character/94036773`, etc.
   Unpublishing a page/tab rejects direct URLs and disables its links on the
   next response, including links inside older cached snapshots.

Available adapters: leaderboard (characters/corporations/alliances), the three
SuperINTEL report views, character/corporation/alliance overview, killboard and
monthly combat stats, location/ship overview and killboard, killmail detail.
The catalog also shows blocked/unreviewed families and tabs, with reasons.
Unfinished modules/skills/commodities, interactive population/capability/history
queries, top-kill scans, global killboard scanning, coalition expansion, map
queries and calculation tools cannot be enabled through crafted requests.
Admin, account and Profiles management routes have no public adapters.

## Cache and resource behavior

Public requests **never** call report adapters, start a scan or populate the
cache. Only the explicit admin preparation action queries existing report/KM
sources. There is no automatic refresh or daily precomputation. Prepare/refresh
only the desired exact URLs; public visitors can navigate the available cached
views. Unsupported arbitrary search/filter parameters are rejected. Killboard
pages and leaderboard pages are restricted to the first 20 pages. Dynamic
forms/scripts are removed from snapshots; valid pagination is ordinary public
GET navigation. Missing/expired snapshots show an explanation and start no work.

Snapshots expire after 24 hours. Limits: 256 views, 64 MiB total, 2 MiB per view;
oldest snapshots are evicted first. A small metadata index avoids loading every
HTML file to build navigation. Disk data lives in `data/public_pages/`; no new
analytics/fact tables or entity-month materializations are introduced. Clear
cache removes only snapshot files and the index, retaining publication settings.
SuperINTEL snapshots omit the private template's hidden search rows: dashboard
lists contain 10 rows, or at most 100 when `more=1` is explicitly prepared.

Links are checked against the current policy on every render. Page content
reuses the existing Jinja templates/fragments; account chrome is generated
separately per request and never stored in a snapshot. Responses use `no-store`
so reverse proxies/browser caches cannot bypass revocation. Anonymous pages
have no application JavaScript or API polling; their CSP blocks network fetches,
forms and frames. Authenticated requests may still query session/menu tables for
normal navigation; anonymous report views never query report or KM tables.

No production publication settings, report data, services or timers are modified
by deploying the code alone, apart from making the requested static Home public.
All other pages require explicit publication and cached content.

## Offline validation

```
python -m unittest scripts.test_public_pages -v
python -m scripts.test_public_pages_browser
```

Fixtures prevent real database connections. HTTP tests cover permission gates,
closed defaults, malformed URLs, direct tab access, link revocation, private
preview, cache limits/expiry, preparation locking, shared combat-stats rendering,
Home/login routing and template compilation. Chromium checks preparation,
publication controls and revocation on an existing cached page.
