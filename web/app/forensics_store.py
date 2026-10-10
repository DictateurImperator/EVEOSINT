"""Persistence and one-trial validation. No DDL, rawkm import or MER mutation."""

import json
import logging
import math
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from psycopg2.extras import Json, RealDictCursor

from .db import db
from .forensics_engine import (
    ranked_candidates,
    combinations,
    killmail_hash,
    matches_snapshot,
    normalize_choices,
)
from .forensics_evidence import analyze
from .forensics_forecast import forecast, EVIDENCE_FILTERS

from .entities import (
    _lookup_entity_names,
    _lookup_system_locations,
    _lookup_type_names,
    _resolved_entity_ref,
    _killmail_type_ref,
    _location_ref,
)

TABLES = (
    "forensics_cases",
    "forensics_attempts",
    "forensics_recovered",
    "forensics_esi_gate",
    "forensics_esi_requests",
    "forensics_analysis_runs",
    "forensics_refresh_queue",
)
CASE_LOCK = 4721190440000
logger = logging.getLogger(__name__)


class ForensicsError(ValueError):
    pass


@contextmanager
def connection():
    conn = db()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def execute(conn, sql, params=(), one=False):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        if cur.description:
            return (
                dict(cur.fetchone()) if one else [dict(row) for row in cur.fetchall()]
            )


def ready():
    with connection() as conn:
        return all(
            execute(conn, "SELECT to_regclass(%s) AS name", ("web." + name,), True)[
                "name"
            ]
            for name in TABLES
        )


