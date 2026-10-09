#!/usr/bin/env python3
"""Blind, read-only evaluation on known MER kills; never calls CCP or web startup.

The target kill is masked from combat evidence and known-ID elimination. A JSON
report caches the evidence so score changes can be compared without more SQL.
Password is prompted privately; no production config or secrets are loaded.
"""

import argparse
import getpass
import importlib
import inspect
import json
import statistics
import sys
import time
import types
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_eveosint_readonly_forensics_eval"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "web/app")]
sys.modules[PACKAGE] = package
db_module = types.ModuleType(PACKAGE + ".db")


def forbidden_db():
    raise RuntimeError("The evaluator must use its explicit read-only connection.")


db_module.db = forbidden_db
sys.modules[db_module.__name__] = db_module
engine = importlib.import_module(PACKAGE + ".forensics_engine")
evidence = importlib.import_module(PACKAGE + ".forensics_evidence")


def encode(value):
    if isinstance(value, datetime):
        return {"__kind__": "datetime", "value": value.isoformat()}
    if isinstance(value, tuple):
        return {"__kind__": "tuple", "value": [encode(v) for v in value]}
    if isinstance(value, set):
        return {"__kind__": "set", "value": [encode(v) for v in sorted(value)]}
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {k: encode(v) for k, v in value.items()}
        return {
            "__kind__": "pairs",
            "value": [[encode(k), encode(v)] for k, v in value.items()],
        }
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        kind = value.get("__kind__")
        if kind == "datetime":
            return datetime.fromisoformat(value["value"])
        if kind == "tuple":
            return tuple(decode(v) for v in value["value"])
        if kind == "set":
            return set(decode(v) for v in value["value"])
        if kind == "pairs":
            return {decode(k): decode(v) for k, v in value["value"]}
        return {k: decode(v) for k, v in value.items()}
    return value


def sample(conn, month, count, objects_only=False):
    end = date(month.year + int(month.month == 12), month.month % 12 + 1, 1)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SET LOCAL statement_timeout='15s'")
        selection = (
            """victim_ship_type_id IN (SELECT t.sde_key::bigint FROM public.sde_types t
            JOIN public.sde_groups g ON g.sde_key=t.data->>'groupID'
            WHERE (g.data->>'categoryID')::int IN (3,22,23,40,46,65,66)) AND resolved_km[1] %% 127=0"""
            if objects_only
            else "resolved_km[1] %% 5003=0"
        )
        cur.execute(
            f"""WITH sampled AS MATERIALIZED (
            SELECT * FROM mer.killmails WHERE source_month=%s AND kill_datetime >= %s AND kill_datetime < %s
            AND array_length(resolved_km,1)=1 AND NOT resolved_km_ambiguous
            AND {selection} ORDER BY md5(resolved_km[1]::text) LIMIT %s)
            SELECT to_jsonb(m) AS snapshot,k.killmail_id,k.victim_character_id AS victim,
                a.character_id AS attacker,k.killmail_hash
            FROM sampled m JOIN rawkm.killmails k ON k.killmail_id=m.resolved_km[1] AND k.killmail_time=m.kill_datetime
            JOIN rawkm.killmail_attackers a ON a.killmail_id=k.killmail_id AND a.killmail_time=k.killmail_time AND a.final_blow IS TRUE
            WHERE a.killmail_time >= %s AND a.killmail_time < %s
            AND k.killmail_time >= %s AND k.killmail_time < %s ORDER BY md5(k.killmail_id::text)""",
            (month, month, end, count * 4, month, end, month, end),
        )
        candidates = [dict(row) for row in cur.fetchall()]
    groups = defaultdict(list)
    for row in candidates:
        category = (
            "unpiloted_victim"
            if row["victim"] is None
            else (
                "npc_or_unpiloted_final_blow"
                if row["attacker"] is None
                else "player_vs_player"
            )
        )
        row["category"] = "structure_or_deployable" if objects_only else category
        category = row["category"]
        groups[category].append(row)
    chosen = []
    seen = set()
    while len(chosen) < count and any(groups.values()):
        for category in sorted(groups):
            if not groups[category] or len(chosen) >= count:
                continue
            row = groups[category].pop(0)
            key = (
                row["snapshot"].get("victim_corporation_id"),
                row["snapshot"].get("killer_corporation_id"),
                row["snapshot"].get("solar_system_id"),
                row["snapshot"].get("victim_ship_type_id"),
                row["snapshot"]["kill_datetime"][:13],
            )
            if key in seen:
                continue
            seen.add(key)
            chosen.append(row)
    return chosen


def rank(info, true_id):
    return next((c["rank"] for c in info["candidates"] if c["id"] == true_id), None)


def trial_position(case, hypotheses, width):
    def pilots(side):
        eligible = [
            c["id"]
            for c in hypotheses[side]["candidates"]
            if c["id"] is not None
            and (width > 9 or side == "victim" or c.get("pvp_priority"))
        ]
        return eligible[:width] + [None]

    choices = {
        "ids": [c["id"] for c in hypotheses["ids"]["candidates"][:20]],
        "victims": pilots("victim"),
        "attackers": pilots("attacker"),
    }
    plan = engine.combinations(choices, hypotheses)
    position = next(
        (
            n
            for n, (i, v, a, _) in enumerate(plan, 1)
            if (i, v, a) == (case["killmail_id"], case["victim"], case["attacker"])
        ),
        None,
    )
    return position, len(plan)


