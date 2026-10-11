"""Monthly SQL passes: bounded memory, transactional checkpoints, no source writes."""

from datetime import datetime, UTC, date, timedelta
from pathlib import Path
import hashlib, json
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor, execute_values
from .core import VERSION, ship_role, space, outcome

SCHEMA = Path(__file__).with_name("schema.sql")


def source_fingerprint(conn, cutoff):
    """Detect registered late imports/backfills without scanning all killmail rows."""
    with conn.cursor() as c:
        c.execute(
            """SELECT day,status,to_jsonb(d)->>'files_count',to_jsonb(d)->>'archive_count'
          FROM rawkm.killmail_import_days d WHERE day<%s::timestamptz::date ORDER BY day""",
            (cutoff,),
        )
        coverage = c.fetchall()
        c.execute("SELECT to_regclass('rawkm.killmail_archive_recoveries')")
        recoveries = None
        if c.fetchone()[0]:
            c.execute(
                "SELECT count(*),max(recovered_at) FROM rawkm.killmail_archive_recoveries WHERE killmail_time<%s",
                (cutoff,),
            )
            recoveries = c.fetchone()
    return hashlib.sha256(
        json.dumps([coverage, recoveries], default=str).encode()
    ).hexdigest()


def verify_sources(conn, cutoff, expected):
    if source_fingerprint(conn, cutoff) != expected:
        raise ValueError(
            "Imported history changed during this study. Rebuild to include late imports consistently."
        )


def months(conn):
    with conn.cursor() as c:
        c.execute("SELECT day,status FROM rawkm.killmail_import_days ORDER BY day")
        coverage = c.fetchall()
        successful = [d for d, s in coverage if s == "success"]
        if not successful:
            raise ValueError("No successfully imported history.")
        first, last = min(successful), max(successful)
        c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='rawkm' AND tablename ~ '^killmails_[0-9]{4}_[0-9]{2}$' ORDER BY tablename"
        )
        parts = [date(int(n[0][-7:-3]), int(n[0][-2:]), 1) for n in c.fetchall()]
    cutoff = datetime.combine(last + timedelta(days=1), datetime.min.time(), UTC)
    missing = []
    day = first
    known = set(successful)
    while day <= last:
        if day not in known:
            missing.append(day)
        day += timedelta(days=1)
    selected = [m for m in parts if m <= last]
    return selected, cutoff, first, missing


def source(name, month):
    if name not in {"killmails", "killmail_attackers", "killmail_items"}:
        raise ValueError("Invalid source table.")
    return sql.Identifier("rawkm", f"{name}_{month.year}_{month.month:02d}")


def completed(conn, run, phase, month):
    with conn.cursor() as c:
        c.execute(
            "SELECT 1 FROM research.first_loss_months WHERE run_id=%s AND phase=%s AND month=%s",
            (run, phase, month),
        )
        return c.fetchone() is not None


def checkpoint(conn, run, phase, month):
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO research.first_loss_months VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (run, phase, month),
        )
    conn.commit()


def collect_losses(conn, run, month, cutoff):
    k = source("killmails", month)
    with conn.cursor() as c:
        c.execute(
            sql.SQL(
                """INSERT INTO research.first_loss_players AS old
            (run_id,character_id,loss_time,loss_id,loss_month,corporation_id,alliance_id,ship_type_id,system_id)
            SELECT %s,victim_character_id,killmail_time,killmail_id,
              date_trunc('month',killmail_time AT TIME ZONE 'UTC')::date,
              victim_corporation_id,victim_alliance_id,victim_ship_type_id,solar_system_id FROM (
              SELECT DISTINCT ON (victim_character_id) * FROM {} WHERE victim_character_id>0 AND killmail_time<%s
              ORDER BY victim_character_id,killmail_time,killmail_id
            ) k ON CONFLICT(run_id,character_id) DO UPDATE SET
              loss_time=excluded.loss_time,loss_id=excluded.loss_id,loss_month=excluded.loss_month,corporation_id=excluded.corporation_id,
              alliance_id=excluded.alliance_id,ship_type_id=excluded.ship_type_id,system_id=excluded.system_id
            WHERE (excluded.loss_time,excluded.loss_id)<(old.loss_time,old.loss_id)"""
            ).format(k),
            (run, cutoff),
        )