def json_safe(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def get_case(conn, case_id):
    found = execute(
        conn, "SELECT * FROM web.forensics_cases WHERE id=%s", (int(case_id),)
    )
    if not found:
        raise ForensicsError("Investigation not found.")
    return found[0]


def lock_case(conn, case_id):
    if not execute(
        conn,
        "SELECT pg_try_advisory_xact_lock(%s) AS locked",
        (CASE_LOCK + int(case_id),),
        True,
    )["locked"]:
        raise ForensicsError(
            "This investigation is currently being validated. Retry after it finishes."
        )


def remaining_plan(conn, case, plan=None):
    plan = combinations(case["choices"], case["hypotheses"]) if plan is None else plan
    done = {
        (a["killmail_id"], a["hash"])
        for a in execute(
            conn,
            "SELECT killmail_id,hash FROM web.forensics_attempts WHERE case_id=%s",
            (case["id"],),
        )
    }
    return [
        item
        for item in plan
        if (
            item[0],
            killmail_hash(
                item[1],
                item[2],
                case["snapshot"]["victim_ship_type_id"],
                case["kill_datetime"],
            ),
        )
        not in done
    ]


def persist_forecast(conn, case, remaining=None):
    remaining = remaining_plan(conn, case) if remaining is None else remaining
    info = forecast(
        case["hypotheses"],
        case["choices"],
        remaining,
        recovered=case["status"] == "recovered",
        analysis_error=case.get("analysis_error"),
    )
    execute(
        conn,
        """UPDATE web.forensics_cases SET forecast=%s,expected_trials=%s,
        recovery_priority=%s,estimated_attempts=%s,analysis_version=%s WHERE id=%s""",
        (
            Json(info),
            info["expected_trials"],
            info["priority"],
            0 if case["status"] == "recovered" else len(remaining),
            case["hypotheses"].get("version", 0),
            case["id"],
        ),
    )
    case.update(
        forecast=info,
        expected_trials=info["expected_trials"],
        recovery_priority=info["priority"],
    )
    return info


def initial_choices(hypotheses):
    def pilots(side):
        return [
            c["id"]
            for c in hypotheses[side]["candidates"]
            if c["id"] is not None and (side == "victim" or c.get("pvp_priority"))
        ][:9] + [None]

    return {
        "ids": [c["id"] for c in hypotheses["ids"]["candidates"][:20]],
        "victims": pilots("victim"),
        "attackers": pilots("attacker"),
    }


def create_case(ref, user_id):
    with connection() as conn:
        existing = execute(
            conn,
            """SELECT * FROM web.forensics_cases WHERE kill_datetime=%s::timestamptz
            AND source_month=%s::date AND source_row=%s""",
            (ref["kill_datetime"], ref["source_month"], int(ref["source_row"])),
        )
        if existing:
            if existing[0].get("forecast") is None:
                persist_forecast(conn, existing[0])
                return json_safe(get_case(conn, existing[0]["id"]))
            return json_safe(existing[0])
        snapshot, hypotheses = analyze(conn, ref)

        choices = initial_choices(hypotheses)
        total = len(combinations(choices, hypotheses))
        found = execute(
            conn,
            """INSERT INTO web.forensics_cases(kill_datetime,source_month,source_row,snapshot,hypotheses,
                choices,estimated_attempts,status,created_by,choices_customized)
            VALUES(%s::timestamptz,%s::date,%s,%s,%s,%s,%s,%s,%s,FALSE)
            ON CONFLICT(kill_datetime,source_month,source_row) DO NOTHING RETURNING *""",
            (
                snapshot["kill_datetime"],
                snapshot["source_month"],
                snapshot["source_row"],
                Json(snapshot),
                Json(hypotheses),
                Json(choices),
                total,
                "ready" if total else "needs_input",
                user_id,
            ),
        )
        if not found:
            found = execute(
                conn,
                """SELECT * FROM web.forensics_cases WHERE kill_datetime=%s::timestamptz
                AND source_month=%s::date AND source_row=%s""",
                (ref["kill_datetime"], ref["source_month"], int(ref["source_row"])),
            )
        persist_forecast(conn, found[0])
        return json_safe(get_case(conn, found[0]["id"]))


def case_displays(conn, cases):
    """Batch-load display metadata for saved cases; keep MER affiliations at kill time."""
    snapshots = [case["snapshot"] for case in cases]
    ships = _lookup_type_names(
        conn,
        [
            s.get(side + "_ship_type_id")
            for s in snapshots
            for side in ("victim", "killer")
        ],
    )
    corps = _lookup_entity_names(
        conn,
        "corporation",
        [
            s.get(side + "_corporation_id")
            for s in snapshots
            for side in ("victim", "killer")
        ],
    )
    alliances = _lookup_entity_names(
        conn,
        "alliance",
        [
            s.get(side + "_alliance_id")
            for s in snapshots
            for side in ("victim", "killer")
        ],
    )
    locations = _lookup_system_locations(
        conn, [s.get("solar_system_id") for s in snapshots]
    )
    for case in cases:
        s = case["snapshot"]
        display = {
            "location": locations.get(s.get("solar_system_id"))
            or {
                "system": _location_ref(
                    "system", s.get("solar_system_id"), s.get("solar_system_name")
                ),
                "constellation": None,
                "region": None,
            }
        }
        for side in ("victim", "killer"):
            ship_id = s.get(side + "_ship_type_id")
            display[side + "_ship"] = _killmail_type_ref(
                ship_id, ships.get(ship_id) or s.get(side + "_ship_type_name"), "ship"
            )
            for kind, lookup in (("corporation", corps), ("alliance", alliances)):
                entity_id = s.get(side + "_" + kind + "_id")
                display[side + "_" + kind] = _resolved_entity_ref(
                    kind,
                    entity_id,
                    s.get(side + "_" + kind + "_name")
                    or (f"{kind.title()} {entity_id}" if entity_id else None),
                    lookup,
                )
        case["display"] = display
    return cases


def list_cases(
    sort="attempts",
    page=1,
    recovered=False,
    assessment="",
    evidence=(),
    evidence_role="either",
):
    actionable = "CASE WHEN status='ready' AND estimated_attempts>0 THEN 0 ELSE 1 END"
    order = {
        "attempts": actionable + ",estimated_attempts ASC,id ASC",
        "estimate": actionable + ",expected_trials ASC NULLS LAST,id ASC",
        "priority": actionable
        + ",recovery_priority DESC NULLS LAST,expected_trials ASC NULLS LAST,id ASC",
        "date": "kill_datetime DESC,id DESC",
    }.get(sort, actionable + ",estimated_attempts ASC,id ASC")
    if assessment not in ("", "strong", "moderate", "weak", "blocked", "exhausted"):
        raise ForensicsError("Invalid recovery assessment filter.")
    if evidence_role not in ("either", "victim", "attacker") or any(
        key not in EVIDENCE_FILTERS for key in evidence
    ):
        raise ForensicsError("Invalid evidence filter.")
    context_flags = [key for key in evidence if EVIDENCE_FILTERS[key][1] == "context"]
    pilot_flags = [key for key in evidence if EVIDENCE_FILTERS[key][1] == "pilot"]
    extra = ""
    params = [recovered, assessment, assessment]
    if context_flags:
        extra += " AND c.forecast->'context_flags' @> %s::jsonb"
        params.append(Json(context_flags))
    if pilot_flags:
        sides = (
            ("victim", "attacker") if evidence_role == "either" else (evidence_role,)
        )
        predicates = []
        for side in sides:
            predicates.append("c.forecast->'candidate_evidence' @> %s::jsonb")
            params.append(Json({side: [{"flags": pilot_flags}]}))
        extra += " AND (" + " OR ".join(predicates) + ")"
    params.append((max(1, int(page)) - 1) * 50)
    with connection() as conn:
        result = execute(
            conn,
            f"""SELECT c.*,r.killmail_id AS recovered_id,r.hash AS recovered_hash,
            EXISTS(SELECT 1 FROM web.forensics_refresh_queue q WHERE q.case_id=c.id) AS refresh_pending,
            (SELECT COUNT(*) FROM web.forensics_attempts a WHERE a.case_id=c.id) AS attempted
            FROM web.forensics_cases c LEFT JOIN web.forensics_recovered r ON r.killmail_id=c.recovered_killmail_id
            WHERE (c.status='recovered')=%s AND (%s='' OR c.forecast->>'assessment'=%s)
            {extra} ORDER BY {order} LIMIT 50 OFFSET %s""",
            params,
        )
        return json_safe(case_displays(conn, result))


def _save_choices(conn, case, choices):
    case_id = case["id"]
    if case["status"] == "recovered":
        raise ForensicsError("A confirmed killmail cannot be changed.")
    for key, side in (("ids", "ids"), ("victims", "victim"), ("attackers", "attacker")):
        candidates = case["hypotheses"][side]["candidates"]
        known = {c["id"] for c in candidates}
        for candidate in choices[key]:
            if candidate not in known:
                candidates.append(
                    {
                        "id": candidate,
                        "name": str(candidate)
                        if key == "ids"
                        else (
                            f"Character {candidate}" if candidate else "No character"
                        ),
                        "weight": 1,
                        "evidence": {"manual": True},
                        "reasons": [
                            "Manually selected candidate; no automatic supporting evidence."
                        ],
                    }
                )
                known.add(candidate)
        case["hypotheses"][side]["candidates"] = ranked_candidates(candidates)
    plan = combinations(choices, case["hypotheses"])
    case["choices_customized"] = (
        case.get("choices_customized", True) or choices != case["choices"]
    )
    case["choices"] = choices
    remaining = remaining_plan(conn, case, plan)
    case["status"] = "ready" if remaining else ("exhausted" if plan else "needs_input")
    execute(
        conn,
        """UPDATE web.forensics_cases SET hypotheses=%s,choices=%s,estimated_attempts=%s,
        plan_cursor=0,status=%s,choices_customized=%s,updated_at=NOW() WHERE id=%s""",
        (
            Json(case["hypotheses"]),
            Json(choices),
            len(remaining),
            case["status"],
            case["choices_customized"],
            case_id,
        ),
    )
    persist_forecast(conn, case, remaining)
    return json_safe(get_case(conn, case_id))


def save_choices(case_id, choices):
    choices = normalize_choices(choices)
    with connection() as conn:
        lock_case(conn, case_id)
        return _save_choices(conn, get_case(conn, case_id), choices)


def refresh_case(case_id):
    with connection() as conn:
        lock_case(conn, case_id)
        case = get_case(conn, case_id)
        if case["status"] == "recovered":
            raise ForensicsError("This killmail is already confirmed.")
        snapshot, hypotheses = analyze(conn, case)
        execute(
            conn,
            "UPDATE web.forensics_cases SET snapshot=%s,hypotheses=%s,analysis_error=NULL,updated_at=NOW() WHERE id=%s",
            (Json(snapshot), Json(hypotheses), case_id),
        )
        # Preserve explicit user choices and attempted combinations.
        case.update(snapshot=snapshot, hypotheses=hypotheses, analysis_error=None)
        choices = (
            case["choices"]
            if case.get("choices_customized", True)
            else initial_choices(hypotheses)
        )
        if not case.get("choices_customized", True):
            case["choices"] = choices
        return _save_choices(conn, case, choices)


def request_budget():
    """Local sliding budget and the last CCP report, without an extra ESI call."""
    with connection() as conn:
        used = execute(
            conn,
            """SELECT COALESCE(SUM(cost),0) AS used,
            EXTRACT(EPOCH FROM MIN(reserved_at)+INTERVAL '15 minutes'-NOW()) AS next_refill
            FROM web.forensics_esi_requests WHERE reserved_at>NOW()-INTERVAL '15 minutes'""",
            one=True,
        )
        gate = execute(
            conn,
            """SELECT to_jsonb(g)->'rate_limit' AS reported,
            GREATEST(0,EXTRACT(EPOCH FROM blocked_until-NOW())) AS wait
            FROM web.forensics_esi_gate g WHERE singleton=TRUE""",
            one=True,
        )
        return {
            "limit": 3300,
            "used": int(used["used"]),
            "remaining": max(0, 3300 - int(used["used"])),
            "next_refill_seconds": math.ceil(used["next_refill"] or 0),
            "wait_seconds": math.ceil(gate["wait"]),
            "reported": gate["reported"],
        }


def reserve_request():
    """One global PostgreSQL gate across web workers, plus a sliding token ledger."""
    with connection() as conn:
        gate = execute(
            conn,
            """SELECT GREATEST(0,EXTRACT(EPOCH FROM blocked_until-NOW())) AS wait
            FROM web.forensics_esi_gate WHERE singleton=TRUE FOR UPDATE""",
            one=True,
        )
        if gate["wait"] > 0:
            return None, max(1, math.ceil(gate["wait"]))
        budget = execute(
            conn,
            """SELECT COALESCE(SUM(cost),0) AS used,
            EXTRACT(EPOCH FROM MIN(reserved_at)+INTERVAL '15 minutes'-NOW()) AS wait
            FROM web.forensics_esi_requests WHERE reserved_at>NOW()-INTERVAL '15 minutes' """,
            one=True,
        )
        if budget["used"] + 5 > 3300:
            return None, max(1, math.ceil(budget["wait"] or 900))
        execute(
            conn,
            "DELETE FROM web.forensics_esi_requests WHERE reserved_at<NOW()-INTERVAL '1 day'",
        )
        request = execute(
            conn,
            "INSERT INTO web.forensics_esi_requests DEFAULT VALUES RETURNING id",
            one=True,
        )
        execute(
            conn,
            "UPDATE web.forensics_esi_gate SET blocked_until=NOW()+INTERVAL '2 seconds' WHERE singleton=TRUE",
        )
        return request["id"], 0


def retry_seconds(headers, status):
    headers = {str(k).lower(): str(v) for k, v in headers.items()}
    delay = 0
    try:
        retry = headers.get("retry-after", "0")
        try:
            delay = max(0, math.ceil(float(retry)))
        except ValueError:
            delay = max(
                0,
                math.ceil(
                    (
                        parsedate_to_datetime(retry) - datetime.now(timezone.utc)
                    ).total_seconds()
                ),
            )
    except (ValueError, TypeError, OverflowError):
        delay = 900
    if status in (420, 429):
        delay = max(
            delay, 1 if headers.get("retry-after") else (60 if status == 420 else 900)
        )
    try:
        if int(headers.get("x-esi-error-limit-remain", "100")) <= 10:
            delay = max(delay, int(headers.get("x-esi-error-limit-reset", "60")) + 1)
        if int(headers.get("x-ratelimit-remaining", "3600")) < 10:
            # Fail closed for the shared IP bucket. Unknown windows get an hour.
            window = headers.get("x-ratelimit-limit", "3600/15m").split("/")[-1]
            seconds = float(window[:-1]) * (3600 if window.endswith("h") else 60)
            delay = max(delay, math.ceil(seconds))
    except (ValueError, TypeError):
        delay = max(delay, 3600)
    return delay


def finish_request(request_id, status, headers):
    delay = retry_seconds(headers, status)
    with connection() as conn:
        lower_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        gate_columns = execute(
            conn,
            "SELECT to_jsonb(g) AS fields FROM web.forensics_esi_gate g WHERE singleton=TRUE",
            one=True,
        )["fields"]
        if "rate_limit" in gate_columns and "x-ratelimit-remaining" in lower_headers:
            try:
                reported = {
                    "remaining": max(0, int(lower_headers["x-ratelimit-remaining"])),
                    "limit": lower_headers.get("x-ratelimit-limit"),
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                }
            except (ValueError, TypeError):
                reported = None
            execute(
                conn,
                "UPDATE web.forensics_esi_gate SET rate_limit=%s WHERE singleton=TRUE",
                (Json(reported),),
            )
        cost = (
            2
            if 200 <= status < 300
            else (
                1
                if 300 <= status < 400
                else (5 if 400 <= status < 500 and status not in (420, 429) else 0)
            )
        )
        try:
            reported_cost = int(lower_headers.get("x-ratelimit-used", cost))
            if 0 <= reported_cost <= 3300:
                cost = reported_cost
        except (ValueError, TypeError):
            pass
        execute(
            conn,
            "UPDATE web.forensics_esi_requests SET cost=%s WHERE id=%s",
            (cost, request_id),
        )
        if delay:
            execute(
                conn,
                "UPDATE web.forensics_esi_gate SET blocked_until=GREATEST(blocked_until,NOW()+%s*INTERVAL '1 second') WHERE singleton=TRUE",
                (delay,),
            )
    return delay


def fetch_ccp(kill_id, hash_value):
    url = f"https://esi.evetech.net/killmails/{kill_id}/{hash_value}/"
    request = Request(
        url,
        headers={
            "User-Agent": "EVEOSINT-Killmail-Forensics/1.0 (+https://github.com/DictateurImperator/EVEOSINT)",
            "Accept": "application/json",
            "X-Compatibility-Date": "2025-11-06",
        },
    )
    try:
        with urlopen(request, timeout=12) as response:
            headers = dict(response.headers)
            try:
                payload = json.loads(response.read(4 * 1024 * 1024))
            except (ValueError, UnicodeError):
                return 503, headers, {}
            return response.status, headers, payload
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read(8192))
        except (ValueError, UnicodeError):
            payload = {}
        return exc.code, dict(exc.headers), payload if isinstance(payload, dict) else {}
    except (URLError, TimeoutError, OSError, ValueError):
        return 503, {}, {}


