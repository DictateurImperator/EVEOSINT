# Killmail Forensics research

The admin workspace is currently a TODO page. No hash search, ESI retrieval,
or database import is implemented yet.

## Hash construction

zKillboard's published reconstruction code computes the SHA-1 of the ASCII
concatenation, without separators, of these four values in this order:

1. Victim character ID.
2. Character ID of the attacker delivering the final blow.
3. Victim ship type ID.
4. Kill time converted to Windows FILETIME ticks.

For a UTC Unix timestamp in whole seconds:

```text
ticks = unix_seconds * 10000000 + 116444736000000000
hash = SHA1(victim_character + final_blow_character + victim_ship_type + ticks)
```

All values are converted to decimal text before concatenation. A character
that is genuinely absent uses the literal `None`, rather than `0` or an empty
string. An unknown character ID is not evidence that the character is absent.
The conversion must use UTC and integer arithmetic.

The solar system, corporation IDs, alliance IDs, values, attacker ship type,
and killmail ID do not enter this formula. The killmail ID is still required,
separately, to retrieve the full record from ESI:

```text
GET /killmails/{killmail_id}/{killmail_hash}/
```

## What the existing MER import provides

The three CSV schema mappings in `web/app/mer.py` retain the kill time and
victim ship type, as well as corporation/alliance information where available.
They do not expose victim character ID, final-blow character ID, or the original
killmail ID. `source_row` is a CSV row number, not a killmail ID.

Before designing the search, inspect the actual source files for the selected
month. Missing fields will require independently justified candidates; computing
a hash is straightforward once its inputs are known. Results should first be
checked against known ID/hash pairs before trying an unresolved MER record.

This is evidence from zKillboard's published code, not a guarantee from the
current ESI specification. No live recovery has been validated in this change.

## Primary sources

- [zKillboard: MER reconstruction](https://github.com/zKillboard/zKillboard/blob/4b3bda3db1c947984cd24c7b21616e6323059d64/mer/load_mer_dump.php).
- [zKillboard: reconstruction from ESI payloads](https://github.com/zKillboard/zKillboard/blob/4b3bda3db1c947984cd24c7b21616e6323059d64/scratch/hashfix.php).
- [CCP: killReport links contain both ID and hash](https://developers.eveonline.com/docs/guides/eve-html/).
