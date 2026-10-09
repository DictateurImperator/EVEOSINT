"""Resumable exhaustive MER analysis. Never submits hashes or changes MER/rawkm."""

import logging
import time
from datetime import date, datetime, timezone

from psycopg2.errors import QueryCanceled
from psycopg2.extras import Json

from . import forensics_store as store
from .forensics_engine import VERSION
from .forensics_forecast import forecast

logger = logging.getLogger(__name__)
ANALYSIS_LOCK = 472119045


def date_scope(date_from=None, date_to=None):
    bounds = [
        date.fromisoformat(str(value)) if value else None
        for value in (date_from, date_to)
    ]
    if all(bounds) and bounds[0] > bounds[1]:
        raise ValueError("The start date must be before the end date.")
    return bounds


CURRENT_ANALYSIS = """NOT EXISTS (
        SELECT 1 FROM web.forensics_cases c WHERE c.kill_datetime=m.kill_datetime
        AND c.source_month=m.source_month AND c.source_row=m.source_row
        AND (c.status='recovered' OR (c.forecast IS NOT NULL AND c.analysis_error IS NULL
        AND COALESCE(c.analysis_version,0)>=%s)))"""


def pending_query(bounds, cursor=None, scan_only=False):
    clauses = ["m.resolved_km IS NULL"]
    params = []
    if not scan_only:
        clauses.append(CURRENT_ANALYSIS)
        params.append(VERSION)
    if bounds[0]:
        clauses.append("m.kill_datetime >= (%s::date::timestamp AT TIME ZONE 'UTC')")
        params.append(bounds[0])
    if bounds[1]:
        clauses.append(
            "m.kill_datetime < ((%s::date + INTERVAL '1 day') AT TIME ZONE 'UTC')"
        )
        params.append(bounds[1])
    if cursor:
        clauses.append(
            "(m.kill_datetime,m.source_month,m.source_row)<(%s::timestamptz,%s::date,%s)"
        )
        params.extend(cursor)
    return " AND ".join(clauses), params


def _record_failure(ref, user_id, message):
    with store.connection() as conn:
        snapshot = store.execute(
            conn,
            """SELECT to_jsonb(m) AS snapshot FROM mer.killmails m
            WHERE kill_datetime=%s::timestamptz AND source_month=%s::date AND source_row=%s
            AND resolved_km IS NULL""",
            (ref["kill_datetime"], ref["source_month"], ref["source_row"]),
        )
        if not snapshot:
            return
        existing = store.execute(
            conn,
            """SELECT id FROM web.forensics_cases
            WHERE kill_datetime=%s::timestamptz AND source_month=%s::date AND source_row=%s""",
            (ref["kill_datetime"], ref["source_month"], ref["source_row"]),
        )
        if existing:
            try:
                store.lock_case(conn, existing[0]["id"])
            except store.ForensicsError:
                return
        empty = {"candidates": [], "truncated": False}
        hypotheses = {
            "version": VERSION,
            "ids": dict(empty),
            "victim": dict(empty),
            "attacker": dict(empty),
            "warnings": [message],
        }
        choices = {"ids": [], "victims": [], "attackers": []}
        info = forecast(hypotheses, choices, [], analysis_error=message)
        # Retain an existing plan, its evidence and all attempted hashes on error.
        store.execute(
            conn,
            """INSERT INTO web.forensics_cases(kill_datetime,source_month,source_row,
            snapshot,hypotheses,choices,status,created_by,analysis_error,forecast,recovery_priority,choices_customized)
            VALUES(%s::timestamptz,%s::date,%s,%s,%s,%s,'needs_input',%s,%s,%s,0,FALSE)
            ON CONFLICT(kill_datetime,source_month,source_row) DO UPDATE
            SET analysis_error=EXCLUDED.analysis_error,forecast=EXCLUDED.forecast,
                expected_trials=NULL,recovery_priority=0,updated_at=NOW()
            WHERE web.forensics_cases.status<>'recovered'""",
            (
                ref["kill_datetime"],
                ref["source_month"],
                ref["source_row"],
                Json(snapshot[0]["snapshot"]),
                Json(hypotheses),
                Json(choices),
                user_id,
                message,
                Json(info),
            ),
        )


def _analyze_reference(ref, user_id):
    with store.connection() as conn:
        existing = store.execute(
            conn,
            """SELECT * FROM web.forensics_cases
            WHERE kill_datetime=%s::timestamptz AND source_month=%s::date AND source_row=%s""",
            (ref["kill_datetime"], ref["source_month"], ref["source_row"]),
        )
    if not existing:
        store.create_case(ref, user_id)
        return
    case = existing[0]
    if case["status"] == "recovered":
        return
    restore_defaults = (
        bool(case.get("analysis_error"))
        and not any(case["choices"].values())
        and not case.get("choices_customized")
    )
    updated = store.refresh_case(case["id"])
    if restore_defaults:
        store.save_choices(case["id"], store.initial_choices(updated["hypotheses"]))


