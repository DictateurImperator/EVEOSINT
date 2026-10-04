import colorsys
import math
from datetime import date
from functools import lru_cache

from .db import db
from .dotlan_region_layouts import DOTLAN_MAP_BOUNDS, DOTLAN_PAGE_HEIGHT, DOTLAN_PAGE_WIDTH, REGION_LAYOUTS


class MapDataError(Exception):
    pass


def _name(data, fallback):
    value = data.get("name")
    if isinstance(value, dict):
        return value.get("en") or value.get("fr") or next((v for v in value.values() if v), None) or fallback
    if isinstance(value, str) and value:
        return value
    for key in ("solarSystemName", "constellationName", "regionName"):
        if data.get(key):
            return str(data[key])
    return fallback


def _position(data):
    pos = data.get("position") or {}
    # SDE coordinates are huge metre values. Scaling keeps browser-side maths stable
    # while preserving the exact relative geometry we need for the layout seed.
    scale = 1_000_000_000_000_000.0
    return {
        "x": float(pos.get("x") or 0.0) / scale,
        "y": float(pos.get("y") or 0.0) / scale,
        "z": float(pos.get("z") or 0.0) / scale,
    }


def _position_2d(data):
    pos = data.get("position2D") or {}
    if isinstance(pos, dict) and ("x" in pos or "y" in pos):
        scale = 1_000_000_000_000_000.0
        return {
            "x": float(pos.get("x") or 0.0) / scale,
            "y": float(pos.get("y") or 0.0) / scale,
        }

    # Fallback for older SDE data: top-down universe projection.
    position = _position(data)
    return {
        "x": position["x"],
        "y": position["z"],
    }


def _int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_table(conn, table_name):
    with conn.cursor() as cur:
        cur.execute(f"SELECT sde_key, data FROM public.{table_name}")
        return cur.fetchall()


