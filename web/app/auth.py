import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi.responses import RedirectResponse

from .config import SESSION_COOKIE, SESSION_DAYS
from .db import db, t
from .security import pwd_context


def _hash_session_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def find_user_by_username(username):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, username, password_hash, is_active
                FROM {t('users')}
                WHERE username = %s
                """,
                (username,),
            )
            return cur.fetchone()


def create_session(user_id):
    token = secrets.token_urlsafe(48)
    token_hash = _hash_session_token(token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {t('sessions')}
                WHERE expires_at < NOW()
                """
            )

            cur.execute(
                f"""
                INSERT INTO {t('sessions')}
                    (token, user_id, expires_at)
                VALUES
                    (%s, %s, %s)
                """,
                (token_hash, user_id, expires_at),
            )

            cur.execute(
                f"""
                UPDATE {t('users')}
                SET last_login_at = NOW()
                WHERE id = %s
                """,
                (user_id,),
            )

        conn.commit()

    return token, expires_at


def delete_session(token):
    if not token:
        return

    token_hash = _hash_session_token(token)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {t('sessions')}
                WHERE token = %s
                """,
                (token_hash,),
            )

        conn.commit()


def get_user_roles(user_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT r.role_key
                FROM {t('user_roles')} ur
                JOIN {t('roles')} r ON r.id = ur.role_id
                WHERE ur.user_id = %s
                ORDER BY r.role_key
                """,
                (user_id,),
            )

            return [row[0] for row in cur.fetchall()]


def get_user_permissions(user_id):
    roles = get_user_roles(user_id)
    inherit_role_permissions = any(role_key != "user" for role_key in roles)

    with db() as conn:
        with conn.cursor() as cur:
            if inherit_role_permissions:
                cur.execute(
                    f"""
                    SELECT DISTINCT permission_key
                    FROM (
                        SELECT p.permission_key
                        FROM {t('user_roles')} ur
                        JOIN {t('role_permissions')} rp ON rp.role_id = ur.role_id
                        JOIN {t('permissions')} p ON p.id = rp.permission_id
                        WHERE ur.user_id = %s

                        UNION

                        SELECT p.permission_key
                        FROM {t('user_permissions')} up
                        JOIN {t('permissions')} p ON p.id = up.permission_id
                        WHERE up.user_id = %s
                    ) perms
                    """,
                    (user_id, user_id),
                )
            else:
                cur.execute(
                    f"""
                    SELECT DISTINCT p.permission_key
                    FROM {t('user_permissions')} up
                    JOIN {t('permissions')} p ON p.id = up.permission_id
                    WHERE up.user_id = %s
                    """,
                    (user_id,),
                )

            return {row[0] for row in cur.fetchall()}


def get_user_direct_permissions(user_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.permission_key
                FROM {t('user_permissions')} up
                JOIN {t('permissions')} p ON p.id = up.permission_id
                WHERE up.user_id = %s
                ORDER BY p.permission_key
                """,
                (user_id,),
            )

            return {row[0] for row in cur.fetchall()}


def get_current_user(request):
    token = request.cookies.get(SESSION_COOKIE)

    if not token:
        return None

    token_hash = _hash_session_token(token)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT u.id, u.username
                FROM {t('sessions')} s
                JOIN {t('users')} u ON u.id = s.user_id
                WHERE s.token = %s
                  AND s.expires_at > NOW()
                  AND u.is_active = TRUE
                """,
                (token_hash,),
            )

            row = cur.fetchone()

    if not row:
        return None

    user_id = row[0]

    return {
        "id": user_id,
        "username": row[1],
        "roles": get_user_roles(user_id),
        "permissions": get_user_permissions(user_id),
    }


def require_login(request):
    return get_current_user(request)


def has_permission(user, permission_key):
    return bool(user) and permission_key in user.get("permissions", set())


def require_permission_or_redirect(user, permission_key):
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if not has_permission(user, permission_key):
        return RedirectResponse(url="/home", status_code=302)

    return None


def verify_password(password, password_hash):
    try:
        return pwd_context.verify(password, password_hash)
    except ValueError:
        return False
