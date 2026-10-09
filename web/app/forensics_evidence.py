"""Bounded read-only evidence collection. The target never enters its own evidence."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import combinations

from psycopg2.extras import RealDictCursor

from .entities import (
    _lookup_entity_names,
    _lookup_system_locations,
    _lookup_type_names,
    _mask_bit_is_set,
    _table_exists,
)
from .forensics_engine import VERSION, infer_ids, pilot_candidates, utc


def rows(conn, sql, params=()):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SET LOCAL statement_timeout = '15000ms'")
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def mer_snapshot(conn, ref):
    at = utc(ref["kill_datetime"])
    result = rows(
        conn,
        """SELECT to_jsonb(m) AS snapshot FROM mer.killmails m
        WHERE kill_datetime=%s AND source_month=%s::date AND source_row=%s""",
        (at, ref["source_month"], int(ref["source_row"])),
    )
    if len(result) != 1:
        raise ValueError("The MER reference does not identify exactly one row.")
    snapshot = result[0]["snapshot"]
    if snapshot.get("resolved_km") is not None:
        raise ValueError("This MER row is already matched. Choose a hidden killmail.")
    if not snapshot.get("victim_ship_type_id"):
        raise ValueError(
            "The victim ship type is missing; the hash cannot be computed."
        )
    if at.microsecond:
        raise ValueError(
            "A whole-second kill time is required for hash reconstruction."
        )
    return snapshot


def id_evidence(conn, snapshot, exclude_kill_ids=()):
    at = utc(snapshot["kill_datetime"])
    month = snapshot["source_month"]
    anchors = []
    for direction, order in (("<", "DESC"), (">", "ASC")):
        # SQL operators here are internal constants, never request text.
        result = rows(
            conn,
            f"""SELECT m.kill_datetime AS time,m.resolved_km[1] AS id
            FROM mer.killmails m WHERE m.source_month=%s::date
            AND m.kill_datetime BETWEEN %s AND %s AND m.kill_datetime {direction} %s
            AND array_length(m.resolved_km,1)=1 AND NOT m.resolved_km_ambiguous
            AND NOT EXISTS (SELECT 1 FROM mer.killmails other
                WHERE other.kill_datetime=m.kill_datetime AND other.source_month=m.source_month
                AND other.source_row<>m.source_row)
            ORDER BY m.kill_datetime {order} LIMIT 1""",
            (month, at - timedelta(days=7), at + timedelta(days=7), at),
        )
        anchors.append(result[0] if result else None)
    before, after = anchors
    if not before or not after:
        return infer_ids(before, after, 0, 0, 0, [])
    counts = rows(
        conn,
        """SELECT COUNT(*) AS between_count,
        COUNT(*) FILTER (WHERE kill_datetime<%s) AS prior_count,
        COUNT(*) FILTER (WHERE kill_datetime=%s) AS same_second_count
        FROM mer.killmails WHERE source_month=%s::date AND kill_datetime>%s AND kill_datetime<%s""",
        (at, at, month, before["time"], after["time"]),
    )[0]
    known = rows(
        conn,
        """SELECT resolved_km[1] AS id FROM mer.killmails
        WHERE source_month=%s::date AND kill_datetime>%s AND kill_datetime<%s
        AND array_length(resolved_km,1)=1 AND NOT resolved_km_ambiguous""",
        (month, before["time"], after["time"]),
    )
    for anchor in anchors:
        anchor["time"] = anchor["time"].isoformat()
    return infer_ids(
        before,
        after,
        counts["prior_count"],
        counts["same_second_count"],
        counts["between_count"],
        [r["id"] for r in known if r["id"] not in exclude_kill_ids],
    )


def nearby_systems(conn, system):
    ids = {system} if system else set()
    if not ids or not _table_exists(conn, "public", "sde_mapstargates"):
        return ids
    for _ in range(2):
        edges = rows(
            conn,
            """SELECT (data->>'solarSystemID')::bigint AS origin,
            (data->'destination'->>'solarSystemID')::bigint AS destination
            FROM public.sde_mapstargates WHERE (data->>'solarSystemID')::bigint=ANY(%s)
            OR (data->'destination'->>'solarSystemID')::bigint=ANY(%s)""",
            (list(ids), list(ids)),
        )
        ids.update(e[k] for e in edges for k in ("origin", "destination") if e[k])
        if len(ids) > 200:
            return {system}
    return ids


def analyze(conn, ref):
    return analyze_snapshot(conn, mer_snapshot(conn, ref))


def analyze_snapshot(conn, snapshot, exclude_kill_ids=()):
    """Read evidence; offline evaluation masks known targets from observations.

    Public routes use analyze(), which rejects resolved MER rows.
    """
    snapshot = dict(snapshot)
    ship_ids = [
        snapshot[k]
        for k in ("victim_ship_type_id", "killer_ship_type_id")
        if snapshot.get(k)
    ]
    ship_names = _lookup_type_names(conn, ship_ids)
    for side in ("victim", "killer"):
        snapshot[side + "_ship_type_name"] = ship_names.get(
            snapshot.get(side + "_ship_type_id")
        )
    system = snapshot.get("solar_system_id")
    snapshot["solar_system_name"] = (
        _lookup_system_locations(conn, [system])
        .get(system, {})
        .get("system", {})
        .get("name")
    )
    corp_ids = [
        snapshot[k]
        for k in ("victim_corporation_id", "killer_corporation_id")
        if snapshot.get(k)
    ]
    corp_names = _lookup_entity_names(conn, "corporation", corp_ids)
    for side in ("victim", "killer"):
        snapshot[side + "_corporation_name"] = corp_names.get(
            snapshot.get(side + "_corporation_id"), {}
        ).get("name")
    at = utc(snapshot["kill_datetime"])
    ids = id_evidence(conn, snapshot, exclude_kill_ids)
    warnings = []
    corps = list(
        {
            snapshot[k]
            for k in ("victim_corporation_id", "killer_corporation_id")
            if snapshot.get(k)
        }
    )
    systems = list(nearby_systems(conn, snapshot.get("solar_system_id")))
    observations = []
    if (
        corps
        and systems
        and _table_exists(conn, "rawkm", "killmails")
        and _table_exists(conn, "rawkm", "killmail_attackers")
    ):
        observations = rows(
            conn,
            """SELECT k.killmail_id,k.killmail_time AS time,k.solar_system_id AS system_id,
                a.character_id,a.corporation_id,a.ship_type_id,a.final_blow,'attacker'::text AS role
            FROM rawkm.killmails k JOIN rawkm.killmail_attackers a
                ON a.killmail_id=k.killmail_id AND a.killmail_time=k.killmail_time
            WHERE k.killmail_time BETWEEN %s AND %s AND a.killmail_time BETWEEN %s AND %s
                AND k.solar_system_id=ANY(%s) AND a.corporation_id=ANY(%s)
                AND NOT (k.killmail_id=ANY(%s::bigint[]))
            UNION ALL
            SELECT k.killmail_id,k.killmail_time,k.solar_system_id,k.victim_character_id,
                k.victim_corporation_id,k.victim_ship_type_id,FALSE,'victim'::text
            FROM rawkm.killmails k WHERE k.killmail_time BETWEEN %s AND %s
                AND k.solar_system_id=ANY(%s) AND k.victim_corporation_id=ANY(%s)
                AND NOT (k.killmail_id=ANY(%s::bigint[]))
            ORDER BY time,killmail_id,character_id LIMIT 5001""",
            (
                at - timedelta(hours=1),
                at + timedelta(hours=1),
                at - timedelta(hours=1),
                at + timedelta(hours=1),
                systems,
                corps,
                list(exclude_kill_ids),
                at - timedelta(hours=1),
                at + timedelta(hours=1),
                systems,
                corps,
                list(exclude_kill_ids),
            ),
        )
    if len(observations) > 5000:
        warnings.append(
            "The combat window exceeds 5,000 appearances; evidence is incomplete."
        )
        observations = observations[:5000]
    past_pvp = {}
    if corps and _table_exists(conn, "rawkm", "killmail_attackers"):
        for corp in corps:
            activity = rows(
                conn,
                """SELECT character_id,MAX(killmail_time) AS last_attack
                FROM rawkm.killmail_attackers WHERE corporation_id=%s
                AND killmail_time >= %s AND killmail_time < %s AND character_id IS NOT NULL
                GROUP BY character_id ORDER BY last_attack DESC,character_id LIMIT 2001""",
                (corp, at - timedelta(days=90), at),
            )
            if len(activity) > 2000:
                warnings.append(
                    f"Corporation {corp}: combatant history is limited to the latest 2,000 active pilots."
                )
            past_pvp[corp] = {
                r["character_id"]: r["last_attack"] for r in activity[:2000]
            }
    priority_ids = list(
        {cid for pilots in past_pvp.values() for cid in pilots}
        | {e["character_id"] for e in observations if e["character_id"]}
    )
    membership = []
    if corps and _table_exists(conn, "entities", "character_corporation_history"):
        membership = rows(
            conn,
            """SELECT DISTINCT h.character_id,h.corporation_id,c.name,
                (h.character_id=ANY(%s)) AS priority
            FROM entities.character_corporation_history h LEFT JOIN entities.characters c USING(character_id)
            WHERE h.corporation_id=ANY(%s) AND h.start_date<=%s AND (h.end_date IS NULL OR h.end_date>%s)
                AND NOT COALESCE(h.is_deleted,FALSE)
            ORDER BY priority DESC,h.character_id LIMIT 2001""",
            (priority_ids, corps, at, at),
        )
    if len(membership) > 2000:
        warnings.append(
            "More than 2,000 historical members; the candidate list is incomplete."
        )
        membership = membership[:2000]
    seeds = list({m["character_id"] for m in membership} | set(priority_ids))
    names = {
        cid: info["name"]
        for cid, info in _lookup_entity_names(conn, "character", seeds).items()
    }
    capabilities = {}
    if (
        seeds
        and _table_exists(conn, "entities", "character_pilotable_ships")
        and _table_exists(conn, "entities", "pilotable_ship_index")
    ):
        ship_ids = [
            snapshot[k]
            for k in ("victim_ship_type_id", "killer_ship_type_id")
            if snapshot.get(k)
        ]
        bits = rows(
            conn,
            "SELECT ship_type_id,bit_index FROM entities.pilotable_ship_index WHERE ship_type_id=ANY(%s) AND enabled IS TRUE",
            (ship_ids,),
        )
        masks = rows(
            conn,
            "SELECT character_id,ship_mask FROM entities.character_pilotable_ships WHERE character_id=ANY(%s)",
            (seeds,),
        )
        for bit in bits:
            capabilities[bit["ship_type_id"]] = {
                m["character_id"]
                for m in masks
                if _mask_bit_is_set(bytes(m["ship_mask"] or b""), bit["bit_index"])
            }
    prior_usage = {}
    if (
        seeds
        and _table_exists(conn, "rawkm", "killmails")
        and _table_exists(conn, "rawkm", "killmail_attackers")
    ):
        ship_ids = [
            snapshot[k]
            for k in ("victim_ship_type_id", "killer_ship_type_id")
            if snapshot.get(k)
        ]
        usage = rows(
            conn,
            """SELECT character_id,ship_type_id,MAX(time) AS last_used FROM (
            SELECT character_id,ship_type_id,killmail_time AS time FROM rawkm.killmail_attackers
            WHERE killmail_time >= %s AND killmail_time < %s AND character_id=ANY(%s) AND ship_type_id=ANY(%s)
            UNION ALL SELECT victim_character_id,victim_ship_type_id,killmail_time FROM rawkm.killmails
            WHERE killmail_time >= %s AND killmail_time < %s AND victim_character_id=ANY(%s) AND victim_ship_type_id=ANY(%s)
        ) usage GROUP BY character_id,ship_type_id""",
            (
                at - timedelta(days=90),
                at,
                seeds,
                ship_ids,
                at - timedelta(days=90),
                at,
                seeds,
                ship_ids,
            ),
        )
        prior_usage = {
            (u["character_id"], u["ship_type_id"]): u["last_used"] for u in usage
        }
    companions = Counter()
    if seeds and _table_exists(conn, "rawkm", "killmail_attackers"):
        history = rows(
            conn,
            """SELECT killmail_id,array_agg(DISTINCT character_id) AS pilots
            FROM rawkm.killmail_attackers WHERE killmail_time >= %s AND killmail_time < %s
            AND corporation_id=ANY(%s) AND character_id=ANY(%s)
            GROUP BY killmail_id ORDER BY killmail_id DESC LIMIT 2001""",
            (at - timedelta(days=30), at, corps, seeds),
        )
        if len(history) > 2000:
            warnings.append(
                "Companion habits use the latest 2,000 observed fights in the preceding 30 days."
            )
        for event in history[:2000]:
            # Bounded per-fight pairs; a large fleet is weaker evidence than a trio.
            pilots = sorted(event["pilots"])
            if len(pilots) <= 20:
                companions.update(combinations(pilots, 2))
    categories = {}
    if _table_exists(conn, "public", "sde_types") and _table_exists(
        conn, "public", "sde_groups"
    ):
        ship_ids = [
            str(snapshot[k])
            for k in ("victim_ship_type_id", "killer_ship_type_id")
            if snapshot.get(k)
        ] + [str(e["ship_type_id"]) for e in observations if e.get("ship_type_id")]
        data = rows(
            conn,
            """SELECT t.sde_key::bigint AS ship,(g.data->>'categoryID')::int AS category,
                (t.data->>'groupID')::bigint AS group_id FROM public.sde_types t
            JOIN public.sde_groups g ON g.sde_key=t.data->>'groupID' WHERE t.sde_key=ANY(%s)""",
            (ship_ids,),
        )
        categories = {r["ship"]: r["category"] for r in data}
        groups = {r["ship"]: r["group_id"] for r in data}
        for side in ("victim", "killer"):
            snapshot[side + "_ship_group_id"] = groups.get(
                snapshot.get(side + "_ship_type_id")
            )
        for event in observations:
            event["ship_category_id"] = categories.get(event.get("ship_type_id"))
            event["ship_group_id"] = groups.get(event.get("ship_type_id"))
    hypotheses = {
        "version": VERSION,
        "ids": ids,
        "warnings": warnings,
        "score_kind": "relative_likelihood",
        "context_minutes": 60,
        "neighbor_jumps": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    for side, corp_key, ship_key in (
        ("victim", "victim_corporation_id", "victim_ship_type_id"),
        ("attacker", "killer_corporation_id", "killer_ship_type_id"),
    ):
        members = {
            m["character_id"]: m["name"] or f"Character {m['character_id']}"
            for m in membership
            if m["corporation_id"] == snapshot.get(corp_key)
        }
        hypotheses[side] = pilot_candidates(
            snapshot,
            side,
            members,
            observations,
            capabilities.get(snapshot.get(ship_key), set()),
            companions,
            category=categories.get(snapshot.get(ship_key)),
            truncated=bool(warnings),
            prior_usage=prior_usage,
            npc_loss=categories.get(snapshot.get("killer_ship_type_id")) == 11,
            past_pvp=past_pvp.get(snapshot.get(corp_key), {}),
            names=names,
        )
    return snapshot, hypotheses
