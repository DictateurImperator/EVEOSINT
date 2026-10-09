#!/usr/bin/env python3
import json
from pathlib import Path

import psycopg2


CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"
SCHEMA = "web"


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


def main():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('roles')} (
                id BIGSERIAL PRIMARY KEY,
                role_key TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                is_system BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('permissions')} (
                id BIGSERIAL PRIMARY KEY,
                permission_key TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('user_roles')} (
                user_id BIGINT NOT NULL REFERENCES {t('users')}(id) ON DELETE CASCADE,
                role_id BIGINT NOT NULL REFERENCES {t('roles')}(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, role_id)
            );
            """)

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('role_permissions')} (
                role_id BIGINT NOT NULL REFERENCES {t('roles')}(id) ON DELETE CASCADE,
                permission_id BIGINT NOT NULL REFERENCES {t('permissions')}(id) ON DELETE CASCADE,
                PRIMARY KEY (role_id, permission_id)
            );
            """)

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('user_permissions')} (
                user_id BIGINT NOT NULL REFERENCES {t('users')}(id) ON DELETE CASCADE,
                permission_id BIGINT NOT NULL REFERENCES {t('permissions')}(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, permission_id)
            );
            """)

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('menu_items')} (
                id BIGSERIAL PRIMARY KEY,
                parent_id BIGINT REFERENCES {t('menu_items')}(id) ON DELETE CASCADE,
                menu_key TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                href TEXT NOT NULL,
                icon TEXT,
                permission_key TEXT REFERENCES {t('permissions')}(permission_key),
                sort_order INTEGER NOT NULL DEFAULT 100,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)

            cur.execute(f"""
            INSERT INTO {t('roles')} (role_key, label, is_system)
            VALUES
                ('admin', 'Administrator', TRUE),
                ('user', 'User', TRUE)
            ON CONFLICT (role_key) DO UPDATE SET
                label = EXCLUDED.label,
                is_system = EXCLUDED.is_system;
            """)

            cur.execute(f"""
            INSERT INTO {t('permissions')} (permission_key, label, description)
            VALUES
                ('home.view', 'View Home', 'Access home page'),

                ('superintel.view', 'View SuperINTEL', 'Access SuperINTEL dashboard'),

                ('entities.view', 'View EntitiesINTEL', 'Access EntitiesINTEL module'),

                ('admin.view', 'View Admin', 'Access admin module'),
                ('admin.users.view', 'View Users', 'View user accounts'),
                ('admin.users.create', 'Create Users', 'Create user accounts'),
                ('admin.users.delete', 'Delete Users', 'Delete user accounts'),
                ('admin.users.edit', 'Edit Users', 'Edit user accounts'),

                ('entities.coalition.admin', 'Admin Coalition', 'Create and manage coalition entities and membership rules'),

                ('admin.system.view', 'View System', 'View system status'),
                ('admin.nginx.manage', 'Manage Nginx', 'View and edit the EVEOSINT Nginx site configuration'),
                ('admin.jobs.view', 'View Jobs', 'View whitelisted jobs and logs'),
                ('admin.jobs.run', 'Run Jobs', 'Run whitelisted jobs'),
                ('admin.mer.view', 'View MER', 'View Monthly Economic Reports administration'),
                ('admin.killmail_forensics.dev', 'Dev · Killmail Forensics', 'Access the killmail hash reconstruction development workspace'),
                ('admin.audit.view', 'View Audit', 'View user action audit log')
            ON CONFLICT (permission_key) DO UPDATE SET
                label = EXCLUDED.label,
                description = EXCLUDED.description;
            """)

            cur.execute(f"""
            INSERT INTO {t('role_permissions')} (role_id, permission_id)
            SELECT r.id, p.id
            FROM {t('roles')} r
            CROSS JOIN {t('permissions')} p
            WHERE r.role_key = 'admin'
              AND p.permission_key <> 'admin.nginx.manage'
            ON CONFLICT DO NOTHING;
            """)

            cur.execute(f"""
            INSERT INTO {t('role_permissions')} (role_id, permission_id)
            SELECT r.id, p.id
            FROM {t('roles')} r
            JOIN {t('permissions')} p ON p.permission_key IN (
                'home.view',
                'superintel.view',
                'entities.view'
            )
            WHERE r.role_key = 'user'
            ON CONFLICT DO NOTHING;
            """)

            cur.execute(f"""
            INSERT INTO {t('menu_items')}
                (parent_id, menu_key, label, href, icon, permission_key, sort_order, is_active)
            VALUES
                (NULL, 'home', 'Home', '/home', '⌂', 'home.view', 10, TRUE),
                (NULL, 'superintel', 'SuperINTEL', '/superintel', '▲', 'superintel.view', 20, TRUE),
                (NULL, 'entities', 'EntitiesINTEL', '/entities/character', '◈', 'entities.view', 25, TRUE),
                (NULL, 'admin', 'Admin', '/admin', '⚙', 'admin.view', 30, TRUE)
            ON CONFLICT (menu_key) DO UPDATE SET
                parent_id = EXCLUDED.parent_id,
                label = EXCLUDED.label,
                href = EXCLUDED.href,
                icon = EXCLUDED.icon,
                permission_key = EXCLUDED.permission_key,
                sort_order = EXCLUDED.sort_order,
                is_active = TRUE;
            """)

            cur.execute(f"""
            UPDATE {t('menu_items')}
            SET is_active = FALSE
            WHERE menu_key IN ('reports', 'reports.super');
            """)

            entities_child_items = [
                ('entities.character', 'Character', '/entities/character', '☻', 'entities.view', 10),
                ('entities.corporation', 'Corporation', '/entities/corporation', '▦', 'entities.view', 20),
                ('entities.alliance', 'Alliance', '/entities/alliance', '◆', 'entities.view', 30),
                ('entities.coalition', 'Coalition', '/entities/coalition', '◇', 'entities.view', 35),
                ('entities.ship', 'Ships', '/entities/ship', '▲', 'entities.view', 40),
                ('entities.weapon', 'Weapons', '/entities/weapon', '⚔', 'entities.view', 50),
                ('entities.skill', 'Skills', '/entities/skill', '✦', 'entities.view', 60),
                ('entities.commodity', 'Commodities', '/entities/commodity', '◍', 'entities.view', 70),
                ('entities.map', 'MAP', '/map', '▧', 'entities.view', 80),
            ]

            for menu_key, label, href, icon, permission_key, sort_order in entities_child_items:
                cur.execute(f"""
                INSERT INTO {t('menu_items')}
                    (parent_id, menu_key, label, href, icon, permission_key, sort_order, is_active)
                SELECT
                    parent.id,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE
                FROM {t('menu_items')} parent
                WHERE parent.menu_key = 'entities'
                ON CONFLICT (menu_key) DO UPDATE SET
                    parent_id = EXCLUDED.parent_id,
                    label = EXCLUDED.label,
                    href = EXCLUDED.href,
                    icon = EXCLUDED.icon,
                    permission_key = EXCLUDED.permission_key,
                    sort_order = EXCLUDED.sort_order,
                    is_active = TRUE;
                """, (menu_key, label, href, icon, permission_key, sort_order))

            cur.execute(f"""
            UPDATE {t('menu_items')}
            SET is_active = FALSE
            WHERE menu_key IN ('entities.system', 'entities.constellation', 'entities.region');
            """)

            cur.execute(f"""
            INSERT INTO {t('user_permissions')} (user_id, permission_id)
            SELECT ur.user_id, p.id
            FROM {t('user_roles')} ur
            JOIN {t('roles')} r ON r.id = ur.role_id
            JOIN {t('permissions')} p ON p.permission_key = 'entities.view'
            WHERE r.role_key = 'user'
            ON CONFLICT DO NOTHING;
            """)

            superintel_child_items = [
                ('superintel.dashboard', 'Dashboard', '/superintel', '▣', 'superintel.view', 10),
            ]

            for menu_key, label, href, icon, permission_key, sort_order in superintel_child_items:
                cur.execute(f"""
                INSERT INTO {t('menu_items')}
                    (parent_id, menu_key, label, href, icon, permission_key, sort_order, is_active)
                SELECT
                    parent.id,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE
                FROM {t('menu_items')} parent
                WHERE parent.menu_key = 'superintel'
                ON CONFLICT (menu_key) DO UPDATE SET
                    parent_id = EXCLUDED.parent_id,
                    label = EXCLUDED.label,
                    href = EXCLUDED.href,
                    icon = EXCLUDED.icon,
                    permission_key = EXCLUDED.permission_key,
                    sort_order = EXCLUDED.sort_order,
                    is_active = TRUE;
                """, (menu_key, label, href, icon, permission_key, sort_order))

            admin_child_items = [
                ('admin.users', 'Users', '/admin', '◎', 'admin.users.view', 10),
                ('admin.system', 'System', '/admin/system', '▣', 'admin.system.view', 20),
                ('admin.nginx', 'Nginx', '/admin/nginx', '⇄', 'admin.nginx.manage', 25),
                ('admin.jobs', 'Jobs', '/admin/jobs', '▶', 'admin.jobs.view', 30),
                ('admin.update_pipeline', 'Update Pipeline', '/admin/update-pipeline', '⟳', 'admin.jobs.view', 35),
                ('admin.mer', 'MER', '/admin/mer', '▤', 'admin.mer.view', 40),
                ('admin.killmail_forensics', 'Killmail Forensics', '/admin/killmail-forensics', '⌕', 'admin.killmail_forensics.dev', 45),
                ('admin.audit', 'Audit', '/admin/audit', '≡', 'admin.audit.view', 50),
            ]

            cur.execute(f"""
            UPDATE {t('menu_items')}
            SET is_active = CASE WHEN menu_key = 'admin.coalitions' THEN FALSE ELSE is_active END,
                permission_key = NULL
            WHERE menu_key = 'admin.coalitions'
               OR permission_key IN ('admin.coalitions.view', 'admin.coalitions.edit');
            """)

            cur.execute(f"""
            DELETE FROM {t('permissions')}
            WHERE permission_key IN ('admin.coalitions.view', 'admin.coalitions.edit');
            """)

            for menu_key, label, href, icon, permission_key, sort_order in admin_child_items:
                cur.execute(f"""
                INSERT INTO {t('menu_items')}
                    (parent_id, menu_key, label, href, icon, permission_key, sort_order, is_active)
                SELECT
                    parent.id,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE
                FROM {t('menu_items')} parent
                WHERE parent.menu_key = 'admin'
                ON CONFLICT (menu_key) DO UPDATE SET
                    parent_id = EXCLUDED.parent_id,
                    label = EXCLUDED.label,
                    href = EXCLUDED.href,
                    icon = EXCLUDED.icon,
                    permission_key = EXCLUDED.permission_key,
                    sort_order = EXCLUDED.sort_order,
                    is_active = TRUE;
                """, (menu_key, label, href, icon, permission_key, sort_order))

        conn.commit()

    print("ACL/menu tables ready.")


if __name__ == "__main__":
    main()
