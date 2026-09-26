#!/usr/bin/env python3
import json
import time
import zipfile
import hashlib
import shutil
from pathlib import Path

import requests
import psycopg2
from psycopg2.extras import Json, execute_values

CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"

def load_db_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)

DB_CONFIG = load_db_config()

BASE_DIR = Path.home() / "eveosint"
DOWNLOAD_DIR = BASE_DIR / "data" / "sde" / "downloads"
EXTRACT_DIR = BASE_DIR / "data" / "sde" / "extracted"

LATEST_URL = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
VARIANT = "jsonl"

USER_AGENT = (
    "DataArcheology/1.0.0 "
    "(discord: 206798315935760386 / eve-character: Dictateur Imperator)"
)


def now():
    return time.time()


def db():
    return psycopg2.connect(
        dbname=DB_CONFIG["db_name"],
        user=DB_CONFIG["db_user"],
        password=DB_CONFIG["db_password"],
        host=DB_CONFIG["db_host"],
        port=DB_CONFIG["db_port"],
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_table_name(file_path: Path) -> str:
    clean = "".join(c if c.isalnum() else "_" for c in file_path.stem)
    return f"sde_{clean.lower()}"


def ensure_base_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sde_import_runs (
            id SERIAL PRIMARY KEY,
            build_number INTEGER,
            variant TEXT,
            status TEXT,
            started_at DOUBLE PRECISION,
            finished_at DOUBLE PRECISION,
            error TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sde_remote_resources (
            url TEXT PRIMARY KEY
        );
        """)

        cur.execute("ALTER TABLE sde_remote_resources ADD COLUMN IF NOT EXISTS etag TEXT;")
        cur.execute("ALTER TABLE sde_remote_resources ADD COLUMN IF NOT EXISTS last_modified TEXT;")
        cur.execute("ALTER TABLE sde_remote_resources ADD COLUMN IF NOT EXISTS last_checked_at DOUBLE PRECISION;")
        cur.execute("ALTER TABLE sde_remote_resources ADD COLUMN IF NOT EXISTS last_status INTEGER;")
        cur.execute("ALTER TABLE sde_remote_resources ADD COLUMN IF NOT EXISTS content_hash TEXT;")

    conn.commit()


def get_cache(conn, url):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT etag, last_modified
            FROM sde_remote_resources
            WHERE url = %s
        """, (url,))
        row = cur.fetchone()
    return row if row else (None, None)


def save_resource_cache(conn, url, response, content_hash=None):
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO sde_remote_resources
            (url, etag, last_modified, last_checked_at, last_status, content_hash)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (url) DO UPDATE SET
            etag = EXCLUDED.etag,
            last_modified = EXCLUDED.last_modified,
            last_checked_at = EXCLUDED.last_checked_at,
            last_status = EXCLUDED.last_status,
            content_hash = COALESCE(EXCLUDED.content_hash, sde_remote_resources.content_hash)
        """, (
            url,
            response.headers.get("ETag"),
            response.headers.get("Last-Modified"),
            now(),
            response.status_code,
            content_hash,
        ))
    conn.commit()


def http_get_cached(conn, url, stream=False):
    etag, last_modified = get_cache(conn, url)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    r = requests.get(url, headers=headers, stream=stream, timeout=(5, 120))
    save_resource_cache(conn, url, r)
    return r


def get_latest_build(conn):
    r = http_get_cached(conn, LATEST_URL)

    if r.status_code == 304:
        current = get_current_build(conn)
        if current is not None:
            return current
        raise RuntimeError("latest.jsonl 304 sans build local dans app_meta")

    r.raise_for_status()

    for line in r.text.splitlines():
        if not line.strip():
            continue

        obj = json.loads(line)

        if obj.get("_key") == "sde":
            if "_value" in obj:
                return int(obj["_value"])
            if "value" in obj:
                return int(obj["value"])
            if "buildNumber" in obj:
                return int(obj["buildNumber"])
            if "build_number" in obj:
                return int(obj["build_number"])

            raise RuntimeError(f"Format latest.jsonl inconnu: {obj}")

    raise RuntimeError("Clé sde introuvable")


def get_current_build(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_meta WHERE key='sde_build_number'")
        row = cur.fetchone()
    return int(row[0]) if row else None


def already_imported(conn, build_number):
    current = get_current_build(conn)
    return current == build_number


def download_sde(conn, build_number):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    url = f"https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-{build_number}-jsonl.zip"
    zip_path = DOWNLOAD_DIR / f"sde_{build_number}.zip"

    r = requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True)
    r.raise_for_status()

    with open(zip_path, "wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            f.write(chunk)

    return zip_path


def extract_sde(zip_path, build_number):
    target = EXTRACT_DIR / str(build_number)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(target)

    return target


def create_table(cur, table):
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {table} (
        sde_key TEXT PRIMARY KEY,
        data JSONB,
        build_number INTEGER
    );
    """)