def collect_groups(conn, run, month, cutoff):
    k, a = source("killmails", month), source("killmail_attackers", month)
    with conn.cursor() as c:
        c.execute(
            sql.SQL("""WITH attack AS (
          SELECT date_trunc('day',a.killmail_time AT TIME ZONE 'UTC')::date AS day,g.kind,g.entity_id,
            count(DISTINCT (a.character_id,a.killmail_id)) attack_events,0::bigint losses,0::bigint industry_losses
          FROM {} a JOIN {} k USING(killmail_id,killmail_time)
          CROSS JOIN LATERAL (VALUES ('corporation',a.corporation_id),('alliance',a.alliance_id)) g(kind,entity_id)
          WHERE a.character_id>0 AND k.victim_character_id>0 AND g.entity_id>0 AND a.killmail_time<%s
          GROUP BY 1,2,3
        ), loss AS (
          SELECT date_trunc('day',k.killmail_time AT TIME ZONE 'UTC')::date AS day,g.kind,g.entity_id,
           0::bigint attack_events,count(*) losses,count(*) FILTER(WHERE s.data->'name'->>'en' IN
           ('Mining Barge','Exhumer','Mining Frigate','Expedition Frigate','Industrial','Hauler','Freighter','Jump Freighter','Transport Ship','Industrial Command Ship','Capital Industrial Ship')) industry_losses
          FROM {} k LEFT JOIN public.sde_types t ON t.sde_key=k.victim_ship_type_id::text
          LEFT JOIN public.sde_groups s ON s.sde_key=t.data->>'groupID'
          CROSS JOIN LATERAL (VALUES ('corporation',k.victim_corporation_id),('alliance',k.victim_alliance_id)) g(kind,entity_id)
          WHERE k.victim_character_id>0 AND g.entity_id>0 AND k.killmail_time<%s GROUP BY 1,2,3
        ) INSERT INTO research.first_loss_group_days
          SELECT %s,day,kind,entity_id,sum(attack_events),sum(losses),sum(industry_losses)
          FROM (SELECT * FROM attack UNION ALL SELECT * FROM loss) x GROUP BY 2,3,4
          ON CONFLICT(run_id,kind,entity_id,day) DO UPDATE SET attack_events=excluded.attack_events,
             losses=excluded.losses,industry_losses=excluded.industry_losses""").format(
                a, k, k
            ),
            (cutoff, cutoff, run),
        )


