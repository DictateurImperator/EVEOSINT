"""Monthly MER coverage and recovery provenance, with read-only queries."""

from datetime import date

from psycopg2.extras import RealDictCursor

from .db import db


def monthly_statistics(year=None):
    conn = db()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            cur.execute("SET LOCAL TIME ZONE 'UTC'")
            cur.execute("""
                SELECT to_regclass('mer.killmails') IS NOT NULL AS mer,
                    to_regclass('rawkm.killmails') IS NOT NULL AS raw,
                    to_regclass('rawkm.killmail_archive_recoveries') IS NOT NULL AS archives,
                    to_regclass('web.forensics_cases') IS NOT NULL
                        AND to_regclass('web.forensics_recovered') IS NOT NULL AS forensics
            """)
            available = dict(cur.fetchone())
            cur.execute(r"""
                SELECT DISTINCT substring(child.relname FROM '_(\d{4})_\d{2}$')::int AS year
                FROM pg_inherits i
                JOIN pg_class parent ON parent.oid=i.inhparent
                JOIN pg_namespace ns ON ns.oid=parent.relnamespace
                JOIN pg_class child ON child.oid=i.inhrelid
                WHERE ns.nspname IN ('mer','rawkm') AND parent.relname='killmails'
                    AND child.relname ~ '^killmails_\d{4}_\d{2}$'
                ORDER BY year DESC
            """)
            years = [row["year"] for row in cur.fetchall()]
            selected = year if year is not None else (years[0] if years else date.today().year)
            if not 2003 <= selected <= 9998:
                raise ValueError("Invalid year")
            if selected not in years:
                years = sorted(years + [selected], reverse=True)
            bounds = (date(selected, 1, 1), date(selected + 1, 1, 1))
            months = {}
            if available["raw"]:
                cur.execute("""
                    SELECT to_char(killmail_time, 'YYYY-MM') AS month, count(*) AS archive_killmails
                    FROM rawkm.killmails WHERE killmail_time >= %s AND killmail_time < %s
                    GROUP BY 1
                """, bounds)
                for row in cur.fetchall():
                    months[row["month"]] = dict(row)
            if available["mer"]:
                archive_join = ""
                forensic_join = ""
                archive_found = "FALSE"
                forensic_found = "FALSE"
                if available["archives"]:
                    archive_join = """LEFT JOIN rawkm.killmail_archive_recoveries a
                        ON a.killmail_id=m.resolved_km[1] AND a.killmail_time=m.kill_datetime"""
                    archive_found = "a.killmail_id IS NOT NULL"
                if available["forensics"]:
                    forensic_join = """LEFT JOIN (
                        SELECT c.kill_datetime, c.source_month, c.source_row
                        FROM web.forensics_cases c JOIN web.forensics_recovered r ON r.case_id=c.id
                        WHERE c.status='recovered'
                    ) f ON f.kill_datetime=m.kill_datetime
                        AND f.source_month=m.source_month AND f.source_row=m.source_row"""
                    forensic_found = "f.source_row IS NOT NULL"
                cur.execute(f"""
                    WITH classified AS (
                        SELECT to_char(m.kill_datetime,'YYYY-MM') AS month,
                            CASE WHEN {forensic_found} THEN 'forensics'
                                WHEN cardinality(m.resolved_km)=1 AND m.resolved_km_ambiguous IS FALSE
                                    THEN CASE WHEN {archive_found} THEN 'archives' ELSE 'known' END
                                WHEN m.resolved_km IS NOT NULL THEN 'ambiguous'
                                ELSE 'hidden' END AS category
                        FROM mer.killmails m {archive_join} {forensic_join}
                        WHERE m.kill_datetime >= %s AND m.kill_datetime < %s
                    )
                    SELECT month, count(*) AS mer_total,
                        count(*) FILTER (WHERE category='known') AS known,
                        count(*) FILTER (WHERE category='hidden') AS hidden,
                        count(*) FILTER (WHERE category='ambiguous') AS ambiguous,
                        count(*) FILTER (WHERE category='archives') AS recovered_archives,
                        count(*) FILTER (WHERE category='forensics') AS recovered_forensics
                    FROM classified GROUP BY month
                """, bounds)
                for row in cur.fetchall():
                    months.setdefault(row["month"], {}).update(dict(row))
            rows = []
            keys = ("archive_killmails", "mer_total", "known", "hidden", "ambiguous", "recovered_archives", "recovered_forensics")
            totals = dict.fromkeys(keys, 0)
            for month in sorted(months, reverse=True):
                row = {key: int(months[month].get(key, 0)) for key in keys}
                row["month"] = month
                row["recovered"] = row["recovered_archives"] + row["recovered_forensics"]
                row["coverage"] = round(100 * (row["known"] + row["recovered"]) / row["mer_total"], 1) if row["mer_total"] else None
                rows.append(row)
                for key in keys:
                    totals[key] += row[key]
            totals["recovered"] = totals["recovered_archives"] + totals["recovered_forensics"]
            totals["coverage"] = round(100 * (totals["known"] + totals["recovered"]) / totals["mer_total"], 1) if totals["mer_total"] else None
            return {"year": selected, "years": years, "months": rows, "totals": totals,
                    "archive_recovery_tracking": available["archives"], "mer_available": available["mer"]}
    finally:
        conn.close()