def import_file(conn, file_path, build_number):
    table = safe_table_name(file_path)

    rows = []
    with open(file_path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if not line.strip():
                continue
            obj = json.loads(line)
            key = str(obj.get("_key", i))
            rows.append((key, Json(obj), build_number))

    with conn.cursor() as cur:
        create_table(cur, table)
        cur.execute(f"TRUNCATE {table}")

        if rows:
            execute_values(cur,
                f"INSERT INTO {table} (sde_key, data, build_number) VALUES %s",
                rows
            )

    print(f"{table} OK ({len(rows)})")



def rebuild_sde_work_ship_entities(conn, build_number):
    """Rebuild denormalized SDE work table used by the web UI."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sde_work;")
        cur.execute("DROP TABLE IF EXISTS sde_work.ship_entities_tmp;")

        cur.execute(f"""
        CREATE TABLE sde_work.ship_entities_tmp AS
        WITH typedogma_rig AS (
            SELECT
                td.sde_key AS type_id,
                MAX((attr->>'value')::numeric)::integer AS rig_size
            FROM public.sde_typedogma td
            CROSS JOIN LATERAL jsonb_array_elements(td.data->'dogmaAttributes') AS attr
            WHERE (attr->>'attributeID')::integer = 1547
            GROUP BY td.sde_key
        ),
        market_path AS (
            WITH RECURSIVE tree AS (
                SELECT
                    t.sde_key AS type_id,
                    mg.sde_key AS market_group_id,
                    mg.data->'name'->>'en' AS market_group_name,
                    NULLIF(mg.data->>'parentGroupID', '') AS parent_market_group_id,
                    0 AS depth
                FROM public.sde_types t
                LEFT JOIN public.sde_marketgroups mg
                  ON mg.sde_key = NULLIF(t.data->>'marketGroupID', '')

                UNION ALL

                SELECT
                    tree.type_id,
                    parent.sde_key AS market_group_id,
                    parent.data->'name'->>'en' AS market_group_name,
                    NULLIF(parent.data->>'parentGroupID', '') AS parent_market_group_id,
                    tree.depth + 1 AS depth
                FROM tree
                JOIN public.sde_marketgroups parent
                  ON parent.sde_key = tree.parent_market_group_id
                WHERE tree.depth < 16
            )
            SELECT
                type_id,
                jsonb_agg(jsonb_build_object('id', market_group_id, 'name', market_group_name, 'depth', depth) ORDER BY depth) AS market_path,
                COALESCE(MIN(CASE market_group_name
                    WHEN 'Standard Frigates' THEN 10
                    WHEN 'Shuttles' THEN 15
                    WHEN 'Faction Frigates' THEN 20
                    WHEN 'Advanced Frigates' THEN 30
                    WHEN 'Standard Destroyers' THEN 40
                    WHEN 'Advanced Destroyers' THEN 50
                    WHEN 'Standard Cruisers' THEN 60
                    WHEN 'Faction Cruisers' THEN 70
                    WHEN 'Advanced Cruisers' THEN 80
                    WHEN 'Standard Battlecruisers' THEN 90
                    WHEN 'Faction Battlecruisers' THEN 100
                    WHEN 'Advanced Battlecruisers' THEN 110
                    WHEN 'Standard Battleships' THEN 120
                    WHEN 'Faction Battleships' THEN 130
                    WHEN 'Advanced Battleships' THEN 140
                    WHEN 'Industrial Ships' THEN 150
                    WHEN 'Mining Barges' THEN 160
                    WHEN 'Freighters' THEN 170
                    WHEN 'Capital Ships' THEN 180
                    WHEN 'Supercapital Ships' THEN 190
                    ELSE NULL
                END), 900) AS ship_sde_band_order,
                COALESCE((ARRAY_AGG(market_group_name ORDER BY CASE market_group_name
                    WHEN 'Standard Frigates' THEN 10
                    WHEN 'Shuttles' THEN 15
                    WHEN 'Faction Frigates' THEN 20
                    WHEN 'Advanced Frigates' THEN 30
                    WHEN 'Standard Destroyers' THEN 40
                    WHEN 'Advanced Destroyers' THEN 50
                    WHEN 'Standard Cruisers' THEN 60
                    WHEN 'Faction Cruisers' THEN 70
                    WHEN 'Advanced Cruisers' THEN 80
                    WHEN 'Standard Battlecruisers' THEN 90
                    WHEN 'Faction Battlecruisers' THEN 100
                    WHEN 'Advanced Battlecruisers' THEN 110
                    WHEN 'Standard Battleships' THEN 120
                    WHEN 'Faction Battleships' THEN 130
                    WHEN 'Advanced Battleships' THEN 140
                    WHEN 'Industrial Ships' THEN 150
                    WHEN 'Mining Barges' THEN 160
                    WHEN 'Freighters' THEN 170
                    WHEN 'Capital Ships' THEN 180
                    WHEN 'Supercapital Ships' THEN 190
                    ELSE 900
                END))[1], 'Other') AS ship_sde_band
            FROM tree
            GROUP BY type_id
        ),
        ship_base AS (
            SELECT
                NULLIF(t.sde_key, '')::bigint AS entity_id,
                t.sde_key AS type_id,
                COALESCE(t.data->'name'->>'en', t.data->>'typeName', t.data->>'name', 'Type ' || t.sde_key) AS name,
                NULLIF(t.data->>'groupID', '')::bigint AS group_id,
                g.data->'name'->>'en' AS group_name,
                NULLIF(g.data->>'categoryID', '')::bigint AS category_id,
                c.data->'name'->>'en' AS category_name,
                NULLIF(t.data->>'raceID', '')::bigint AS race_id,
                NULLIF(t.data->>'factionID', '')::bigint AS faction_id,
                NULLIF(t.data->>'metaGroupID', '')::bigint AS meta_group_id,
                mg.data->'name'->>'en' AS meta_group_name,
                td.rig_size,
                mp.market_path,
                COALESCE(mp.ship_sde_band, 'Other') AS ship_sde_band,
                COALESCE(mp.ship_sde_band_order, 900) AS ship_sde_band_order,
                f.data->'name'->>'en' AS sde_faction_name
            FROM public.sde_types t
            JOIN public.sde_groups g ON g.sde_key = NULLIF(t.data->>'groupID', '')
            JOIN public.sde_categories c ON c.sde_key = NULLIF(g.data->>'categoryID', '')
            LEFT JOIN public.sde_factions f ON f.sde_key = NULLIF(t.data->>'factionID', '')
            LEFT JOIN public.sde_metagroups mg ON mg.sde_key = NULLIF(t.data->>'metaGroupID', '')
            LEFT JOIN typedogma_rig td ON td.type_id = t.sde_key
            LEFT JOIN market_path mp ON mp.type_id = t.sde_key
            WHERE lower(c.data->'name'->>'en') = 'ship'
              AND COALESCE((t.data->>'published')::boolean, TRUE) IS TRUE
        )
        SELECT
            entity_id,
            type_id,
            name,
            group_id,
            group_name,
            category_id,
            category_name,
            race_id,
            faction_id,
            COALESCE(sde_faction_name,
                CASE race_id WHEN 1 THEN 'Caldari' WHEN 2 THEN 'Minmatar' WHEN 4 THEN 'Amarr' WHEN 8 THEN 'Gallente' ELSE 'Other' END
            ) AS faction_name,
            CASE
                WHEN faction_id IS NOT NULL THEN 'https://images.evetech.net/corporations/' || faction_id::text || '/logo?size=64'
                WHEN race_id = 1 THEN 'https://images.evetech.net/corporations/500001/logo?size=64'
                WHEN race_id = 2 THEN 'https://images.evetech.net/corporations/500002/logo?size=64'
                WHEN race_id = 4 THEN 'https://images.evetech.net/corporations/500003/logo?size=64'
                WHEN race_id = 8 THEN 'https://images.evetech.net/corporations/500004/logo?size=64'
                ELSE NULL
            END AS faction_logo_url,
            CASE COALESCE(sde_faction_name,
                CASE race_id WHEN 1 THEN 'Caldari' WHEN 2 THEN 'Minmatar' WHEN 4 THEN 'Amarr' WHEN 8 THEN 'Gallente' ELSE 'Other' END)
                WHEN 'Amarr Empire' THEN 10 WHEN 'Caldari State' THEN 20 WHEN 'Gallente Federation' THEN 30 WHEN 'Minmatar Republic' THEN 40
                WHEN 'Amarr' THEN 10 WHEN 'Caldari' THEN 20 WHEN 'Gallente' THEN 30 WHEN 'Minmatar' THEN 40
                WHEN 'Outer Ring Excavations' THEN 50 WHEN 'ORE' THEN 50 WHEN 'Angel Cartel' THEN 60 WHEN 'Blood Raider Covenant' THEN 70
                WHEN 'Guristas Pirates' THEN 80 WHEN 'Sansha''s Nation' THEN 90 WHEN 'Serpentis' THEN 100 WHEN 'Mordu''s Legion Command' THEN 110
                WHEN 'Sisters of EVE' THEN 120 WHEN 'The Society of Conscious Thought' THEN 130 WHEN 'Triglavian Collective' THEN 140 WHEN 'EDENCOM' THEN 150
                ELSE 500
            END AS faction_order,
            meta_group_id,
            meta_group_name,
            CASE
                WHEN lower(COALESCE(meta_group_name, '')) LIKE '%tech ii%' THEN 'T2'
                WHEN lower(COALESCE(meta_group_name, '')) LIKE '%tech iii%' THEN 'T3'
                WHEN lower(COALESCE(meta_group_name, '')) LIKE '%faction%' THEN 'Faction'
                WHEN lower(COALESCE(meta_group_name, '')) LIKE '%storyline%' THEN 'Storyline'
                WHEN lower(COALESCE(meta_group_name, '')) LIKE '%officer%' THEN 'Officer'
                WHEN lower(COALESCE(meta_group_name, '')) LIKE '%deadspace%' THEN 'Deadspace'
                ELSE 'T1'
            END AS ship_tier,
            CASE
                WHEN lower(COALESCE(meta_group_name, '')) LIKE '%faction%' THEN 20
                WHEN lower(COALESCE(meta_group_name, '')) LIKE '%tech ii%' THEN 30
                WHEN lower(COALESCE(meta_group_name, '')) LIKE '%tech iii%' THEN 40
                ELSE 10
            END AS ship_tier_order,
            rig_size,
            CASE
                WHEN rig_size = 1 THEN 'S'
                WHEN rig_size = 2 THEN 'M'
                WHEN rig_size = 3 THEN 'L'
                WHEN rig_size = 4 THEN 'XL'
                WHEN lower(group_name) IN ('capsule', 'corvette', 'shuttle') THEN 'S'
                WHEN lower(name) = 'zephyr' THEN 'S'
                WHEN lower(name) = 'primae' THEN 'M'
                ELSE 'UNKNOWN SIZE'
            END AS ship_display_size,
            CASE
                WHEN lower(group_name) LIKE '%capsule%' THEN 'Capsule'
                WHEN lower(ship_sde_band) LIKE '%shuttle%' THEN 'Shuttle'
                WHEN lower(ship_sde_band) LIKE '%frigate%' THEN 'Frigate'
                WHEN lower(ship_sde_band) LIKE '%destroyer%' THEN 'Destroyer'
                WHEN lower(ship_sde_band) LIKE '%cruiser%' AND lower(ship_sde_band) NOT LIKE '%battlecruiser%' THEN 'Cruiser'
                WHEN lower(ship_sde_band) LIKE '%battlecruiser%' THEN 'Battlecruiser'
                WHEN lower(ship_sde_band) LIKE '%battleship%' THEN 'Battleship'
                WHEN lower(ship_sde_band) LIKE '%freighter%' THEN 'Capital Industrial'
                WHEN lower(group_name) = 'capital industrial ship' THEN 'Capital Industrial'
                WHEN lower(group_name) IN ('hauler', 'blockade runner', 'deep space transport', 'industrial command ship') THEN 'Industrial'
                WHEN lower(group_name) LIKE '%industrial%' THEN 'Industrial'
                WHEN lower(ship_sde_band) LIKE '%capital%' AND lower(group_name) LIKE '%titan%' THEN 'Supercapital'
                WHEN lower(ship_sde_band) LIKE '%supercapital%' THEN 'Supercapital'
                WHEN lower(ship_sde_band) LIKE '%capital%' THEN 'Capital'
                WHEN lower(ship_sde_band) LIKE '%industrial%' THEN 'Industrial'
                WHEN lower(ship_sde_band) LIKE '%mining%' THEN 'Mining'
                WHEN lower(group_name) LIKE '%corvette%' THEN 'Corvette'
                ELSE COALESCE(group_name, 'Other')
            END AS ship_size,
            CASE
                WHEN lower(group_name) LIKE '%capsule%' THEN 5
                WHEN lower(group_name) LIKE '%corvette%' THEN 10
                WHEN lower(ship_sde_band) LIKE '%shuttle%' THEN 20
                WHEN lower(ship_sde_band) LIKE '%frigate%' THEN 30
                WHEN lower(ship_sde_band) LIKE '%destroyer%' THEN 40
                WHEN lower(ship_sde_band) LIKE '%cruiser%' AND lower(ship_sde_band) NOT LIKE '%battlecruiser%' THEN 50
                WHEN lower(ship_sde_band) LIKE '%battlecruiser%' THEN 60
                WHEN lower(ship_sde_band) LIKE '%battleship%' THEN 70
                WHEN lower(group_name) IN ('hauler', 'blockade runner', 'deep space transport', 'industrial command ship') THEN 80
                WHEN lower(group_name) LIKE '%industrial%' AND lower(group_name) <> 'capital industrial ship' THEN 80
                WHEN lower(ship_sde_band) LIKE '%industrial%' AND lower(group_name) <> 'capital industrial ship' THEN 80
                WHEN lower(ship_sde_band) LIKE '%mining%' THEN 85
                WHEN lower(ship_sde_band) LIKE '%freighter%' THEN 90
                WHEN lower(group_name) = 'capital industrial ship' THEN 90
                WHEN lower(ship_sde_band) LIKE '%capital%' THEN 100
                WHEN lower(ship_sde_band) LIKE '%supercapital%' THEN 110
                ELSE 900
            END AS ship_size_order,
            CASE
                WHEN lower(COALESCE(sde_faction_name, '')) IN ('ore', 'outer ring excavations') THEN 'industrial'
                WHEN lower(group_name) IN ('hauler', 'blockade runner', 'deep space transport', 'industrial command ship') THEN 'industrial'
                WHEN lower(group_name) LIKE '%industrial%' THEN 'industrial'
                WHEN lower(ship_sde_band) LIKE '%industrial%' OR lower(ship_sde_band) LIKE '%mining%' OR lower(ship_sde_band) LIKE '%freighter%' THEN 'industrial'
                ELSE 'combat'
            END AS ship_tree_row,
            ship_sde_band,
            ship_sde_band_order,
            market_path,
            'https://images.evetech.net/types/' || type_id || '/icon?size=32' AS image_url,
            '/entities/ship/' || type_id AS url,
            {int(build_number)} AS build_number,
            now() AS rebuilt_at
        FROM ship_base;
        """)

        cur.execute("""
        CREATE INDEX ON sde_work.ship_entities_tmp (entity_id);
        CREATE INDEX ON sde_work.ship_entities_tmp (faction_order, ship_size_order, ship_sde_band_order, ship_tier_order, name);
        CREATE INDEX ON sde_work.ship_entities_tmp (ship_tier);
        CREATE INDEX ON sde_work.ship_entities_tmp (ship_display_size);
        CREATE INDEX ON sde_work.ship_entities_tmp (ship_size);
        CREATE INDEX ON sde_work.ship_entities_tmp (faction_name);
        """)

        cur.execute("DROP TABLE IF EXISTS sde_work.ship_entities_old;")
        cur.execute("ALTER TABLE IF EXISTS sde_work.ship_entities RENAME TO ship_entities_old;")
        cur.execute("ALTER TABLE sde_work.ship_entities_tmp RENAME TO ship_entities;")
        cur.execute("DROP TABLE IF EXISTS sde_work.ship_entities_old;")

    conn.commit()
    print("sde_work.ship_entities OK")


def rebuild_sde_work_type_required_skills(conn, build_number):
    """Rebuild recursive SDE required skills table for every type."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sde_work;")
        cur.execute("DROP TABLE IF EXISTS sde_work.type_required_skills_tmp;")

        cur.execute(f"""
        CREATE TABLE sde_work.type_required_skills_tmp AS
        WITH RECURSIVE typedogma_attrs AS (
            SELECT
                td.sde_key::bigint AS type_id,

                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 182
                ))::bigint AS required_skill_1,
                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 277
                ))::integer AS required_skill_1_level,

                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 183
                ))::bigint AS required_skill_2,
                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 278
                ))::integer AS required_skill_2_level,

                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 184
                ))::bigint AS required_skill_3,
                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 279
                ))::integer AS required_skill_3_level,

                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 1285
                ))::bigint AS required_skill_4,
                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 1286
                ))::integer AS required_skill_4_level,

                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 1289
                ))::bigint AS required_skill_5,
                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 1287
                ))::integer AS required_skill_5_level,

                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 1290
                ))::bigint AS required_skill_6,
                (MAX((attr->>'value')::numeric) FILTER (
                    WHERE (attr->>'attributeID')::integer = 1288
                ))::integer AS required_skill_6_level
            FROM public.sde_typedogma td
            CROSS JOIN LATERAL jsonb_array_elements(td.data->'dogmaAttributes') AS attr
            WHERE td.sde_key ~ '^[0-9]+$'
              AND (attr->>'attributeID')::integer IN (
                  182, 183, 184, 1285, 1289, 1290,
                  277, 278, 279, 1286, 1287, 1288
              )
            GROUP BY td.sde_key
        ),
        direct_requires AS (
            SELECT type_id, required_skill_1 AS skill_type_id, required_skill_1_level AS required_level
            FROM typedogma_attrs
            WHERE required_skill_1 IS NOT NULL AND required_skill_1_level IS NOT NULL AND required_skill_1_level > 0

            UNION ALL
            SELECT type_id, required_skill_2, required_skill_2_level
            FROM typedogma_attrs
            WHERE required_skill_2 IS NOT NULL AND required_skill_2_level IS NOT NULL AND required_skill_2_level > 0

            UNION ALL
            SELECT type_id, required_skill_3, required_skill_3_level
            FROM typedogma_attrs
            WHERE required_skill_3 IS NOT NULL AND required_skill_3_level IS NOT NULL AND required_skill_3_level > 0

            UNION ALL
            SELECT type_id, required_skill_4, required_skill_4_level
            FROM typedogma_attrs
            WHERE required_skill_4 IS NOT NULL AND required_skill_4_level IS NOT NULL AND required_skill_4_level > 0

            UNION ALL
            SELECT type_id, required_skill_5, required_skill_5_level
            FROM typedogma_attrs
            WHERE required_skill_5 IS NOT NULL AND required_skill_5_level IS NOT NULL AND required_skill_5_level > 0

            UNION ALL
            SELECT type_id, required_skill_6, required_skill_6_level
            FROM typedogma_attrs
            WHERE required_skill_6 IS NOT NULL AND required_skill_6_level IS NOT NULL AND required_skill_6_level > 0
        ),
        req_tree AS (
            SELECT
                dr.type_id AS root_type_id,
                dr.skill_type_id,
                dr.required_level,
                1 AS depth,
                ARRAY[dr.type_id, dr.skill_type_id]::bigint[] AS path
            FROM direct_requires dr

            UNION ALL

            SELECT
                rt.root_type_id,
                dr.skill_type_id,
                dr.required_level,
                rt.depth + 1,
                rt.path || dr.skill_type_id
            FROM req_tree rt
            JOIN direct_requires dr
              ON dr.type_id = rt.skill_type_id
            WHERE rt.depth < 32
              AND NOT dr.skill_type_id = ANY(rt.path)
        ),
        final_requires AS (
            SELECT
                root_type_id AS type_id,
                skill_type_id,
                MAX(required_level)::integer AS required_level
            FROM req_tree
            GROUP BY root_type_id, skill_type_id
        )
        SELECT
            fr.type_id,
            COALESCE(root_t.data->'name'->>'en', root_t.data->>'typeName', root_t.data->>'name', 'Type ' || fr.type_id::text) AS type_name,
            NULLIF(root_t.data->>'groupID', '')::bigint AS type_group_id,
            root_g.data->'name'->>'en' AS type_group_name,
            NULLIF(root_g.data->>'categoryID', '')::bigint AS type_category_id,
            root_c.data->'name'->>'en' AS type_category_name,
            fr.skill_type_id,
            COALESCE(skill_t.data->'name'->>'en', skill_t.data->>'typeName', skill_t.data->>'name', 'Type ' || fr.skill_type_id::text) AS skill_name,
            fr.required_level,
            {int(build_number)} AS build_number,
            now() AS rebuilt_at
        FROM final_requires fr
        JOIN public.sde_types root_t
          ON root_t.sde_key = fr.type_id::text
        LEFT JOIN public.sde_groups root_g
          ON root_g.sde_key = NULLIF(root_t.data->>'groupID', '')
        LEFT JOIN public.sde_categories root_c
          ON root_c.sde_key = NULLIF(root_g.data->>'categoryID', '')
        JOIN public.sde_types skill_t
          ON skill_t.sde_key = fr.skill_type_id::text;
        """)

        cur.execute("""
        ALTER TABLE sde_work.type_required_skills_tmp
        ADD PRIMARY KEY (type_id, skill_type_id);
        """)

        cur.execute("DROP INDEX IF EXISTS sde_work.type_required_skills_type_idx;")
        cur.execute("DROP INDEX IF EXISTS sde_work.type_required_skills_skill_idx;")
        cur.execute("DROP INDEX IF EXISTS sde_work.type_required_skills_category_idx;")

        cur.execute("""
        CREATE INDEX type_required_skills_type_idx
        ON sde_work.type_required_skills_tmp (type_id);
        """)

        cur.execute("""
        CREATE INDEX type_required_skills_skill_idx
        ON sde_work.type_required_skills_tmp (skill_type_id);
        """)

        cur.execute("""
        CREATE INDEX type_required_skills_category_idx
        ON sde_work.type_required_skills_tmp (type_category_name, type_group_name, type_name);
        """)

        cur.execute("DROP TABLE IF EXISTS sde_work.type_required_skills_old;")
        cur.execute("ALTER TABLE IF EXISTS sde_work.type_required_skills RENAME TO type_required_skills_old;")
        cur.execute("ALTER TABLE sde_work.type_required_skills_tmp RENAME TO type_required_skills;")
        cur.execute("DROP TABLE IF EXISTS sde_work.type_required_skills_old;")

    conn.commit()
    print("sde_work.type_required_skills OK")

