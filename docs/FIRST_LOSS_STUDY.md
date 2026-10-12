# First loss: observable return and subsequent trajectories

The primary question is which factors predict observed return after the first
known loss, in which direction and by how much. The second question is what
changes when the character returns. The initial loss and its social
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

### Adjusted return factors: model version 4

The primary result is **Which factors are associated with return?**, rather
than a cluster identifier or a PCA spectrum. A separate supervised logistic
model controls the other recorded initial-loss variables and first-loss year.
Models cover 7-, 30- and 90-day observed return for first losses against players,
against NPCs/objects, and all first losses. The default population is first
losses against players. NPC-only losses remain included and separately
evaluable. Return includes subsequent non-PvP killmail activity.

Age bands are <7 days, 7–30, 30–365, 1–5 years, 5+ years and unknown. References
are <7 days for age, combat for ship role, HS for location, player corporations
and player opponents, stated in each row. Categorical contrasts average the
predicted probability difference against the reference, holding other recorded
predictors and year fixed. Each evaluated year needs at least 20 training
characters on both sides. Numeric contrasts increase a known transformed value
by one within-year training standard deviation, bounded at the preprocessing
limit. Effects use **percentage points**, with positive/negative/uncertain
labels, distinct from PCA variance and raw category return rates.

A deterministic character-ID hash creates an approximately 80/20 training/test
split shared across horizons. Every eligible complete character enters one side;
evaluation is not limited to a small sample. Preprocessing uses training data
only. Streaming IRLS fits ridge logistic regression (penalty 0.001 on mean
training loss), with loss-year controls. The first four iterations reuse reads
from PCA/clustering; additional prepared-data passes run only as needed, up to
16 iterations. Non-convergence is flagged. The model uses original predictors,
not clusters, PCA scores or future activity.

Approximate 95% intervals use training-only sandwich covariance and the delta
method. They assume independent characters: shared battles, corporations and
accounts can make them too narrow. Shrinkage, model misspecification and
unsupported combinations limit interpretation. Contrasts can extrapolate, for
example when changing age while holding corporation tenure fixed. These are
exploratory adjusted associations, not experimental causal effects or corrected
significance tests over all comparisons.

Held-out Brier error is compared with loss-year rates learned from training
characters. AUC uses 1,024 probability bins. Grouped permutation importance
shuffles a factor, including missing/category columns, across the **entire
held-out population of each loss year**, with three repeats. It is not limited
to adjacent ID batches, which could otherwise hide age-related information.
Every held-out complete case contributes. Correlated factors can share information
and create unrealistic shuffled combinations. Positive Brier-error increases
are normalized to shares summing to 100%. These are **predictive importance
under this model**, not fractions of a player's causal decision. Shares are
withheld if the model does not converge or beat the year baseline. A small or
uncertain estimate does not prove absence of an effect.

Year-specific evaluation uses the globally fitted model and the held-out
characters in that year, not a separate yearly refit. Additive slopes can miss
era-specific interactions. Random within-era validation does not validate
future-era performance. The existing within-year descriptive comparisons remain
available to identify possible changes in associations.

Unix-epoch/pre-2000 inferred birthdays are unknown. Original invalid ages are
preserved as `age_days_raw`, with `age_quality=invalid_birthday`. The correction
applies to both newly extracted and previously prepared rows. Refitting stores
corrected features alongside the projection update, without another rewrite
pass. Exact missing-feature and invalid-birthday counts are retained.

A completed version-3 study needs **Analyze all / resume**, not Rebuild. With
unchanged source inventory it reuses loss/features/follow-up checkpoints and
refits models without repeating raw killmail extraction. The schema is unchanged
and the existing NumPy dependency suffices. Prepared-data reads use bounded
primary-key pages, avoiding multi-gigabyte external sorts. Held-out predictor
vectors are retained in RAM for exact year-wide permutations: approximately
0.5 GB at 3.7 million characters, without a disk matrix or new database table.
Existing one-worker,
BLAS/thread and PostgreSQL memory limits remain in force. Coefficients, covariance,
training scales, seed and assessment are preserved for reuse.

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

Selecting a loss year now shows associations **within that year**, rather than
repeating the global pooled comparison. The same minimum of 20 complete cases
on both sides applies. Each row preserves the profile count, return rate and
Wilson interval, comparison count, rate and interval, and their difference in
percentage points. A factor/profile selector compares that profile across all
years in a table and chart. Missing or unsupported years remain unavailable;
chart segments do not bridge missing calendar years. No significance test or
claim of an established change in effect is made from these descriptive series.
Changes can also reflect population mix, coverage and other confounders.