def collect_followup(conn, run, month, cutoff, episode_minutes):
    k, a = source("killmails", month), source("killmail_attackers", month)
    with conn.cursor() as c:
        # Prior counts cannot use anything at/after the first loss.
        c.execute(
            sql.SQL("""WITH prior AS (
          SELECT p.character_id,count(DISTINCT a.killmail_id) attacks,
            count(DISTINCT (a.killmail_time AT TIME ZONE 'UTC')::date) days
          FROM {} a JOIN research.first_loss_players p ON p.run_id=%s AND p.character_id=a.character_id
          WHERE p.loss_time>=%s AND a.killmail_time<p.loss_time GROUP BY p.character_id
        ) UPDATE research.first_loss_players p SET prior_attacks=p.prior_attacks+x.attacks,
           prior_attack_days=p.prior_attack_days+x.days FROM prior x
           WHERE p.run_id=%s AND p.character_id=x.character_id""").format(a),
            (run, month, run),
        )
        c.execute(
            sql.SQL(
                """WITH player_kills AS MATERIALIZED (
          SELECT killmail_id,killmail_time FROM {} WHERE character_id>0 GROUP BY 1,2
        ), events AS (
          SELECT k.victim_character_id character_id,k.killmail_id,k.killmail_time,k.victim_ship_type_id ship,
            k.solar_system_id system,'victim'::text role,pk.killmail_id IS NOT NULL pvp
          FROM {} k LEFT JOIN player_kills pk USING(killmail_id,killmail_time)
          WHERE k.victim_character_id>0 AND k.killmail_time<%s
          UNION ALL
          SELECT DISTINCT a.character_id,k.killmail_id,k.killmail_time,a.ship_type_id,k.solar_system_id,
            'attacker'::text,k.victim_character_id>0
          FROM {} a JOIN {} k USING(killmail_id,killmail_time)
          WHERE a.character_id>0 AND k.killmail_time<%s
        ), eligible AS MATERIALIZED (
          SELECT e.*,p.loss_time FROM events e JOIN research.first_loss_players p ON p.run_id=%s AND p.character_id=e.character_id
          WHERE e.killmail_time>p.loss_time+%s*interval '1 minute'
        ), agg AS (
          SELECT character_id,min(killmail_time) first_return,min(killmail_time) FILTER(WHERE pvp) first_pvp,
            min(killmail_time) FILTER(WHERE role='attacker') first_attack,
            min(killmail_time) FILTER(WHERE role='victim') next_loss,
            count(DISTINCT killmail_id) FILTER(WHERE role='victim' AND killmail_time<=loss_time+interval '30 days') losses_30,
            count(DISTINCT killmail_id) FILTER(WHERE role='attacker' AND killmail_time<=loss_time+interval '30 days') attacks_30,
            count(DISTINCT killmail_id) FILTER(WHERE role='victim' AND killmail_time<=loss_time+interval '90 days') losses_90,
            count(DISTINCT killmail_id) FILTER(WHERE role='attacker' AND killmail_time<=loss_time+interval '90 days') attacks_90
          FROM eligible GROUP BY character_id
        ), firsts AS (
          SELECT DISTINCT ON(character_id) character_id,killmail_time,ship,system,role
          FROM eligible ORDER BY character_id,killmail_time,killmail_id,role
        ) UPDATE research.first_loss_players p SET
          return_ship=CASE WHEN p.first_return IS NULL OR x.first_return<p.first_return THEN f.ship ELSE p.return_ship END,
          return_system=CASE WHEN p.first_return IS NULL OR x.first_return<p.first_return THEN f.system ELSE p.return_system END,
          return_role=CASE WHEN p.first_return IS NULL OR x.first_return<p.first_return THEN f.role ELSE p.return_role END,
          first_return=LEAST(p.first_return,x.first_return),first_pvp=LEAST(p.first_pvp,x.first_pvp),
          first_attack=LEAST(p.first_attack,x.first_attack),next_loss=LEAST(p.next_loss,x.next_loss),
          losses_30=p.losses_30+x.losses_30,attacks_30=p.attacks_30+x.attacks_30,
          losses_90=p.losses_90+x.losses_90,attacks_90=p.attacks_90+x.attacks_90
          FROM agg x JOIN firsts f USING(character_id) WHERE p.run_id=%s AND p.character_id=x.character_id"""
            ).format(a, k, a, k),
            (cutoff, cutoff, run, episode_minutes, run),
        )


def load_sde(conn):
    with conn.cursor() as c:
        c.execute(
            "SELECT t.sde_key,g.data->'name'->>'en' FROM public.sde_types t LEFT JOIN public.sde_groups g ON g.sde_key=t.data->>'groupID'"
        )
        roles = {int(t): ship_role(g) for t, g in c.fetchall() if t.isdigit()}
        c.execute("SELECT sde_key,data FROM public.sde_mapsolarsystems")
        systems = {int(key): data for key, data in c.fetchall() if key.isdigit()}
    return roles, systems


