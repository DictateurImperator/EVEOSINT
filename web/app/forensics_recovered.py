"""Confirmed killboard filters and manual, recorded zKillboard publication."""

import gzip
import json
import math
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import forensics_store as store
from .entities import (
    _append_killmail_builder_clauses,
    _normalize_killmail_filters,
    _lookup_entity_names,
    _resolved_entity_ref,
    _table_exists,
    _format_isk,
)

PUBLISH_LOCK = 472119049

# Expose the confirmed payload in the same shape expected by shared killboard filters.
RECOVERED_SQL = """WITH km AS (
 SELECT r.*,c.snapshot,c.source_month,c.source_row,
 c.kill_datetime AS killmail_time,
 (r.payload->>'solar_system_id')::bigint AS solar_system_id,
 (r.payload->'victim'->>'ship_type_id')::bigint AS victim_ship_type_id,
 COALESCE((r.payload->'victim'->>'character_id')::bigint,0) AS victim_character_id,
 COALESCE((r.payload->'victim'->>'corporation_id')::bigint,0) AS victim_corporation_id,
 (r.payload->'victim'->>'alliance_id')::bigint AS victim_alliance_id
 FROM web.forensics_recovered r JOIN web.forensics_cases c ON c.id=r.case_id
), recovered_attackers AS (
 SELECT km.killmail_id,km.killmail_time,
 (a->>'character_id')::bigint AS character_id,
 (a->>'corporation_id')::bigint AS corporation_id,
 (a->>'alliance_id')::bigint AS alliance_id,
 (a->>'ship_type_id')::bigint AS ship_type_id
 FROM km CROSS JOIN LATERAL jsonb_array_elements(km.payload->'attackers') a
) """


