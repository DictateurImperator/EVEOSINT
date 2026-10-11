# First loss: observable return and subsequent trajectories

The study answers two questions: does a character reappear after their first
known loss, and what changes when they do? The initial loss and its social
context are compared with subsequent return, PvP participation, further losses,
attacker activity, and changes of location and ship. Clusters support these
questions; they are not the final result by themselves.

The experimental screen is **Admin → Debug → First-loss return & trajectories**.
It is also linked from Admin Jobs. Viewing the screen requires
`admin.jobs.view`; starting, stopping or rebuilding requires `admin.jobs.run`.
There is no automatic schedule or processing triggered by visiting a page.

## Population and observation

Every identifiable character with at least one recorded loss is included,
regardless of age or year. NPC losses qualify as the first loss as well as
subsequent activity; the cohort does not require a first PvP loss.
Characters with no loss are excluded. The first loss
is selected across the complete imported history, ordered by UTC time then ID.
An anonymous MER loss cannot identify a character and cannot enter this cohort.
An old character's first recorded loss need not be their first actual loss.
Birthdays and coverage since birth are retained to make that distinction visible.

Return means a subsequent recorded killmail as victim or attacker, including
NPC losses and attacks on objects. PvP requires an identified player victim
and at least one identified player attacker. The two are not interchangeable.
No observed return is not evidence that someone stopped logging in: the database
does not contain logins, subscriptions or ordinary mining and trading activity.

By default, events within 30 minutes of the initial loss belong to the initial
episode. This prevents a ship followed immediately by a pod from automatically
counting as a return. The CLI supports `--episode-minutes` from 0 to 120 for
sensitivity studies. This setting stays frozen when resuming a run.

The outcome horizons are 7, 30 and 90 days. Imported-day coverage and the last
successfully imported day determine follow-up completeness. Missing days or
insufficient elapsed time prevent a negative return label. A known return is
still retained for incomplete follow-up, but incomplete cases are excluded from
rates, comparisons and trajectory totals to avoid unequal observation windows.
The screen reports their number separately. Successful archive imports do not
prove that every EVE loss was published; that publication bias remains.

## Computed initial-loss features

Only information from before or at the initial loss is used for profile
clustering. Outcomes never enter the feature vector. Character, corporation,
alliance, ship and system identifiers are provenance, not numeric predictors.

| Family | Initial implementation |
| --- | --- |
| Character | Age, corporation tenure, earlier attacker events and distinct active days |
| Loss | Player/NPC attacker counts, fitted item rows, cargo rows, implant rows |
| Location | HS, LS, NS, WH or Pochven at the loss; unknown when unavailable |
| Ship | Probable role from the SDE group: mining, transport, combat, support, pod, etc. |
| Entourage | Player/NPC corporation; prior corporation/alliance population snapshots; attacker participations and industrial-loss proportions in the previous 30 UTC days |
| Coverage | Missing feature flags, coverage of prior group history, birthday relative to imported history |

Population snapshots are strictly earlier than the UTC loss day. Group activity
also excludes the loss day: later fighting cannot become a predictor of the
earlier loss. Group attack counts are participations, not distinct active
members; they can include the character's own earlier participation. These
proxies do not certify a corporation as a farmer or combat group. Item counts
count recorded item rows, not quantities, module value or fitting quality.