def feature_batch(conn, run, month, after, batch_size):
    """All t0 predictors read in keyset batches; no global unbounded Python list."""
    with conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute(
            """SELECT p.*,ch.birthday,co.is_npc,
          h.start_date corp_start,cp.member_count corp_members,ap.member_count alliance_members,
          cg.attack_events corp_attacks,cg.losses corp_losses,cg.industry_losses corp_industry,
          ag.attack_events alliance_attacks,ag.losses alliance_losses,ag.industry_losses alliance_industry
          FROM (SELECT * FROM research.first_loss_players WHERE run_id=%s AND loss_month=%s
             AND character_id>%s ORDER BY character_id LIMIT %s) p
          LEFT JOIN entities.characters ch USING(character_id)
          LEFT JOIN entities.corporations co ON co.corporation_id=p.corporation_id
          LEFT JOIN LATERAL(SELECT start_date FROM entities.character_corporation_history h
            WHERE h.character_id=p.character_id AND h.corporation_id=p.corporation_id AND h.start_date<=p.loss_time
              AND (h.end_date IS NULL OR h.end_date>p.loss_time) ORDER BY start_date DESC LIMIT 1) h ON TRUE
          LEFT JOIN LATERAL(SELECT member_count FROM population.corporation_daily d WHERE d.corporation_id=p.corporation_id
            AND d.snapshot_date<(p.loss_time AT TIME ZONE 'UTC')::date ORDER BY snapshot_date DESC LIMIT 1) cp ON TRUE
          LEFT JOIN LATERAL(SELECT member_count FROM population.alliance_daily d WHERE d.alliance_id=p.alliance_id
            AND d.snapshot_date<(p.loss_time AT TIME ZONE 'UTC')::date ORDER BY snapshot_date DESC LIMIT 1) ap ON TRUE
          LEFT JOIN LATERAL(SELECT sum(attack_events) attack_events,sum(losses) losses,sum(industry_losses) industry_losses
            FROM research.first_loss_group_days d WHERE d.run_id=p.run_id AND d.kind='corporation' AND d.entity_id=p.corporation_id
            AND d.day>=(p.loss_time AT TIME ZONE 'UTC')::date-30 AND d.day<(p.loss_time AT TIME ZONE 'UTC')::date) cg ON TRUE
          LEFT JOIN LATERAL(SELECT sum(attack_events) attack_events,sum(losses) losses,sum(industry_losses) industry_losses
            FROM research.first_loss_group_days d WHERE d.run_id=p.run_id AND d.kind='alliance' AND d.entity_id=p.alliance_id
            AND d.day>=(p.loss_time AT TIME ZONE 'UTC')::date-30 AND d.day<(p.loss_time AT TIME ZONE 'UTC')::date) ag ON TRUE
          ORDER BY p.character_id""",
            (run, month, after, batch_size),
        )
        return [dict(r) for r in c.fetchall()]


def loss_details(conn, run, month, ids):
    a, i = source("killmail_attackers", month), source("killmail_items", month)
    with conn.cursor() as c:
        c.execute(
            sql.SQL(
                """SELECT p.character_id,count(*) FILTER(WHERE a.character_id>0),
          count(*) FILTER(WHERE COALESCE(a.character_id,0)<=0) FROM research.first_loss_players p
          JOIN {} a ON a.killmail_id=p.loss_id AND a.killmail_time=p.loss_time
          WHERE p.run_id=%s AND p.character_id=ANY(%s) GROUP BY p.character_id"""
            ).format(a),
            (run, ids),
        )
        attackers = {cid: (players, npc) for cid, players, npc in c.fetchall()}
        c.execute(
            sql.SQL(
                """SELECT p.character_id,count(*) FILTER(WHERE f.is_fitted_slot OR f.flag_category='rig'),
           count(*) FILTER(WHERE f.is_hold),count(*) FILTER(WHERE i.flag=89)
          FROM research.first_loss_players p JOIN {} i ON i.killmail_id=p.loss_id AND i.killmail_time=p.loss_time
          LEFT JOIN sde_work.killmail_item_flags f ON f.flag_id=i.flag
          WHERE p.run_id=%s AND p.character_id=ANY(%s) GROUP BY p.character_id"""
            ).format(i),
            (run, ids),
        )
        items = {
            cid: (fit, cargo, implant) for cid, fit, cargo, implant in c.fetchall()
        }
    return attackers, items