@lru_cache(maxsize=1)
def _topology():
    with db() as conn:
        region_rows = _load_table(conn, "sde_mapregions")
        constellation_rows = _load_table(conn, "sde_mapconstellations")
        system_rows = _load_table(conn, "sde_mapsolarsystems")
        stargate_rows = _load_table(conn, "sde_mapstargates")
        faction_rows = _load_table(conn, "sde_factions")

        wormhole_systems_with_moons = set()
        wormhole_moon_data_available = False
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.sde_mapmoons')")
            if cur.fetchone()[0] is not None:
                wormhole_moon_data_available = True
                cur.execute("""
                    SELECT DISTINCT NULLIF(data->>'solarSystemID', '')::bigint
                    FROM public.sde_mapmoons
                    WHERE NULLIF(data->>'solarSystemID', '') ~ '^[0-9]+
    regions = {}
    for sde_key, data in region_rows:
        data = data or {}
        region_id = _int(data.get("_key")) or _int(sde_key)
        if region_id is None:
            continue
        regions[region_id] = {
            "id": region_id,
            "name": _name(data, f"Region {region_id}"),
            "position": _position(data),
            "faction_id": _int(data.get("factionID")),
            "wormhole_class_id": _int(data.get("wormholeClassID")),
            "constellation_ids": [_int(v) for v in (data.get("constellationIDs") or []) if _int(v) is not None],
        }

    constellations = {}
    for sde_key, data in constellation_rows:
        data = data or {}
        constellation_id = _int(data.get("_key")) or _int(sde_key)
        if constellation_id is None:
            continue
        constellations[constellation_id] = {
            "id": constellation_id,
            "name": _name(data, f"Constellation {constellation_id}"),
            "position": _position(data),
            "region_id": _int(data.get("regionID")),
            "faction_id": _int(data.get("factionID")),
            "wormhole_class_id": _int(data.get("wormholeClassID")),
            "system_ids": [_int(v) for v in (data.get("solarSystemIDs") or []) if _int(v) is not None],
        }

    factions = {}
    for sde_key, data in faction_rows:
        data = data or {}
        faction_id = _int(data.get("_key")) or _int(sde_key)
        if faction_id is None:
            continue
        factions[faction_id] = {
            "id": faction_id,
            "name": _name(data, f"Faction {faction_id}"),
        }

    systems = {}
    for sde_key, data in system_rows:
        data = data or {}
        system_id = _int(data.get("_key")) or _int(sde_key)
        if system_id is None:
            continue
        systems[system_id] = {
            "id": system_id,
            "name": _name(data, f"System {system_id}"),
            "position": _position(data),
            "position_2d": _position_2d(data),
            "constellation_id": _int(data.get("constellationID")),
            "region_id": _int(data.get("regionID")),
            "security": data.get("securityStatus"),
            "faction_id": _int(data.get("factionID")),
            "wormhole_class_id": _int(data.get("wormholeClassID")),
            "stargate_ids": [_int(v) for v in (data.get("stargateIDs") or []) if _int(v) is not None],
        }

    system_edges = set()
    for _, data in stargate_rows:
        data = data or {}
        source_id = _int(data.get("solarSystemID"))
        destination = data.get("destination") or {}
        destination_id = _int(destination.get("solarSystemID"))
        if source_id is None or destination_id is None or source_id == destination_id:
            continue
        if source_id not in systems or destination_id not in systems:
            continue
        system_edges.add(tuple(sorted((source_id, destination_id))))

    # Repair parent links from the constellation tables when a system row does not
    # carry them for any reason.
    for constellation in constellations.values():
        for system_id in constellation["system_ids"]:
            system = systems.get(system_id)
            if not system:
                continue
            if system.get("constellation_id") is None:
                system["constellation_id"] = constellation["id"]
            if system.get("region_id") is None:
                system["region_id"] = constellation.get("region_id")

    region_system_ids = {region_id: [] for region_id in regions}
    constellation_system_ids = {constellation_id: [] for constellation_id in constellations}
    for system in systems.values():
        region_id = system.get("region_id")
        constellation_id = system.get("constellation_id")
        if region_id in region_system_ids:
            region_system_ids[region_id].append(system["id"])
        if constellation_id in constellation_system_ids:
            constellation_system_ids[constellation_id].append(system["id"])

    region_edges = set()
    region_has_gate = set()
    system_neighbors = {system_id: set() for system_id in systems}
    for source_id, destination_id in system_edges:
        system_neighbors[source_id].add(destination_id)
        system_neighbors[destination_id].add(source_id)
        source_region = systems[source_id].get("region_id")
        destination_region = systems[destination_id].get("region_id")
        if source_region is not None:
            region_has_gate.add(source_region)
        if destination_region is not None:
            region_has_gate.add(destination_region)
        if source_region is not None and destination_region is not None and source_region != destination_region:
            region_edges.add(tuple(sorted((source_region, destination_region))))

    return {
        "regions": regions,
        "constellations": constellations,
        "factions": factions,
        "systems": systems,
        "system_edges": system_edges,
        "region_edges": region_edges,
        "region_has_gate": region_has_gate,
        "region_system_ids": region_system_ids,
        "constellation_system_ids": constellation_system_ids,
        "system_neighbors": system_neighbors,
        "wormhole_systems_with_moons": wormhole_systems_with_moons,
        "wormhole_moon_data_available": wormhole_moon_data_available,
    }


def _system_node(topology, system_id):
    system = topology["systems"][system_id]
    constellation = topology["constellations"].get(system.get("constellation_id"))
    region = topology["regions"].get(system.get("region_id"))
    external_neighbors = []
    for neighbor_id in topology["system_neighbors"].get(system_id, set()):
        neighbor = topology["systems"].get(neighbor_id)
        if not neighbor:
            continue
        if neighbor.get("region_id") != system.get("region_id"):
            external_neighbors.append({
                "id": neighbor_id,
                "name": neighbor["name"],
                "region_id": neighbor.get("region_id"),
                "region_name": (topology["regions"].get(neighbor.get("region_id")) or {}).get("name"),
            })
    external_neighbors.sort(key=lambda item: item["name"].lower())
    return {
        "id": system["id"],
        "name": system["name"],
        "position": system["position"],
        "security": system.get("security"),
        "constellation_id": system.get("constellation_id"),
        "constellation_name": constellation.get("name") if constellation else None,
        "region_id": system.get("region_id"),
        "region_name": region.get("name") if region else None,
        "gate_count": len(topology["system_neighbors"].get(system_id, set())),
        "external_neighbors": external_neighbors,
        "url": f"/map/system/{system_id}",
    }


def get_universe_map():
    topology = _topology()

    visible_region_ids = set(topology["region_has_gate"])
    if not visible_region_ids:
        visible_region_ids = set(topology["regions"])

    nodes = []
    for region_id in sorted(visible_region_ids):
        region = topology["regions"].get(region_id)
        if not region:
            continue
        constellation_ids = [
            cid for cid, constellation in topology["constellations"].items()
            if constellation.get("region_id") == region_id
        ]
        system_ids = topology["region_system_ids"].get(region_id, [])
        neighbor_ids = set()
        for a, b in topology["region_edges"]:
            if a == region_id:
                neighbor_ids.add(b)
            elif b == region_id:
                neighbor_ids.add(a)

        security_counts = {"hs": 0, "ls": 0, "null": 0}
        for system_id in system_ids:
            system = topology["systems"].get(system_id)
            if not system:
                continue
            security = system.get("security")
            try:
                security = float(security)
            except (TypeError, ValueError):
                security = 0.0
            if security >= 0.45:
                security_counts["hs"] += 1
            elif security > 0:
                security_counts["ls"] += 1
            else:
                security_counts["null"] += 1

        security_bands = [
            band for band in ("hs", "ls", "null")
            if security_counts[band] > 0
        ]

        # NPC ownership/influence comes from the region itself when CCP marks the
        # whole region as NPC space. For mixed nullsec regions (Delve, Fountain,
        # Pure Blind, Geminate, ...), constellation factionID gives the NPC pocket.
        faction_system_counts = {}
        region_faction_id = region.get("faction_id")
        if region_faction_id is not None:
            faction_system_counts[region_faction_id] = len(system_ids)
        else:
            for constellation_id in constellation_ids:
                constellation = topology["constellations"].get(constellation_id)
                if not constellation:
                    continue
                faction_id = constellation.get("faction_id")
                if faction_id is None:
                    continue
                count = sum(
                    1 for system_id in topology["constellation_system_ids"].get(constellation_id, [])
                    if system_id in system_ids
                )
                if count:
                    faction_system_counts[faction_id] = faction_system_counts.get(faction_id, 0) + count

        faction_breakdown = []
        for faction_id, count in sorted(faction_system_counts.items(), key=lambda item: (-item[1], item[0])):
            faction = topology["factions"].get(faction_id) or {}
            faction_breakdown.append({
                "id": faction_id,
                "name": faction.get("name") or f"Faction {faction_id}",
                "systems": count,
                "percent": round((count * 100.0 / len(system_ids)), 1) if system_ids else 0.0,
            })

        dominant_faction = faction_breakdown[0] if faction_breakdown else None
        npc_system_count = sum(item["systems"] for item in faction_breakdown)
        npc_control_percent = round((npc_system_count * 100.0 / len(system_ids)), 1) if system_ids else 0.0

        nodes.append({
            "id": region_id,
            "name": region["name"],
            "position": region["position"],
            "constellation_count": len(constellation_ids),
            "system_count": len(system_ids),
            "neighbor_count": len(neighbor_ids),
            "security_counts": security_counts,
            "security_bands": security_bands,
            "region_faction_id": region_faction_id,
            "dominant_faction_id": dominant_faction["id"] if dominant_faction else None,
            "dominant_faction_name": dominant_faction["name"] if dominant_faction else None,
            "npc_control_percent": npc_control_percent,
            "faction_breakdown": faction_breakdown,
            "url": f"/map/region/{region_id}",
        })

    edges = [
        {"source": a, "target": b}
        for a, b in sorted(topology["region_edges"])
        if a in visible_region_ids and b in visible_region_ids
    ]

    side_items = []
    for node in sorted(nodes, key=lambda item: item["name"].lower()):
        parts = []
        counts = node["security_counts"]
        if counts["hs"]:
            parts.append(f"{counts['hs']} HS")
        if counts["ls"]:
            parts.append(f"{counts['ls']} LS")
        if counts["null"]:
            parts.append(f"{counts['null']} 0.0")
        side_items.append({
            "id": node["id"],
            "name": node["name"],
            "meta": " · ".join(parts) or f"{node['system_count']} systems",
            "url": node["url"],
            "security_bands": node["security_bands"],
        })

    return {
        "scope": "universe",
        "title": "New Eden",
        "subtitle": f"{len(nodes)} regions · {len(edges)} inter-region connections",
        "breadcrumbs": [{"label": "New Eden", "url": "/map"}],
        "nodes": nodes,
        "edges": edges,
        "groups": [],
        "side_title": "Regions",
        "side_items": side_items,
    }


def _effective_wormhole_class_id(topology, system):
    class_id = system.get("wormhole_class_id")
    if class_id is not None:
        return int(class_id)

    constellation = topology["constellations"].get(
        system.get("constellation_id")
    ) or {}
    class_id = constellation.get("wormhole_class_id")
    if class_id is not None:
        return int(class_id)

    region = topology["regions"].get(system.get("region_id")) or {}
    class_id = region.get("wormhole_class_id")
    return int(class_id) if class_id is not None else None


def _is_shattered_wormhole(topology, system_id, class_id):
    if class_id == 13:
        return True

    if class_id not in {1, 2, 3, 4, 5, 6}:
        return False

    # Standard shattered systems have no moons. Prefer the current SDE moon
    # data when available; the canonical Rhea shattered ID range is only a
    # fallback for installations whose SDE predates mapMoons import.
    if topology.get("wormhole_moon_data_available"):
        return int(system_id) not in topology["wormhole_systems_with_moons"]

    return 31_002_505 <= int(system_id) <= 31_002_604


def _anoikis_layout(topology):
    buckets = {
        "c1": [],
        "c2": [],
        "c3": [],
        "c4": [],
        "c5": [],
        "c6": [],
        "shattered": [],
    }

    for system_id, system in topology["systems"].items():
        system_id = int(system_id)
        if not (31_000_000 <= system_id <= 31_999_999):
            continue

        class_id = _effective_wormhole_class_id(topology, system)
        shattered = _is_shattered_wormhole(
            topology,
            system_id,
            class_id,
        )

        if shattered:
            bucket = "shattered"
        elif class_id in {1, 2, 3, 4, 5, 6}:
            bucket = f"c{class_id}"
        else:
            # Thera / Drifter / other exceptional spaces are deliberately not
            # mixed into the C1-C6 constellation layout.
            continue

        buckets[bucket].append((system_id, system, class_id, shattered))

    block_specs = {
        "c1": ("C1", 0.0, 0.0, 540.0, 360.0),
        "c2": ("C2", 600.0, 0.0, 540.0, 360.0),
        "c3": ("C3", 1200.0, 0.0, 540.0, 360.0),
        "c4": ("C4", 0.0, 420.0, 540.0, 360.0),
        "c5": ("C5", 600.0, 420.0, 540.0, 360.0),
        "c6": ("C6", 1200.0, 420.0, 540.0, 360.0),
        "shattered": ("SHATTERED", 0.0, 840.0, 1740.0, 320.0),
    }

    nodes = []
    class_groups = []
    constellation_groups = []

    for bucket_key in ("c1", "c2", "c3", "c4", "c5", "c6", "shattered"):
        label, box_x, box_y, box_w, box_h = block_specs[bucket_key]
        members = buckets[bucket_key]
        if not members:
            continue

        by_constellation = {}
        for item in members:
            system_id, system, class_id, shattered = item
            constellation_id = system.get("constellation_id")
            by_constellation.setdefault(constellation_id, []).append(item)

        constellation_ids = sorted(
            by_constellation,
            key=lambda constellation_id: (
                (
                    topology["constellations"].get(constellation_id) or {}
                ).get("name") or "",
                int(constellation_id or 0),
            ),
        )

        usable_w = box_w - 36.0
        usable_h = box_h - 58.0
        constellation_count = max(1, len(constellation_ids))
        cols = max(
            1,
            int(math.ceil(math.sqrt(
                constellation_count * usable_w / max(usable_h, 1.0)
            ))),
        )
        rows = int(math.ceil(constellation_count / cols))
        cell_w = usable_w / cols
        cell_h = usable_h / max(rows, 1)

        for constellation_index, constellation_id in enumerate(constellation_ids):
            col = constellation_index % cols
            row = constellation_index // cols
            cell_x = box_x + 18.0 + col * cell_w
            cell_y = box_y + 40.0 + row * cell_h
            constellation = (
                topology["constellations"].get(constellation_id) or {}
            )
            systems = sorted(
                by_constellation[constellation_id],
                key=lambda item: (item[1].get("name") or "", item[0]),
            )

            constellation_groups.append({
                "id": constellation_id,
                "name": constellation.get("name") or (
                    f"Constellation {constellation_id}"
                ),
                "bucket": bucket_key,
                "class_label": label,
                "x": cell_x,
                "y": cell_y,
                "width": cell_w,
                "height": cell_h,
                "system_count": len(systems),
            })

            system_count = len(systems)
            sys_cols = max(1, int(math.ceil(math.sqrt(system_count))))
            sys_rows = int(math.ceil(system_count / sys_cols))
            pad_x = min(12.0, cell_w * 0.12)
            pad_y = min(13.0, cell_h * 0.18)
            inner_w = max(4.0, cell_w - pad_x * 2.0)
            inner_h = max(4.0, cell_h - pad_y * 2.0)

            for system_index, (system_id, system, class_id, shattered) in enumerate(systems):
                sys_col = system_index % sys_cols
                sys_row = system_index // sys_cols
                layout_x = cell_x + pad_x + (
                    (sys_col + 0.5) * inner_w / sys_cols
                )
                layout_y = cell_y + pad_y + (
                    (sys_row + 0.5) * inner_h / max(sys_rows, 1)
                )
                region = topology["regions"].get(system.get("region_id")) or {}
                nodes.append({
                    "id": system_id,
                    "name": system["name"],
                    "layout_x": layout_x,
                    "layout_y": layout_y,
                    "security": system.get("security"),
                    "region_id": system.get("region_id"),
                    "region_name": region.get("name"),
                    "constellation_id": constellation_id,
                    "constellation_name": constellation.get("name"),
                    "wormhole_class_id": class_id,
                    "wormhole_group": label,
                    "shattered": bool(shattered),
                    "space": "anoikis",
                    "url": f"/map/system/{system_id}",
                })

        class_groups.append({
            "key": bucket_key,
            "label": label,
            "x": box_x,
            "y": box_y,
            "width": box_w,
            "height": box_h,
            "system_count": len(members),
            "constellation_count": len(constellation_ids),
        })

    return {
        "nodes": nodes,
        "classes": class_groups,
        "constellations": constellation_groups,
        "width": 1740.0,
        "height": 1160.0,
    }


def get_eve_2d_map():
    topology = _topology()

    # CCP's in-game New Eden map uses this solar-system ID range.
    visible_system_ids = {
        system_id
        for system_id in topology["systems"]
        if 30_000_000 <= int(system_id) <= 30_999_999
    }

    nodes = []
    for system_id in sorted(visible_system_ids):
        system = topology["systems"][system_id]
        region = topology["regions"].get(system.get("region_id")) or {}
        constellation = topology["constellations"].get(system.get("constellation_id")) or {}
        pos = system.get("position_2d") or {}
        nodes.append({
            "id": system_id,
            "name": system["name"],
            "x": float(pos.get("x") or 0.0),
            "y": float(pos.get("y") or 0.0),
            "security": system.get("security"),
            "region_id": system.get("region_id"),
            "region_name": region.get("name"),
            "constellation_id": system.get("constellation_id"),
            "constellation_name": constellation.get("name"),
            "space": "new_eden",
            "url": f"/map/system/{system_id}",
        })

    edges = [
        {"source": source_id, "target": target_id}
        for source_id, target_id in sorted(topology["system_edges"])
        if source_id in visible_system_ids and target_id in visible_system_ids
    ]

    anoikis = _anoikis_layout(topology)

    return {
        "scope": "eve_2d",
        "title": "New Eden · 2D EVE Map",
        "subtitle": (
            f"{len(nodes)} New Eden systems · "
            f"{len(edges)} stargate connections · "
            f"{len(anoikis['nodes'])} Anoikis systems"
        ),
        "nodes": nodes,
        "edges": edges,
        "anoikis_nodes": anoikis["nodes"],
        "anoikis_classes": anoikis["classes"],
        "anoikis_constellations": anoikis["constellations"],
        "anoikis_layout": {
            "width": anoikis["width"],
            "height": anoikis["height"],
        },
        "breadcrumbs": [
            {"label": "New Eden", "url": "/map"},
            {"label": "2D EVE Map", "url": "/map/eve-2d"},
        ],
    }


YELLOW_INFLUENCE_COLOR = ("hsl(52 92% 56%)", 52.0, 92.0, 56.0)


def _ensure_influence_color_tables(cur):
    cur.execute("""
        CREATE SCHEMA IF NOT EXISTS sovereignty
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sovereignty.influence_color_assignments (
            assignment_id BIGSERIAL PRIMARY KEY,
            entity_type TEXT NOT NULL
                CHECK (entity_type IN ('alliance', 'coalition')),
            entity_id BIGINT NOT NULL,
            color TEXT NOT NULL,
            color_hue DOUBLE PRECISION NOT NULL,
            color_saturation DOUBLE PRECISION NOT NULL,
            color_lightness DOUBLE PRECISION NOT NULL,
            valid_from DATE NOT NULL,
            valid_to DATE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (valid_to IS NULL OR valid_to >= valid_from)
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS sov_influence_color_active_entity_idx
        ON sovereignty.influence_color_assignments (entity_type, entity_id)
        WHERE valid_to IS NULL
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS sov_influence_color_history_idx
        ON sovereignty.influence_color_assignments (
            entity_type, entity_id, valid_from, valid_to
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sovereignty.influence_color_state (
            entity_type TEXT PRIMARY KEY
                CHECK (entity_type IN ('alliance', 'coalition')),
            initialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def _influence_color_candidates():
    # Large fixed palette. Allocation is persisted, so IDs keep their colour
    # until they lose all SOV. Freed colours can then be reused.
    candidates = []
    for saturation, lightness, offset in (
        (76.0, 56.0, 0.0),
        (82.0, 68.0, 5.0),
        (68.0, 45.0, 10.0),
    ):
        for hue in range(0, 360, 15):
            value = float((hue + offset) % 360)
            candidates.append((
                f"hsl({value:.0f} {saturation:.0f}% {lightness:.0f}%)",
                value,
                saturation,
                lightness,
            ))
    return candidates


INFLUENCE_COLOR_CANDIDATES = _influence_color_candidates()


def _color_rgb(hue, saturation, lightness):
    red, green, blue = colorsys.hls_to_rgb(
        (float(hue) % 360.0) / 360.0,
        float(lightness) / 100.0,
        float(saturation) / 100.0,
    )
    return red, green, blue


def _pick_influence_color(used_colors, entity_id):
    used_rgb = [
        _color_rgb(hue, saturation, lightness)
        for hue, saturation, lightness in used_colors
    ]
    start = abs(int(entity_id)) % len(INFLUENCE_COLOR_CANDIDATES)
    ordered = (
        INFLUENCE_COLOR_CANDIDATES[start:]
        + INFLUENCE_COLOR_CANDIDATES[:start]
    )

    best = None
    best_score = -1.0
    for candidate in ordered:
        _color, hue, saturation, lightness = candidate
        rgb = _color_rgb(hue, saturation, lightness)

        if not used_rgb:
            return candidate

        score = min(
            (rgb[0] - other[0]) ** 2
            + (rgb[1] - other[1]) ** 2
            + (rgb[2] - other[2]) ** 2
            for other in used_rgb
        )
        if score > best_score:
            best_score = score
            best = candidate

    return best or ordered[0]


def _load_influence_assignments(cur, entity_type, entity_ids, selected_date):
    if not entity_ids:
        return {}

    cur.execute("""
        SELECT
            entity_id,
            color,
            color_hue,
            color_saturation,
            color_lightness
        FROM sovereignty.influence_color_assignments
        WHERE entity_type = %s
          AND entity_id = ANY(%s)
          AND valid_from <= %s
          AND (valid_to IS NULL OR %s < valid_to)
        ORDER BY assignment_id
    """, (entity_type, entity_ids, selected_date, selected_date))

    return {
        int(entity_id): {
            "color": color,
            "components": (
                float(hue),
                float(saturation),
                float(lightness),
            ),
        }
        for entity_id, color, hue, saturation, lightness in cur.fetchall()
    }


def _apply_persistent_influence_colors(
    groups,
    entity_type,
    selected_date,
    latest_date,
):
    entity_groups = {
        int(group["entity_id"]): group
        for group in groups
        if group.get("entity_id") is not None
    }
    if not entity_groups:
        if selected_date == latest_date:
            with db() as conn:
                with conn.cursor() as cur:
                    _ensure_influence_color_tables(cur)
                    cur.execute("SELECT pg_advisory_xact_lock(184624, 2)")
                    cur.execute("""
                        UPDATE sovereignty.influence_color_assignments
                        SET valid_to = %s
                        WHERE entity_type = %s
                          AND valid_to IS NULL
                    """, (selected_date, entity_type))
                conn.commit()
        return

    entity_ids = sorted(entity_groups)
    with db() as conn:
        with conn.cursor() as cur:
            _ensure_influence_color_tables(cur)
            cur.execute("SELECT pg_advisory_xact_lock(184624, 2)")

            if selected_date == latest_date:
                # An entity owns its colour only while it owns at least one
                # sovereignty system in this grouping mode.
                cur.execute("""
                    UPDATE sovereignty.influence_color_assignments
                    SET valid_to = %s
                    WHERE entity_type = %s
                      AND valid_to IS NULL
                      AND NOT (entity_id = ANY(%s))
                """, (selected_date, entity_type, entity_ids))

                assignments = _load_influence_assignments(
                    cur,
                    entity_type,
                    entity_ids,
                    selected_date,
                )

                # One-time initial seeds. Once an entity later reaches zero SOV,
                # this seed is never applied again: it receives a normal new
                # colour if it comes back.
                cur.execute("""
                    SELECT 1
                    FROM sovereignty.influence_color_state
                    WHERE entity_type = %s
                """, (entity_type,))
                initialized = cur.fetchone() is not None

                if not initialized:
                    seed_id = None
                    if entity_type == "alliance":
                        if 1354830081 in entity_groups:  # Goonswarm Federation
                            seed_id = 1354830081
                    else:
                        for entity_id, group in entity_groups.items():
                            if "imperium" in str(group.get("name") or "").casefold():
                                seed_id = entity_id
                                break

                    if seed_id is not None and seed_id not in assignments:
                        color, hue, saturation, lightness = YELLOW_INFLUENCE_COLOR
                        cur.execute("""
                            INSERT INTO sovereignty.influence_color_assignments (
                                entity_type,
                                entity_id,
                                color,
                                color_hue,
                                color_saturation,
                                color_lightness,
                                valid_from
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (
                            entity_type,
                            seed_id,
                            color,
                            hue,
                            saturation,
                            lightness,
                            selected_date,
                        ))

                    cur.execute("""
                        INSERT INTO sovereignty.influence_color_state (
                            entity_type,
                            initialized_at
                        )
                        VALUES (%s, NOW())
                        ON CONFLICT (entity_type) DO NOTHING
                    """, (entity_type,))

                    assignments = _load_influence_assignments(
                        cur,
                        entity_type,
                        entity_ids,
                        selected_date,
                    )

                used_colors = [
                    row["components"]
                    for row in assignments.values()
                ]

                missing_ids = [
                    entity_id
                    for entity_id in entity_ids
                    if entity_id not in assignments
                ]
                for entity_id in missing_ids:
                    color, hue, saturation, lightness = _pick_influence_color(
                        used_colors,
                        entity_id,
                    )
                    cur.execute("""
                        INSERT INTO sovereignty.influence_color_assignments (
                            entity_type,
                            entity_id,
                            color,
                            color_hue,
                            color_saturation,
                            color_lightness,
                            valid_from
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        entity_type,
                        entity_id,
                        color,
                        hue,
                        saturation,
                        lightness,
                        selected_date,
                    ))
                    assignments[entity_id] = {
                        "color": color,
                        "components": (hue, saturation, lightness),
                    }
                    used_colors.append((hue, saturation, lightness))
            else:
                assignments = _load_influence_assignments(
                    cur,
                    entity_type,
                    entity_ids,
                    selected_date,
                )

                # Dates older than the colour table itself have no factual
                # assignment to recover. Keep those historical views usable
                # without inventing persisted history.
                used_colors = [
                    row["components"]
                    for row in assignments.values()
                ]
                for entity_id in entity_ids:
                    if entity_id in assignments:
                        continue
                    color, hue, saturation, lightness = _pick_influence_color(
                        used_colors,
                        entity_id,
                    )
                    assignments[entity_id] = {
                        "color": color,
                        "components": (hue, saturation, lightness),
                    }
                    used_colors.append((hue, saturation, lightness))

        conn.commit()

    for entity_id, group in entity_groups.items():
        row = assignments.get(entity_id)
        if row:
            group["color"] = row["color"]


def get_eve_2d_influence(target_date=None, grouping="coalition"):
    topology = _topology()
    grouping = str(grouping or "coalition").strip().lower()
    if grouping not in {"alliance", "coalition"}:
        raise MapDataError("influence_grouping_invalid")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    to_regclass('sovereignty.current_map'),
                    to_regclass('sovereignty.map_changes')
            """)
            current_table, changes_table = cur.fetchone()
            if current_table is None or changes_table is None:
                raise MapDataError("sovereignty_history_unavailable")

            cur.execute("SELECT MAX((observed_at AT TIME ZONE 'UTC')::date) FROM sovereignty.current_map")
            latest_date = cur.fetchone()[0]
            if latest_date is None:
                raise MapDataError("sovereignty_history_unavailable")

            cur.execute("SELECT MIN((source_observed_at AT TIME ZONE 'UTC')::date) FROM sovereignty.map_changes")
            earliest_change_date = cur.fetchone()[0]

            if target_date is None:
                selected_date = latest_date
            elif isinstance(target_date, date):
                selected_date = target_date
            else:
                try:
                    selected_date = date.fromisoformat(str(target_date))
                except ValueError as exc:
                    raise MapDataError("influence_date_invalid") from exc

            if selected_date > latest_date:
                selected_date = latest_date
            if earliest_change_date is not None and selected_date < earliest_change_date:
                raise MapDataError("influence_date_before_history")

            cur.execute("""
                SELECT system_id, alliance_id, corporation_id, faction_id
                FROM sovereignty.current_map
            """)
            owners = {
                int(system_id): (
                    int(alliance_id) if alliance_id is not None else None,
                    int(corporation_id) if corporation_id is not None else None,
                    int(faction_id) if faction_id is not None else None,
                )
                for system_id, alliance_id, corporation_id, faction_id in cur.fetchall()
            }

            cur.execute("""
                SELECT
                    system_id,
                    change_type,
                    old_alliance_id,
                    old_corporation_id,
                    old_faction_id,
                    new_alliance_id,
                    new_corporation_id,
                    new_faction_id
                FROM sovereignty.map_changes
                WHERE (source_observed_at AT TIME ZONE 'UTC')::date > %s
                ORDER BY source_observed_at DESC, change_id DESC
            """, (selected_date,))

            for (
                system_id,
                change_type,
                old_alliance_id,
                old_corporation_id,
                old_faction_id,
                _new_alliance_id,
                _new_corporation_id,
                _new_faction_id,
            ) in cur.fetchall():
                system_id = int(system_id)
                if change_type == "GAIN":
                    owners[system_id] = (None, None, None)
                elif change_type == "LOST":
                    owners[system_id] = (
                        int(old_alliance_id) if old_alliance_id is not None else None,
                        int(old_corporation_id) if old_corporation_id is not None else None,
                        int(old_faction_id) if old_faction_id is not None else None,
                    )

            alliance_ids = sorted({
                owner[0]
                for owner in owners.values()
                if owner and owner[0] is not None
            })

            coalition_rows = []
            membership_rows = []
            if alliance_ids:
                cur.execute("""
                    SELECT coalition_id, name, short_name
                    FROM entities.coalitions
                    ORDER BY coalition_id
                """)
                coalition_rows = cur.fetchall()

                cur.execute("""
                    SELECT coalition_id, operation, member_type, member_id, valid_from, valid_to
                    FROM entities.coalition_memberships
                    ORDER BY id
                """)
                membership_rows = cur.fetchall()

                cur.execute("""
                    SELECT alliance_id, name
                    FROM entities.alliances
                    WHERE alliance_id = ANY(%s)
                """, (alliance_ids,))
                alliance_names = {
                    int(alliance_id): (name or f"Alliance {alliance_id}")
                    for alliance_id, name in cur.fetchall()
                }
            else:
                alliance_names = {}

    groups = {}

    if grouping == "alliance":
        for system_id, owner in owners.items():
            if int(system_id) not in topology["systems"]:
                continue
            alliance_id = owner[0] if owner else None
            if alliance_id is None:
                continue

            group_id = f"alliance:{alliance_id}"
            if group_id not in groups:
                groups[group_id] = {
                    "id": group_id,
                    "entity_type": "alliance",
                    "entity_id": int(alliance_id),
                    "name": alliance_names.get(int(alliance_id), f"Alliance {alliance_id}"),
                    "color": None,
                    "system_ids": [],
                }
            groups[group_id]["system_ids"].append(int(system_id))
    else:
        coalitions = {
            int(coalition_id): {
                "id": int(coalition_id),
                "name": name or short_name or f"Coalition {coalition_id}",
                "short_name": short_name,
            }
            for coalition_id, name, short_name in coalition_rows
        }

        rules_by_coalition = {coalition_id: [] for coalition_id in coalitions}
        active_child_coalitions = set()
        for coalition_id, operation, member_type, member_id, valid_from, valid_to in membership_rows:
            if valid_from is not None and valid_from > selected_date:
                continue
            if valid_to is not None and valid_to < selected_date:
                continue
            coalition_id = int(coalition_id)
            member_id = int(member_id)
            rule = {
                "operation": operation,
                "member_type": member_type,
                "member_id": member_id,
            }
            rules_by_coalition.setdefault(coalition_id, []).append(rule)
            if operation == "include" and member_type == "coalition":
                active_child_coalitions.add(member_id)

        resolved_cache = {}

        def resolve_coalition(coalition_id, stack=frozenset()):
            coalition_id = int(coalition_id)
            if coalition_id in resolved_cache:
                return resolved_cache[coalition_id]
            if coalition_id in stack:
                return set()

            includes = set()
            excludes = set()
            next_stack = stack | {coalition_id}
            for rule in rules_by_coalition.get(coalition_id, []):
                member_type = rule["member_type"]
                member_id = rule["member_id"]
                if member_type == "alliance":
                    target = {member_id}
                elif member_type == "coalition":
                    target = resolve_coalition(member_id, next_stack)
                else:
                    continue

                if rule["operation"] == "exclude":
                    excludes.update(target)
                else:
                    includes.update(target)

            result = includes - excludes
            resolved_cache[coalition_id] = result
            return result

        alliance_to_group = {}
        top_level_ids = [
            coalition_id
            for coalition_id in coalitions
            if coalition_id not in active_child_coalitions
        ]
        top_level_ids.sort(
            key=lambda coalition_id: (
                -len(resolve_coalition(coalition_id)),
                coalitions[coalition_id]["name"].casefold(),
                coalition_id,
            )
        )

        for coalition_id in top_level_ids:
            members = resolve_coalition(coalition_id)
            if not members:
                continue
            group_id = f"coalition:{coalition_id}"
            group = {
                "id": group_id,
                "entity_type": "coalition",
                "entity_id": coalition_id,
                "name": coalitions[coalition_id]["name"],
                "color": None,
                "system_ids": [],
            }
            groups[group_id] = group
            for alliance_id in members:
                alliance_to_group.setdefault(int(alliance_id), group_id)

        unaligned_group_id = "coalition:none"
        groups[unaligned_group_id] = {
            "id": unaligned_group_id,
            "entity_type": "coalition",
            "entity_id": None,
            "name": "Sans coalition",
            "color": "hsl(0 0% 100%)",
            "system_ids": [],
        }

        for system_id, owner in owners.items():
            if int(system_id) not in topology["systems"]:
                continue
            alliance_id = owner[0] if owner else None
            if alliance_id is None:
                continue

            group_id = alliance_to_group.get(int(alliance_id), unaligned_group_id)
            groups[group_id]["system_ids"].append(int(system_id))

    result_groups = [
        group
        for group in groups.values()
        if group["system_ids"]
    ]

    _apply_persistent_influence_colors(
        result_groups,
        grouping,
        selected_date,
        latest_date,
    )

    result_groups.sort(key=lambda item: (-len(item["system_ids"]), item["name"].casefold()))

    return {
        "date": selected_date.isoformat(),
        "grouping": grouping,
        "min_date": earliest_change_date.isoformat() if earliest_change_date else selected_date.isoformat(),
        "max_date": latest_date.isoformat(),
        "groups": result_groups,
    }


def get_region_map(region_id):
    topology = _topology()
    region_id = int(region_id)
    region = topology["regions"].get(region_id)
    if not region:
        raise MapDataError("region_not_found")

    system_ids = sorted(topology["region_system_ids"].get(region_id, []))
    system_set = set(system_ids)
    nodes = [_system_node(topology, system_id) for system_id in system_ids]

    dotlan_layout = REGION_LAYOUTS.get(region["name"]) or {}
    dotlan_nodes = dotlan_layout.get("nodes") or {}
    dotlan_matched_nodes = 0
    for node in nodes:
        anchor = dotlan_nodes.get(node["name"])
        if not anchor:
            continue
        node["dotlan_position"] = {"x": float(anchor["x"]), "y": float(anchor["y"])}
        dotlan_matched_nodes += 1

    edges = [
        {"source": a, "target": b}
        for a, b in sorted(topology["system_edges"])
        if a in system_set and b in system_set
    ]

    # External gates are rendered as DOTLAN-like exits on the edge of the region
    # map.  Keep one entry per real inter-region stargate connection so the
    # source system stays visually connected to the neighbouring region.
    exits = []
    neighbour_region_ids = set()
    for a, b in sorted(topology["system_edges"]):
        if (a in system_set) == (b in system_set):
            continue
        source_id, target_id = (a, b) if a in system_set else (b, a)
        target_system = topology["systems"].get(target_id)
        if not target_system:
            continue
        target_region_id = target_system.get("region_id")
        if target_region_id is None or target_region_id == region_id:
            continue
        target_region = topology["regions"].get(target_region_id) or {}
        neighbour_region_ids.add(target_region_id)
        target_system_name = target_system.get("name") or f"System {target_id}"
        exit_payload = {
            "id": f"exit-{source_id}-{target_id}",
            "source_system_id": source_id,
            "target_system_id": target_id,
            "target_system_name": target_system_name,
            "target_region_id": target_region_id,
            "target_region_name": target_region.get("name") or f"Region {target_region_id}",
            "target_region_position": target_region.get("position") or {"x": 0.0, "y": 0.0, "z": 0.0},
            "url": f"/map/region/{target_region_id}",
        }
        dotlan_exit = (dotlan_layout.get("exits") or {}).get(target_system_name)
        if dotlan_exit:
            exit_payload["dotlan_position"] = {
                "x": float(dotlan_exit["x"]),
                "y": float(dotlan_exit["y"]),
            }
        exits.append(exit_payload)

    constellation_ids = sorted({
        topology["systems"][system_id].get("constellation_id")
        for system_id in system_ids
        if topology["systems"][system_id].get("constellation_id") is not None
    })
    groups = []
    side_items = []
    for constellation_id in constellation_ids:
        constellation = topology["constellations"].get(constellation_id)
        if not constellation:
            continue
        count = sum(1 for system_id in system_ids if topology["systems"][system_id].get("constellation_id") == constellation_id)
        groups.append({
            "id": constellation_id,
            "name": constellation["name"],
            "position": constellation["position"],
            "url": f"/map/constellation/{constellation_id}",
        })
        side_items.append({
            "id": constellation_id,
            "name": constellation["name"],
            "meta": f"{count} systems",
            "url": f"/map/constellation/{constellation_id}",
        })

    return {
        "scope": "region",
        "title": region["name"],
        "subtitle": (
            f"{len(system_ids)} systems · {len(constellation_ids)} constellations · "
            f"{len(edges)} internal connections · {len(exits)} external gates to {len(neighbour_region_ids)} regions"
        ),
        "breadcrumbs": [
            {"label": "New Eden", "url": "/map"},
            {"label": region["name"], "url": f"/map/region/{region_id}"},
        ],
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "exits": exits,
        "region_position": region.get("position") or {"x": 0.0, "y": 0.0, "z": 0.0},
        "dotlan_layout": {
            "available": bool(dotlan_nodes),
            "complete": bool(dotlan_nodes) and dotlan_matched_nodes == len(nodes),
            "matched_nodes": dotlan_matched_nodes,
            "total_nodes": len(nodes),
            "page": dotlan_layout.get("page"),
            "page_width": DOTLAN_PAGE_WIDTH,
            "page_height": DOTLAN_PAGE_HEIGHT,
            "bounds": list(DOTLAN_MAP_BOUNDS),
            "node_width": dotlan_layout.get("node_width"),
            "node_height": dotlan_layout.get("node_height"),
        },
        "side_title": "Constellations",
        "side_items": sorted(side_items, key=lambda item: item["name"].lower()),
        "region_id": region_id,
    }


def get_constellation_map(constellation_id):
    topology = _topology()
    constellation_id = int(constellation_id)
    constellation = topology["constellations"].get(constellation_id)
    if not constellation:
        raise MapDataError("constellation_not_found")

    region = topology["regions"].get(constellation.get("region_id"))
    system_ids = sorted(topology["constellation_system_ids"].get(constellation_id, []))
    system_set = set(system_ids)
    nodes = [_system_node(topology, system_id) for system_id in system_ids]
    edges = [
        {"source": a, "target": b}
        for a, b in sorted(topology["system_edges"])
        if a in system_set and b in system_set
    ]

    side_items = [
        {
            "id": node["id"],
            "name": node["name"],
            "meta": (f"sec {float(node['security']):.2f}" if node.get("security") is not None else "system"),
            "url": node["url"],
        }
        for node in sorted(nodes, key=lambda item: item["name"].lower())
    ]

    breadcrumbs = [{"label": "New Eden", "url": "/map"}]
    if region:
        breadcrumbs.append({"label": region["name"], "url": f"/map/region/{region['id']}"})
    breadcrumbs.append({"label": constellation["name"], "url": f"/map/constellation/{constellation_id}"})

    return {
        "scope": "constellation",
        "title": constellation["name"],
        "subtitle": f"{len(system_ids)} systems · {len(edges)} internal connections",
        "breadcrumbs": breadcrumbs,
        "nodes": nodes,
        "edges": edges,
        "groups": [],
        "side_title": "Systems",
        "side_items": side_items,
        "constellation_id": constellation_id,
        "region_id": constellation.get("region_id"),
    }



def _preview_fit(points, padding=8.0):
    if not points:
        return {}
    xs = [float(point[1]) for point in points]
    ys = [float(point[2]) for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    usable = 100.0 - padding * 2.0
    scale = min(usable / span_x, usable / span_y)
    draw_w = span_x * scale
    draw_h = span_y * scale
    off_x = (100.0 - draw_w) / 2.0
    off_y = (100.0 - draw_h) / 2.0
    return {
        point_id: {
            "x": off_x + (float(x) - min_x) * scale,
            "y": off_y + (float(y) - min_y) * scale,
        }
        for point_id, x, y in points
    }


def get_location_preview(entity_type, entity_id):
    """Small, fixed, non-interactive map payload used in location profile cards."""
    entity_type = str(entity_type or "").strip().lower()
    entity_id = int(entity_id)
    topology = _topology()

    if entity_type == "region":
        region = topology["regions"].get(entity_id)
        if not region:
            raise MapDataError("region_not_found")
        system_ids = topology["region_system_ids"].get(entity_id, [])
        system_set = set(system_ids)
        dotlan_layout = REGION_LAYOUTS.get(region["name"]) or {}
        dotlan_nodes = dotlan_layout.get("nodes") or {}
        raw_points = []
        for system_id in system_ids:
            system = topology["systems"].get(system_id)
            if not system:
                continue
            anchor = dotlan_nodes.get(system["name"])
            if anchor:
                raw_points.append((system_id, float(anchor["x"]), float(anchor["y"])))
            else:
                pos = system.get("position") or {}
                raw_points.append((system_id, float(pos.get("x") or 0.0), -float(pos.get("z") or 0.0)))
        fitted = _preview_fit(raw_points, padding=5.0)
        edges = [
            {
                "x1": fitted[a]["x"], "y1": fitted[a]["y"],
                "x2": fitted[b]["x"], "y2": fitted[b]["y"],
            }
            for a, b in topology["system_edges"]
            if a in system_set and b in system_set and a in fitted and b in fitted
        ]
        return {
            "kind": "region",
            "nodes": [{"id": sid, **coords} for sid, coords in fitted.items()],
            "edges": edges,
        }

    if entity_type == "constellation":
        constellation = topology["constellations"].get(entity_id)
        if not constellation:
            raise MapDataError("constellation_not_found")
        system_ids = topology["constellation_system_ids"].get(entity_id, [])
        system_set = set(system_ids)
        region = topology["regions"].get(constellation.get("region_id")) or {}
        dotlan_nodes = (REGION_LAYOUTS.get(region.get("name")) or {}).get("nodes") or {}
        raw_points = []
        for system_id in system_ids:
            system = topology["systems"].get(system_id)
            if not system:
                continue
            anchor = dotlan_nodes.get(system["name"])
            if anchor:
                raw_points.append((system_id, float(anchor["x"]), float(anchor["y"])))
            else:
                pos = system.get("position") or {}
                raw_points.append((system_id, float(pos.get("x") or 0.0), -float(pos.get("z") or 0.0)))
        fitted = _preview_fit(raw_points, padding=10.0)
        edges = [
            {
                "x1": fitted[a]["x"], "y1": fitted[a]["y"],
                "x2": fitted[b]["x"], "y2": fitted[b]["y"],
            }
            for a, b in topology["system_edges"]
            if a in system_set and b in system_set and a in fitted and b in fitted
        ]
        return {
            "kind": "constellation",
            "nodes": [{"id": sid, **coords} for sid, coords in fitted.items()],
            "edges": edges,
        }

    if entity_type == "system":
        system = topology["systems"].get(entity_id)
        if not system:
            raise MapDataError("system_not_found")
        payload = get_system_map(entity_id)
        planets = [item for item in payload.get("objects", []) if item.get("kind") == "planet"]
        gates = [item for item in payload.get("objects", []) if item.get("kind") == "gate"]
        raw_points = []
        for item in planets + gates:
            pos = item.get("position") or {}
            raw_points.append((item["id"], float(pos.get("x") or 0.0), -float(pos.get("z") or 0.0)))
        fitted = _preview_fit(raw_points, padding=12.0) if raw_points else {}
        nodes = [{"id": "star", "x": 50.0, "y": 50.0, "kind": "star"}]
        for item in planets:
            coords = fitted.get(item["id"])
            if coords:
                nodes.append({"id": item["id"], **coords, "kind": "planet"})
        for item in gates:
            coords = fitted.get(item["id"])
            if coords:
                nodes.append({"id": item["id"], **coords, "kind": "gate"})
        return {
            "kind": "system",
            "nodes": nodes,
            "edges": [],
        }

    raise MapDataError("preview_type_invalid")


def _raw_position(data):
    pos = (data or {}).get("position") or {}
    return {
        "x": float(pos.get("x") or 0.0),
        "y": float(pos.get("y") or 0.0),
        "z": float(pos.get("z") or 0.0),
    }


def _load_sde_rows_by_ids(conn, table_name, ids):
    ids = [int(v) for v in ids if v is not None]
    if not ids:
        return []
    keys = [str(v) for v in ids]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT sde_key, data FROM public.{table_name} WHERE sde_key = ANY(%s)",
            (keys,),
        )
        return cur.fetchall()


def _load_sde_rows_for_system(conn, table_name, system_id):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT sde_key, data FROM public.{table_name} WHERE data->>'solarSystemID' = %s",
            (str(int(system_id)),),
        )
        return cur.fetchall()


def _celestial_label(system_name, kind, data, object_id):
    if kind == "star":
        return f"{system_name} Star"
    celestial_index = _int((data or {}).get("celestialIndex"))
    orbit_index = _int((data or {}).get("orbitIndex"))
    if kind == "planet":
        return f"{system_name} {celestial_index or '?'}"
    if kind == "moon":
        return f"Moon {orbit_index or '?'}"
    if kind == "belt":
        return f"Asteroid Belt {orbit_index or '?'}"
    if kind == "station":
        return f"NPC Station {object_id}"
    return f"{kind.title()} {object_id}"


def _lookup_sde_names(conn, candidate_tables, ids):
    ids = sorted({int(v) for v in ids if v is not None})
    if not ids:
        return {}
    lowered = [str(name).lower() for name in candidate_tables]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.table_name
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.column_name = 'data'
              AND lower(c.table_name) = ANY(%s)
            ORDER BY array_position(%s::text[], lower(c.table_name))
            LIMIT 1
            """,
            (lowered, lowered),
        )
        row = cur.fetchone()
    if not row:
        return {}
    table = '"' + str(row[0]).replace('"', '""') + '"'
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COALESCE(NULLIF(data->>'_key',''), NULLIF(sde_key,''))::BIGINT AS object_id,
                COALESCE(
                    data->'name'->>'en',
                    data->>'typeName',
                    data->>'corporationName',
                    data->>'operationName',
                    CASE WHEN jsonb_typeof(data->'name') = 'string' THEN data->>'name' END
                ) AS object_name
            FROM public.{table}
            WHERE COALESCE(NULLIF(data->>'_key',''), NULLIF(sde_key,''))::BIGINT = ANY(%s)
            """,
            (ids,),
        )
        return {int(object_id): object_name for object_id, object_name in cur.fetchall() if object_name}


def _physical_details(data):
    data = data or {}
    stats = data.get("statistics") or {}
    return {
        "radius_m": data.get("radius"),
        "density": stats.get("density"),
        "mass_gas": stats.get("massGas"),
        "mass_dust": stats.get("massDust"),
        "pressure": stats.get("pressure"),
        "temperature_k": stats.get("temperature"),
        "orbit_period_s": stats.get("orbitPeriod"),
        "orbit_radius_m": stats.get("orbitRadius"),
        "eccentricity": stats.get("eccentricity"),
        "rotation_rate_s": stats.get("rotationRate"),
        "spectral_class": stats.get("spectralClass"),
        "escape_velocity_m_s": stats.get("escapeVelocity"),
        "surface_gravity_m_s2": stats.get("surfaceGravity"),
        "locked": stats.get("locked"),
        "age_s": stats.get("age"),
        "life_s": stats.get("life"),
        "luminosity": stats.get("luminosity"),
    }


def get_system_map(system_id):
    topology = _topology()
    system_id = int(system_id)
    system = topology["systems"].get(system_id)
    if not system:
        raise MapDataError("system_not_found")

    constellation = topology["constellations"].get(system.get("constellation_id"))
    region = topology["regions"].get(system.get("region_id"))

    # Use the IDs embedded in the system/planet SDE rows whenever available.  It
    # avoids scanning every celestial table on each click.  Fallbacks keep this
    # compatible with SDE builds that omit one of the convenience arrays.
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM public.sde_mapsolarsystems WHERE sde_key = %s LIMIT 1",
                (str(system_id),),
            )
            row = cur.fetchone()
        system_data = (row[0] if row else {}) or {}

        star_id = _int(system_data.get("starID"))
        planet_ids = [_int(v) for v in (system_data.get("planetIDs") or []) if _int(v) is not None]
        stargate_ids = [_int(v) for v in (system_data.get("stargateIDs") or []) if _int(v) is not None]

        star_rows = _load_sde_rows_by_ids(conn, "sde_mapstars", [star_id]) if star_id else []
        if not star_rows:
            star_rows = _load_sde_rows_for_system(conn, "sde_mapstars", system_id)

        planet_rows = _load_sde_rows_by_ids(conn, "sde_mapplanets", planet_ids) if planet_ids else []
        if not planet_rows:
            planet_rows = _load_sde_rows_for_system(conn, "sde_mapplanets", system_id)

        moon_ids = []
        belt_ids = []
        for _, pdata in planet_rows:
            pdata = pdata or {}
            moon_ids.extend(_int(v) for v in (pdata.get("moonIDs") or []) if _int(v) is not None)
            belt_ids.extend(_int(v) for v in (pdata.get("asteroidBeltIDs") or []) if _int(v) is not None)

        moon_rows = _load_sde_rows_by_ids(conn, "sde_mapmoons", moon_ids) if moon_ids else []
        if not moon_rows:
            moon_rows = _load_sde_rows_for_system(conn, "sde_mapmoons", system_id)

        belt_rows = _load_sde_rows_by_ids(conn, "sde_mapasteroidbelts", belt_ids) if belt_ids else []
        if not belt_rows:
            belt_rows = _load_sde_rows_for_system(conn, "sde_mapasteroidbelts", system_id)

        gate_rows = _load_sde_rows_by_ids(conn, "sde_mapstargates", stargate_ids) if stargate_ids else []
        if not gate_rows:
            gate_rows = _load_sde_rows_for_system(conn, "sde_mapstargates", system_id)

        station_rows = _load_sde_rows_for_system(conn, "sde_npcstations", system_id)

        all_rows = star_rows + planet_rows + moon_rows + belt_rows + gate_rows + station_rows
        type_ids = [_int((data or {}).get("typeID")) for _, data in all_rows]
        type_names = _lookup_sde_names(conn, ["sde_types", "sde_invtypes", "sde_invTypes"], type_ids)
        owner_ids = [_int((data or {}).get("ownerID")) for _, data in station_rows]
        owner_names = _lookup_sde_names(
            conn,
            ["sde_npccorporations", "sde_npcCorporations", "sde_corpprofiles"],
            owner_ids,
        )
        operation_ids = [_int((data or {}).get("operationID")) for _, data in station_rows]
        operation_names = _lookup_sde_names(
            conn,
            ["sde_stationoperations", "sde_stationOperations"],
            operation_ids,
        )

    objects = []

    for sde_key, data in star_rows:
        data = data or {}
        object_id = _int(data.get("_key")) or _int(sde_key)
        type_id = _int(data.get("typeID"))
        objects.append({
            "id": object_id,
            "kind": "star",
            "name": _celestial_label(system["name"], "star", data, object_id),
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
            "orbit_id": None,
            "type_id": type_id,
            "type_name": type_names.get(type_id),
            "details": _physical_details(data),
        })

    for sde_key, data in planet_rows:
        data = data or {}
        object_id = _int(data.get("_key")) or _int(sde_key)
        type_id = _int(data.get("typeID"))
        objects.append({
            "id": object_id,
            "kind": "planet",
            "name": _celestial_label(system["name"], "planet", data, object_id),
            "position": _raw_position(data),
            "orbit_id": _int(data.get("orbitID")),
            "orbit_index": _int(data.get("orbitIndex")),
            "celestial_index": _int(data.get("celestialIndex")),
            "type_id": type_id,
            "type_name": type_names.get(type_id),
            "details": _physical_details(data),
            "population": (data.get("attributes") or {}).get("population"),
        })

    for kind, rows in (("moon", moon_rows), ("belt", belt_rows), ("station", station_rows)):
        for sde_key, data in rows:
            data = data or {}
            object_id = _int(data.get("_key")) or _int(sde_key)
            type_id = _int(data.get("typeID"))
            owner_id = _int(data.get("ownerID"))
            operation_id = _int(data.get("operationID"))
            objects.append({
                "id": object_id,
                "kind": kind,
                "name": _celestial_label(system["name"], kind, data, object_id),
                "position": _raw_position(data),
                "orbit_id": _int(data.get("orbitID")),
                "orbit_index": _int(data.get("orbitIndex")),
                "celestial_index": _int(data.get("celestialIndex")),
                "type_id": type_id,
                "type_name": type_names.get(type_id),
                "owner_id": owner_id,
                "owner_name": owner_names.get(owner_id),
                "operation_id": operation_id,
                "operation_name": operation_names.get(operation_id),
                "details": _physical_details(data),
                "reprocessing_efficiency": data.get("reprocessingEfficiency"),
                "reprocessing_take": data.get("reprocessingStationsTake"),
            })

    gates = []
    for sde_key, data in gate_rows:
        data = data or {}
        gate_id = _int(data.get("_key")) or _int(sde_key)
        destination = data.get("destination") or {}
        destination_system_id = _int(destination.get("solarSystemID"))
        destination_system = topology["systems"].get(destination_system_id) or {}
        destination_region = topology["regions"].get(destination_system.get("region_id")) or {}
        type_id = _int(data.get("typeID"))
        gates.append({
            "id": gate_id,
            "kind": "gate",
            "name": f"→ {destination_system.get('name') or ('System ' + str(destination_system_id))}",
            "position": _raw_position(data),
            "type_id": type_id,
            "type_name": type_names.get(type_id),
            "destination_system_id": destination_system_id,
            "destination_system_name": destination_system.get("name") or f"System {destination_system_id}",
            "destination_region_id": destination_system.get("region_id"),
            "destination_region_name": destination_region.get("name"),
            "destination_gate_id": _int(destination.get("stargateID")),
            "url": f"/map/system/{destination_system_id}" if destination_system_id else None,
        })
    gates.sort(key=lambda item: item["destination_system_name"].lower())

    kind_counts = {}
    for item in objects:
        kind_counts[item["kind"]] = kind_counts.get(item["kind"], 0) + 1

    breadcrumbs = [{"label": "New Eden", "url": "/map"}]
    if region:
        breadcrumbs.append({"label": region["name"], "url": f"/map/region/{region['id']}"})
    if constellation:
        breadcrumbs.append({"label": constellation["name"], "url": f"/map/constellation/{constellation['id']}"})
    breadcrumbs.append({"label": system["name"], "url": f"/map/system/{system_id}"})

    side_items = []
    for gate in gates:
        side_items.append({
            "id": f"gate-{gate['id']}",
            "name": gate["name"],
            "meta": gate.get("destination_region_name") or "Stargate",
            "url": gate.get("url"),
            "kind": "gate",
        })

    return {
        "scope": "system",
        "title": system["name"],
        "subtitle": (
            f"{kind_counts.get('planet', 0)} planets · {kind_counts.get('moon', 0)} moons · "
            f"{kind_counts.get('belt', 0)} belts · {kind_counts.get('station', 0)} NPC stations · "
            f"{len(gates)} stargates"
        ),
        "breadcrumbs": breadcrumbs,
        "system_id": system_id,
        "system_profile_url": f"/system/{system_id}",
        "region_id": system.get("region_id"),
        "constellation_id": system.get("constellation_id"),
        "security": system.get("security"),
        "system_info": {
            "name": system["name"],
            "security": system.get("security"),
            "security_class": system_data.get("securityClass"),
            "region_name": region.get("name") if region else None,
            "constellation_name": constellation.get("name") if constellation else None,
            "faction_name": (
                (topology["factions"].get(system.get("faction_id")) or {}).get("name")
                or (topology["factions"].get((region or {}).get("faction_id")) or {}).get("name")
            ),
            "planet_count": kind_counts.get("planet", 0),
            "moon_count": kind_counts.get("moon", 0),
            "station_count": kind_counts.get("station", 0),
            "gate_count": len(gates),
        },
        "objects": objects,
        "gates": gates,
        "side_title": "Stargates",
        "side_items": side_items,
    }

                      AND NULLIF(data->>'solarSystemID', '')::bigint
                          BETWEEN 31000000 AND 31999999
                """)
                wormhole_systems_with_moons = {
                    int(row[0])
                    for row in cur.fetchall()
                    if row[0] is not None
                }

    regions = {}
    for sde_key, data in region_rows:
        data = data or {}
        region_id = _int(data.get("_key")) or _int(sde_key)
        if region_id is None:
            continue
        regions[region_id] = {
            "id": region_id,
            "name": _name(data, f"Region {region_id}"),
            "position": _position(data),
            "faction_id": _int(data.get("factionID")),
            "wormhole_class_id": _int(data.get("wormholeClassID")),
            "constellation_ids": [_int(v) for v in (data.get("constellationIDs") or []) if _int(v) is not None],
        }

    constellations = {}
    for sde_key, data in constellation_rows:
        data = data or {}
        constellation_id = _int(data.get("_key")) or _int(sde_key)
        if constellation_id is None:
            continue
        constellations[constellation_id] = {
            "id": constellation_id,
            "name": _name(data, f"Constellation {constellation_id}"),
            "position": _position(data),
            "region_id": _int(data.get("regionID")),
            "faction_id": _int(data.get("factionID")),
            "system_ids": [_int(v) for v in (data.get("solarSystemIDs") or []) if _int(v) is not None],
        }

    factions = {}
    for sde_key, data in faction_rows:
        data = data or {}
        faction_id = _int(data.get("_key")) or _int(sde_key)
        if faction_id is None:
            continue
        factions[faction_id] = {
            "id": faction_id,
            "name": _name(data, f"Faction {faction_id}"),
        }

    systems = {}
    for sde_key, data in system_rows:
        data = data or {}
        system_id = _int(data.get("_key")) or _int(sde_key)
        if system_id is None:
            continue
        systems[system_id] = {
            "id": system_id,
            "name": _name(data, f"System {system_id}"),
            "position": _position(data),
            "position_2d": _position_2d(data),
            "constellation_id": _int(data.get("constellationID")),
            "region_id": _int(data.get("regionID")),
            "security": data.get("securityStatus"),
            "faction_id": _int(data.get("factionID")),
            "stargate_ids": [_int(v) for v in (data.get("stargateIDs") or []) if _int(v) is not None],
        }

    system_edges = set()
    for _, data in stargate_rows:
        data = data or {}
        source_id = _int(data.get("solarSystemID"))
        destination = data.get("destination") or {}
        destination_id = _int(destination.get("solarSystemID"))
        if source_id is None or destination_id is None or source_id == destination_id:
            continue
        if source_id not in systems or destination_id not in systems:
            continue
        system_edges.add(tuple(sorted((source_id, destination_id))))

    # Repair parent links from the constellation tables when a system row does not
    # carry them for any reason.
    for constellation in constellations.values():
        for system_id in constellation["system_ids"]:
            system = systems.get(system_id)
            if not system:
                continue
            if system.get("constellation_id") is None:
                system["constellation_id"] = constellation["id"]
            if system.get("region_id") is None:
                system["region_id"] = constellation.get("region_id")

    region_system_ids = {region_id: [] for region_id in regions}
    constellation_system_ids = {constellation_id: [] for constellation_id in constellations}
    for system in systems.values():
        region_id = system.get("region_id")
        constellation_id = system.get("constellation_id")
        if region_id in region_system_ids:
            region_system_ids[region_id].append(system["id"])
        if constellation_id in constellation_system_ids:
            constellation_system_ids[constellation_id].append(system["id"])

    region_edges = set()
    region_has_gate = set()
    system_neighbors = {system_id: set() for system_id in systems}
    for source_id, destination_id in system_edges:
        system_neighbors[source_id].add(destination_id)
        system_neighbors[destination_id].add(source_id)
        source_region = systems[source_id].get("region_id")
        destination_region = systems[destination_id].get("region_id")
        if source_region is not None:
            region_has_gate.add(source_region)
        if destination_region is not None:
            region_has_gate.add(destination_region)
        if source_region is not None and destination_region is not None and source_region != destination_region:
            region_edges.add(tuple(sorted((source_region, destination_region))))

    return {
        "regions": regions,
        "constellations": constellations,
        "factions": factions,
        "systems": systems,
        "system_edges": system_edges,
        "region_edges": region_edges,
        "region_has_gate": region_has_gate,
        "region_system_ids": region_system_ids,
        "constellation_system_ids": constellation_system_ids,
        "system_neighbors": system_neighbors,
    }


def _system_node(topology, system_id):
    system = topology["systems"][system_id]
    constellation = topology["constellations"].get(system.get("constellation_id"))
    region = topology["regions"].get(system.get("region_id"))
    external_neighbors = []
    for neighbor_id in topology["system_neighbors"].get(system_id, set()):
        neighbor = topology["systems"].get(neighbor_id)
        if not neighbor:
            continue
        if neighbor.get("region_id") != system.get("region_id"):
            external_neighbors.append({
                "id": neighbor_id,
                "name": neighbor["name"],
                "region_id": neighbor.get("region_id"),
                "region_name": (topology["regions"].get(neighbor.get("region_id")) or {}).get("name"),
            })
    external_neighbors.sort(key=lambda item: item["name"].lower())
    return {
        "id": system["id"],
        "name": system["name"],
        "position": system["position"],
        "security": system.get("security"),
        "constellation_id": system.get("constellation_id"),
        "constellation_name": constellation.get("name") if constellation else None,
        "region_id": system.get("region_id"),
        "region_name": region.get("name") if region else None,
        "gate_count": len(topology["system_neighbors"].get(system_id, set())),
        "external_neighbors": external_neighbors,
        "url": f"/map/system/{system_id}",
    }


def get_universe_map():
    topology = _topology()

    visible_region_ids = set(topology["region_has_gate"])
    if not visible_region_ids:
        visible_region_ids = set(topology["regions"])

    nodes = []
    for region_id in sorted(visible_region_ids):
        region = topology["regions"].get(region_id)
        if not region:
            continue
        constellation_ids = [
            cid for cid, constellation in topology["constellations"].items()
            if constellation.get("region_id") == region_id
        ]
        system_ids = topology["region_system_ids"].get(region_id, [])
        neighbor_ids = set()
        for a, b in topology["region_edges"]:
            if a == region_id:
                neighbor_ids.add(b)
            elif b == region_id:
                neighbor_ids.add(a)

        security_counts = {"hs": 0, "ls": 0, "null": 0}
        for system_id in system_ids:
            system = topology["systems"].get(system_id)
            if not system:
                continue
            security = system.get("security")
            try:
                security = float(security)
            except (TypeError, ValueError):
                security = 0.0
            if security >= 0.45:
                security_counts["hs"] += 1
            elif security > 0:
                security_counts["ls"] += 1
            else:
                security_counts["null"] += 1

        security_bands = [
            band for band in ("hs", "ls", "null")
            if security_counts[band] > 0
        ]

        # NPC ownership/influence comes from the region itself when CCP marks the
        # whole region as NPC space. For mixed nullsec regions (Delve, Fountain,
        # Pure Blind, Geminate, ...), constellation factionID gives the NPC pocket.
        faction_system_counts = {}
        region_faction_id = region.get("faction_id")
        if region_faction_id is not None:
            faction_system_counts[region_faction_id] = len(system_ids)
        else:
            for constellation_id in constellation_ids:
                constellation = topology["constellations"].get(constellation_id)
                if not constellation:
                    continue
                faction_id = constellation.get("faction_id")
                if faction_id is None:
                    continue
                count = sum(
                    1 for system_id in topology["constellation_system_ids"].get(constellation_id, [])
                    if system_id in system_ids
                )
                if count:
                    faction_system_counts[faction_id] = faction_system_counts.get(faction_id, 0) + count

        faction_breakdown = []
        for faction_id, count in sorted(faction_system_counts.items(), key=lambda item: (-item[1], item[0])):
            faction = topology["factions"].get(faction_id) or {}
            faction_breakdown.append({
                "id": faction_id,
                "name": faction.get("name") or f"Faction {faction_id}",
                "systems": count,
                "percent": round((count * 100.0 / len(system_ids)), 1) if system_ids else 0.0,
            })

        dominant_faction = faction_breakdown[0] if faction_breakdown else None
        npc_system_count = sum(item["systems"] for item in faction_breakdown)
        npc_control_percent = round((npc_system_count * 100.0 / len(system_ids)), 1) if system_ids else 0.0

        nodes.append({
            "id": region_id,
            "name": region["name"],
            "position": region["position"],
            "constellation_count": len(constellation_ids),
            "system_count": len(system_ids),
            "neighbor_count": len(neighbor_ids),
            "security_counts": security_counts,
            "security_bands": security_bands,
            "region_faction_id": region_faction_id,
            "dominant_faction_id": dominant_faction["id"] if dominant_faction else None,
            "dominant_faction_name": dominant_faction["name"] if dominant_faction else None,
            "npc_control_percent": npc_control_percent,
            "faction_breakdown": faction_breakdown,
            "url": f"/map/region/{region_id}",
        })

    edges = [
        {"source": a, "target": b}
        for a, b in sorted(topology["region_edges"])
        if a in visible_region_ids and b in visible_region_ids
    ]

    side_items = []
    for node in sorted(nodes, key=lambda item: item["name"].lower()):
        parts = []
        counts = node["security_counts"]
        if counts["hs"]:
            parts.append(f"{counts['hs']} HS")
        if counts["ls"]:
            parts.append(f"{counts['ls']} LS")
        if counts["null"]:
            parts.append(f"{counts['null']} 0.0")
        side_items.append({
            "id": node["id"],
            "name": node["name"],
            "meta": " · ".join(parts) or f"{node['system_count']} systems",
            "url": node["url"],
            "security_bands": node["security_bands"],
        })

    return {
        "scope": "universe",
        "title": "New Eden",
        "subtitle": f"{len(nodes)} regions · {len(edges)} inter-region connections",
        "breadcrumbs": [{"label": "New Eden", "url": "/map"}],
        "nodes": nodes,
        "edges": edges,
        "groups": [],
        "side_title": "Regions",
        "side_items": side_items,
    }


def get_eve_2d_map():
    topology = _topology()

    # CCP's in-game New Eden map uses this solar-system ID range.
    visible_system_ids = {
        system_id
        for system_id in topology["systems"]
        if 30_000_000 <= int(system_id) <= 30_999_999
    }

    nodes = []
    for system_id in sorted(visible_system_ids):
        system = topology["systems"][system_id]
        region = topology["regions"].get(system.get("region_id")) or {}
        constellation = topology["constellations"].get(system.get("constellation_id")) or {}
        pos = system.get("position_2d") or {}
        nodes.append({
            "id": system_id,
            "name": system["name"],
            "x": float(pos.get("x") or 0.0),
            "y": float(pos.get("y") or 0.0),
            "security": system.get("security"),
            "region_id": system.get("region_id"),
            "region_name": region.get("name"),
            "constellation_id": system.get("constellation_id"),
            "constellation_name": constellation.get("name"),
            "url": f"/map/system/{system_id}",
        })

    edges = [
        {"source": source_id, "target": target_id}
        for source_id, target_id in sorted(topology["system_edges"])
        if source_id in visible_system_ids and target_id in visible_system_ids
    ]

    return {
        "scope": "eve_2d",
        "title": "New Eden · 2D EVE Map",
        "subtitle": f"{len(nodes)} systems · {len(edges)} stargate connections",
        "nodes": nodes,
        "edges": edges,
        "breadcrumbs": [
            {"label": "New Eden", "url": "/map"},
            {"label": "2D EVE Map", "url": "/map/eve-2d"},
        ],
    }


YELLOW_INFLUENCE_COLOR = ("hsl(52 92% 56%)", 52.0, 92.0, 56.0)


def _ensure_influence_color_tables(cur):
    cur.execute("""
        CREATE SCHEMA IF NOT EXISTS sovereignty
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sovereignty.influence_color_assignments (
            assignment_id BIGSERIAL PRIMARY KEY,
            entity_type TEXT NOT NULL
                CHECK (entity_type IN ('alliance', 'coalition')),
            entity_id BIGINT NOT NULL,
            color TEXT NOT NULL,
            color_hue DOUBLE PRECISION NOT NULL,
            color_saturation DOUBLE PRECISION NOT NULL,
            color_lightness DOUBLE PRECISION NOT NULL,
            valid_from DATE NOT NULL,
            valid_to DATE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (valid_to IS NULL OR valid_to >= valid_from)
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS sov_influence_color_active_entity_idx
        ON sovereignty.influence_color_assignments (entity_type, entity_id)
        WHERE valid_to IS NULL
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS sov_influence_color_history_idx
        ON sovereignty.influence_color_assignments (
            entity_type, entity_id, valid_from, valid_to
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sovereignty.influence_color_state (
            entity_type TEXT PRIMARY KEY
                CHECK (entity_type IN ('alliance', 'coalition')),
            initialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def _influence_color_candidates():
    # Large fixed palette. Allocation is persisted, so IDs keep their colour
    # until they lose all SOV. Freed colours can then be reused.
    candidates = []
    for saturation, lightness, offset in (
        (76.0, 56.0, 0.0),
        (82.0, 68.0, 5.0),
        (68.0, 45.0, 10.0),
    ):
        for hue in range(0, 360, 15):
            value = float((hue + offset) % 360)
            candidates.append((
                f"hsl({value:.0f} {saturation:.0f}% {lightness:.0f}%)",
                value,
                saturation,
                lightness,
            ))
    return candidates


INFLUENCE_COLOR_CANDIDATES = _influence_color_candidates()


def _color_rgb(hue, saturation, lightness):
    red, green, blue = colorsys.hls_to_rgb(
        (float(hue) % 360.0) / 360.0,
        float(lightness) / 100.0,
        float(saturation) / 100.0,
    )
    return red, green, blue


def _pick_influence_color(used_colors, entity_id):
    used_rgb = [
        _color_rgb(hue, saturation, lightness)
        for hue, saturation, lightness in used_colors
    ]
    start = abs(int(entity_id)) % len(INFLUENCE_COLOR_CANDIDATES)
    ordered = (
        INFLUENCE_COLOR_CANDIDATES[start:]
        + INFLUENCE_COLOR_CANDIDATES[:start]
    )

    best = None
    best_score = -1.0
    for candidate in ordered:
        _color, hue, saturation, lightness = candidate
        rgb = _color_rgb(hue, saturation, lightness)

        if not used_rgb:
            return candidate

        score = min(
            (rgb[0] - other[0]) ** 2
            + (rgb[1] - other[1]) ** 2
            + (rgb[2] - other[2]) ** 2
            for other in used_rgb
        )
        if score > best_score:
            best_score = score
            best = candidate

    return best or ordered[0]


def _load_influence_assignments(cur, entity_type, entity_ids, selected_date):
    if not entity_ids:
        return {}

    cur.execute("""
        SELECT
            entity_id,
            color,
            color_hue,
            color_saturation,
            color_lightness
        FROM sovereignty.influence_color_assignments
        WHERE entity_type = %s
          AND entity_id = ANY(%s)
          AND valid_from <= %s
          AND (valid_to IS NULL OR %s < valid_to)
        ORDER BY assignment_id
    """, (entity_type, entity_ids, selected_date, selected_date))

    return {
        int(entity_id): {
            "color": color,
            "components": (
                float(hue),
                float(saturation),
                float(lightness),
            ),
        }
        for entity_id, color, hue, saturation, lightness in cur.fetchall()
    }


def _apply_persistent_influence_colors(
    groups,
    entity_type,
    selected_date,
    latest_date,
):
    entity_groups = {
        int(group["entity_id"]): group
        for group in groups
        if group.get("entity_id") is not None
    }
    if not entity_groups:
        if selected_date == latest_date:
            with db() as conn:
                with conn.cursor() as cur:
                    _ensure_influence_color_tables(cur)
                    cur.execute("SELECT pg_advisory_xact_lock(184624, 2)")
                    cur.execute("""
                        UPDATE sovereignty.influence_color_assignments
                        SET valid_to = %s
                        WHERE entity_type = %s
                          AND valid_to IS NULL
                    """, (selected_date, entity_type))
                conn.commit()
        return

    entity_ids = sorted(entity_groups)
    with db() as conn:
        with conn.cursor() as cur:
            _ensure_influence_color_tables(cur)
            cur.execute("SELECT pg_advisory_xact_lock(184624, 2)")

            if selected_date == latest_date:
                # An entity owns its colour only while it owns at least one
                # sovereignty system in this grouping mode.
                cur.execute("""
                    UPDATE sovereignty.influence_color_assignments
                    SET valid_to = %s
                    WHERE entity_type = %s
                      AND valid_to IS NULL
                      AND NOT (entity_id = ANY(%s))
                """, (selected_date, entity_type, entity_ids))

                assignments = _load_influence_assignments(
                    cur,
                    entity_type,
                    entity_ids,
                    selected_date,
                )

                # One-time initial seeds. Once an entity later reaches zero SOV,
                # this seed is never applied again: it receives a normal new
                # colour if it comes back.
                cur.execute("""
                    SELECT 1
                    FROM sovereignty.influence_color_state
                    WHERE entity_type = %s
                """, (entity_type,))
                initialized = cur.fetchone() is not None

                if not initialized:
                    seed_id = None
                    if entity_type == "alliance":
                        if 1354830081 in entity_groups:  # Goonswarm Federation
                            seed_id = 1354830081
                    else:
                        for entity_id, group in entity_groups.items():
                            if "imperium" in str(group.get("name") or "").casefold():
                                seed_id = entity_id
                                break

                    if seed_id is not None and seed_id not in assignments:
                        color, hue, saturation, lightness = YELLOW_INFLUENCE_COLOR
                        cur.execute("""
                            INSERT INTO sovereignty.influence_color_assignments (
                                entity_type,
                                entity_id,
                                color,
                                color_hue,
                                color_saturation,
                                color_lightness,
                                valid_from
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (
                            entity_type,
                            seed_id,
                            color,
                            hue,
                            saturation,
                            lightness,
                            selected_date,
                        ))

                    cur.execute("""
                        INSERT INTO sovereignty.influence_color_state (
                            entity_type,
                            initialized_at
                        )
                        VALUES (%s, NOW())
                        ON CONFLICT (entity_type) DO NOTHING
                    """, (entity_type,))

                    assignments = _load_influence_assignments(
                        cur,
                        entity_type,
                        entity_ids,
                        selected_date,
                    )

                used_colors = [
                    row["components"]
                    for row in assignments.values()
                ]

                missing_ids = [
                    entity_id
                    for entity_id in entity_ids
                    if entity_id not in assignments
                ]
                for entity_id in missing_ids:
                    color, hue, saturation, lightness = _pick_influence_color(
                        used_colors,
                        entity_id,
                    )
                    cur.execute("""
                        INSERT INTO sovereignty.influence_color_assignments (
                            entity_type,
                            entity_id,
                            color,
                            color_hue,
                            color_saturation,
                            color_lightness,
                            valid_from
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        entity_type,
                        entity_id,
                        color,
                        hue,
                        saturation,
                        lightness,
                        selected_date,
                    ))
                    assignments[entity_id] = {
                        "color": color,
                        "components": (hue, saturation, lightness),
                    }
                    used_colors.append((hue, saturation, lightness))
            else:
                assignments = _load_influence_assignments(
                    cur,
                    entity_type,
                    entity_ids,
                    selected_date,
                )

                # Dates older than the colour table itself have no factual
                # assignment to recover. Keep those historical views usable
                # without inventing persisted history.
                used_colors = [
                    row["components"]
                    for row in assignments.values()
                ]
                for entity_id in entity_ids:
                    if entity_id in assignments:
                        continue
                    color, hue, saturation, lightness = _pick_influence_color(
                        used_colors,
                        entity_id,
                    )
                    assignments[entity_id] = {
                        "color": color,
                        "components": (hue, saturation, lightness),
                    }
                    used_colors.append((hue, saturation, lightness))

        conn.commit()

    for entity_id, group in entity_groups.items():
        row = assignments.get(entity_id)
        if row:
            group["color"] = row["color"]


def get_eve_2d_influence(target_date=None, grouping="coalition"):
    topology = _topology()
    grouping = str(grouping or "coalition").strip().lower()
    if grouping not in {"alliance", "coalition"}:
        raise MapDataError("influence_grouping_invalid")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    to_regclass('sovereignty.current_map'),
                    to_regclass('sovereignty.map_changes')
            """)
            current_table, changes_table = cur.fetchone()
            if current_table is None or changes_table is None:
                raise MapDataError("sovereignty_history_unavailable")

            cur.execute("SELECT MAX((observed_at AT TIME ZONE 'UTC')::date) FROM sovereignty.current_map")
            latest_date = cur.fetchone()[0]
            if latest_date is None:
                raise MapDataError("sovereignty_history_unavailable")

            cur.execute("SELECT MIN((source_observed_at AT TIME ZONE 'UTC')::date) FROM sovereignty.map_changes")
            earliest_change_date = cur.fetchone()[0]

            if target_date is None:
                selected_date = latest_date
            elif isinstance(target_date, date):
                selected_date = target_date
            else:
                try:
                    selected_date = date.fromisoformat(str(target_date))
                except ValueError as exc:
                    raise MapDataError("influence_date_invalid") from exc

            if selected_date > latest_date:
                selected_date = latest_date
            if earliest_change_date is not None and selected_date < earliest_change_date:
                raise MapDataError("influence_date_before_history")

            cur.execute("""
                SELECT system_id, alliance_id, corporation_id, faction_id
                FROM sovereignty.current_map
            """)
            owners = {
                int(system_id): (
                    int(alliance_id) if alliance_id is not None else None,
                    int(corporation_id) if corporation_id is not None else None,
                    int(faction_id) if faction_id is not None else None,
                )
                for system_id, alliance_id, corporation_id, faction_id in cur.fetchall()
            }

            cur.execute("""
                SELECT
                    system_id,
                    change_type,
                    old_alliance_id,
                    old_corporation_id,
                    old_faction_id,
                    new_alliance_id,
                    new_corporation_id,
                    new_faction_id
                FROM sovereignty.map_changes
                WHERE (source_observed_at AT TIME ZONE 'UTC')::date > %s
                ORDER BY source_observed_at DESC, change_id DESC
            """, (selected_date,))

            for (
                system_id,
                change_type,
                old_alliance_id,
                old_corporation_id,
                old_faction_id,
                _new_alliance_id,
                _new_corporation_id,
                _new_faction_id,
            ) in cur.fetchall():
                system_id = int(system_id)
                if change_type == "GAIN":
                    owners[system_id] = (None, None, None)
                elif change_type == "LOST":
                    owners[system_id] = (
                        int(old_alliance_id) if old_alliance_id is not None else None,
                        int(old_corporation_id) if old_corporation_id is not None else None,
                        int(old_faction_id) if old_faction_id is not None else None,
                    )

            alliance_ids = sorted({
                owner[0]
                for owner in owners.values()
                if owner and owner[0] is not None
            })

            coalition_rows = []
            membership_rows = []
            if alliance_ids:
                cur.execute("""
                    SELECT coalition_id, name, short_name
                    FROM entities.coalitions
                    ORDER BY coalition_id
                """)
                coalition_rows = cur.fetchall()

                cur.execute("""
                    SELECT coalition_id, operation, member_type, member_id, valid_from, valid_to
                    FROM entities.coalition_memberships
                    ORDER BY id
                """)
                membership_rows = cur.fetchall()

                cur.execute("""
                    SELECT alliance_id, name
                    FROM entities.alliances
                    WHERE alliance_id = ANY(%s)
                """, (alliance_ids,))
                alliance_names = {
                    int(alliance_id): (name or f"Alliance {alliance_id}")
                    for alliance_id, name in cur.fetchall()
                }
            else:
                alliance_names = {}

    groups = {}

    if grouping == "alliance":
        for system_id, owner in owners.items():
            if int(system_id) not in topology["systems"]:
                continue
            alliance_id = owner[0] if owner else None
            if alliance_id is None:
                continue

            group_id = f"alliance:{alliance_id}"
            if group_id not in groups:
                groups[group_id] = {
                    "id": group_id,
                    "entity_type": "alliance",
                    "entity_id": int(alliance_id),
                    "name": alliance_names.get(int(alliance_id), f"Alliance {alliance_id}"),
                    "color": None,
                    "system_ids": [],
                }
            groups[group_id]["system_ids"].append(int(system_id))
    else:
        coalitions = {
            int(coalition_id): {
                "id": int(coalition_id),
                "name": name or short_name or f"Coalition {coalition_id}",
                "short_name": short_name,
            }
            for coalition_id, name, short_name in coalition_rows
        }

        rules_by_coalition = {coalition_id: [] for coalition_id in coalitions}
        active_child_coalitions = set()
        for coalition_id, operation, member_type, member_id, valid_from, valid_to in membership_rows:
            if valid_from is not None and valid_from > selected_date:
                continue
            if valid_to is not None and valid_to < selected_date:
                continue
            coalition_id = int(coalition_id)
            member_id = int(member_id)
            rule = {
                "operation": operation,
                "member_type": member_type,
                "member_id": member_id,
            }
            rules_by_coalition.setdefault(coalition_id, []).append(rule)
            if operation == "include" and member_type == "coalition":
                active_child_coalitions.add(member_id)

        resolved_cache = {}

        def resolve_coalition(coalition_id, stack=frozenset()):
            coalition_id = int(coalition_id)
            if coalition_id in resolved_cache:
                return resolved_cache[coalition_id]
            if coalition_id in stack:
                return set()

            includes = set()
            excludes = set()
            next_stack = stack | {coalition_id}
            for rule in rules_by_coalition.get(coalition_id, []):
                member_type = rule["member_type"]
                member_id = rule["member_id"]
                if member_type == "alliance":
                    target = {member_id}
                elif member_type == "coalition":
                    target = resolve_coalition(member_id, next_stack)
                else:
                    continue

                if rule["operation"] == "exclude":
                    excludes.update(target)
                else:
                    includes.update(target)

            result = includes - excludes
            resolved_cache[coalition_id] = result
            return result

        alliance_to_group = {}
        top_level_ids = [
            coalition_id
            for coalition_id in coalitions
            if coalition_id not in active_child_coalitions
        ]
        top_level_ids.sort(
            key=lambda coalition_id: (
                -len(resolve_coalition(coalition_id)),
                coalitions[coalition_id]["name"].casefold(),
                coalition_id,
            )
        )

        for coalition_id in top_level_ids:
            members = resolve_coalition(coalition_id)
            if not members:
                continue
            group_id = f"coalition:{coalition_id}"
            group = {
                "id": group_id,
                "entity_type": "coalition",
                "entity_id": coalition_id,
                "name": coalitions[coalition_id]["name"],
                "color": None,
                "system_ids": [],
            }
            groups[group_id] = group
            for alliance_id in members:
                alliance_to_group.setdefault(int(alliance_id), group_id)

        unaligned_group_id = "coalition:none"
        groups[unaligned_group_id] = {
            "id": unaligned_group_id,
            "entity_type": "coalition",
            "entity_id": None,
            "name": "Sans coalition",
            "color": "hsl(0 0% 100%)",
            "system_ids": [],
        }

        for system_id, owner in owners.items():
            if int(system_id) not in topology["systems"]:
                continue
            alliance_id = owner[0] if owner else None
            if alliance_id is None:
                continue

            group_id = alliance_to_group.get(int(alliance_id), unaligned_group_id)
            groups[group_id]["system_ids"].append(int(system_id))

    result_groups = [
        group
        for group in groups.values()
        if group["system_ids"]
    ]

    _apply_persistent_influence_colors(
        result_groups,
        grouping,
        selected_date,
        latest_date,
    )

    result_groups.sort(key=lambda item: (-len(item["system_ids"]), item["name"].casefold()))

    return {
        "date": selected_date.isoformat(),
        "grouping": grouping,
        "min_date": earliest_change_date.isoformat() if earliest_change_date else selected_date.isoformat(),
        "max_date": latest_date.isoformat(),
        "groups": result_groups,
    }


def get_region_map(region_id):
    topology = _topology()
    region_id = int(region_id)
    region = topology["regions"].get(region_id)
    if not region:
        raise MapDataError("region_not_found")

    system_ids = sorted(topology["region_system_ids"].get(region_id, []))
    system_set = set(system_ids)
    nodes = [_system_node(topology, system_id) for system_id in system_ids]

    dotlan_layout = REGION_LAYOUTS.get(region["name"]) or {}
    dotlan_nodes = dotlan_layout.get("nodes") or {}
    dotlan_matched_nodes = 0
    for node in nodes:
        anchor = dotlan_nodes.get(node["name"])
        if not anchor:
            continue
        node["dotlan_position"] = {"x": float(anchor["x"]), "y": float(anchor["y"])}
        dotlan_matched_nodes += 1

    edges = [
        {"source": a, "target": b}
        for a, b in sorted(topology["system_edges"])
        if a in system_set and b in system_set
    ]

    # External gates are rendered as DOTLAN-like exits on the edge of the region
    # map.  Keep one entry per real inter-region stargate connection so the
    # source system stays visually connected to the neighbouring region.
    exits = []
    neighbour_region_ids = set()
    for a, b in sorted(topology["system_edges"]):
        if (a in system_set) == (b in system_set):
            continue
        source_id, target_id = (a, b) if a in system_set else (b, a)
        target_system = topology["systems"].get(target_id)
        if not target_system:
            continue
        target_region_id = target_system.get("region_id")
        if target_region_id is None or target_region_id == region_id:
            continue
        target_region = topology["regions"].get(target_region_id) or {}
        neighbour_region_ids.add(target_region_id)
        target_system_name = target_system.get("name") or f"System {target_id}"
        exit_payload = {
            "id": f"exit-{source_id}-{target_id}",
            "source_system_id": source_id,
            "target_system_id": target_id,
            "target_system_name": target_system_name,
            "target_region_id": target_region_id,
            "target_region_name": target_region.get("name") or f"Region {target_region_id}",
            "target_region_position": target_region.get("position") or {"x": 0.0, "y": 0.0, "z": 0.0},
            "url": f"/map/region/{target_region_id}",
        }
        dotlan_exit = (dotlan_layout.get("exits") or {}).get(target_system_name)
        if dotlan_exit:
            exit_payload["dotlan_position"] = {
                "x": float(dotlan_exit["x"]),
                "y": float(dotlan_exit["y"]),
            }
        exits.append(exit_payload)

    constellation_ids = sorted({
        topology["systems"][system_id].get("constellation_id")
        for system_id in system_ids
        if topology["systems"][system_id].get("constellation_id") is not None
    })
    groups = []
    side_items = []
    for constellation_id in constellation_ids:
        constellation = topology["constellations"].get(constellation_id)
        if not constellation:
            continue
        count = sum(1 for system_id in system_ids if topology["systems"][system_id].get("constellation_id") == constellation_id)
        groups.append({
            "id": constellation_id,
            "name": constellation["name"],
            "position": constellation["position"],
            "url": f"/map/constellation/{constellation_id}",
        })
        side_items.append({
            "id": constellation_id,
            "name": constellation["name"],
            "meta": f"{count} systems",
            "url": f"/map/constellation/{constellation_id}",
        })

    return {
        "scope": "region",
        "title": region["name"],
        "subtitle": (
            f"{len(system_ids)} systems · {len(constellation_ids)} constellations · "
            f"{len(edges)} internal connections · {len(exits)} external gates to {len(neighbour_region_ids)} regions"
        ),
        "breadcrumbs": [
            {"label": "New Eden", "url": "/map"},
            {"label": region["name"], "url": f"/map/region/{region_id}"},
        ],
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "exits": exits,
        "region_position": region.get("position") or {"x": 0.0, "y": 0.0, "z": 0.0},
        "dotlan_layout": {
            "available": bool(dotlan_nodes),
            "complete": bool(dotlan_nodes) and dotlan_matched_nodes == len(nodes),
            "matched_nodes": dotlan_matched_nodes,
            "total_nodes": len(nodes),
            "page": dotlan_layout.get("page"),
            "page_width": DOTLAN_PAGE_WIDTH,
            "page_height": DOTLAN_PAGE_HEIGHT,
            "bounds": list(DOTLAN_MAP_BOUNDS),
            "node_width": dotlan_layout.get("node_width"),
            "node_height": dotlan_layout.get("node_height"),
        },
        "side_title": "Constellations",
        "side_items": sorted(side_items, key=lambda item: item["name"].lower()),
        "region_id": region_id,
    }


def get_constellation_map(constellation_id):
    topology = _topology()
    constellation_id = int(constellation_id)
    constellation = topology["constellations"].get(constellation_id)
    if not constellation:
        raise MapDataError("constellation_not_found")

    region = topology["regions"].get(constellation.get("region_id"))
    system_ids = sorted(topology["constellation_system_ids"].get(constellation_id, []))
    system_set = set(system_ids)
    nodes = [_system_node(topology, system_id) for system_id in system_ids]
    edges = [
        {"source": a, "target": b}
        for a, b in sorted(topology["system_edges"])
        if a in system_set and b in system_set
    ]

    side_items = [
        {
            "id": node["id"],
            "name": node["name"],
            "meta": (f"sec {float(node['security']):.2f}" if node.get("security") is not None else "system"),
            "url": node["url"],
        }
        for node in sorted(nodes, key=lambda item: item["name"].lower())
    ]

    breadcrumbs = [{"label": "New Eden", "url": "/map"}]
    if region:
        breadcrumbs.append({"label": region["name"], "url": f"/map/region/{region['id']}"})
    breadcrumbs.append({"label": constellation["name"], "url": f"/map/constellation/{constellation_id}"})

    return {
        "scope": "constellation",
        "title": constellation["name"],
        "subtitle": f"{len(system_ids)} systems · {len(edges)} internal connections",
        "breadcrumbs": breadcrumbs,
        "nodes": nodes,
        "edges": edges,
        "groups": [],
        "side_title": "Systems",
        "side_items": side_items,
        "constellation_id": constellation_id,
        "region_id": constellation.get("region_id"),
    }



def _preview_fit(points, padding=8.0):
    if not points:
        return {}
    xs = [float(point[1]) for point in points]
    ys = [float(point[2]) for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    usable = 100.0 - padding * 2.0
    scale = min(usable / span_x, usable / span_y)
    draw_w = span_x * scale
    draw_h = span_y * scale
    off_x = (100.0 - draw_w) / 2.0
    off_y = (100.0 - draw_h) / 2.0
    return {
        point_id: {
            "x": off_x + (float(x) - min_x) * scale,
            "y": off_y + (float(y) - min_y) * scale,
        }
        for point_id, x, y in points
    }


def get_location_preview(entity_type, entity_id):
    """Small, fixed, non-interactive map payload used in location profile cards."""
    entity_type = str(entity_type or "").strip().lower()
    entity_id = int(entity_id)
    topology = _topology()

    if entity_type == "region":
        region = topology["regions"].get(entity_id)
        if not region:
            raise MapDataError("region_not_found")
        system_ids = topology["region_system_ids"].get(entity_id, [])
        system_set = set(system_ids)
        dotlan_layout = REGION_LAYOUTS.get(region["name"]) or {}
        dotlan_nodes = dotlan_layout.get("nodes") or {}
        raw_points = []
        for system_id in system_ids:
            system = topology["systems"].get(system_id)
            if not system:
                continue
            anchor = dotlan_nodes.get(system["name"])
            if anchor:
                raw_points.append((system_id, float(anchor["x"]), float(anchor["y"])))
            else:
                pos = system.get("position") or {}
                raw_points.append((system_id, float(pos.get("x") or 0.0), -float(pos.get("z") or 0.0)))
        fitted = _preview_fit(raw_points, padding=5.0)
        edges = [
            {
                "x1": fitted[a]["x"], "y1": fitted[a]["y"],
                "x2": fitted[b]["x"], "y2": fitted[b]["y"],
            }
            for a, b in topology["system_edges"]
            if a in system_set and b in system_set and a in fitted and b in fitted
        ]
        return {
            "kind": "region",
            "nodes": [{"id": sid, **coords} for sid, coords in fitted.items()],
            "edges": edges,
        }

    if entity_type == "constellation":
        constellation = topology["constellations"].get(entity_id)
        if not constellation:
            raise MapDataError("constellation_not_found")
        system_ids = topology["constellation_system_ids"].get(entity_id, [])
        system_set = set(system_ids)
        region = topology["regions"].get(constellation.get("region_id")) or {}
        dotlan_nodes = (REGION_LAYOUTS.get(region.get("name")) or {}).get("nodes") or {}
        raw_points = []
        for system_id in system_ids:
            system = topology["systems"].get(system_id)
            if not system:
                continue
            anchor = dotlan_nodes.get(system["name"])
            if anchor:
                raw_points.append((system_id, float(anchor["x"]), float(anchor["y"])))
            else:
                pos = system.get("position") or {}
                raw_points.append((system_id, float(pos.get("x") or 0.0), -float(pos.get("z") or 0.0)))
        fitted = _preview_fit(raw_points, padding=10.0)
        edges = [
            {
                "x1": fitted[a]["x"], "y1": fitted[a]["y"],
                "x2": fitted[b]["x"], "y2": fitted[b]["y"],
            }
            for a, b in topology["system_edges"]
            if a in system_set and b in system_set and a in fitted and b in fitted
        ]
        return {
            "kind": "constellation",
            "nodes": [{"id": sid, **coords} for sid, coords in fitted.items()],
            "edges": edges,
        }

    if entity_type == "system":
        system = topology["systems"].get(entity_id)
        if not system:
            raise MapDataError("system_not_found")
        payload = get_system_map(entity_id)
        planets = [item for item in payload.get("objects", []) if item.get("kind") == "planet"]
        gates = [item for item in payload.get("objects", []) if item.get("kind") == "gate"]
        raw_points = []
        for item in planets + gates:
            pos = item.get("position") or {}
            raw_points.append((item["id"], float(pos.get("x") or 0.0), -float(pos.get("z") or 0.0)))
        fitted = _preview_fit(raw_points, padding=12.0) if raw_points else {}
        nodes = [{"id": "star", "x": 50.0, "y": 50.0, "kind": "star"}]
        for item in planets:
            coords = fitted.get(item["id"])
            if coords:
                nodes.append({"id": item["id"], **coords, "kind": "planet"})
        for item in gates:
            coords = fitted.get(item["id"])
            if coords:
                nodes.append({"id": item["id"], **coords, "kind": "gate"})
        return {
            "kind": "system",
            "nodes": nodes,
            "edges": [],
        }

    raise MapDataError("preview_type_invalid")


def _raw_position(data):
    pos = (data or {}).get("position") or {}
    return {
        "x": float(pos.get("x") or 0.0),
        "y": float(pos.get("y") or 0.0),
        "z": float(pos.get("z") or 0.0),
    }


def _load_sde_rows_by_ids(conn, table_name, ids):
    ids = [int(v) for v in ids if v is not None]
    if not ids:
        return []
    keys = [str(v) for v in ids]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT sde_key, data FROM public.{table_name} WHERE sde_key = ANY(%s)",
            (keys,),
        )
        return cur.fetchall()


def _load_sde_rows_for_system(conn, table_name, system_id):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT sde_key, data FROM public.{table_name} WHERE data->>'solarSystemID' = %s",
            (str(int(system_id)),),
        )
        return cur.fetchall()


def _celestial_label(system_name, kind, data, object_id):
    if kind == "star":
        return f"{system_name} Star"
    celestial_index = _int((data or {}).get("celestialIndex"))
    orbit_index = _int((data or {}).get("orbitIndex"))
    if kind == "planet":
        return f"{system_name} {celestial_index or '?'}"
    if kind == "moon":
        return f"Moon {orbit_index or '?'}"
    if kind == "belt":
        return f"Asteroid Belt {orbit_index or '?'}"
    if kind == "station":
        return f"NPC Station {object_id}"
    return f"{kind.title()} {object_id}"


def _lookup_sde_names(conn, candidate_tables, ids):
    ids = sorted({int(v) for v in ids if v is not None})
    if not ids:
        return {}
    lowered = [str(name).lower() for name in candidate_tables]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.table_name
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.column_name = 'data'
              AND lower(c.table_name) = ANY(%s)
            ORDER BY array_position(%s::text[], lower(c.table_name))
            LIMIT 1
            """,
            (lowered, lowered),
        )
        row = cur.fetchone()
    if not row:
        return {}
    table = '"' + str(row[0]).replace('"', '""') + '"'
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COALESCE(NULLIF(data->>'_key',''), NULLIF(sde_key,''))::BIGINT AS object_id,
                COALESCE(
                    data->'name'->>'en',
                    data->>'typeName',
                    data->>'corporationName',
                    data->>'operationName',
                    CASE WHEN jsonb_typeof(data->'name') = 'string' THEN data->>'name' END
                ) AS object_name
            FROM public.{table}
            WHERE COALESCE(NULLIF(data->>'_key',''), NULLIF(sde_key,''))::BIGINT = ANY(%s)
            """,
            (ids,),
        )
        return {int(object_id): object_name for object_id, object_name in cur.fetchall() if object_name}


def _physical_details(data):
    data = data or {}
    stats = data.get("statistics") or {}
    return {
        "radius_m": data.get("radius"),
        "density": stats.get("density"),
        "mass_gas": stats.get("massGas"),
        "mass_dust": stats.get("massDust"),
        "pressure": stats.get("pressure"),
        "temperature_k": stats.get("temperature"),
        "orbit_period_s": stats.get("orbitPeriod"),
        "orbit_radius_m": stats.get("orbitRadius"),
        "eccentricity": stats.get("eccentricity"),
        "rotation_rate_s": stats.get("rotationRate"),
        "spectral_class": stats.get("spectralClass"),
        "escape_velocity_m_s": stats.get("escapeVelocity"),
        "surface_gravity_m_s2": stats.get("surfaceGravity"),
        "locked": stats.get("locked"),
        "age_s": stats.get("age"),
        "life_s": stats.get("life"),
        "luminosity": stats.get("luminosity"),
    }


def get_system_map(system_id):
    topology = _topology()
    system_id = int(system_id)
    system = topology["systems"].get(system_id)
    if not system:
        raise MapDataError("system_not_found")

    constellation = topology["constellations"].get(system.get("constellation_id"))
    region = topology["regions"].get(system.get("region_id"))

    # Use the IDs embedded in the system/planet SDE rows whenever available.  It
    # avoids scanning every celestial table on each click.  Fallbacks keep this
    # compatible with SDE builds that omit one of the convenience arrays.
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM public.sde_mapsolarsystems WHERE sde_key = %s LIMIT 1",
                (str(system_id),),
            )
            row = cur.fetchone()
        system_data = (row[0] if row else {}) or {}

        star_id = _int(system_data.get("starID"))
        planet_ids = [_int(v) for v in (system_data.get("planetIDs") or []) if _int(v) is not None]
        stargate_ids = [_int(v) for v in (system_data.get("stargateIDs") or []) if _int(v) is not None]

        star_rows = _load_sde_rows_by_ids(conn, "sde_mapstars", [star_id]) if star_id else []
        if not star_rows:
            star_rows = _load_sde_rows_for_system(conn, "sde_mapstars", system_id)

        planet_rows = _load_sde_rows_by_ids(conn, "sde_mapplanets", planet_ids) if planet_ids else []
        if not planet_rows:
            planet_rows = _load_sde_rows_for_system(conn, "sde_mapplanets", system_id)

        moon_ids = []
        belt_ids = []
        for _, pdata in planet_rows:
            pdata = pdata or {}
            moon_ids.extend(_int(v) for v in (pdata.get("moonIDs") or []) if _int(v) is not None)
            belt_ids.extend(_int(v) for v in (pdata.get("asteroidBeltIDs") or []) if _int(v) is not None)

        moon_rows = _load_sde_rows_by_ids(conn, "sde_mapmoons", moon_ids) if moon_ids else []
        if not moon_rows:
            moon_rows = _load_sde_rows_for_system(conn, "sde_mapmoons", system_id)

        belt_rows = _load_sde_rows_by_ids(conn, "sde_mapasteroidbelts", belt_ids) if belt_ids else []
        if not belt_rows:
            belt_rows = _load_sde_rows_for_system(conn, "sde_mapasteroidbelts", system_id)

        gate_rows = _load_sde_rows_by_ids(conn, "sde_mapstargates", stargate_ids) if stargate_ids else []
        if not gate_rows:
            gate_rows = _load_sde_rows_for_system(conn, "sde_mapstargates", system_id)

        station_rows = _load_sde_rows_for_system(conn, "sde_npcstations", system_id)

        all_rows = star_rows + planet_rows + moon_rows + belt_rows + gate_rows + station_rows
        type_ids = [_int((data or {}).get("typeID")) for _, data in all_rows]
        type_names = _lookup_sde_names(conn, ["sde_types", "sde_invtypes", "sde_invTypes"], type_ids)
        owner_ids = [_int((data or {}).get("ownerID")) for _, data in station_rows]
        owner_names = _lookup_sde_names(
            conn,
            ["sde_npccorporations", "sde_npcCorporations", "sde_corpprofiles"],
            owner_ids,
        )
        operation_ids = [_int((data or {}).get("operationID")) for _, data in station_rows]
        operation_names = _lookup_sde_names(
            conn,
            ["sde_stationoperations", "sde_stationOperations"],
            operation_ids,
        )

    objects = []

    for sde_key, data in star_rows:
        data = data or {}
        object_id = _int(data.get("_key")) or _int(sde_key)
        type_id = _int(data.get("typeID"))
        objects.append({
            "id": object_id,
            "kind": "star",
            "name": _celestial_label(system["name"], "star", data, object_id),
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
            "orbit_id": None,
            "type_id": type_id,
            "type_name": type_names.get(type_id),
            "details": _physical_details(data),
        })

    for sde_key, data in planet_rows:
        data = data or {}
        object_id = _int(data.get("_key")) or _int(sde_key)
        type_id = _int(data.get("typeID"))
        objects.append({
            "id": object_id,
            "kind": "planet",
            "name": _celestial_label(system["name"], "planet", data, object_id),
            "position": _raw_position(data),
            "orbit_id": _int(data.get("orbitID")),
            "orbit_index": _int(data.get("orbitIndex")),
            "celestial_index": _int(data.get("celestialIndex")),
            "type_id": type_id,
            "type_name": type_names.get(type_id),
            "details": _physical_details(data),
            "population": (data.get("attributes") or {}).get("population"),
        })

    for kind, rows in (("moon", moon_rows), ("belt", belt_rows), ("station", station_rows)):
        for sde_key, data in rows:
            data = data or {}
            object_id = _int(data.get("_key")) or _int(sde_key)
            type_id = _int(data.get("typeID"))
            owner_id = _int(data.get("ownerID"))
            operation_id = _int(data.get("operationID"))
            objects.append({
                "id": object_id,
                "kind": kind,
                "name": _celestial_label(system["name"], kind, data, object_id),
                "position": _raw_position(data),
                "orbit_id": _int(data.get("orbitID")),
                "orbit_index": _int(data.get("orbitIndex")),
                "celestial_index": _int(data.get("celestialIndex")),
                "type_id": type_id,
                "type_name": type_names.get(type_id),
                "owner_id": owner_id,
                "owner_name": owner_names.get(owner_id),
                "operation_id": operation_id,
                "operation_name": operation_names.get(operation_id),
                "details": _physical_details(data),
                "reprocessing_efficiency": data.get("reprocessingEfficiency"),
                "reprocessing_take": data.get("reprocessingStationsTake"),
            })

    gates = []
    for sde_key, data in gate_rows:
        data = data or {}
        gate_id = _int(data.get("_key")) or _int(sde_key)
        destination = data.get("destination") or {}
        destination_system_id = _int(destination.get("solarSystemID"))
        destination_system = topology["systems"].get(destination_system_id) or {}
        destination_region = topology["regions"].get(destination_system.get("region_id")) or {}
        type_id = _int(data.get("typeID"))
        gates.append({
            "id": gate_id,
            "kind": "gate",
            "name": f"→ {destination_system.get('name') or ('System ' + str(destination_system_id))}",
            "position": _raw_position(data),
            "type_id": type_id,
            "type_name": type_names.get(type_id),
            "destination_system_id": destination_system_id,
            "destination_system_name": destination_system.get("name") or f"System {destination_system_id}",
            "destination_region_id": destination_system.get("region_id"),
            "destination_region_name": destination_region.get("name"),
            "destination_gate_id": _int(destination.get("stargateID")),
            "url": f"/map/system/{destination_system_id}" if destination_system_id else None,
        })
    gates.sort(key=lambda item: item["destination_system_name"].lower())

    kind_counts = {}
    for item in objects:
        kind_counts[item["kind"]] = kind_counts.get(item["kind"], 0) + 1

    breadcrumbs = [{"label": "New Eden", "url": "/map"}]
    if region:
        breadcrumbs.append({"label": region["name"], "url": f"/map/region/{region['id']}"})
    if constellation:
        breadcrumbs.append({"label": constellation["name"], "url": f"/map/constellation/{constellation['id']}"})
    breadcrumbs.append({"label": system["name"], "url": f"/map/system/{system_id}"})

    side_items = []
    for gate in gates:
        side_items.append({
            "id": f"gate-{gate['id']}",
            "name": gate["name"],
            "meta": gate.get("destination_region_name") or "Stargate",
            "url": gate.get("url"),
            "kind": "gate",
        })

    return {
        "scope": "system",
        "title": system["name"],
        "subtitle": (
            f"{kind_counts.get('planet', 0)} planets · {kind_counts.get('moon', 0)} moons · "
            f"{kind_counts.get('belt', 0)} belts · {kind_counts.get('station', 0)} NPC stations · "
            f"{len(gates)} stargates"
        ),
        "breadcrumbs": breadcrumbs,
        "system_id": system_id,
        "system_profile_url": f"/system/{system_id}",
        "region_id": system.get("region_id"),
        "constellation_id": system.get("constellation_id"),
        "security": system.get("security"),
        "system_info": {
            "name": system["name"],
            "security": system.get("security"),
            "security_class": system_data.get("securityClass"),
            "region_name": region.get("name") if region else None,
            "constellation_name": constellation.get("name") if constellation else None,
            "faction_name": (
                (topology["factions"].get(system.get("faction_id")) or {}).get("name")
                or (topology["factions"].get((region or {}).get("faction_id")) or {}).get("name")
            ),
            "planet_count": kind_counts.get("planet", 0),
            "moon_count": kind_counts.get("moon", 0),
            "station_count": kind_counts.get("station", 0),
            "gate_count": len(gates),
        },
        "objects": objects,
        "gates": gates,
        "side_title": "Stargates",
        "side_items": side_items,
    }