## Full-population PCA and clustering

The complete spectrum is visible by default. A correlation circle uses
variable/component correlations, not raw eigenvector coefficients. A paired
character plane distinguishes return and no observed return, with full-population
group means. All 52 axes have an outcome-correlation table for each horizon,
cohort and initial-loss population. Return is a **supplementary label** and never
changes the fitted PCA axes. Every complete case enters these statistics; a
uniform reservoir of at most 2,048 characters supplies display points only.
PCA variance describes profiles, while supplementary outcome correlations link
them to observed return. Supervised probability contrasts are separate results.

Continuous features use `log1p` except proportions; missingness has explicit
indicator columns. Numeric features are centered and scaled within first-loss
year, with standardized values bounded to ±5. Categorical features use one-hot
encoding. PCA is calculated from streaming sums and cross-products of **all**
characters. Clustering retains enough components for at least 90% of profile
variance, without a 16-component cap. Degenerate data has no explained-variance
ratio and is marked accordingly.

Every character's `projection` archives **all** global PCA coordinates, in PC
order, including directions with negligible variance. Coordinates are not
truncated or zeroed according to the clustering retention threshold. The current encoding has
52 dimensions. The model stores the full basis and mean, retained clustering
basis, eigenvalues and preprocessing scales. Archiving all coordinates makes
other axis views possible without repeating source extraction. Variance per
axis, cumulative variance, retained axes and the largest input coefficients are
shown in Admin Debug. Squared coefficients are axis weights, not causal effects
or percentages of return explained. PCA variance describes profile dispersion
([PCA documentation](https://scikit-learn.org/stable/modules/generated/sklearn.decomposition.PCA.html)).

Separate yearly covariance matrices are accumulated in the same streaming PCA
pass. Their spectra and input coefficients describe changes in profile structure.
The yearly means and full bases are preserved in the model. These diagnostic
PCAs have independent axes: PC1 in two years need not represent the same
direction. Archived player coordinates and clustering always use the shared
global basis. Year-specific return associations, computed separately from PCA,
are the relevant descriptive comparison for changes associated with return.

**Auto** is the default: a single job compares 2–30 groups, selects a count and
fits the final model on every character. A manual count from 2 to 30 remains
available. Model selection uses a uniform reservoir of up to 12,288 profiles,
split into at most 8,192 fitting, 2,048 selection and 2,048 confirmation profiles.
Smaller populations use proportionally smaller subsets and fewer candidate
counts when necessary. The comparison does not repeat source extraction.

Each count has three deterministic k-means++ starts, each fitted for at most
25 iterations. The selection subset supplies mean Euclidean silhouette,
its deviation between starts, and mean pairwise adjusted Rand index (ARI).
Auto prefers candidates with ARI ≥ 0.80 and chooses the smallest count within
0.01 of the best mean silhouette in that set. If none are sufficiently stable,
the same rule applies to all valid candidates with an explicit notice. These
thresholds are research heuristics, not proof of natural player categories.
Silhouette measures profile separation; ARI compares assignments independently
of label numbering ([silhouette documentation](https://scikit-learn.org/stable/auto_examples/cluster/plot_kmeans_silhouette_analysis.html),
[ARI documentation](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.adjusted_rand_score.html)).

The confirmation subset is excluded from pilot fitting and count selection.
Its pilot scores are preserved. Weak separation, unstable starts, tiny fitting
groups, a count at the search boundary and failure to find evaluable groups are
reported. Too few profiles or no variation yields one group without inventing
a silhouette score. A manual count that cannot be evaluated fails explicitly.

The selected count then gets three complete streaming passes for all three
starts together, followed by a full-population assignment pass. The final start
has the best silhouette on the selection subset. Final silhouette scores are
descriptive because these profiles also participate in full-population fitting;
they are not an independent validation of the final model. Population-wide ARI
and group sizes are computed from every character. Unstable final assignments
or unused groups remain visible. Clusters show their most frequent initial
profiles and observed return outcomes, evaluated afterwards. No return outcome
is used to choose the count or centers. IDs are local to one fitted model.

Model metadata preserves feature names, cohort scales, PCA axes, centers,
random seeds, selected count, all candidate diagnostics, population size and
NumPy version. Admin Debug shows comparison progress and the candidate table
while the job runs, then the selected grouping and return/trajectory results.

## English PDF export

**Download PDF · English** exports the completed snapshot: full study or current
horizon/cohort/initial-loss selection. The cover uses the supplied Imperium logo
and red CLASSIFIED marking. Contents include method, raw return counts, adjusted
factor effects, independent assessment, importance, all PCA axes, correlation
circle, character plane, trajectories, interpretation, conclusions and limitations.
Earlier reports remain exportable, marked preliminary when adjusted analysis is
absent. Missing results are never replaced with invented values.

The standard-library exporter needs no new server dependency. It reads the stored
run snapshot; no raw killmail query, job launch or result write occurs. Permission
is `admin.jobs.view`. `GET /admin/debug/first-loss/report.pdf` accepts
`horizon=all|7|30|90`, `year=all|YYYY`, `scope=all|pvp|npc`, with no-store download
headers. Missing reports/cohorts are 404, incomplete studies 409. Local review
can call `train(..., persist=False)` using a read-only connection; the resulting
report states that updated coordinates were not written to production. Status
polling sends only metadata when the completed revision is already loaded.

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
source tables. Changing Auto/manual or the requested count reuses extracted
features and outcomes, then refits the model in the same study. Repeating an
already completed model with the same settings/version reuses its results.
Existing studies made before Auto or full PCA coordinates can reuse their
prepared data. Rebuild is needed to incorporate changed source history, not to
compare group counts.

CLI equivalents:

```sh
python scripts/analyze_first_losses.py                  # Auto
python scripts/analyze_first_losses.py --clusters auto  # Explicit Auto
python scripts/analyze_first_losses.py --clusters 8     # Refit prepared data
python scripts/analyze_first_losses.py --rebuild         # Reread sources, then Auto
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

Cutoff, missing days and episode window are frozen for a study. The grouping
mode and count can change without extracting its source history again.
Model version 3 refits completed version-2 studies from their prepared features
and replaces 2D projections with complete vectors. For a running worker, deploy
the change, then Stop and Analyze / resume to load the new code consistently.
The finished monthly stages remain checkpointed; Rebuild is unnecessary for
this upgrade.
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
The Auto comparison adds bounded numerical work. One 2,048-by-2,048 distance
matrix holds about 32 MiB, with additional arrays during calculations. It does
not perform 29 full-history database fits. The three selected starts share the
same full-population streaming passes; their distance calculations add CPU work.
Full projection vectors require more per-character storage than the old two
coordinates. Yearly moments are bounded by the feature dimension and number
of cohorts; they do not create a separate database table per year or add raw
source passes. Model and summary metadata store aggregate diagnostics only.

For later analyses, preserve `first_loss_players` (all coordinates, original
derived features, outcomes and cohort) and `first_loss_runs` (model, bases,
scaling, report and source coverage). A future purge of working group aggregates
must not delete this reusable dataset. No purge is performed by this change.
Full coordinates reconstruct the normalized/clipped encoded profile; keeping
the original derived features also preserves information lost in preprocessing.

## Validation

Offline tests cover labels, missing history, historical Pochven, leakage,
year adjustment, non-PvP transitions, JSON conversion of PostgreSQL aggregates,
manual controls, permissions and stale progress. Diagnostic tests check metric
formulas, recovery of three separated synthetic profiles, deterministic Auto
selection, unstable candidates, degenerate data and interruption. Browser checks
exercise default Auto, the live candidate table, notices, manual selection and
replacement of results after refitting the same study ID.
PCA tests check retention beyond two and sixteen axes, variance ratios,
reconstruction from the full basis, degenerate data and different dominant input
coefficients across synthetic years. Annual association tests expose opposite
yearly differences that cancel in the pooled result. Browser tests check the
annual selector, variance table, sign changes, gaps and legacy reports.

The PostgreSQL integration test uses a disposable database named
`first_loss_test*`; its guard refuses other names. It exercises all 601 fixture
characters, real SQL extraction, PCA, assignment to clusters, the real worker,
SIGTERM during slow SQL, successful resumption and default Auto. Refitting and
reuse of completed results are tested with all monthly source tables temporarily
renamed in the disposable database: they must succeed without rereading sources.
Changing grouping must preserve overall return counts and other factor results.
The test also upgrades a completed version-2 report while sources are unavailable,
checks complete coordinate vectors for all 601 characters and reconstructs an
encoded profile from its archived coordinates.
Fixture counts are test data, not scientific results from the live population.

```sh
python -m unittest scripts.test_first_loss_pca scripts.test_first_loss_clustering scripts.test_first_loss_study scripts.test_first_loss_return_model scripts.test_first_loss_admin scripts.test_first_loss_pdf -v
python -m scripts.test_first_loss_admin_browser
# Optional integration, only against a disposable database:
FIRST_LOSS_TEST_DSN='host=/tmp/lab port=55444 dbname=first_loss_test user=codex' \
  python -m unittest scripts.test_first_loss_study -v
```