def analysis_status():
    with store.connection() as conn:
        result = store.execute(
            conn, "SELECT * FROM web.forensics_analysis_runs ORDER BY id DESC LIMIT 1"
        )
        pending = store.execute(
            conn, "SELECT COUNT(*) AS n FROM web.forensics_refresh_queue", one=True
        )["n"]
        summary = store.execute(
            conn,
            """SELECT COUNT(*) AS investigations,
            COUNT(*) FILTER (WHERE forecast->>'assessment'='strong') AS strong,
            COUNT(*) FILTER (WHERE forecast->>'assessment'='moderate') AS moderate,
            COUNT(*) FILTER (WHERE forecast->>'assessment'='weak') AS weak,
            COUNT(*) FILTER (WHERE forecast->>'assessment' IN ('blocked','exhausted')) AS blocked,
            COUNT(*) FILTER (WHERE status='recovered') AS recovered
            FROM web.forensics_cases""",
            one=True,
        )
        run = result[0] if result else None
        if run and run["status"] == "running":
            locked = store.execute(
                conn,
                """SELECT EXISTS(SELECT 1 FROM pg_locks
                WHERE locktype='advisory' AND classid=0 AND objid=%s AND granted) AS held""",
                (ANALYSIS_LOCK,),
                True,
            )["held"]
            if not locked:
                run["status"] = "interrupted"
        if run:
            elapsed = max(
                1,
                (
                    (run["finished_at"] or datetime.now(timezone.utc))
                    - run["started_at"]
                ).total_seconds(),
            )
            done = run["analyzed"] + run["failed"]
            run["remaining_seconds"] = (
                round(max(0, run["total"] - done) * elapsed / done)
                if run["total"] is not None and done
                else None
            )
    return store.json_safe({"run": run, "summary": summary, "pending_refresh": pending})


def stop_analysis():
    with store.connection() as conn:
        store.execute(
            conn,
            """UPDATE web.forensics_analysis_runs SET stop_requested=TRUE
            WHERE id=(SELECT id FROM web.forensics_analysis_runs WHERE status='running' ORDER BY id DESC LIMIT 1)""",
        )
    return {
        "message": "Stop requested. The current analysis can finish; saved estimates are kept."
    }


def drain_refresh_queue(control, run_id, user_id, should_stop):
    queue = store.execute(
        control,
        """SELECT q.*,c.kill_datetime,c.source_month,c.source_row,c.status
        FROM web.forensics_refresh_queue q JOIN web.forensics_cases c ON c.id=q.case_id
        WHERE q.next_retry_at<=NOW() ORDER BY q.queued_at,q.case_id LIMIT 20""",
    )
    control.commit()
    for item in queue:
        state = store.execute(
            control,
            "SELECT stop_requested FROM web.forensics_analysis_runs WHERE id=%s",
            (run_id,),
            True,
        )
        control.commit()
        if should_stop() or state["stop_requested"]:
            return
        try:
            if item["status"] != "recovered":
                ref = store.json_safe(
                    {
                        k: item[k]
                        for k in ("kill_datetime", "source_month", "source_row")
                    }
                )
                _analyze_reference(ref, user_id)
            store.execute(
                control,
                "DELETE FROM web.forensics_refresh_queue WHERE case_id=%s AND queued_at=%s",
                (item["case_id"], item["queued_at"]),
            )
            store.execute(
                control,
                "UPDATE web.forensics_analysis_runs SET refreshed=refreshed+1,updated_at=NOW() WHERE id=%s",
                (run_id,),
            )
            control.commit()
        except Exception:
            logger.exception(
                "Neighbor refresh failed for investigation %s", item["case_id"]
            )
            control.rollback()
            store.execute(
                control,
                """UPDATE web.forensics_refresh_queue SET failures=failures+1,
                next_retry_at=NOW()+INTERVAL '5 minutes' WHERE case_id=%s AND queued_at=%s""",
                (item["case_id"], item["queued_at"]),
            )
            control.commit()


