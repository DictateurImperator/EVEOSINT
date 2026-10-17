"""Cheap transactional invalidation; importing/valuing works before rankings exist."""
from datetime import UTC, date, datetime


def month_of(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('Killmail timestamp needs a timezone')
        value = value.astimezone(UTC).date()
    if not isinstance(value, date):
        raise ValueError('Expected a date or aware timestamp')
    return value.replace(day=1)


def mark_months_dirty(conn, months):
    months = sorted({month_of(month) for month in months})
    if not months:
        return
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('analytics.killmail_ranking_versions')")
        if cur.fetchone()[0] is None:
            return
        cur.execute('''INSERT INTO analytics.killmail_ranking_versions(month,revision)
                       SELECT unnest(%s::date[]),1
                       ON CONFLICT(month) DO UPDATE
                       SET revision=killmail_ranking_versions.revision+1''', (months,))
