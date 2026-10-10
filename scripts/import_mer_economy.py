#!/usr/bin/env python3
"""Import economic MER data from existing local ZIPs. No downloads or kill CSV reads.

Validate without PostgreSQL: python scripts/import_mer_economy.py --dry-run
Import manually: python scripts/import_mer_economy.py
Schema only: python scripts/import_mer_economy.py --schema-only
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import logging
import re
import struct
import sys
import zipfile
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

LOG = logging.getLogger("mer_economy")
VERSION = 1
SCHEMA = Path(__file__).with_name("mer_economy_schema.sql")
BATCH_SIZE = 1000
LOCK_ID = 472119057


def token(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


DATASETS = {
    "regionalstats": "regional", "keyeconomicfiguresbyregion": "regional",
    "miningbyregion": "mining_region", "moonmaterialsbyregion": "moon_region",
    "economyindicesdetails": "price_indices", "moneysupply": "money_supply",
    "iskvolume": "trade_volume", "produceddestroyedmined": "economic_activity",
    "miningproductiondestruction": "economic_activity",
    "mininghistorybysecurityband": "mining_volume",
    "moonmaterialshistorybyclass": "moon_materials",
    "sinksfaucets": "isk_flows", "topsinksfaucetsovertime": "isk_flows",
    "sinksandfaucetsovertime": "isk_flows", "sinksandfaucetshistory": "isk_flows",
    "commoditysinksandfaucetsovertime": "commodity_flows",
    "commoditysinksandfaucetshistory": "commodity_flows", "wormholetrade": "wormhole_trade",
}
REFERENCES = {"indexbaskets", "markers", "oretypemapping", "staticoretypemapping",
              "staticoremetagroups", "staticsolarsystems", "statictriglaviansites",
              "statictypevalues", "zkbpricinghistory"}
DIMENSIONS = {
    "locationmetagroup": "space_group", "securityband": "security_band",
    "primaryindex": "primary_index", "indexname": "primary_index", "subindex": "sub_index",
    "source": "source", "moonclass": "moon_class", "keytext": "entry_name",
    "entryname": "entry_name", "category": "category", "groupid": "group_id",
    "groupname": "group_name", "itemcategory": "item_category", "importorexport": "direction",
}
# metric, unit. Different monthly/regional moon fields have different units.
METRICS = {
    "produced": ("production_isk", "ISK"), "producedvalue": ("production_isk", "ISK"),
    "productionisk": ("production_isk", "ISK"), "totalproduction": ("production_isk", "ISK"),
    "destroyed": ("destruction_isk", "ISK"), "destroyedvalue": ("destruction_isk", "ISK"),
    "destructionisk": ("destruction_isk", "ISK"), "totaldestroyed": ("destruction_isk", "ISK"),
    "miningvalue": ("mining_isk", "ISK"), "minedvalue": ("mining_isk", "ISK"),
    "miningisk": ("mining_isk", "ISK"), "moonminingvalue": ("moon_mining_isk", "ISK"),
    "tradevalue": ("trade_isk", "ISK"), "npcbounties": ("npc_bounties_isk", "ISK"),
    "loyaltypoints": ("loyalty_points", "LP"), "imports": ("imports_isk", "ISK"),
    "exports": ("exports_isk", "ISK"), "importsm3": ("imports_volume", "m3"),
    "exportsm3": ("exports_volume", "m3"), "netexports": ("net_exports_isk", "ISK"),
    "netimports": ("net_imports_isk", "ISK"), "character": ("character_isk", "ISK"),
    "characterisk": ("character_isk", "ISK"), "corporation": ("corporation_isk", "ISK"),
    "corporationisk": ("corporation_isk", "ISK"), "totalisk": ("total_isk", "ISK"),
    "iskvolume": ("trade_volume_isk", "ISK"), "iskvelocity": ("isk_velocity", "ratio"),
    "iskvelocitywoaccessories": ("isk_velocity_without_accessories", "ratio"),
    "pricechange": ("price_change_factor", "ratio"), "totalvalue": ("basket_value_isk", "ISK"),
    "pricechangeweighted": ("weighted_price_change", "ratio"),
    "entrysinkvalue": ("sink_isk", "ISK"), "sink": ("sink_isk", "ISK"),
    "entryfaucetvalue": ("faucet_isk", "ISK"), "faucet": ("faucet_isk", "ISK"),
    "groupvalue": ("net_isk", "ISK"), "value": ("net_isk", "ISK"),
}
for _kind in ("asteroid", "gas", "ice", "moon"):
    for _activity in ("mined", "wasted"):
        METRICS[_kind + "volume" + _activity] = (_kind + "_volume_" + _activity, "m3")
IGNORED_FIELDS = {"", "unnamed0", "date", "historydate", "period", "regionid", "regionname",
                  "wormholeclass", "entryid", "sortvalue"}


def member_kind(name):
    stem = token(re.sub(r"^\d+[_-]", "", Path(name).stem))
    if "kill" in stem:
        return "skip"
    if Path(name).suffix.lower() == ".html":
        return "chart" if stem in {"economyindices", "shipandmoduleindex"} else "skip"
    if Path(name).suffix.lower() != ".csv" or stem in REFERENCES:
        return "skip"
    return DATASETS.get(stem, "unknown")


def report_month(path):
    name = path.stem.lower()
    compact = re.search(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)", name)
    if compact:
        return date(int(compact[1]), int(compact[2]), 1)
    months = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    match = re.search(r"(jan\w*?|feb\w*?|mar\w*?|apr\w*?|may|jun\w*?|jul\w*?|aug\w*?|sep\w*?|oct\w*?|nov\w*?|dec\w*?)[_ -]*(20\d{2}|\d{2})(?!\d)", name)
    if match:
        year = int(match[2])
        return date(year if year >= 2000 else 2000 + year, months.index(match[1][:3]) + 1, 1)
    raise ValueError(f"Cannot identify report month: {path.name}; use --month with --archive")


def number(value):
    value = str(value).strip()
    if value.lower() in {"", "na", "n/a", "null", "none", "nan"}:
        return None
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value {value!r}") from exc
    if not result.is_finite():
        raise ValueError("Non-finite economic value")
    return result


def dimensions(row):
    result = {label: value.strip() for key, value in row.items()
              if key in DIMENSIONS for label in [DIMENSIONS[key]] if value.strip()}
    if result.get("primary_index") == "CPI":
        result["primary_index"] = "Consumer Price Index"
    if result.get("source") == "metanox_mining":
        result["source"] = "metenox_mining"
    return result


def normalized_row(dataset, original, month, regions):
    if dataset == "unknown":
        return [], original
    row = {token(k): str(v or "").strip() for k, v in original.items()}
    dt = row.get("historydate") or row.get("date") or row.get("period")
    period = date.fromisoformat(dt[:10]) if dt else month
    dims = dimensions(row)
    grain = "month" if not dt or dataset == "price_indices" else "day"
    if dataset == "price_indices":
        period = period.replace(day=1)
    table = "isk_flow_history" if dataset in {"isk_flows", "commodity_flows"} else "global_economy_history"
    scope = None
    if dataset in {"regional", "mining_region", "moon_region", "wormhole_trade"}:
        period = month
        table = "region_economy_monthly"
        label = row.get("regionname", "")
        region_id = int(row["regionid"]) if row.get("regionid") else regions.get(label.casefold())
        if dataset == "wormhole_trade":
            label = row["wormholeclass"]
            match = re.fullmatch(r"Class\s+(\d+)", label, re.IGNORECASE)
            scope = ("wormhole_class", match[1] if match else label, label, None)
        elif region_id is not None:
            scope = ("region", str(region_id), label, region_id)
        elif label.lower().startswith("wormhole") or label.lower().startswith("non-universe"):
            scope = ("space_group", label, label, None)
        elif label:
            scope = ("region_name", label.casefold(), label, None)
        else:
            raise ValueError("Regional row has neither region ID nor name")
    facts = []
    known = set(IGNORED_FIELDS) | set(DIMENSIONS)
    for key, value in row.items():
        definition = METRICS.get(key)
        if key == "total":
            definition = ("total_isk" if dataset == "money_supply" else "trade_isk", "ISK")
        if key == "quantity" and dataset in {"moon_region", "moon_materials"}:
            definition = ("moon_material_value_isk", "ISK") if dataset == "moon_region" else ("moon_material_quantity", "items")
        if definition:
            known.add(key)
            parsed = number(value)
            if parsed is not None:
                facts.append((table, period, grain, dataset, definition[0], dims, parsed, definition[1], scope))
    extra = {k: v for k, v in original.items() if token(k) not in known}
    if not facts and dataset != "unknown" and not extra:
        # A row with empty metrics is legitimate; missing values are not zeros.
        return facts, None
    return facts, extra or (original if dataset == "unknown" else None)


def chart_values(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and "bdata" in value:
        formats = {"f8": "d", "f4": "f", "i8": "q", "i4": "i", "i2": "h", "i1": "b", "u1": "B"}
        dtype = value.get("dtype")
        if dtype not in formats or value.get("shape") and "," in str(value["shape"]):
            raise ValueError(f"Unsupported chart array dtype/shape: {dtype}")
        payload = base64.b64decode(value["bdata"], validate=True)
        return [item[0] for item in struct.iter_unpack("<" + formats[dtype], payload)]
    raise ValueError("Unsupported chart series encoding")


def chart_facts(text, member):
    matches = list(re.finditer(r'Plotly\.newPlot\(\s*"[^"]+"\s*,\s*', text))
    if not matches:
        raise ValueError("Cannot locate economic chart data")
    decoder = json.JSONDecoder(parse_float=Decimal)
    traces, _end = decoder.raw_decode(text, matches[-1].end())
    for trace in traces:
        # Unnamed traces are label/annotation markers, not economic series.
        if not trace.get("name") or "x" not in trace or "y" not in trace:
            continue
        xs, ys = chart_values(trace["x"]), chart_values(trace["y"])
        if len(xs) != len(ys):
            raise ValueError("Chart dates and values differ in length")
        for dt, value in zip(xs, ys):
            parsed = number(value)
            if parsed is not None:
                yield ("global_economy_history", date.fromisoformat(str(dt)[:10]).replace(day=1), "month",
                       "price_index_levels", "index_level",
                       {"index": trace["name"], "chart": Path(member).stem}, parsed, "index", None)


def manifest(archive):
    return [{"member": i.filename, "crc": i.CRC, "size": i.file_size, "kind": member_kind(i.filename)}
            for i in archive.infolist() if not i.is_dir() and member_kind(i.filename) != "skip"]


def fingerprint(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def iter_archive(path, month, regions, stats):
    with zipfile.ZipFile(path) as archive:
        for item in manifest(archive):
            name, kind = item["member"], item["kind"]
            LOG.info("MEMBER_START month=%s member=%s kind=%s", month, name, kind)
            if kind == "chart":
                for fact in chart_facts(archive.read(name).decode("utf-8-sig"), name):
                    stats["facts"] += 1
                    yield fact[0], fact, name, None
                continue
            with archive.open(name) as stream, io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as text:
                reader = csv.DictReader(text)
                if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
                    raise ValueError(f"Missing or duplicate CSV headers: {name}")
                for row_number, row in enumerate(reader, 2):
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(f"CSV column count mismatch: {name}:{row_number}")
                    stats["rows"] += 1
                    try:
                        facts, extra = normalized_row(kind, row, month, regions)
                    except Exception as exc:
                        raise ValueError(f"{name}:{row_number}: {exc}") from exc
                    for fact in facts:
                        stats["facts"] += 1
                        yield fact[0], fact, name, None
                    if any(fact[-1] and fact[-1][0] == "region_name" for fact in facts):
                        stats["unresolved_region_rows"] += 1
                    if extra:
                        stats["unmapped_rows"] += 1
                        yield "economy_unmapped_rows", (month, name, row_number, "unknown_dataset" if kind == "unknown" else "unknown_columns", json.dumps(row)), name, None
            LOG.info("MEMBER_DONE month=%s member=%s rows=%s facts=%s", month, name, stats["rows"], stats["facts"])


def write_batch(conn, table, records, month):
    from psycopg2.extras import execute_values
    if table == "economy_unmapped_rows":
        with conn.cursor() as cur:
            execute_values(cur, "INSERT INTO mer.economy_unmapped_rows VALUES %s ON CONFLICT (source_month,source_member,source_row) DO UPDATE SET reason=excluded.reason,payload=excluded.payload", records, page_size=BATCH_SIZE)
        return len(records)
    region = table == "region_economy_monthly"
    values = []
    for fact, member in records:
        _table, period, grain, dataset, metric, dims, value, unit, scope = fact
        prefix = (period, *scope) if region else (period, grain)
        values.append((*prefix, dataset, metric, json.dumps(dims, sort_keys=True), value, unit, month, member))
    columns = "period_start,scope_kind,scope_key,scope_name,region_id" if region else "period_start,period_grain"
    columns += ",dataset,metric,dimensions,value,unit,source_month,source_member"
    key = "period_start,scope_kind,scope_key,dataset,metric,dimensions" if region else "period_start,period_grain,dataset,metric,dimensions"
    updates = "value=excluded.value,unit=excluded.unit,source_month=excluded.source_month,source_member=excluded.source_member"
    if region:
        updates += ",scope_name=excluded.scope_name,region_id=excluded.region_id"
    with conn.cursor() as cur:
        execute_values(cur, f"INSERT INTO mer.{table} ({columns}) VALUES %s ON CONFLICT ({key}) DO UPDATE SET {updates} WHERE excluded.source_month >= {table}.source_month", values, page_size=BATCH_SIZE)
    return len(values)


def import_archive(conn, path, month, regions, force=False):
    with zipfile.ZipFile(path) as archive:
        entries = manifest(archive)
    if not entries:
        raise ValueError("Archive contains no economic data")
    signature = fingerprint(entries)
    with conn.cursor() as cur:
        cur.execute("SELECT economic_fingerprint,parser_version,status FROM mer.economy_imports WHERE report_month=%s", (month,))
        prior = cur.fetchone()
    if prior == (signature, VERSION, "success") and not force:
        conn.rollback()
        LOG.info("ARCHIVE_SKIPPED month=%s reason=already_imported", month)
        return {"skipped": 1}
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO mer.economy_imports(report_month,archive_path,economic_fingerprint,parser_version,status,manifest)
            VALUES (%s,%s,%s,%s,'running',%s) ON CONFLICT(report_month) DO UPDATE SET
            archive_path=excluded.archive_path,economic_fingerprint=excluded.economic_fingerprint,
            parser_version=excluded.parser_version,status='running',manifest=excluded.manifest,
            started_at=now(),finished_at=NULL,error=NULL""", (month, str(path), signature, VERSION, json.dumps(entries)))
    conn.commit()
    stats = Counter()
    try:
        # One transaction per archive: parsing/import failures retain old facts.
        with conn.cursor() as cur:
            for table in ("region_economy_monthly", "global_economy_history", "isk_flow_history"):
                cur.execute(f"DELETE FROM mer.{table} WHERE source_month=%s", (month,))
            cur.execute("DELETE FROM mer.economy_unmapped_rows WHERE source_month=%s", (month,))
        buffers = {}
        for table, fact, member, _ in iter_archive(path, month, regions, stats):
            batch = buffers.setdefault(table, [])
            batch.append(fact if table == "economy_unmapped_rows" else (fact, member))
            if len(batch) >= BATCH_SIZE:
                write_batch(conn, table, batch, month)
                batch.clear()
        for table, batch in buffers.items():
            if batch:
                write_batch(conn, table, batch, month)
        with conn.cursor() as cur:
            cur.execute("UPDATE mer.economy_imports SET status='success',rows_read=%s,facts_written=%s,finished_at=now(),error=NULL WHERE report_month=%s", (stats["rows"], stats["facts"], month))
            cur.execute("SELECT to_regclass('web.mer_catalog')")
            if cur.fetchone()[0]:
                cur.execute("UPDATE web.mer_catalog SET economic_dump_state=%s,updated_at=now() WHERE month=%s", ("imported_with_unmapped" if stats["unmapped_rows"] else "imported", month))
        conn.commit()
        return dict(stats)
    except BaseException as exc:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("UPDATE mer.economy_imports SET status='failed',error=%s,finished_at=now() WHERE report_month=%s", (str(exc)[:2000], month))
        conn.commit()
        raise


