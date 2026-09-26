from .auth import get_user_direct_permissions
from .db import db, t
from .security import pwd_context


def list_admin_users():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    u.id,
                    u.username,
                    u.is_active,
                    u.created_at,
                    u.last_login_at,
                    COALESCE(string_agg(r.role_key, ', ' ORDER BY r.role_key), '') AS roles
                FROM {t('users')} u
                LEFT JOIN {t('user_roles')} ur ON ur.user_id = u.id
                LEFT JOIN {t('roles')} r ON r.id = ur.role_id
                GROUP BY u.id, u.username, u.is_active, u.created_at, u.last_login_at
                ORDER BY u.username
                """
            )
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "username": row[1],
            "is_active": row[2],
            "created_at": row[3],
            "last_login_at": row[4],
            "roles": row[5],
        }
        for row in rows
    ]


def list_roles():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT role_key, label FROM {t('roles')} ORDER BY role_key")
            rows = cur.fetchall()

    return [{"role_key": row[0], "label": row[1]} for row in rows]


def list_permission_groups():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT permission_key, label, description
                FROM {t('permissions')}
                ORDER BY permission_key
                """
            )
            rows = cur.fetchall()

    groups = {}
    for row in rows:
        permission_key = row[0]
        root = permission_key.split(".", 1)[0]
        group_key = "all" if root == "home" else root
        group_label = "ALL" if root == "home" else root.upper()

        if group_key not in groups:
            groups[group_key] = {
                "key": group_key,
                "label": group_label,
                "open": group_key == "all",
                "permissions": [],
            }

        groups[group_key]["permissions"].append({
            "permission_key": permission_key,
            "label": row[1],
            "description": row[2],
            "locked": permission_key == "home.view",
        })

    return sorted(groups.values(), key=lambda group: (0 if group["key"] == "all" else 1, group["label"]))


def _copy_role_permissions_to_user(cur, user_id, role_id):
    cur.execute(
        f"""
        DELETE FROM {t('user_permissions')}
        WHERE user_id = %s
        """,
        (user_id,),
    )

    cur.execute(
        f"""
        INSERT INTO {t('user_permissions')} (user_id, permission_id)
        SELECT %s, permission_id
        FROM {t('role_permissions')}
        WHERE role_id = %s
        ON CONFLICT DO NOTHING
        """,
        (user_id, role_id),
    )


def get_admin_user_detail(user_id):
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
            user_row = cur.fetchone()
            if not user_row:
                return None

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
            roles = [row[0] for row in cur.fetchall()]
            has_role_inheritance = any(role_key != "user" for role_key in roles)

            if has_role_inheritance:
                cur.execute(
                    f"""
                    SELECT DISTINCT p.permission_key, p.label, p.description
                    FROM (
                        SELECT rp.permission_id
                        FROM {t('user_roles')} ur
                        JOIN {t('role_permissions')} rp ON rp.role_id = ur.role_id
                        WHERE ur.user_id = %s

                        UNION

                        SELECT up.permission_id
                        FROM {t('user_permissions')} up
                        WHERE up.user_id = %s
                    ) effective
                    JOIN {t('permissions')} p ON p.id = effective.permission_id
                    ORDER BY p.permission_key
                    """,
                    (user_id, user_id),
                )
            else:
                cur.execute(
                    f"""
                    SELECT DISTINCT p.permission_key, p.label, p.description
                    FROM {t('user_permissions')} up
                    JOIN {t('permissions')} p ON p.id = up.permission_id
                    WHERE up.user_id = %s
                    ORDER BY p.permission_key
                    """,
                    (user_id,),
                )

            permissions = [
                {"permission_key": row[0], "label": row[1], "description": row[2]}
                for row in cur.fetchall()
            ]

    return {
        "id": user_row[0],
        "username": user_row[1],
        "is_active": user_row[2],
        "created_at": user_row[3],
        "last_login_at": user_row[4],
        "roles": roles,
        "permissions": permissions,
        "direct_permissions": get_user_direct_permissions(user_id),
        "has_role_inheritance": has_role_inheritance,
    }


