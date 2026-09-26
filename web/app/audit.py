import csv
import io
from datetime import date, datetime, time, timezone

from .db import db, t


PER_PAGE_DEFAULT = 50


def get_client_ip(request):
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return None


def audit_log(request, action, user_id=None, username=None, target_type=None, target_id=None, details=None):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {t('audit_logs')}
                        (user_id, username, action, target_type, target_id, details, ip_address, user_agent)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        username,
                        action,
                        target_type,
                        str(target_id) if target_id is not None else None,
                        details,
                        get_client_ip(request),
                        request.headers.get("user-agent"),
                    ),
                )
            conn.commit()
    except Exception:
        # L'audit ne doit pas casser l'action principale.
        pass


def normalize_page(page):
    if page < 1:
        return 1
    return page


def build_page(items, total, page, per_page):
    page = normalize_page(page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1 if page > 1 else 1,
        "next_page": page + 1 if page < total_pages else total_pages,
    }


def list_audit_users(page=1, per_page=PER_PAGE_DEFAULT):
    page = normalize_page(page)
    offset = (page - 1) * per_page

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {t('users')}")
            total = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT
                    u.id,
                    u.username,
                    COUNT(a.id) AS actions_count,
                    MAX(a.created_at) AS last_action_at,
                    (
                        SELECT a2.ip_address
                        FROM {t('audit_logs')} a2
                        WHERE a2.user_id = u.id
                        ORDER BY a2.created_at DESC
                        LIMIT 1
                    ) AS last_ip
                FROM {t('users')} u
                LEFT JOIN {t('audit_logs')} a ON a.user_id = u.id
                GROUP BY u.id, u.username
                ORDER BY MAX(a.created_at) DESC NULLS LAST, u.username ASC
                LIMIT %s OFFSET %s
                """,
                (per_page, offset),
            )
            rows = cur.fetchall()

    items = [
        {
            "id": row[0],
            "username": row[1],
            "actions_count": row[2],
            "last_action_at": row[3],
            "last_ip": row[4],
        }
        for row in rows
    ]

    return build_page(items, total, page, per_page)


def list_recent_actions(limit=50):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    created_at,
                    user_id,
                    username,
                    action,
                    target_type,
                    target_id,
                    details,
                    ip_address
                FROM {t('audit_logs')}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "created_at": row[1],
            "user_id": row[2],
            "username": row[3],
            "action": row[4],
            "target_type": row[5],
            "target_id": row[6],
            "details": row[7],
            "ip_address": row[8],
        }
        for row in rows
    ]


def list_audit_logs(limit=200):
    return list_recent_actions(limit=limit)


def get_audit_user(user_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, username, is_active, created_at, last_login_at
                FROM {t('users')}
                WHERE id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()

    if not row:
        return None

    return {
        "id": row[0],
        "username": row[1],
        "is_active": row[2],
        "created_at": row[3],
        "last_login_at": row[4],
    }


def list_user_actions(user_id, page=1, per_page=PER_PAGE_DEFAULT):
    page = normalize_page(page)
    offset = (page - 1) * per_page

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM {t('audit_logs')} WHERE user_id = %s",
                (user_id,),
            )
            total = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT
                    id,
                    created_at,
                    username,
                    action,
                    target_type,
                    target_id,
                    details,
                    ip_address
                FROM {t('audit_logs')}
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (user_id, per_page, offset),
            )
            rows = cur.fetchall()

    items = [
        {
            "id": row[0],
            "created_at": row[1],
            "username": row[2],
            "action": row[3],
            "target_type": row[4],
            "target_id": row[5],
            "details": row[6],
            "ip_address": row[7],
        }
        for row in rows
    ]

    return build_page(items, total, page, per_page)


def parse_since_date(raw_since):
    parsed = datetime.strptime(raw_since, "%Y-%m-%d").date()
    return datetime.combine(parsed, time.min, tzinfo=timezone.utc)


def get_user_actions_since(user_id, since_dt):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    created_at,
                    username,
                    action,
                    target_type,
                    target_id,
                    details,
                    ip_address,
                    user_agent
                FROM {t('audit_logs')}
                WHERE user_id = %s
                  AND created_at >= %s
                ORDER BY created_at DESC
                """,
                (user_id, since_dt),
            )
            rows = cur.fetchall()

    return [
        {
            "created_at": row[0],
            "username": row[1],
            "action": row[2],
            "target_type": row[3],
            "target_id": row[4],
            "details": row[5],
            "ip_address": row[6],
            "user_agent": row[7],
        }
        for row in rows
    ]


def _csv_safe(value):
    if value is None:
        return ""

    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text

    return text


def render_actions_csv(rows):
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "created_at",
        "username",
        "action",
        "target_type",
        "target_id",
        "details",
        "ip_address",
        "user_agent",
    ])

    for row in rows:
        writer.writerow([
            _csv_safe(row["created_at"].isoformat() if row["created_at"] else ""),
            _csv_safe(row["username"]),
            _csv_safe(row["action"]),
            _csv_safe(row["target_type"]),
            _csv_safe(row["target_id"]),
            _csv_safe(row["details"]),
            _csv_safe(row["ip_address"]),
            _csv_safe(row["user_agent"]),
        ])

    return output.getvalue()
