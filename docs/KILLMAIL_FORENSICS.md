# Killmail Forensics

The English admin workspace at `/admin/killmail-forensics` requires
`admin.killmail_forensics.dev` for every page and endpoint. It investigates
unmatched MER losses (`resolved_km IS NULL`) with the existing killboard table
and advanced filters. Character and module filters cannot be evaluated from MER.

## Enable the tables

After deploying this branch, open **Administration → Jobs** and run
**Forensics · Create investigation tables (run once)** using **Create Forensics
tables**. This executes `scripts/setup_killmail_forensics.py` with the application
DB configuration and `web/app/forensics_schema.sql`. The eight Forensics tables are
created in one transaction with timeouts and an advisory lock. After upgrading
from an earlier Forensics branch, run this same job again to add forecast columns,
the analysis-run table, refresh queue, sorting indexes, zKillboard submission ledger
and last CCP credit report. Repeating the script
preserves records and existing custom plans. The job requires `admin.jobs.run`.

The web process only checks whether the tables exist. It never creates them at
startup, and registering the job does not start it. Until setup succeeds, the
workspace explains which job to run. The migration has not been run on the live DB.

## Workflow

1. Filter hidden losses, check rows or **Select displayed**, then **Investigate
   selected**. Browser selection uses user-scoped session storage and the full
   MER key `(kill_datetime, source_month, source_row)`. `source_row` is a CSV row
   number, not a CCP ID. Selection falls back to memory if storage is unavailable.
2. Review CCP IDs, known hash inputs, victim and final-blow hypotheses. Rank 1
   is best. Hover for reasons, expand to change choices, or add IDs manually.
   **Apply choices** persists the plan and recalculates remaining attempts.
3. Sort by **Fewest remaining attempts first**. One ID × two victims × five
   final-blow pilots means ten combinations, minus stored tests.
4. **Test selected** applies choices and checks combinations in descending score
   order. Keep the tab open. **Stop**, closing or reloading the tab stops further
   trials; an already submitted call can finish. Completed tests persist and are
   skipped when resuming, changing choices or refreshing hypotheses.
5. Confirmed results move to **Recovered killmails**, showing ID, hash, time and
   MER reference. Open a result for victim, attackers, full CCP JSON including
   fitting/items, and the CCP link.

Investigations store snapshots, ranked evidence, choices, outcomes, combined
attempt scores, confirmed hashes and CCP payloads. They are shared by users with
the dev permission. This feature does not import into `rawkm` or mutate MER.

## Estimate and sort every hidden kill

**Analyze / resume all hidden kills** launches the registered Admin Job
`scripts/analyze_hidden_killmails.py`. It requires both the Forensics dev permission
and `admin.jobs.run`. Leave both dates empty to cover all available unmatched MER
exports, or choose an inclusive date range. The batch scope is separate from the
advanced browsing filters used for individual selection. The job sends no CCP
requests and only writes Forensics records. No job was launched on the live DB
while implementing this feature.

The worker uses batches of 100 composite MER references and a global advisory
lock. It saves each completed investigation and the run's progress. Closing the
tab leaves it running; **Stop analysis** stops between cases. Restarting skips
current successfully analyzed records, upgrades old evidence and retries failures.
Failed analyses are retained with an explanation and an unknown estimate. New
imports after a scan cursor has passed are included by running the job again.

Each investigation shows:

- **Estimated search**: the 50th–80th percentile trial positions under the relative
  candidate weights, conditional on the correct answer being in the remaining
  selected plan. This is a conservative heuristic range, not a calibrated interval.
- **Recovery outlook**: strong, moderate or weak evidence, or a blocked/exhausted
  plan. It assesses viable hypotheses, local ship/capsule clues, recent ship use,
  prior combat activity, ID deductions, excluded candidates and incomplete evidence.
  It is not a percentage probability of success. Hover for reasons.
