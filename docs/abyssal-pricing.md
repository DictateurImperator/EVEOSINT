# Local abyssal module pricing

Policy `abyssal-t2-or-t1-v1` takes priority over every zKill abyssal quote,
including an already imported quote and cached quotes. It applies to module
items; mutaplasmids themselves retain their own prices.

CCP SDE `dynamicItemAttributes.inputOutputMapping` identifies compatible input
modules. The price is the ordinary T2 module's price on the UTC loss date,
using the existing historical price fallback when necessary. If no compatible
T2 exists, use the ordinary T1. Named and faction variants are not T1 defaults.

Approved exceptions:

- Small/Medium armor plates: cheapest compatible T2 size at the loss date.
- Abyssal Deep Core Strip Miner: Strip Miner I (the only original base is ORE).

Unknown mappings or unavailable equivalent prices never fall back to an
abyssal quote. They remain unavailable for calculation and are reported.

Admin Jobs → **Killmails · Reprice abyssal modules (T2 / T1)** recalculates the
full value of matching killmails only. It commits by month, preserves existing
values when a replacement cannot be calculated, and avoids writing unchanged
values. No killmail, attacker, or item is deleted. Changed months invalidate
ranking snapshots through the existing revision hook. The job then waits for
any running ranking builder and refreshes initialized rankings. If rankings
have not been initialized, their first build will use the corrected values.

The ordinary daily NULL-value calculator, item-price recovery, and killmail
inventory display all use the same local policy. No change to zKill's prices
can override it.