def evaluate(case, omit_capability=False):
    def inputs(side):
        values = decode(case["inputs"][side])
        if omit_capability:
            values["capable"] = set()
        return values

    hypotheses = {
        side: engine.pilot_candidates(**inputs(side)) for side in ("victim", "attacker")
    }
    hypotheses["ids"] = case["ids"]
    initial, initial_budget = trial_position(case, hypotheses, 9)
    broad, broad_budget = trial_position(case, hypotheses, 49)
    victim_input = decode(case["inputs"]["victim"])
    category = case["category"]
    if victim_input["category"] in engine.OBJECT_CATEGORIES:
        category = "structure_or_deployable"
    elif (
        category == "player_vs_player"
        and victim_input["snapshot"]["victim_ship_type_id"] in engine.CAPSULE_TYPES
    ):
        category = "pvp_capsule"
    return {
        "killmail_id": case["killmail_id"],
        "category": category,
        "victim_rank": rank(hypotheses["victim"], case["victim"]),
        "attacker_rank": rank(hypotheses["attacker"], case["attacker"]),
        "initial_trials": initial,
        "initial_budget": initial_budget,
        "broad_trials": broad,
        "broad_budget": broad_budget,
        "victim_top": hypotheses["victim"]["candidates"][:3],
        "attacker_top": hypotheses["attacker"]["candidates"][:3],
    }


def summarize(results):
    summary = {}
    for category in ["all"] + sorted({r["category"] for r in results}):
        group = (
            results
            if category == "all"
            else [r for r in results if r["category"] == category]
        )
        data = {"count": len(group)}
        for side in ("victim", "attacker"):
            data[side + "_top1"] = sum(r[side + "_rank"] == 1 for r in group)
            data[side + "_top5"] = sum(
                r[side + "_rank"] is not None and r[side + "_rank"] <= 5 for r in group
            )
            data[side + "_top10"] = sum(
                r[side + "_rank"] is not None and r[side + "_rank"] <= 10 for r in group
            )
            data[side + "_listed"] = sum(r[side + "_rank"] is not None for r in group)
        for plan in ("initial", "broad"):
            positions = sorted(
                r[plan + "_trials"] for r in group if r[plan + "_trials"] is not None
            )
            data[plan + "_covered"] = len(positions)
            data[plan + "_mean_trials_on_covered"] = (
                round(statistics.mean(positions), 2) if positions else None
            )
            data[plan + "_median_trials_on_covered"] = (
                statistics.median(positions) if positions else None
            )
            data[plan + "_p90_trials_on_covered"] = (
                positions[max(0, (9 * len(positions) + 9) // 10 - 1)]
                if positions
                else None
            )
            # Includes failed searches: the whole selected budget is spent on misses.
            data[plan + "_mean_spent_including_exhausted"] = (
                round(
                    statistics.mean(
                        r[plan + "_trials"]
                        if r[plan + "_trials"] is not None
                        else r[plan + "_budget"]
                        for r in group
                    ),
                    2,
                )
                if group
                else None
            )
        summary[category] = data
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", type=date.fromisoformat, default=date(2026, 8, 1))
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument(
        "--objects",
        action="store_true",
        help="Supplement the benchmark with structures/deployables.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", default="eveosint")
    parser.add_argument("--user", default="codex_ro")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay", type=Path)
    parser.add_argument(
        "--no-current-capability",
        action="store_true",
        help="Exclude current skill inference, which may itself have learned from the masked kill.",
    )
    args = parser.parse_args()
    if not 1 <= args.samples <= 500:
        parser.error("--samples must be between 1 and 500")
    if args.month.day != 1:
        parser.error("--month must be the first day of a month")
    if args.replay:
        cases = json.loads(args.replay.read_text())["cases"]
    else:
        password = getpass.getpass("Read-only PostgreSQL password: ")
        conn = psycopg2.connect(
            host=args.host,
            port=args.port,
            dbname=args.database,
            user=args.user,
            password=password,
        )
        del password
        conn.set_session(readonly=True)
        cases = []
        try:
            with conn:
                targets = sample(conn, args.month, args.samples, args.objects)
            original = evidence.pilot_candidates
            for index, target in enumerate(targets, 1):
                captured = {}

                def capture(*positional, **kwargs):
                    bound = inspect.signature(original).bind(*positional, **kwargs)
                    bound.apply_defaults()
                    captured[bound.arguments["role"]] = encode(dict(bound.arguments))
                    return original(*positional, **kwargs)

                start = time.monotonic()
                try:
                    with (
                        conn,
                        patch.object(evidence, "pilot_candidates", side_effect=capture),
                    ):
                        _, hypotheses = evidence.analyze_snapshot(
                            conn,
                            target["snapshot"],
                            exclude_kill_ids=[target["killmail_id"]],
                        )
                    case = {
                        **target,
                        "inputs": captured,
                        "ids": hypotheses["ids"],
                        "warnings": hypotheses["warnings"],
                    }
                    cases.append(case)
                    print(
                        json.dumps(
                            {
                                "progress": f"{index}/{len(targets)}",
                                "seconds": round(time.monotonic() - start, 2),
                                **{
                                    k: v
                                    for k, v in evaluate(case).items()
                                    if k not in ("victim_top", "attacker_top")
                                },
                            }
                        ),
                        flush=True,
                    )
                except psycopg2.Error as exc:
                    conn.rollback()
                    print(
                        json.dumps(
                            {
                                "progress": f"{index}/{len(targets)}",
                                "sql_error": exc.pgcode,
                            }
                        ),
                        flush=True,
                    )
        finally:
            conn.close()
    results = [evaluate(case, args.no_current_capability) for case in cases]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "algorithm_version": engine.VERSION,
        "method": "Known targets masked; no CCP calls. Selected known MER sample, not a representative population or calibrated probability.",
        "summary": summarize(results),
        "cases": cases,
        "results": results,
    }
    args.output.write_text(json.dumps(report, default=str, indent=2))
    print(
        json.dumps({"summary": report["summary"], "report": str(args.output)}),
        flush=True,
    )


if __name__ == "__main__":
    main()