- **Maximum attempts remaining**: the exact selected combinations minus stored
  tests. This is the full budget if the search fails.

Sort the entire saved collection by estimated cost, strongest evidence then cost,
maximum remaining trials, or date. Filter by outlook and combine specific evidence
filters, optionally restricting them to the victim or final blow. All checked
pilot clues must match **one selected candidate** in the chosen role. Context
filters such as ID deduction, NPC ship type and evidence truncation match the case.
Draft choices affect estimates/filters after **Apply choices**.

The 98 previously masked benchmark cases were replayed with the new forecast.
Pilot ordering and initial-plan coverage stayed unchanged (83/98). The scoring
range overestimated the successful trial positions in this sample; all 83 covered
cases fell below its 80th percentile. It should be used to prioritize investigation,
not as a promise about hidden kills. The observed sample and score-based forecast
are described in [the replay check](forensics_forecast_check_2026-10-09.json).

## A recovered kill enriches its neighbors

CCP-confirmed Forensics payloads participate in nearby pilot/ship evidence and
prior local ship use, and their IDs become known chronological anchors. They stay
in Forensics storage; MER and `rawkm` are not changed. Raw and recovered appearances
are deduplicated, target masking is preserved, and collection limits remain explicit.

After confirmation, existing unrecovered investigations within one hour in the
same system or up to two stargate jumps are queued for recalculation. A background
worker drains the durable queue without CCP calls, including while a global scan
is running. Pending recalculations appear on rows and in the progress summary;
launch/interruption failures leave the queue intact for a subsequent run. Failed
recalculations wait five minutes before becoming eligible again.

Automatic plans adopt newly ranked candidates and IDs. Explicit user choices and
all completed tests persist. Investigations created before this upgrade retain
their previous choices. Closing the validation tab does not cancel already queued
neighbor maintenance.

## ID and pilot deductions

The ID analysis finds uniquely resolved anchors before and after the target,
within seven days and the same export month. Both anchors must occupy their
timestamp alone. Their ID difference must match the intervening MER row count
before chronological rank is used. Tied seconds share an ID interval, minus
already uniquely known IDs. Inconsistent counts retain the whole anchor interval;
missing anchors require manual IDs. Capped coverage is explicitly marked.

Read-only live checks on August 2026 supported chronological rank: 336,287
uniquely timed known kills matched their expected IDs, and all 477,266 known IDs
fell within their same-second intervals. August 2025 had export gaps, so this
is a locally checked hypothesis, not a universal guarantee. CCP proves recovery.

Pilot ranking uses:

- Corporation membership at the loss time and prior combat affiliation.
- Combat in the same system within an hour, or up to two stargate jumps away
  where SDE gate data is available; closer observations weigh more.
- The relevant ship in the nearby fight within 30 minutes, especially if only
  one observed corporation pilot uses it.
- Known ship use in the preceding 90 days, with more weight for recent use.
- **Combat in ship X, then a hidden loss of ship X in the same corporation**,
  for every ship type. NPC final blows add evidence to this sequence.
- Disappearance while at least two subsequent observed kills suggest the fight
  continues. Delayed attribution and reshipping remain possible.
- A known ship loss shortly before a capsule-related kill, or a known capsule
  loss shortly after the hidden ship loss, in the same corporation and system.
  This also handles final blows attributed after the attacker's ship died.
- Prior co-participation with local pilots in small groups over 30 days, and
  nearby final blows using the relevant ship. Large fleets do not count as
  strong trio evidence.
- Current inferred pilotability with a small weight, explicitly distinguished
  from historical ship use. It does not prove skills at the loss time.

A nearby known loss lowers a pilot's weight without excluding them. Final-blow
defaults select known attackers from the preceding 90 days or nearby fight.
Other members stay available but initially unselected: absent observations mean
unknown activity, not proof of never doing PvP. Prior combat affiliation without
recorded membership at the exact loss time is marked as uncertain.

