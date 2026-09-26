from .db import db, t


def ensure_tables():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS web;")
            cur.execute("CREATE SCHEMA IF NOT EXISTS entities;")

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('users')} (
                id BIGSERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_login_at TIMESTAMPTZ
            );
            """)

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('sessions')} (
                token TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES {t('users')}(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL
            );
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS sessions_user_id_idx
            ON {t('sessions')}(user_id);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS sessions_expires_at_idx
            ON {t('sessions')}(expires_at);
            """)

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('audit_logs')} (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_id BIGINT REFERENCES {t('users')}(id) ON DELETE SET NULL,
                username TEXT,
                action TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                details TEXT,
                ip_address TEXT,
                user_agent TEXT
            );
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS audit_logs_created_at_idx
            ON {t('audit_logs')}(created_at DESC);
            """)

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t('mer_catalog')} (
                month DATE PRIMARY KEY,
                article_url TEXT,
                zip_url TEXT,
                zip_http_status INTEGER,
                zip_size_bytes BIGINT,
                archive_filename TEXT,
                scan_status TEXT NOT NULL DEFAULT 'pending',
                local_path TEXT,
                kill_dump_state TEXT NOT NULL DEFAULT 'unknown',
                economic_dump_state TEXT NOT NULL DEFAULT 'unknown',
                last_error TEXT,
                scanned_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS mer_catalog_scanned_at_idx
            ON {t('mer_catalog')}(scanned_at DESC);
            """)

            cur.execute(f"""
            ALTER TABLE {t('mer_catalog')}
                ADD COLUMN IF NOT EXISTS archive_csv_count INTEGER,
                ADD COLUMN IF NOT EXISTS kill_dump_candidate_count INTEGER,
                ADD COLUMN IF NOT EXISTS kill_dump_member TEXT,
                ADD COLUMN IF NOT EXISTS kill_dump_path TEXT,
                ADD COLUMN IF NOT EXISTS kill_dump_columns JSONB,
                ADD COLUMN IF NOT EXISTS kill_dump_column_count INTEGER,
                ADD COLUMN IF NOT EXISTS kill_dump_schema_hash TEXT,
                ADD COLUMN IF NOT EXISTS kill_dump_size_bytes BIGINT,
                ADD COLUMN IF NOT EXISTS kill_dump_analyzed_at TIMESTAMPTZ;
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS mer_catalog_kill_schema_idx
            ON {t('mer_catalog')}(kill_dump_schema_hash);
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS entities.coalitions (
                coalition_id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                short_name TEXT,
                description TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_by BIGINT REFERENCES web.users(id) ON DELETE SET NULL,
                updated_by BIGINT REFERENCES web.users(id) ON DELETE SET NULL
            );
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS coalitions_name_idx
            ON entities.coalitions (lower(name));
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS coalitions_short_name_idx
            ON entities.coalitions (lower(short_name));
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS entities.coalition_memberships (
                id BIGSERIAL PRIMARY KEY,
                coalition_id BIGINT NOT NULL
                    REFERENCES entities.coalitions(coalition_id)
                    ON DELETE CASCADE,
                operation TEXT NOT NULL
                    CHECK (operation IN ('include', 'exclude')),
                member_type TEXT NOT NULL
                    CHECK (member_type IN ('coalition', 'alliance', 'corporation')),
                member_id BIGINT NOT NULL,
                valid_from DATE,
                valid_to DATE,
                source TEXT,
                notes TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_by BIGINT REFERENCES web.users(id) ON DELETE SET NULL,
                updated_by BIGINT REFERENCES web.users(id) ON DELETE SET NULL,
                CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
            );
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS coalition_memberships_parent_idx
            ON entities.coalition_memberships (coalition_id);
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS coalition_memberships_member_idx
            ON entities.coalition_memberships (member_type, member_id);
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS coalition_memberships_dates_idx
            ON entities.coalition_memberships (valid_from, valid_to);
            """)


        conn.commit()