def build_features(
    row, attackers, items, roles, systems, cutoff, missing, history_start
):
    players, npc = attackers.get(row["character_id"], (None, None))
    # No item rows has its own unknown state; never certify a pod as empty.
    fit, cargo, implants = items.get(row["character_id"], (None, None, None))
    loss = row["loss_time"]
    s = systems.get(row["system_id"], {})
    next_s = systems.get(row["return_system"], {})

    def elapsed(day):
        return (
            max(0.0, (loss - day).total_seconds() / 86400)
            if day is not None and day <= loss
            else None
        )

    def fraction(prefix):
        n = row[prefix + "_losses"]
        return float(row[prefix + "_industry"]) / float(n) if n else None

    group_complete = loss.date() - timedelta(days=30) >= history_start and not any(
        loss.date() - timedelta(days=30) <= d < loss.date() for d in missing
    )
    f = {
        "age_days": elapsed(row["birthday"]),
        "corp_tenure_days": elapsed(row["corp_start"]),
        "corp_members": row["corp_members"],
        "alliance_members": row["alliance_members"],
        "prior_attacks": row["prior_attacks"],
        "prior_attack_days": row["prior_attack_days"],
        "players_attacking": players,
        "npc_attackers": npc,
        "fitted_items": fit,
        "cargo_items": cargo,
        "implants": implants,
        "space": space(s, loss),
        "region_id": (
            s.get("regionID")
            if s.get("regionID") != 10000070 or space(s, loss) == "Pochven"
            else None
        ),
        "ship_role": roles.get(row["ship_type_id"], "unknown"),
        "ship_type_id": row["ship_type_id"],
        "opponent": (
            "mixed"
            if players and npc
            else "players" if players else "NPC/object" if npc else "unknown"
        ),
        "corp_kind": (
            "unknown" if row["is_npc"] is None else "NPC" if row["is_npc"] else "player"
        ),
        "corp_attacks_30": (
            int(row["corp_attacks"] or 0)
            if group_complete and row["corporation_id"]
            else None
        ),
        "corp_industry_loss_fraction": fraction("corp") if group_complete else None,
        "alliance_attacks_30": (
            int(row["alliance_attacks"] or 0)
            if group_complete and row["alliance_id"]
            else None
        ),
        "alliance_industry_loss_fraction": (
            fraction("alliance") if group_complete else None
        ),
        "group_history_complete": group_complete,
        "observed_since_birth": row["birthday"] is not None
        and history_start <= row["birthday"].date() <= loss.date(),
    }
    o = outcome(
        loss,
        row["first_return"],
        row["first_pvp"],
        row["first_attack"],
        row["next_loss"],
        cutoff,
        missing,
        row["return_role"],
        row["ship_type_id"],
        row["return_ship"],
        row["system_id"],
        row["return_system"],
        row["losses_30"],
        row["attacks_30"],
        row["losses_90"],
        row["attacks_90"],
        None if players is None else players > 0,
    )
    next_zone = space(next_s, row["first_return"]) if row["first_return"] else None
    next_region = (
        next_s.get("regionID")
        if next_s.get("regionID") != 10000070 or next_zone == "Pochven"
        else None
    )
    o.update(
        {
            "same_zone": (
                None
                if row["first_return"] is None
                or f["space"] == "unknown"
                or next_zone == "unknown"
                else f["space"] == next_zone
            ),
            "return_zone": next_zone,
            "same_region": (
                None
                if not f["region_id"] or not next_region
                else f["region_id"] == next_region
            ),
            "same_ship_role": (
                None
                if row["first_return"] is None
                or f["ship_role"] == "unknown"
                or roles.get(row["return_ship"], "unknown") == "unknown"
                else f["ship_role"] == roles.get(row["return_ship"], "unknown")
            ),
            "return_ship_role": (
                roles.get(row["return_ship"], "unknown")
                if row["first_return"]
                else None
            ),
        }
    )
    return f, o


def store_features(conn, run, rows):
    with conn.cursor() as c:
        execute_values(
            c,
            """UPDATE research.first_loss_players p SET features=v.features::jsonb,outcomes=v.outcomes::jsonb,cohort=v.cohort
           FROM (VALUES %s) v(character_id,features,outcomes,cohort) WHERE p.run_id=%s AND p.character_id=v.character_id"""
            % ("%s", int(run)),
            [(cid, Json(f), Json(o), year) for cid, f, o, year in rows],
            page_size=1000,
        )
    conn.commit()


def stream(conn, run, batch_size=10000):
    with conn.cursor(name="first_loss_stream", cursor_factory=RealDictCursor) as c:
        c.itersize = batch_size
        c.execute(
            "SELECT character_id,cohort,features,outcomes FROM research.first_loss_players WHERE run_id=%s AND features IS NOT NULL ORDER BY character_id",
            (run,),
        )
        while True:
            rows = c.fetchmany(batch_size)
            if not rows:
                break
            yield [dict(r) for r in rows]
    conn.rollback()
