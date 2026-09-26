#!/usr/bin/env python3
import getpass
import json
from pathlib import Path

import psycopg2
from passlib.context import CryptContext


CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"

SCHEMA = "web"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def load_db_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


DB_CONFIG = load_db_config()


def db():
    return psycopg2.connect(
        dbname=DB_CONFIG["db_name"],
        user=DB_CONFIG["db_user"],
        password=DB_CONFIG["db_password"],
        host=DB_CONFIG["db_host"],
        port=DB_CONFIG["db_port"],
    )


def t(name):
    return f"{SCHEMA}.{name}"


def copy_role_permissions_to_user(cur, user_id, role_id):
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


def main():
    username = input("Username: ").strip()
    role_key = input("Role [admin/user]: ").strip().lower()
    password = getpass.getpass("Password: ")
    password_confirm = getpass.getpass("Confirm password: ")

    if not username:
        raise SystemExit("Username vide.")

    if role_key not in ("admin", "user"):
        raise SystemExit("Role invalide. Utilise admin ou user.")

    if password != password_confirm:
        raise SystemExit("Les mots de passe ne correspondent pas.")

    if len(password.encode("utf-8")) > 72:
        raise SystemExit("Mot de passe trop long : bcrypt limite à 72 bytes.")

    if len(password) < 12:
        raise SystemExit("Mot de passe trop court : minimum 12 caractères.")

    password_hash = pwd_context.hash(password)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")

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
                INSERT INTO {t('users')}
                    (username, password_hash, is_active)
                VALUES
                    (%s, %s, TRUE)
                ON CONFLICT (username)
                DO UPDATE SET
                    password_hash = EXCLUDED.password_hash,
                    is_active = TRUE
                RETURNING id
                """,
                (username, password_hash),
            )
            user_id = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT id
                FROM {t('roles')}
                WHERE role_key = %s
                """,
                (role_key,),
            )
            row = cur.fetchone()

            if not row:
                raise SystemExit(f"Role introuvable en DB: {role_key}. Lance setup_acl.py avant.")

            role_id = row[0]

            cur.execute(
                f"""
                DELETE FROM {t('user_roles')}
                WHERE user_id = %s
                """,
                (user_id,),
            )

            cur.execute(
                f"""
                INSERT INTO {t('user_roles')}
                    (user_id, role_id)
                VALUES
                    (%s, %s)
                """,
                (user_id, role_id),
            )

            if role_key == "user" and previous_role_key != "user":
                copy_role_permissions_to_user(cur, user_id, role_id)

        conn.commit()

    print(f"Utilisateur prêt: {username} / role={role_key}")


if __name__ == "__main__":
    main()