def create_or_update_user(username, password, role_key):
    password_hash = pwd_context.hash(password)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {t('roles')} WHERE role_key = %s", (role_key,))
            role_row = cur.fetchone()
            if not role_row:
                raise ValueError("Rôle introuvable.")

            role_id = role_row[0]

            cur.execute(
                f"""
                SELECT r.role_key
                FROM {t('users')} u
                LEFT JOIN {t('user_roles')} ur ON ur.user_id = u.id
                LEFT JOIN {t('roles')} r ON r.id = ur.role_id
                WHERE u.username = %s
                LIMIT 1
                """,
                (username,),
            )
            previous_role_row = cur.fetchone()
            previous_role_key = previous_role_row[0] if previous_role_row else None

            cur.execute(
                f"""
                INSERT INTO {t('users')} (username, password_hash, is_active)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (username)
                DO UPDATE SET password_hash = EXCLUDED.password_hash, is_active = TRUE
                RETURNING id
                """,
                (username, password_hash),
            )
            user_id = cur.fetchone()[0]

            cur.execute(f"DELETE FROM {t('user_roles')} WHERE user_id = %s", (user_id,))
            cur.execute(
                f"INSERT INTO {t('user_roles')} (user_id, role_id) VALUES (%s, %s)",
                (user_id, role_id),
            )

            if role_key == "user" and previous_role_key != "user":
                _copy_role_permissions_to_user(cur, user_id, role_id)

        conn.commit()


def update_user_account(target_user_id, is_active, role_key):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {t('roles')} WHERE role_key = %s", (role_key,))
            role_row = cur.fetchone()
            if not role_row:
                raise ValueError("Rôle introuvable.")

            role_id = role_row[0]

            cur.execute(
                f"""
                SELECT r.role_key
                FROM {t('user_roles')} ur
                JOIN {t('roles')} r ON r.id = ur.role_id
                WHERE ur.user_id = %s
                ORDER BY r.role_key
                LIMIT 1
                """,
                (target_user_id,),
            )
            previous_role_row = cur.fetchone()
            previous_role_key = previous_role_row[0] if previous_role_row else None

            cur.execute(f"UPDATE {t('users')} SET is_active = %s WHERE id = %s", (is_active, target_user_id))
            cur.execute(f"DELETE FROM {t('user_roles')} WHERE user_id = %s", (target_user_id,))
            cur.execute(
                f"INSERT INTO {t('user_roles')} (user_id, role_id) VALUES (%s, %s)",
                (target_user_id, role_id),
            )

            if role_key == "user" and previous_role_key != "user":
                _copy_role_permissions_to_user(cur, target_user_id, role_id)

        conn.commit()


def update_user_direct_permissions(target_user_id, permission_keys):
    permission_keys = list(set(permission_keys))

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {t('user_permissions')} WHERE user_id = %s", (target_user_id,))
            if permission_keys:
                cur.execute(
                    f"""
                    INSERT INTO {t('user_permissions')} (user_id, permission_id)
                    SELECT %s, id
                    FROM {t('permissions')}
                    WHERE permission_key = ANY(%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (target_user_id, permission_keys),
                )
        conn.commit()


def reset_user_password(target_user_id, password):
    password_hash = pwd_context.hash(password)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {t('users')} SET password_hash = %s WHERE id = %s",
                (password_hash, target_user_id),
            )
            cur.execute(
                f"DELETE FROM {t('sessions')} WHERE user_id = %s",
                (target_user_id,),
            )
        conn.commit()


def count_admin_users():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(DISTINCT u.id)
                FROM {t('users')} u
                JOIN {t('user_roles')} ur ON ur.user_id = u.id
                JOIN {t('roles')} r ON r.id = ur.role_id
                WHERE r.role_key = 'admin'
                  AND u.is_active = TRUE
                """
            )
            return cur.fetchone()[0]


def is_user_admin(user_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT 1
                FROM {t('user_roles')} ur
                JOIN {t('roles')} r ON r.id = ur.role_id
                WHERE ur.user_id = %s
                  AND r.role_key = 'admin'
                LIMIT 1
                """,
                (user_id,),
            )
            return cur.fetchone() is not None


def delete_user_by_id(target_user_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {t('users')} WHERE id = %s", (target_user_id,))
        conn.commit()
