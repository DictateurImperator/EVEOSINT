"""EVEOSINT's local module valuation policy, independent of abyssal zKill quotes.

CCP's mutaplasmid input/output mappings identify compatible ordinary modules.
Prefer compatible T2 modules; otherwise the ordinary T1. For an ambiguous
plate size, use the cheapest compatible T2 at the loss date. The ORE-only
Deep Core Strip Miner uses Strip Miner I, as agreed for this policy.
"""
POLICY_VERSION = 'abyssal-t2-or-t1-v1'
ORE_ONLY_STRIP_MINER = 90498
STRIP_MINER_I = 17482


def is_abyssal_module(metadata, type_id):
    _group, category, name = metadata.get(type_id, (None, None, ''))
    return category == 7 and 'Abyssal' in name


def derive_equivalents(rows, input_mappings):
    """Rows: id, group, category, English name, meta group, tech level."""
    types = {int(row[0]): row[1:] for row in rows}
    applicable = {}
    for mappings in input_mappings:
        for mapping in mappings or []:
            applicable.setdefault(int(mapping['resultingType']), set()).update(map(int, mapping.get('applicableTypes', [])))
    equivalents = {}
    for ident, (group, category, name, meta, _tech) in types.items():
        if category != 7 or (meta != 15 and 'Abyssal' not in name):
            continue
        inputs = [i for i in applicable.get(ident, ()) if i in types and types[i][1] == 7]
        t2 = [i for i in inputs if types[i][3] == 2 and types[i][4] == 2]
        if t2:
            equivalents[ident] = tuple(sorted(t2))
            continue
        base_name = ' '.join(name.replace('Abyssal', '').split())
        # Exact ordinary name, rather than a named/faction variant of the same group.
        t1 = [i for i in inputs if types[i][4] == 1 and types[i][2] in (base_name, base_name + ' I')]
        if ident == ORE_ONLY_STRIP_MINER and STRIP_MINER_I in types:
            t1 = [STRIP_MINER_I]
        if not t1:
            raise ValueError(f'No approved T2/T1 equivalent for abyssal module {ident}: {name}')
        equivalents[ident] = tuple(sorted(t1))
    return equivalents


def load_equivalents(conn):
    # Small scalar metadata only, never the large translated SDE descriptions.
    with conn.cursor() as cur:
        cur.execute("""SELECT t.sde_key::bigint,(t.data->>'groupID')::int,
                   (g.data->>'categoryID')::int,COALESCE(t.data->'name'->>'en',''),
                   (t.data->>'metaGroupID')::int,(t.data->>'techLevel')::int
            FROM public.sde_types t JOIN public.sde_groups g ON g.sde_key=t.data->>'groupID'
            WHERE g.data->>'categoryID'='7'""")
        rows = cur.fetchall()
        if not any(row[4] == 15 or 'Abyssal' in row[3] for row in rows):
            return {}
        cur.execute("SELECT data->'inputOutputMapping' FROM public.sde_dynamicitemattributes")
        mappings = [row[0] for row in cur]
    return derive_equivalents(rows, mappings)