def run_analysis(
    user_id, date_from=None, date_to=None, should_stop=lambda: False, refresh_only=False
):
    bounds = date_scope(date_from, date_to)
    if not store.ready():
        raise ValueError("Run the Forensics table setup/upgrade job first.")
    with store.connection() as control:
        held = store.execute(
            control, "SELECT pg_try_advisory_lock(%s) AS held", (ANALYSIS_LOCK,), True
        )["held"]
        control.commit()
        if not held:
            raise ValueError("A global hidden-kill analysis is already running.")
        run_id = None
        analyzed = failed = 0
        try:
            store.execute(
                control,
                """UPDATE web.forensics_analysis_runs SET status='stopped',finished_at=NOW(),
                message='Interrupted worker; saved estimates will be reused.' WHERE status='running'""",
            )
            run_id = store.execute(
                control,
                """INSERT INTO web.forensics_analysis_runs(status,date_from,date_to,created_by,refresh_only)
                VALUES('running',%s,%s,%s,%s) RETURNING id""",
                (*bounds, user_id, refresh_only),
                True,
            )["id"]
            control.commit()
            where, params = pending_query(bounds)
            total = None
            try:
                store.execute(control, "SET LOCAL statement_timeout='30s'")
                total = (
                    0
                    if refresh_only
                    else store.execute(
                        control,
                        "SELECT COUNT(*) AS n FROM mer.killmails m WHERE " + where,
                        params,
                        True,
                    )["n"]
                )
            except QueryCanceled:
                control.rollback()
            store.execute(
                control,
                "UPDATE web.forensics_analysis_runs SET total=%s,updated_at=NOW() WHERE id=%s",
                (total, run_id),
            )
            control.commit()
            cursor = None
            final_status = "complete"
            while True:
                state = store.execute(
                    control,
                    "SELECT stop_requested FROM web.forensics_analysis_runs WHERE id=%s",
                    (run_id,),
                    True,
                )
                control.commit()
                if should_stop() or state["stop_requested"]:
                    final_status = "stopped"
                    break
                drain_refresh_queue(control, run_id, user_id, should_stop)
                if refresh_only:
                    available = store.execute(
                        control,
                        "SELECT COUNT(*) AS n FROM web.forensics_refresh_queue WHERE next_retry_at<=NOW()",
                        one=True,
                    )["n"]
                    control.commit()
                    if available:
                        continue
                    break
                where, params = pending_query(bounds, cursor, scan_only=True)
                store.execute(control, "SET LOCAL statement_timeout='30s'")
                refs = store.execute(
                    control,
                    """SELECT m.kill_datetime,m.source_month,m.source_row,"""
                    + CURRENT_ANALYSIS
                    + " AS requires_analysis FROM mer.killmails m WHERE "
                    + where
                    + " ORDER BY m.kill_datetime DESC,m.source_month DESC,m.source_row DESC LIMIT 100",
                    [VERSION] + params,
                )
                control.commit()
                if not refs:
                    drain_refresh_queue(control, run_id, user_id, should_stop)
                    available = store.execute(
                        control,
                        "SELECT COUNT(*) AS n FROM web.forensics_refresh_queue WHERE next_retry_at<=NOW()",
                        one=True,
                    )["n"]
                    control.commit()
                    if available:
                        continue
                    break
                for raw_ref in refs:
                    cursor = (
                        raw_ref["kill_datetime"],
                        raw_ref["source_month"],
                        raw_ref["source_row"],
                    )
                    if not raw_ref.pop("requires_analysis"):
                        continue
                    state = store.execute(
                        control,
                        "SELECT stop_requested FROM web.forensics_analysis_runs WHERE id=%s",
                        (run_id,),
                        True,
                    )
                    control.commit()
                    if should_stop() or state["stop_requested"]:
                        final_status = "stopped"
                        break
                    ref = store.json_safe(raw_ref)
                    try:
                        _analyze_reference(ref, user_id)
                        analyzed += 1
                    except Exception as exc:
                        logger.exception(
                            "Hidden MER analysis failed at %s / %s / %s",
                            ref["kill_datetime"],
                            ref["source_month"],
                            ref["source_row"],
                        )
                        message = (
                            "Evidence query timed out; refresh or run analysis again."
                            if isinstance(exc, QueryCanceled)
                            else "Evidence analysis failed; refresh or run analysis again. See the Admin Jobs log."
                        )
                        _record_failure(ref, user_id, message)
                        failed += 1
                    cursor = (
                        ref["kill_datetime"],
                        ref["source_month"],
                        ref["source_row"],
                    )
                    store.execute(
                        control,
                        """UPDATE web.forensics_analysis_runs SET analyzed=%s,failed=%s,
                        updated_at=NOW() WHERE id=%s""",
                        (analyzed, failed, run_id),
                    )
                    control.commit()
                    if (analyzed + failed) % 25 == 0:
                        drain_refresh_queue(control, run_id, user_id, should_stop)
                        print(
                            f"Hidden analysis: {analyzed} analyzed, {failed} failed, total {total if total is not None else 'unknown'}.",
                            flush=True,
                        )
                    # Yield between cases; no DB transaction is held while throttling.
                    time.sleep(0.05)
                if final_status == "stopped":
                    break
            store.execute(
                control,
                """UPDATE web.forensics_analysis_runs SET status=%s,analyzed=%s,failed=%s,
                updated_at=NOW(),finished_at=NOW(),message=%s WHERE id=%s""",
                (
                    final_status,
                    analyzed,
                    failed,
                    "Saved estimates are kept; running again skips current analyses and retries failures.",
                    run_id,
                ),
            )
            control.commit()
            print(
                f"Hidden analysis {final_status}: {analyzed} analyzed, {failed} failed.",
                flush=True,
            )
            return {"status": final_status, "analyzed": analyzed, "failed": failed}
        except BaseException:
            control.rollback()
            if run_id:
                store.execute(
                    control,
                    """UPDATE web.forensics_analysis_runs SET status='failed',finished_at=NOW(),
                    updated_at=NOW(),message='Worker interrupted. Run analysis again to resume.' WHERE id=%s""",
                    (run_id,),
                )
                control.commit()
            raise
        finally:
            control.rollback()
            store.execute(control, "SELECT pg_advisory_unlock(%s)", (ANALYSIS_LOCK,))
            control.commit()