For an owned structure/deployable, a known loss supports ownership and does not
mean the character died. Object ownership alone does not establish physical
presence. CCP sometimes supplies an owner character for these victims.

Every pilot list includes **No character** for NPCs, structures, stations,
deployables and other unpiloted objects. Relevant SDE categories increase its
weight. An NPC corporation or an unknown MER character field alone proves no absence.

Percentages are normalized heuristic weights among listed candidates,
**not calibrated probabilities**. Combined scores order attempts; correlated
clues are not independent probabilities. Defaults select up to 20 IDs and nine
eligible pilots plus the absent-character alternative per side. Choices allow
200 candidates per list and at most 100,000 combinations per plan. Evidence
bounds are 5,000 nearby appearances, 2,000 historical members, 2,000 recent
combatants per corporation and 2,000 companion fights. Truncation produces warnings.

## Hash and validation

zKillboard's published code uses ASCII decimal text without separators:

```text
ticks = unix_seconds * 10000000 + 116444736000000000
hash = SHA1(victim_character + final_blow_character + victim_ship_type + ticks)
GET https://esi.evetech.net/killmails/{killmail_id}/{killmail_hash}/
```

A truly absent character is literal `None`. Times require an explicit UTC offset
and whole seconds; tick arithmetic is integer. CCP ID enters the URL, not the
hash. Corporation, alliance, system and final-blow ship are supporting information.

HTTP 200 must match the selected ID/characters, time, victim ship and MER system.
Affiliation/final-blow ship fields must agree where both sources supply them;
CCP may omit NPC affiliations that MER supplies. Invalid hashes
and valid-but-mismatched payloads are separate stored outcomes. Transient errors,
420/429 and network failures leave the hypothesis untested. Request/auth errors
pause validation. Invalid ID/hash pairs are cached across cases; confirmed
payloads can be reused for matching duplicate MER references. Advisory locks
prevent concurrent edits/validation of a case. Connections close after each operation.

On October 9, 2026, the live endpoint advertised `killmail`, **3,600 tokens / 15
minutes**. CCP charges two tokens for 2xx and five for 4xx, excluding 429. The
workspace reserves five tokens, adjusts after responses, caps its sliding ledger
at 3,300 tokens and spaces requests at least two seconds apart through a shared
PostgreSQL gate. It respects `Retry-After`, new rate headers and legacy error-limit
headers. Other services can share the public IP bucket; limits can change.

## Verification

Tests use isolated modules/configuration, with no production startup:

```bash
python -m unittest scripts.test_killmail_forensics scripts.test_forensics_reconstruction scripts.test_forensics_forecasts scripts.test_code_update -v
```

Optional persistence tests require a **disposable** PostgreSQL instance: Unix
socket below `/tmp`, port `55444`, DB `postgres`, user `codex`. They recreate
fixture schemas in that lab and never read live DB configuration. Browser tests
also require Playwright and Chromium, and mock CCP responses:

```bash
EVEOSINT_FORENSICS_TEST_SOCKET=/tmp/eveosint-forensics-lab python -m unittest scripts.test_forensics_reconstruction scripts.test_forensics_forecasts -v
EVEOSINT_FORENSICS_TEST_SOCKET=/tmp/eveosint-forensics-lab python -m scripts.test_forensics_browser
```

Browser checks cover selection, tooltips, counts, manual candidates, persistence,
sorting, refresh, rate-limit stop/resume, recovery and consultation. Live checks
used read-only SQL: three hidden losses produced hypotheses in about one to two
seconds, five known hashes matched the formula, and one known hash returned CCP
HTTP 200. No hidden killmail hypotheses were submitted to CCP during these checks.

The pilot algorithm was additionally evaluated on **98 known kills with targets
masked**. The initial plan covered 83/98 and an expanded plan covered 90/98.
See [the benchmark and category trial counts](KILLMAIL_FORENSICS_BENCHMARK.md),
including failed-search budgets, a separate-month check and a replay without
current inferred skills. These are simulated attempts, not CCP recoveries.