def validate_next(case_id, user_id):
    # Session lock spans committed writes and HTTP without holding row locks or
    # leaving a transaction open while waiting for CCP.
    with connection() as conn:
        locked = execute(
            conn,
            "SELECT pg_try_advisory_lock(%s) AS locked",
            (CASE_LOCK + int(case_id),),
            True,
        )["locked"]
        conn.commit()
        if not locked:
            raise ForensicsError("This investigation is already being validated.")
        try:
            case = get_case(conn, case_id)
            conn.commit()
            if case["status"] == "recovered":
                return {"done": True, "recovered": True}
            plan = combinations(case["choices"], case["hypotheses"])
            cursor = case["plan_cursor"] - 1
            for cursor in range(
                case["plan_cursor"], min(len(plan), case["plan_cursor"] + 20)
            ):
                kill_id, victim, attacker, score = plan[cursor]
                hash_value = killmail_hash(
                    victim,
                    attacker,
                    case["snapshot"]["victim_ship_type_id"],
                    case["kill_datetime"],
                )
                cached = execute(
                    conn,
                    """SELECT case_id,outcome FROM web.forensics_attempts
                    WHERE killmail_id=%s AND hash=%s AND (case_id=%s OR outcome='invalid')
                    ORDER BY (case_id=%s) DESC LIMIT 1""",
                    (kill_id, hash_value, case_id, case_id),
                )
                recovered = execute(
                    conn,
                    "SELECT payload FROM web.forensics_recovered WHERE killmail_id=%s AND hash=%s",
                    (kill_id, hash_value),
                )
                conn.commit()
                sent = False
                payload = None
                if cached and cached[0]["case_id"] == case_id:
                    execute(
                        conn,
                        "UPDATE web.forensics_cases SET plan_cursor=%s WHERE id=%s",
                        (cursor + 1, case_id),
                    )
                    conn.commit()
                    continue
                if recovered:
                    payload = recovered[0]["payload"]
                    outcome = (
                        "recovered"
                        if matches_snapshot(
                            payload, case["snapshot"], kill_id, victim, attacker
                        )
                        else "mismatch"
                    )
                elif cached:
                    outcome = cached[0]["outcome"]
                else:
                    request_id, wait = reserve_request()
                    if request_id is None:
                        return {
                            "done": False,
                            "retry_after": wait,
                            "message": "Waiting for the shared CCP request budget.",
                        }
                    status, headers, payload = fetch_ccp(kill_id, hash_value)
                    delay = finish_request(request_id, status, headers)
                    sent = True
                    if status == 200:
                        outcome = (
                            "recovered"
                            if matches_snapshot(
                                payload, case["snapshot"], kill_id, victim, attacker
                            )
                            else "mismatch"
                        )
                    elif status == 404 or (
                        status == 403
                        and "hash" in str(payload.get("error", "")).lower()
                    ):
                        outcome = "invalid"
                    else:
                        return {
                            "done": False,
                            "paused": status in (400, 401, 403, 422),
                            "retry_after": max(delay, 30),
                            "message": f"CCP returned {status}; this hypothesis remains untested.",
                        }
                execute(
                    conn,
                    """INSERT INTO web.forensics_attempts(case_id,killmail_id,hash,victim_id,attacker_id,score,outcome)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (case_id, kill_id, hash_value, victim, attacker, score, outcome),
                )
                if outcome == "recovered" and payload is not None:
                    execute(
                        conn,
                        """INSERT INTO web.forensics_recovered(killmail_id,hash,case_id,payload,confirmed_by)
                        VALUES(%s,%s,%s,%s,%s) ON CONFLICT(killmail_id) DO NOTHING""",
                        (kill_id, hash_value, case_id, Json(payload), user_id),
                    )
                    execute(
                        conn,
                        "UPDATE web.forensics_cases SET status='recovered',recovered_killmail_id=%s,estimated_attempts=0,updated_at=NOW() WHERE id=%s",
                        (kill_id, case_id),
                    )
                    # Commit the CCP result before optional neighbor maintenance.
                    case["status"] = "recovered"
                    persist_forecast(conn, case, [])
                    conn.commit()
                    queued = []
                    refresh_error = False
                    try:
                        from .forensics_evidence import nearby_systems

                        systems = list(
                            nearby_systems(
                                conn, case["snapshot"].get("solar_system_id")
                            )
                        )
                        queued = execute(
                            conn,
                            """INSERT INTO web.forensics_refresh_queue(case_id,trigger_killmail_id)
                            SELECT id,%s FROM web.forensics_cases WHERE status<>'recovered'
                            AND kill_datetime BETWEEN %s::timestamptz - INTERVAL '1 hour' AND %s::timestamptz + INTERVAL '1 hour'
                            AND (snapshot->>'solar_system_id')::bigint=ANY(%s)
                            ON CONFLICT(case_id) DO UPDATE SET trigger_killmail_id=EXCLUDED.trigger_killmail_id,
                                queued_at=NOW(),failures=0,next_retry_at=NOW() RETURNING case_id""",
                            (
                                kill_id,
                                case["kill_datetime"],
                                case["kill_datetime"],
                                systems,
                            ),
                        )
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        refresh_error = True
                        logger.exception(
                            "Killmail %s confirmed; neighbor queueing failed.", kill_id
                        )
                    return {
                        "done": True,
                        "recovered": True,
                        "killmail_id": kill_id,
                        "hash": hash_value,
                        "neighbors_queued": len(queued),
                        "neighbor_refresh_error": refresh_error,
                    }
                execute(
                    conn,
                    """UPDATE web.forensics_cases SET plan_cursor=%s,estimated_attempts=GREATEST(0,estimated_attempts-1),
                    updated_at=NOW() WHERE id=%s""",
                    (cursor + 1, case_id),
                )
                persist_forecast(conn, case)
                conn.commit()
                if sent:
                    return {
                        "done": False,
                        "retry_after": 2,
                        "message": "Hypothesis checked. Continuing with the next combination.",
                    }
            if not plan or cursor + 1 >= len(plan):
                execute(
                    conn,
                    "UPDATE web.forensics_cases SET status=%s,estimated_attempts=0,updated_at=NOW() WHERE id=%s",
                    ("exhausted" if plan else "needs_input", case_id),
                )
                case["status"] = "exhausted" if plan else "needs_input"
                persist_forecast(conn, case, [])
                conn.commit()
                return {
                    "done": True,
                    "recovered": False,
                    "message": "All selected hypotheses have been checked. Add candidates to widen the search.",
                }
            return {"done": False, "retry_after": 1}
        finally:
            conn.rollback()
            execute(conn, "SELECT pg_advisory_unlock(%s)", (CASE_LOCK + int(case_id),))
            conn.commit()


def recovered_detail(kill_id):
    with connection() as conn:
        found = execute(
            conn,
            """SELECT r.*,c.snapshot,(SELECT COUNT(*) FROM web.forensics_attempts a WHERE a.case_id=r.case_id) AS hash_tests FROM web.forensics_recovered r
            JOIN web.forensics_cases c ON c.id=r.case_id WHERE killmail_id=%s""",
            (kill_id,),
        )
        if not found:
            raise ForensicsError("Recovered killmail not found.")
        return json_safe(found[0])