Current inferred skills and current ship capability masks are excluded. Absence
of item rows is unknown, not proof of an empty pod. Ship class suggests an
activity but does not establish what the character was doing. Current SDE
geography is a limitation for historical security changes. In particular,
systems now in Pochven are marked unknown through its transition day instead
of being retroactively labelled Pochven. Its formation date is documented by
[CCP](https://www.eveonline.com/news/view/celebrate-pochvens-formation-on-totality-day).

The [parameter catalogue](first_loss_parameter_catalog.csv) lists 226 candidate
parameters and their data limitations. It is an exploration catalogue, **not a
list of implemented features**. Historical MER prices, inflation-adjusted loss
cost, fitting intent, sovereignty, regular companions, original-adversary
encounters and more detailed battle reconstruction require additional work.
No current price is substituted for a missing historical loss value.

## Return and trajectory results

For each horizon and first-loss year the screen shows complete cases, observed
returns, no observed return, the return rate and a Wilson 95% interval. Returned
characters are split into PvP and no observed PvP during that horizon.

Each return path has separate same/different/unknown comparisons for zone,
system, region, ship and ship role. Location and ship compare the **first**
subsequent event with the initial loss. Named zone and role transitions are
counted as well. Further losses and attacker activity are reported at 30 and
90 days; the interface does not display 30-day counts as 7-day results.
Attacker activity is not labelled revenge against the original aggressor.

The factor table compares a profile with other profiles **within each first-loss
year**, requiring at least 20 complete cases on each side in each supported
year. The difference is weighted by that profile's supported yearly population.
Displayed raw rates and adjusted differences have different denominators and
are labelled accordingly. These are exploratory associations, not causal
effects, a validated prediction model or tests of statistical significance.
No p-values or claims of discovery from many simultaneous comparisons are made.
Year adjustment alone does not control age, corp dependence or battle effects.

## Full-population PCA and clustering

Continuous features use `log1p` except proportions; missingness has explicit
indicator columns. Numeric features are centered and scaled within first-loss
year, with standardized values bounded to ±5. Categorical features use one-hot
encoding. PCA is calculated from streaming sums and cross-products of **all**
characters, then retains 90% of variance up to 16 components.

Three complete streaming clustering passes and a final assignment pass include
every character. A reservoir of up to 2,048 profiles initializes centers only;
it is not the population used to train or evaluate. Eight clusters are the
default, adjustable from 2 to 30. Clusters show their most frequent initial
profiles and their observed outcomes. IDs are local to one run.

Model metadata preserves feature names, cohort scales, PCA axes, centers,
random seed, population size and NumPy version. Labels describe initial loss
profiles rather than future trajectories; outcomes are evaluated afterwards.

## Manual operation and dependencies

The script creates its tables in `research` on first run. Source killmail,
entity, population and SDE tables are read only. Install the one additional
dependency in the same interpreter used by the web job before the first run:

```sh
/home/ubuntu/eveosint/venv/bin/python -m pip install -r /home/ubuntu/eveosint/scripts/first_loss_study/requirements.txt
```

No installation or production processing is performed by deploying this code.
A missing NumPy produces an explicit log message before research tables are
changed. Psycopg2 is already an application dependency.

**Analyze all / resume** starts the worker; **Stop** sends SIGTERM, cancels a
pending SQL query and leaves committed checkpoints. **Rebuild study** deletes
previous research runs and derived rows, then rereads history. It does not alter
source tables. Completed studies require an explicit rebuild to refresh data.

CLI equivalents:

```sh
python scripts/analyze_first_losses.py --clusters 8
python scripts/analyze_first_losses.py --rebuild --clusters 8
python scripts/analyze_first_losses.py --schema-only
```

Defaults use `~/eveosint/config/db.json` and write atomic progress snapshots to
`~/eveosint/data/research/first_loss_progress.json`. Admin job logs are in
`~/eveosint/data/logs/first_loss_study.log`.

The loss selection, group aggregation and follow-up passes commit checkpoints
per monthly source partition. Feature extraction commits keyset batches and
reprocesses an unfinished month safely. Cancelled monthly SQL is rolled back;
counts cannot be added twice on resume. An interrupted model pass restarts
model training, then replaces partial assignments.

Cutoff, missing days, cluster count and episode window are frozen for a run.
Registered import counters and archive-recovery metadata are fingerprinted on
resume, between source passes, and before publication. A detected historical
backfill requires rebuilding instead of mixing old loss selection and new
follow-up. This guard cannot detect arbitrary unregistered manual source edits.

There is one worker, one BLAS thread, no PostgreSQL parallel query workers,
64 MB work memory per SQL operation, and a 15-minute statement timeout. Python
uses bounded batches rather than loading all killmails into memory. PostgreSQL
can spill monthly sorts/aggregates to disk. Full-history passes and per-character
research storage are substantial; this is a manual background study, not a
request-time calculation. No claim about production run duration is made.

## Validation

Offline tests cover labels, missing history, historical Pochven, leakage,
year adjustment, non-PvP transitions, JSON conversion of PostgreSQL aggregates,
manual controls, permissions and stale progress. Browser checks exercise the
tables, cohort/horizon selection and start/stop actions.

The PostgreSQL integration test uses a disposable database named
`first_loss_test*`; its guard refuses other names. It exercises all 601 fixture
characters, real SQL extraction, PCA, assignment to clusters, the real worker,
SIGTERM during slow SQL, and successful resumption. Fixture counts are test
data, not scientific results from the live population.

```sh
python -m unittest scripts.test_first_loss_study scripts.test_first_loss_admin -v
python -m scripts.test_first_loss_admin_browser
# Optional integration, only against a disposable database:
FIRST_LOSS_TEST_DSN='host=/tmp/lab port=55444 dbname=first_loss_test user=codex' \
  python -m unittest scripts.test_first_loss_study -v
```
