# Published pages: on-demand reads with bounded caching

Anonymous visitors land on `/public`, the welcome page ("Welcome to EVE OSINT", "OSINT website for EVE Online — in development.") with **Log in**
at the top right. Logged-in visitors keep the standard app menu. Existing
`/public/groups/{token}` links remain independent.

## Publication

**Admin → Public pages** requires `admin.system.view`. Enable a page family and
its desired tabs, then save. Both switches must be on. Home is public; all other
families/tabs default off. Character/corporation/alliance Combat stats, Top kills,
History, Pilotable ships and Population (where supported) are selectable.
Enabling **Killboard** and its Killboard tab also enables the menu search,
all character/corporation/alliance/coalition/location/ship killboards and kill
details. Bare entity links open their killboard when no overview is published.
This access is derived, not saved into the other switches: disabling Killboard
removes inherited access, while independently published pages remain available.
Search uses the same grouped suggestions and portraits/logos as the normal menu.
Other entity tabs keep their own publication switches.

Every catalog page and tab is selectable, including coalitions, maps, unfinished
item pages and calculation tools. Notes about load and completion are informational;
they never disable publication controls. Only the administrator decides which
ones become public. Admin, Account and Profiles remain private.

Public URLs use `/public/leaderboard`, `/public/killboard`,
`/public/character/94036773?tab=stats`, etc. Valid links, GET forms, standard
filters, pagination and the existing tab scripts work normally. The original
read/render paths and shared templates are reused, rather than summing
preselected prepared views. There is no artificial 20-page browsing cutoff.

**No preparation is required.** An uncached published page/data request reads or
calculates its requested result immediately. The optional Admin warm/preview
action is only a convenience. Nothing is computed just because a tab exists or
a group is managed. Opening a tab triggers that tab's normal reads/scans; top-kill
and pilotable-ship progress continues using the existing interfaces. Running
results are not frozen in the response cache.

## Server and browser cache

- Admin chooses **X recent results**, default 256, range 8–4096. HTML views and
  data responses share this limit. Recently accessed entries survive eviction.
- Disk cache (including its index) is capped at 64 MiB total and 2 MiB per entry. An oversized result
  can display without being retained. Results refresh after five minutes.
- A small metadata index avoids reading all cached bodies for navigation.
- Cross-worker locks bound demand rendering to two concurrent reads and prevent
  duplicate work on the same result; busy requests can retry. Existing heavy
  tab workers retain their own concurrency limits.
- Anonymous HTML/data responses use `private, max-age=60, must-revalidate` and an
  ETag. Browsers can answer repeated requests locally for one minute, then use
  conditional requests; unchanged responses return 304 without retransmitting
  the body. Logged-in page chrome uses `no-store`; public data is independent of
  the viewer's identity. Running progress responses use `no-store`.
- Publication is checked before cache reads and again after calculations.
  Disabled pages/tabs reject direct access and their links are removed from
  responses. A browser may retain its already-fresh page for the remaining
  **60-second browser freshness window**; origin revocation is immediate.

Cache files live under `data/public_pages/`. There are no new fact/analytics
result tables and no daily/full-universe precomputation. Cache eviction removes
only cache files. Lowering X prunes existing entries immediately.

## Read access boundary

The public bridge forwards GET data requests through `/public/read`.
Explicitly published Ship analysis and Super evolution tools can also submit
their read-only calculations through `/public/compute`, with a 64 KiB payload
limit and the same bounded result cache. These tool responses are collected
before being returned; streamed progress is delivered with the completed response.
The gateway validates the published source, matching entity ID, required tab and
an explicit endpoint allowlist. It never accepts arbitrary internal URLs,
external URLs, management POST actions, admin/account routes or forced scan restarts.
The separate computation gateway accepts only the selected tool’s explicit
calculation endpoints and rechecks publication before returning results.

An internal ContextVar grants a public **read identity** (`entities.view` and
`superintel.view`, no admin/jobs/account permissions) only while invoking these
validated read adapters. It cannot be selected through a request parameter,
header or cookie. Internal ASGI requests forward no cookies or credentials;
normal authentication resumes after the scope ends. Private data handlers reject anonymous direct API requests instead of ignoring a missing user. Account chrome is generated
separately and never cached with public results. Public population views do not
launch the private affiliation-refresh initializer.

Links created dynamically by tab scripts are also restricted to published
pages. API responses containing HTML are checked against the current link
policy when served. Both fetch and the legacy killboard XMLHttpRequest path
use the same public gateway/cache.

## Verification

```
python -m unittest scripts.test_public_pages -v
python -m scripts.test_public_pages_browser
```

All fixtures prevent real DB access. Tests cover demand rendering, cache hits,
ETag/304, LRU/size/expiry limits, direct tab revocation, publication controls,
read-identity isolation, proxy allowlists, transient scan progress, Home/login
routing and template compilation. Chromium exercises the shared entity killboard,
page 2/back navigation, standard zone filters and cached result reuse.

The native Chromium cache test uses a real local HTTP fixture (no interception):
the second read makes no server request, and forced revalidation reads no DB again.