def main():
    conn = db()
    ensure_base_tables(conn)

    build = get_latest_build(conn)

    if already_imported(conn, build):
        # Even when the remote SDE has not changed, the two denormalized work
        # tables are rebuilt. Expose that work as a real X/X progress counter.
        total_tables = 2
        done_tables = 0
        print("déjà à jour")
        print(f"SDE TABLES total={total_tables}")

        rebuild_sde_work_ship_entities(conn, build)
        done_tables += 1
        print(f"SDE PROGRESS done={done_tables} total={total_tables}")

        rebuild_sde_work_type_required_skills(conn, build)
        done_tables += 1
        print(f"SDE PROGRESS done={done_tables} total={total_tables}")

        conn.close()
        print("DONE")
        return

    print("build:", build)

    zip_path = download_sde(conn, build)
    extracted = extract_sde(zip_path, build)
    files = sorted(extracted.rglob("*.jsonl"), key=lambda path: str(path))

    total_tables = len(files) + 2
    done_tables = 0
    print(f"SDE TABLES total={total_tables}")

    for file in files:
        import_file(conn, file, build)
        conn.commit()
        done_tables += 1
        print(f"SDE PROGRESS done={done_tables} total={total_tables}")

    rebuild_sde_work_ship_entities(conn, build)
    done_tables += 1
    print(f"SDE PROGRESS done={done_tables} total={total_tables}")

    rebuild_sde_work_type_required_skills(conn, build)
    done_tables += 1
    print(f"SDE PROGRESS done={done_tables} total={total_tables}")

    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO app_meta (key,value)
        VALUES ('sde_build_number',%s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """, (str(build),))

    conn.commit()
    conn.close()

    print("DONE")


if __name__ == "__main__":
    main()