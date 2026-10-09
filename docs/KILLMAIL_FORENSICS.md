# Killmail Forensics

The English admin workspace at `/admin/killmail-forensics` requires
`admin.killmail_forensics.dev` for every page and endpoint. It investigates
unmatched MER losses (`resolved_km IS NULL`) with the existing killboard table
and advanced filters. Character and module filters cannot be evaluated from MER.

## Enable the tables

After deploying this branch, open **Administration → Jobs** and run
**Forensics · Create investigation tables (run once)** using **Create Forensics
tables**. This executes `scripts/setup_killmail_forensics.py` with the application
DB configuration and `web/app/forensics_schema.sql`. The five new tables are
created in one transaction with timeouts and an advisory lock. Repeating the
script preserves records. The job requires `admin.jobs.run`.

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
python -m unittest scripts.test_killmail_forensics scripts.test_forensics_reconstruction scripts.test_code_update -v
```

Optional persistence tests require a **disposable** PostgreSQL instance: Unix
socket below `/tmp`, port `55444`, DB `postgres`, user `codex`. They recreate
fixture schemas in that lab and never read live DB configuration. Browser tests
also require Playwright and Chromium, and mock CCP responses:

```bash
EVEOSINT_FORENSICS_TEST_SOCKET=/tmp/eveosint-forensics-lab python -m unittest scripts.test_forensics_reconstruction -v
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
