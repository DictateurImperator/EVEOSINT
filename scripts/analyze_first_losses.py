#!/usr/bin/env python3
"""Manual all-character study. Creates research tables; never modifies killmail sources."""

import argparse, json, logging, os, signal, sys
from datetime import date, datetime, UTC
from pathlib import Path

for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg2
from psycopg2.extras import Json, wait_select
from scripts.first_loss_study import database as data
from scripts.first_loss_study.core import VERSION

LOG = logging.getLogger("first_loss")
LOCK = 79130421


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-config", type=Path, default=Path.home() / "eveosint/config/db.json"
    )
    parser.add_argument(
        "--progress",
        type=Path,
        default=Path.home() / "eveosint/data/research/first_loss_progress.json",
    )
    parser.add_argument("--clusters", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--episode-minutes", type=int, default=30)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Replace research results only; source tables are unchanged.",
    )
    parser.add_argument("--schema-only", action="store_true")
    args = parser.parse_args()
    if (
        not 2 <= args.clusters <= 30
        or not 100 <= args.batch_size <= 20000
        or not 0 <= args.episode_minutes <= 120
    ):
        parser.error("Invalid clusters, batch size or episode window.")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    conn = writer = None
    run = None
    stopping = False
    state = {"pid": os.getpid(), "phase": "starting"}

    def progress(phase, **kwargs):
        state.update(phase=phase, updated_at=datetime.now(UTC).isoformat(), **kwargs)
        args.progress.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.progress.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(args.progress)
        LOG.info("%s %s", phase, json.dumps(kwargs))

    def stop(*_):
        nonlocal stopping
        stopping = True
        if conn:
            try:
                conn.cancel()
            except psycopg2.Error:
                pass

    def check_stop():
        if stopping:
            raise InterruptedError(
                "Stop requested; restart resumes committed checkpoints."
            )

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Python must receive SIGTERM while libpq waits for a long monthly query.
    # A Python wait callback makes SQL waiting interruptible by the stop handler.
    psycopg2.extensions.set_wait_callback(wait_select)
    try:
        # Fail early before touching research tables when optional scientific dependencies are absent.
        if not args.schema_only:
            try:
                import numpy
            except ImportError:
                raise RuntimeError(
                    "Install scripts/first_loss_study/requirements.txt in this job interpreter, then retry."
                )
        cfg = json.loads(args.db_config.read_text())

        def connect():
            return psycopg2.connect(
                dbname=cfg["db_name"],
                user=cfg["db_user"],
                password=cfg["db_password"],
                host=cfg["db_host"],
                port=cfg.get("db_port", 5432),
                application_name="first_loss_research",
            )

        conn = connect()
        with conn.cursor() as c:
            c.execute(
                "SET statement_timeout='15min'; SET work_mem='64MB'; SET max_parallel_workers_per_gather=0; SET timezone='UTC'"
            )
            c.execute("SELECT pg_try_advisory_lock(%s)", (LOCK,))
            if not c.fetchone()[0]:
                raise RuntimeError("Another first-loss study is running.")
            c.execute(data.SCHEMA.read_text())
        conn.commit()
        if args.schema_only:
            progress("schema_ready")
            return 0
        with conn.cursor() as c:
            if args.rebuild:
                c.execute("DELETE FROM research.first_loss_runs")
            c.execute(
                "SELECT id,version,status,cutoff,history_start,config FROM research.first_loss_runs ORDER BY id DESC LIMIT 1"
            )
            old = c.fetchone()
            if old and old[2] == "completed":
                raise ValueError(
                    "Study already completed. Use Rebuild to refresh sources and replace research results."
                )
            if old:
                run, version, status, cutoff, history_start, config = old
                if version != VERSION:
                    raise ValueError(
                        "Research code version changed; rebuild is required."
                    )
                partitions = [date.fromisoformat(m) for m in config["months"]]
                missing = [date.fromisoformat(m) for m in config["missing_days"]]
                args.clusters = config["clusters"]
                args.episode_minutes = config["episode_minutes"]
                data.verify_sources(conn, cutoff, config["source_fingerprint"])
            else:
                partitions, cutoff, history_start, missing = data.months(conn)
                config = {
                    "months": [m.isoformat() for m in partitions],
                    "missing_days": [m.isoformat() for m in missing],
                    "clusters": args.clusters,
                    "episode_minutes": args.episode_minutes,
                    "numpy_version": numpy.__version__,
                    "source_fingerprint": data.source_fingerprint(conn, cutoff),
                }
                c.execute(
                    "INSERT INTO research.first_loss_runs(version,status,cutoff,history_start,config) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                    (VERSION, "running", cutoff, history_start, Json(config)),
                )
                run = c.fetchone()[0]
            c.execute(
                "UPDATE research.first_loss_runs SET status='running',error=NULL,updated_at=now() WHERE id=%s",
                (run,),
            )
        conn.commit()
        state["run_id"] = run
        state["cutoff"] = cutoff.isoformat()
        state["total_months"] = len(partitions)
        for phase, function in [
            ("losses", data.collect_losses),
            ("groups", data.collect_groups),
            ("followup", data.collect_followup),
        ]:
            data.verify_sources(conn, cutoff, config["source_fingerprint"])
            for index, month in enumerate(partitions):
                check_stop()
                if data.completed(conn, run, phase, month):
                    continue
                progress(
                    phase,
                    month=month.isoformat(),
                    processed_months=index,
                    total_months=len(partitions),
                )
                params = (conn, run, month, cutoff) + (
                    (args.episode_minutes,) if phase == "followup" else ()
                )
                function(*params)
                check_stop()
                data.checkpoint(conn, run, phase, month)
            progress(phase, processed_months=len(partitions))
        roles, systems = data.load_sde(conn)
        conn.commit()
        for index, month in enumerate(partitions):
            check_stop()
            if data.completed(conn, run, "features", month):
                continue
            after = 0
            processed = 0
            while True:
                check_stop()
                batch = data.feature_batch(conn, run, month, after, args.batch_size)
                if not batch:
                    break
                attackers, items = data.loss_details(
                    conn, run, month, [r["character_id"] for r in batch]
                )
                output = []
                for row in batch:
                    f, o = data.build_features(
                        row,
                        attackers,
                        items,
                        roles,
                        systems,
                        cutoff,
                        missing,
                        history_start,
                    )
                    output.append((row["character_id"], f, o, row["loss_time"].year))
                data.store_features(conn, run, output)
                after = batch[-1]["character_id"]
                processed += len(batch)
                progress(
                    "features",
                    month=month.isoformat(),
                    processed_months=index,
                    month_players=processed,
                )
            data.checkpoint(conn, run, "features", month)
        from scripts.first_loss_study.model import train

        writer = connect()
        model, summary = train(
            conn, writer, run, args.clusters, progress, check_stop, args.batch_size
        )
        data.verify_sources(conn, cutoff, config["source_fingerprint"])
        with conn.cursor() as c:
            c.execute(
                "UPDATE research.first_loss_runs SET model=%s,summary=%s,status='completed',phase='completed',updated_at=now() WHERE id=%s",
                (Json(model), Json(summary), run),
            )
        conn.commit()
        progress("completed", players=summary["players"], month=None)
        return 0
    except Exception as exc:
        paused = stopping or isinstance(exc, InterruptedError)
        if conn:
            conn.rollback()
            if run:
                with conn.cursor() as c:
                    c.execute(
                        "UPDATE research.first_loss_runs SET status=%s,error=%s,updated_at=now() WHERE id=%s",
                        ("paused" if paused else "failed", str(exc), run),
                    )
                conn.commit()
        progress("paused" if paused else "failed", error=str(exc))
        if not paused:
            LOG.exception("FIRST_LOSS_FAILED")
        return 0 if paused else 1
    finally:
        if writer:
            writer.close()
        if conn:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