def page(filters=None, page=1, publication=""):
    normalized = _normalize_killmail_filters(filters)
    if publication not in ("", "pending", "accepted", "failed", "uncertain"):
        raise ValueError("Invalid publication filter.")
    clauses, params = [], []
    if normalized["date_from"]:
        clauses.append("km.killmail_time >= %s::date")
        params.append(normalized["date_from"])
    if normalized["date_to"]:
        clauses.append("km.killmail_time < %s::date + INTERVAL '1 day'")
        params.append(normalized["date_to"])
    with store.connection() as conn:
        store.execute(conn, "SET LOCAL statement_timeout='15s'")
        publishing_ready = _table_exists(conn, "web", "forensics_zkill_submissions")
        _append_killmail_builder_clauses(
            conn, normalized, clauses, params, attacker_table_sql="recovered_attackers"
        )
        # Missing NPC/object fields must not remove rows from exclusion filters.
        clauses = [
            "NOT COALESCE(" + clause[5:-1] + ",FALSE)"
            if clause.startswith("NOT (") and clause.endswith(")")
            else "COALESCE(" + clause + ",FALSE)"
            for clause in clauses
        ]
        state = "COALESCE(p.status,'pending')" if publishing_ready else "'pending'"
        if publication:
            clauses.append(state + "=%s")
            params.append(publication)
        join = (
            "LEFT JOIN web.forensics_zkill_submissions p ON p.killmail_id=km.killmail_id"
            if publishing_ready
            else ""
        )
        params.append((max(1, int(page)) - 1) * 50)
        result = store.execute(
            conn,
            RECOVERED_SQL
            + f"SELECT km.*,{state} AS publication_status FROM km {join} WHERE "
            + (" AND ".join(clauses) or "TRUE")
            + " ORDER BY km.killmail_time DESC,km.killmail_id DESC LIMIT 51 OFFSET %s",
            params,
        )
        has_next = len(result) > 50
        cases = result[:50]
        char_ids = []
        for c in cases:
            victim = c["payload"]["victim"]
            killer = next(
                (a for a in c["payload"]["attackers"] if a.get("final_blow")), {}
            )
            c["victim"], c["killer"] = victim, killer
            c["snapshot"] = dict(c["snapshot"])
            c["snapshot"]["solar_system_id"] = c["solar_system_id"]
            for side, actor in (("victim", victim), ("killer", killer)):
                for field in ("ship_type_id", "corporation_id", "alliance_id"):
                    c["snapshot"][side + "_" + field] = actor.get(field)
                char_ids.append(actor.get("character_id"))
            value = c["snapshot"].get("ccp_isk_lost")
            c["mer_value"] = _format_isk(value) if value is not None else None
        store.case_displays(conn, cases)
        case_ids = [c["case_id"] for c in cases]
        trials = {
            row["case_id"]: row["n"]
            for row in store.execute(
                conn,
                "SELECT case_id,COUNT(*) AS n FROM web.forensics_attempts WHERE case_id=ANY(%s::bigint[]) GROUP BY case_id",
                (case_ids,),
            )
        }
        names = _lookup_entity_names(conn, "character", char_ids)
        for c in cases:
            c["hash_tests"] = trials.get(c["case_id"], 0)
            for side in ("victim", "killer"):
                character_id = c[side].get("character_id")
                c["display"][side + "_character"] = _resolved_entity_ref(
                    "character", character_id, f"Character {character_id}", names
                )
        return {
            "kills": cases,
            "has_next": has_next,
            "publishing_ready": publishing_ready,
        }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_zkill(kill_id, hash_value):
    request = Request(
        f"https://zkillboard.com/api/killmail/add/{int(kill_id)}/{hash_value}/",
        data=b"",
        method="POST",
        headers={
            "User-Agent": "EVEOSINT-Forensics/1.0 (+https://github.com/DictateurImperator/EVEOSINT)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            body = response.read(2 * 1024 * 1024)
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return response.status, dict(response.headers), json.loads(body)
    except HTTPError as exc:
        return exc.code, dict(exc.headers), {}
    except (URLError, TimeoutError, OSError, ValueError):
        return 0, {}, {}


def submit(kill_id, user_id):
    with store.connection() as conn:
        if not _table_exists(conn, "web", "forensics_zkill_submissions"):
            raise ValueError(
                "Run the Forensics table setup job after deploying this version."
            )
        held = store.execute(
            conn, "SELECT pg_try_advisory_lock(%s) AS held", (PUBLISH_LOCK,), True
        )["held"]
        if not held:
            return {
                "accepted": False,
                "retry_after": 2,
                "message": "Another zKillboard submission is in progress.",
            }
        try:
            kill = store.execute(
                conn,
                "SELECT killmail_id,hash FROM web.forensics_recovered WHERE killmail_id=%s",
                (kill_id,),
            )
            if not kill:
                raise ValueError(
                    "Only CCP-confirmed recovered killmails can be submitted."
                )
            previous = store.execute(
                conn,
                "SELECT status FROM web.forensics_zkill_submissions WHERE killmail_id=%s",
                (kill_id,),
            )
            if previous and previous[0]["status"] == "accepted":
                return {"accepted": True, "message": "Already accepted by zKillboard."}
            wait = store.execute(
                conn,
                "SELECT GREATEST(0,EXTRACT(EPOCH FROM MAX(next_retry_at)-NOW())) AS wait FROM web.forensics_zkill_submissions",
                one=True,
            )["wait"]
            if wait:
                return {
                    "accepted": False,
                    "retry_after": math.ceil(wait),
                    "message": "zKillboard submissions are cooling down. Please retry later.",
                }
            # Persist an uncertain reservation first: a crashed worker must not hammer zKill.
            store.execute(
                conn,
                """INSERT INTO web.forensics_zkill_submissions(killmail_id,status,submitted_by,next_retry_at)
                VALUES(%s,'uncertain',%s,NOW()+INTERVAL '2 minutes') ON CONFLICT(killmail_id)
                DO UPDATE SET status='uncertain',submitted_by=EXCLUDED.submitted_by,last_attempt_at=NOW(),next_retry_at=EXCLUDED.next_retry_at""",
                (kill_id, user_id),
            )
            conn.commit()
            status, headers, result = post_zkill(kill_id, kill[0]["hash"])
            accepted = (
                200 <= status < 300
                and isinstance(result, dict)
                and result.get("status") == "success"
            )
            outcome = (
                "accepted"
                if accepted
                else ("uncertain" if status in (0, 408) or 500 <= status else "failed")
            )
            delay = 2 if accepted else max(120, store.retry_seconds(headers, status))
            store.execute(
                conn,
                """UPDATE web.forensics_zkill_submissions SET status=%s,http_status=%s,
                accepted_at=CASE WHEN %s THEN NOW() ELSE NULL END,
                next_retry_at=NOW()+%s*INTERVAL '1 second' WHERE killmail_id=%s""",
                (outcome, status, accepted, delay, kill_id),
            )
            return {
                "accepted": accepted,
                "retry_after": 0 if accepted else delay,
                "message": "Accepted by zKillboard. Publication may take a moment."
                if accepted
                else (
                    "Submission result is uncertain. Check zKillboard before retrying."
                    if outcome == "uncertain"
                    else f"zKillboard did not accept the submission (HTTP {status}). Retry later."
                ),
            }
        finally:
            conn.commit()
            store.execute(conn, "SELECT pg_advisory_unlock(%s)", (PUBLISH_LOCK,))