def connect(config_path):
    import psycopg2
    config = json.loads(config_path.read_text())
    return psycopg2.connect(dbname=config["db_name"], user=config["db_user"], password=config["db_password"],
                            host=config["db_host"], port=config["db_port"])


def load_regions(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT sde_key,data FROM public.sde_mapregions")
        result = {}
        for key, data in cur.fetchall():
            name = data.get("name") or {}
            label = name.get("en") if isinstance(name, dict) else name
            if label:
                result[str(label).casefold()] = int(data.get("_key") or key)
    return result


def month_arg(value):
    result = date.fromisoformat(value + "-01" if len(value) == 7 else value)
    if result.day != 1:
        raise argparse.ArgumentTypeError("Month must start on day 1")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, default=Path.home() / "eveosint/data/mer/archive")
    parser.add_argument("--archive", action="append", type=Path)
    parser.add_argument("--month", type=month_arg, help="Explicit report month for one --archive")
    parser.add_argument("--from", dest="start", type=month_arg)
    parser.add_argument("--to", dest="end", type=month_arg)
    parser.add_argument("--db-config", type=Path, default=Path.home() / "eveosint/config/db.json")
    parser.add_argument("--dry-run", action="store_true", help="Parse archives without connecting to PostgreSQL")
    parser.add_argument("--schema-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.month and (not args.archive or len(args.archive) != 1):
        parser.error("--month requires exactly one --archive")
    if args.start and args.end and args.start > args.end:
        parser.error("--from must not be after --to")
    if args.dry_run and args.schema_only:
        parser.error("--schema-only cannot be combined with --dry-run")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    paths = args.archive or sorted(args.archive_dir.rglob("*.zip"))
    # Latest reports often repeat years of history. Import them first so older
    # archives fill missing points without rewriting the newer observations.
    archives = sorted(((args.month or report_month(p), p.resolve()) for p in paths), reverse=True)
    archives = [(m,p) for m,p in archives if (not args.start or m>=args.start) and (not args.end or m<=args.end)]
    if len({m for m,p in archives}) != len(archives):
        parser.error("Multiple archives for one month; select the desired file explicitly with --archive")
    if not archives and not args.schema_only:
        parser.error("No MER archives found")
    conn = None
    totals = Counter()
    failed = 0
    try:
        regions = {}
        if not args.dry_run:
            conn = connect(args.db_config)
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,))
                if not cur.fetchone()[0]:
                    raise RuntimeError("Another economic MER import is running")
                cur.execute(SCHEMA.read_text())
            conn.commit()
            if args.schema_only:
                LOG.info("SCHEMA_READY")
                return 0
            regions = load_regions(conn)
            conn.commit()
        for month, path in archives:
            try:
                LOG.info("ARCHIVE_START month=%s file=%s dry_run=%s", month, path.name, args.dry_run)
                if conn is None:
                    stats = Counter()
                    for _record in iter_archive(path, month, regions, stats):
                        pass
                else:
                    stats = import_archive(conn, path, month, regions, args.force)
                totals.update(stats)
                LOG.info("ARCHIVE_DONE month=%s %s", month, json.dumps(dict(stats), sort_keys=True))
            except Exception:
                failed += 1
                LOG.exception("ARCHIVE_FAILED month=%s", month)
        LOG.info("DONE archives=%s failed=%s totals=%s", len(archives), failed, json.dumps(dict(totals)))
        return 1 if failed else 0
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
