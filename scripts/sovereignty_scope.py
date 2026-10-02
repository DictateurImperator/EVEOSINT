"""Shared SDE scope helpers for sovereignty collectors.

EVEOSINT sovereignty scope = conquerable nullsec only:
- securityStatus <= 0.0
- New Eden known-space system/region ID ranges (not wormhole/J-space, not Pochven)
- no NPC faction ownership at system, constellation or region level

The faction checks intentionally exclude NPC nullsec regions and NPC pockets in
otherwise conquerable regions.
"""

POCHVEN_REGION_ID = 10000070
NEW_EDEN_REGION_MIN = 10000000
NEW_EDEN_REGION_MAX = 10999999
NEW_EDEN_SYSTEM_MIN = 30000000
NEW_EDEN_SYSTEM_MAX = 30999999


def _int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _name(data, fallback):
    value = (data or {}).get("name")
    if isinstance(value, dict):
        return (
            value.get("en")
            or value.get("fr")
            or next((item for item in value.values() if item), None)
            or fallback
        )
    if isinstance(value, str) and value:
        return value
    return str((data or {}).get("solarSystemName") or fallback)


def claimable_sov_systems_from_rows(system_rows, constellation_rows, region_rows):
    """Return {system_id: metadata} for conquerable 0.0 systems only."""
    regions = {}
    for key, raw in region_rows:
        data = raw or {}
        region_id = _int(data.get("_key")) or _int(key)
        if region_id is not None:
            regions[region_id] = data

    constellations = {}
    for key, raw in constellation_rows:
        data = raw or {}
        constellation_id = _int(data.get("_key")) or _int(key)
        if constellation_id is not None:
            constellations[constellation_id] = data

    result = {}
    for key, raw in system_rows:
        data = raw or {}
        system_id = _int(data.get("_key")) or _int(key)
        region_id = _int(data.get("regionID"))
        constellation_id = _int(data.get("constellationID"))
        security = _float(data.get("securityStatus"))
        if system_id is None or region_id is None or security is None:
            continue
        if security > 0.0:
            continue
        if not (NEW_EDEN_SYSTEM_MIN <= system_id <= NEW_EDEN_SYSTEM_MAX):
            continue
        if not (NEW_EDEN_REGION_MIN <= region_id <= NEW_EDEN_REGION_MAX):
            continue
        if region_id == POCHVEN_REGION_ID:
            continue

        region = regions.get(region_id) or {}
        constellation = constellations.get(constellation_id) or {}

        # NPC nullsec and NPC pockets can be marked at any of these levels.
        if any(
            _int(source.get("factionID")) is not None
            for source in (data, constellation, region)
        ):
            continue

        result[system_id] = {
            "system_id": system_id,
            "name": _name(data, f"System {system_id}"),
            "region_id": region_id,
            "constellation_id": constellation_id,
            "security": security,
        }
    return result


def load_claimable_sov_systems(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT sde_key, data FROM public.sde_mapsolarsystems")
        systems = cur.fetchall()
        cur.execute("SELECT sde_key, data FROM public.sde_mapconstellations")
        constellations = cur.fetchall()
        cur.execute("SELECT sde_key, data FROM public.sde_mapregions")
        regions = cur.fetchall()
    return claimable_sov_systems_from_rows(systems, constellations, regions)