## Primary sources

- [zKillboard: MER reconstruction](https://github.com/zKillboard/zKillboard/blob/4b3bda3db1c947984cd24c7b21616e6323059d64/mer/load_mer_dump.php).
- [zKillboard: reconstruction from ESI payloads](https://github.com/zKillboard/zKillboard/blob/4b3bda3db1c947984cd24c7b21616e6323059d64/scratch/hashfix.php).
- [CCP: killReport links contain ID and hash](https://developers.eveonline.com/docs/guides/eve-html/).
- [CCP: rate limiting](https://developers.eveonline.com/docs/services/esi/rate-limiting/).
- [CCP: best practices and error limits](https://developers.eveonline.com/docs/services/esi/best-practices/).


## Recovered killboard, publishing and credits

The recovered screen uses the killboard's ships, corporation/alliance logos,
confirmed character portraits and location links. It shows the MER `ccp_isk_lost`
estimate (including a zero estimate) and the number of distinct stored hash tests
per recovered case. This count includes reused results and excludes temporary API
errors; it is not a count of transport retries. Individual scores, candidate IDs,
results and timestamps remain in `web.forensics_attempts` for later evaluation.
MER value is also visible on the investigation rows.

Recovered filters use the shared advanced builder: inclusive UTC date bounds,
ships, characters, corporations/alliances with victim/attacker/both roles,
locations/security presets and exclusions. Attacker filtering examines every
attacker in the confirmed CCP payload, rather than only the final blow. Filtering
and pagination apply to the full saved recovered collection. Missing NPC/object
fields do not incorrectly exclude rows from negative filters.

**Send to zKillboard** and **Send selected to zKillboard** publish only after an
explicit click. They use the official POST submission API:
https://zkillboard.com/api/docs/#posting-killmails . Only stored, CCP-confirmed
ID/hash pairs can be sent. Accepted responses are recorded and subsequent clicks
do not repost. Status can be filtered as not submitted, accepted, failed or
uncertain. Accepted means zKillboard accepted the request; publication may lag.
The server serializes submissions across users/workers, spaces successes by two
seconds and respects rate-limit retries. Failed/ambiguous requests stop the batch
and apply a cooldown. The reservation is committed before HTTP, preserving an
uncertain state if a worker exits. Stop sending stops between requests.

After deploying, rerun **Forensics · Create investigation tables (run once)** to
create the publication ledger and CCP report column. Reading recovered kills and
the local credit budget still works before this upgrade; publishing stays disabled.
No live zKillboard posts or production migrations were made during development.

The CCP credit card refreshes every ten seconds and after each validation. It
shows the remaining local Forensics allowance (3,300 credits per rolling fifteen
minutes), when credits first return, and any active cooldown. Each reservation
initially costs five; finishing updates to CCP's `X-Ratelimit-Used`, or documented
status costs when that header is absent. Expired calls leave the rolling sum.
The last `X-Ratelimit-Remaining` report is stored separately with its observation
time and bucket limit. It is not presented as an exact live server-wide balance:
other services on the same IP can consume the same CCP bucket. No extra CCP call
is made to display this card. See https://developers.eveonline.com/docs/services/esi/rate-limiting/ .

## Refresh archives that received late killmails

In **Administration → Jobs**, start **Killmails · Refresh updated EVE Ref
archives** manually. Optional **From / Through** dates select archive days,
inclusively. Empty dates check every day successfully imported by the regular
killmail importer. This job does not schedule itself or backfill days that have
never been imported.

EVE Ref updates daily archives in place, using the kill date. The job reads each
relevant yearly `index.json` and compares `last_modified` with the local archive's
download time (its filesystem mtime, preserved by the regular importer's move).
Only a strictly later remote modification triggers a download, including on the
first run. Counts and ETag changes cannot override this rule. Missing download
dates are logged and skipped; the worker never assumes all history needs fetching.
New refreshes also record `downloaded_at` in the checkpoint. Downloads bypass the
old cached archive and replace it at the same path after successful import.
Interrupted temporary downloads are cleaned under the worker lock on relaunch.
Only missing killmails,
attackers and items are inserted; existing kill data is preserved. Payload kill
timestamps determine the destination partition, including an unexpected date in
another month. The job then runs strict MER matching for the affected months.

Progress is in the job log (`INDEX_START`, `ARCHIVE_CHANGED`, `ARCHIVE_PROGRESS`,
`ARCHIVE_DONE`, `MER_MATCH_DONE`, `DONE`). The durable checkpoint is
`data/killmails/refresh_index.json`. Failed downloads preserve the old archive;
committed batches and pending MER matching survive interruption. Relaunch to
retry. **Stop archive refresh** in Admin Jobs requests a graceful stop: its temporary
download is removed and committed progress is retained. Concurrent refresh workers are refused; Admin launches also prevent
running the regular importer and refresh together. MER matching has a database
lock shared by the manual MER action and this worker.

The worker itself creates `rawkm.killmail_archive_recoveries` on its first manual
launch, recording each newly imported kill in the same transaction. No extra
migration is needed, and loading the website creates no tables. It calls EVE Ref,
without spending CCP request credits or submitting anything to zKillboard.

## Monthly coverage

**Administration → Killmail Statistics** (`/admin/killmail-statistics`) requires
`admin.jobs.view`. Select a year; statistics count kills by their actual UTC kill
month. Archive killmails count all locally imported raw kills. MER losses have
mutually exclusive categories:

- **Known**: a unique MER match, without a tracked recovery.
- **Hidden**: no MER match and no confirmed Forensics recovery.
- **Recovered**: a unique match to an archive-refresh addition, or a CCP-confirmed
  Forensics recovery. The two sources are shown separately; Forensics takes
  precedence if both exist, so a loss is counted once.
- **Ambiguous**: a non-unique MER match, without a confirmed Forensics recovery.

MER coverage is `(known + recovered) / MER losses`. Archive recoveries are tracked
from the first refresh-job run; historical recoveries cannot be inferred
retroactively. The statistics page works before optional recovery tables exist.
It performs read-only queries for the selected year and does not start any job.

Validation: `scripts.test_killmail_archive_refresh` exercises decisions, Admin
access, late additions, unchanged archives, duplicate avoidance, corrupted
archives, resumable MER matching, month scope, concurrent matching and recovery
counting in the disposable PostgreSQL lab. `scripts.test_killmail_statistics_browser`
checks the year selector, rendered counts, recovery breakdown and error recovery
in Chromium with all external traffic intercepted.

## Live archive refresh progress

The archive refresh card in **Administration → Jobs** polls its read-only progress
endpoint every three seconds while the tab is visible. The worker checks the
selected archive dates first, so the exact **To update** count is known before
any archive download begins. During scanning the card shows how many days have
been checked and how many updates have been found so far. It then displays
processed/total archives, successful updates, errors, newly imported killmails,
the current archive, download bytes and import file count. MER matching is a
separate visible phase. Completed and stopped summaries remain available.

The worker writes `data/killmails/refresh_progress.json` atomically; reading the
card starts no job, imports no data and calls no external API. Viewing requires
`admin.jobs.view`. The launch/stop controls follow the live worker state. The
reader detects a newly started worker and an unexpectedly terminated worker
instead of displaying another run's progress as current. Workers launched before
this version cannot emit this progress file; use the next manual launch after
deploying.

Tests cover exact scan totals before downloads, persisted counters, worker
identity, interrupted state and endpoint access.
`scripts.test_archive_refresh_progress_browser` exercises automatic transitions
from scanning through downloading/importing to completion, button state and
recovery from a non-JSON error with no real worker or external requests.
