# Pilot reconstruction benchmark — October 9, 2026

Version 2 was evaluated on **98 known MER kills** from July/August 2026. Each
target's raw combat record and known-ID elimination were masked before scoring.
The algorithm received MER attributes, other kills, historical affiliations and
current inferred skills. No trial hashes from this benchmark were sent to CCP.

The first 60 August cases identified useful capsule transitions and object
ownership behavior. A separate 30-case July sample checked the resulting rules;
eight additional August structures/deployables covered uncommon cases. Kill IDs
are unique across these samples. This selected known-kill dataset is not a
representative sample of all hidden losses or a probability calibration.

## Initial selected plan

The initial plan selects up to 20 inferred CCP IDs, nine victim candidates plus
`None`, and nine known combatants plus `None` for the final blow. A kill is covered
only when its exact ID/victim/final-blow tuple occurs in that plan. Trials are
ordered by the combined heuristic score, including tied-ID alternatives.

| Category | Kills | Covered | Mean trials to success, covered cases | Mean spent, including exhausted misses |
| --- | ---: | ---: | ---: | ---: |
| PvP ship losses | 52 | 40/52 | 2.20 | 23.23 |
| PvP capsule losses | 15 | 15/15 | 2.13 | 2.13 |
| NPC / absent-character final blow | 17 | 15/17 | 1.47 | 2.47 |
| Structures and deployables | 14 | 13/14 | 4.00 | 10.86 |
| Total | 98 | 83/98 | 2.34 | 14.63 |

Mean trials to success excludes uncovered kills, which have no successful trial
in that plan. The last column accounts for the entire exhausted budget on those
misses. It therefore avoids treating an unknown or unlisted pilot as a cheap
successful search. Among covered cases, the median is one trial and the 90th
percentile is five. These are simulated positions in a plan, not actual API
recoveries. Scheduling delays, retries and other services' CCP budgets are extra.

A broader plan of up to 49 pilots plus `None` per side covers **90/98**, with
5.19 trials on average among covered cases. Its average spent including exhausted
misses is 133.85, illustrating the cost of widening a search without new evidence.
The separate July sample covers 28/30 initially and 29/30 when widened.

Across all cases, the true final-blow character is rank 1 in 87/98 and in the
top five in 95/98. The true victim/owner is rank 1 in 65/98 and in the top five in
84/98. A remaining weakness is sparse victim evidence, particularly large
corporations, new or inactive pilots and owners without nearby known losses.

## Rules added from existing cases

- A known ship loss shortly before a capsule-related kill supports that character.
- A known capsule loss shortly after a hidden ship loss supports the same victim.
- An attacker whose ship died shortly before the final-blow kill may still receive
  credit in a capsule; ship death does not exclude delayed attribution.
- Repeated losses of an owned deployable/structure support ownership. They do not
  imply that its owner died or needed to reship. CCP can expose an owner character
  on an unpiloted object's victim record; `None` is an alternative, not a rule.
- Missing NPC affiliation fields in CCP do not contradict affiliations supplied
  by MER. Core hash/ID/time/ship/system matching remains required; contradictory
  optional fields that both sources supply are rejected.

On the same 60 August cases, these changes increase initial coverage from 44 to
47 and reduce mean trials among covered cases from 2.89 to 2.15. Larger coverage
and lower conditional mean are reported together. These cases informed the new
rules, so the separate July result is the check on a different month.

Current inferred skills may have learned from a masked known kill. A replay
without that signal still covers 83/98, with 2.42 trials among covered cases, and
90/98 when widened. This limits the impact of that derived-data shortcut; it does
not remove all differences between known and hidden killmail populations.

## Reproduce

`scripts/evaluate_forensics_pilots.py` uses an explicit PostgreSQL connection in
read-only mode and privately prompts for its password. It does not import
production configuration or web startup, perform DDL/writes, or call CCP. The
report caches evidence for scoring-only replay. Keep full caches outside Git:

```bash
python scripts/evaluate_forensics_pilots.py --month 2026-08-01 --samples 60 --output /tmp/forensics-august.json
python scripts/evaluate_forensics_pilots.py --month 2026-07-01 --samples 30 --output /tmp/forensics-july.json
python scripts/evaluate_forensics_pilots.py --month 2026-08-01 --objects --samples 8 --output /tmp/forensics-objects.json
python scripts/evaluate_forensics_pilots.py --replay /tmp/forensics-august.json --no-current-capability --output /tmp/forensics-without-skills.json
```

The default connection is `codex_ro` / `eveosint` at `127.0.0.1:5432`; flags can
override it. Time-limited SQL failures are reported and excluded from completed
case counts. Later DB imports or code changes can change the selected evidence
and results. The checked-in summary and per-case ranks/trial counts are in
[`forensics_benchmark_2026-10-09.json`](forensics_benchmark_2026-10-09.json).
