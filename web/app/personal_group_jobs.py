"""Owner-started group preparation. Only monthly/yearly aggregates are persisted."""
from contextlib import closing
from datetime import UTC,date,datetime,timedelta
from decimal import Decimal
import logging
import threading
from psycopg2.extras import Json
from .db import db
from . import personal_groups as store,personal_group_queries as q

LOG=logging.getLogger(__name__)
LOCK_ID=920134801
FIELDS=('kills','losses','destroyed_cents','weighted_destroyed_cents','lost_cents','unvalued_kills','unvalued_losses','matched')

class Cancelled(Exception):pass
class Changed(Exception):pass


def clean(value):
    if isinstance(value,dict):return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [clean(v) for v in value]
    if isinstance(value,(Decimal,date,datetime)):return str(value)
    return value


def _check(conn,group):
    with conn.cursor() as cur:
        cur.execute("SELECT revision,progress->>'stop_requested' FROM web.personal_groups WHERE id=%s AND owner_id=%s",(group['id'],group['owner_id']))
        row=cur.fetchone()
    conn.commit()
    if not row or row[0]!=group['revision']:raise Changed()
    if row[1]=='true':raise Cancelled()


def _progress(conn,group,**values):
    values['updated_at']=datetime.now(UTC).isoformat()
    with conn.cursor() as cur:
        cur.execute('UPDATE web.personal_groups SET progress=progress || %s WHERE id=%s AND revision=%s',(Json(clean(values)),group['id'],group['revision']))
        if not cur.rowcount:raise Changed()
    conn.commit()


def _save_period(conn,group,period,start,end,totals,pilots):
    with conn.cursor() as cur:
        cur.execute('UPDATE web.personal_groups SET updated_at=now() WHERE id=%s AND revision=%s',(group['id'],group['revision']))
        if not cur.rowcount:raise Changed()
        totals=dict(totals,ships_ready=pilots['ready'])
        cur.execute('''INSERT INTO web.personal_group_periods(group_id,revision,period,from_date,through_date,totals,ship_counts,roster_count,skills_count)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(group_id,period) DO UPDATE SET
          revision=excluded.revision,from_date=excluded.from_date,through_date=excluded.through_date,totals=excluded.totals,
          ship_counts=excluded.ship_counts,roster_count=excluded.roster_count,skills_count=excluded.skills_count,built_at=now()''',
          (group['id'],group['revision'],period,start,end-timedelta(days=1),Json(clean(totals)),Json(pilots['counts']),pilots['roster_count'],pilots['skills_count']))
    conn.commit()


def _sum(rows):
    return {key:sum((Decimal(str(row.get(key,0))) for row in rows),Decimal(0)) for key in FIELDS}


def prepare(writer,group,can_super,custom=None):
    try:
        with closing(db()) as source:
            source.set_session(readonly=True,isolation_level='REPEATABLE READ')
            with source,source.cursor() as cur:
                cur.execute("SET LOCAL timezone='UTC'");cur.execute('SET LOCAL jit=off');cur.execute("SET LOCAL statement_timeout='120s'")
                relations=q.relations(source)
                if not relations:raise ValueError('No imported killmail months are available.')
                config=group['definition']
                first=max(store.day(config.get('from')) or relations[-1][0],relations[-1][0])
                last=min((store.day(config.get('through'))+timedelta(days=1)) if config.get('through') else q.next_month(relations[0][0]),q.next_month(relations[0][0]))
                if custom:
                    first=max(first,custom[0]);last=min(last,custom[1]+timedelta(days=1))
                if first>=last:raise ValueError('This period does not overlap imported history or the group dates.')
                selected=[r for r in relations if r[0]<last and q.next_month(r[0])>first]
                rules=group['prepared_rules'] if custom else q.load_rules(source)
                zones=q.zone_ids(source,config)
                # Compile once before scanning: reject recursive or overly complex coalition graphs.
                q.actor_predicate(config,rules,'a')
                if not custom:
                    with writer.cursor() as save:
                        save.execute('UPDATE web.personal_groups SET prepared_rules=%s WHERE id=%s AND revision=%s',(Json(rules),group['id'],group['revision']))
                        if not save.rowcount:raise Changed()
                    writer.commit()
                denied=set()
                if not can_super:
                    cur.execute("SELECT to_regclass('entities.pilotable_ship_index'),to_regclass('sde_work.ship_entities')")
                    if all(cur.fetchone()):
                        from .entities import _pilotable_ship_index_rows,_ship_counts_to_payload
                        metadata=_ship_counts_to_payload(source,{ship:1 for ship,bit in _pilotable_ship_index_rows(source)})
                        denied={str(s['entity_id']) for s in metadata if (s.get('group_name') or '').lower() in ('titan','supercarrier') or (s.get('ship_sde_band') or '').lower()=='supercapital ships'}
                def pilots(start,end,period):
                    _check(writer,group)
                    _progress(writer,group,state='running',phase='Counting all known members and pilotable ships',current_period=period)
                    cur.execute("SET LOCAL statement_timeout='120s'")
                    data=q.pilotable_counts(source,config,rules,start,end,check=lambda:_check(writer,group))
                    data['counts']={key:value for key,value in data['counts'].items() if key not in denied}
                    return data
                facts=[];matched=0
                _progress(writer,group,state='running',total=len(selected),checked=0,kills_examined=0,phase='Scanning killmails')
                for index,relation in enumerate(selected):
                    _check(writer,group)
                    month=relation[0];start=max(month,first);end=min(q.next_month(month),last);period=month.strftime('%Y-%m')
                    _progress(writer,group,state='running',phase='Scanning killmails',current_period=period,checked=index)
                    cur.execute("SET LOCAL statement_timeout='120s'")
                    totals=q.month_totals(source,config,rules,relation,start,end,zones)
                    matched+=int(totals['matched']);facts.append((month,start,end,totals))
                    if not custom:_save_period(writer,group,period,start,end,totals,pilots(start,end,period))
                    _progress(writer,group,checked=index+1,kills_examined=matched)
                if custom:
                    _save_period(writer,group,'custom',first,last,_sum([f[3] for f in facts]),pilots(first,last,'custom'))
                else:
                    for year in sorted({f[0].year for f in facts},reverse=True):
                        subset=[f for f in facts if f[0].year==year]
                        start=min(f[1] for f in subset);end=max(f[2] for f in subset)
                        _save_period(writer,group,str(year),start,end,_sum([f[3] for f in subset]),pilots(start,end,str(year)))
                    _save_period(writer,group,'all',first,last,_sum([f[3] for f in facts]),pilots(first,last,'all'))
                    with writer.cursor() as save:
                        save.execute('UPDATE web.personal_groups SET prepared_revision=revision,prepared_at=now() WHERE id=%s AND revision=%s',(group['id'],group['revision']))
                        if not save.rowcount:raise Changed()
                    writer.commit()
                _progress(writer,group,state='completed',phase='Complete',current_period='custom' if custom else 'all')
    except Changed:
        writer.rollback()
    except Cancelled:
        writer.rollback();_progress(writer,group,state='cancelled',phase='Stopped; completed periods are retained.')
    except Exception as exc:
        writer.rollback();LOG.exception('Personal group preparation failed group=%s',group['id'])
        message=str(exc) if isinstance(exc,ValueError) else 'Preparation failed. Completed periods are retained; retry to rebuild.'
        try:_progress(writer,group,state='failed',phase=message)
        except Exception:writer.rollback()
    finally:
        writer.close()  # Releases the session advisory lock, also on cancellation/error.


def start(group,can_super,custom=None,*,actor_id=None):
    writer=db()
    try:
        with writer.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,))
            if not cur.fetchone()[0]:raise ValueError('Another group is being prepared. Please retry when it finishes.')
            if actor_id is not None and actor_id!=group['owner_id']:store._assert_manager(cur,group['id'],group['owner_id'],actor_id)
            cur.execute('SELECT revision,prepared_revision FROM web.personal_groups WHERE id=%s AND owner_id=%s FOR UPDATE',(group['id'],group['owner_id']))
            row=cur.fetchone()
            if not row or row[0]!=group['revision']:raise ValueError('The group changed. Reload before preparing it.')
            if custom and row[0]!=row[1]:raise ValueError('Prepare the complete group before calculating a custom period.')
            cur.execute('UPDATE web.personal_groups SET progress=%s WHERE id=%s',(Json(dict(state='running',phase='Starting',checked=0,total=0,stop_requested=False)),group['id']))
            if not custom:
                cur.execute('UPDATE web.personal_groups SET prepared_revision=NULL WHERE id=%s',(group['id'],))
                cur.execute('DELETE FROM web.personal_group_periods WHERE group_id=%s',(group['id'],))
        writer.commit()
        threading.Thread(target=prepare,args=(writer,group,can_super,custom),name=f"personal-group-{group['id']}",daemon=True).start()
    except Exception:
        writer.rollback();writer.close();raise


def stop(group,*,actor_id=None):
    with closing(db()) as conn,conn,conn.cursor() as cur:
        if actor_id is not None and actor_id!=group['owner_id']:store._assert_manager(cur,group['id'],group['owner_id'],actor_id)
        cur.execute("UPDATE web.personal_groups SET progress=jsonb_set(progress,'{stop_requested}','true') WHERE id=%s AND owner_id=%s",(group['id'],group['owner_id']))


def recover_interrupted(group):
    """A process restart must not leave its former owner's controls stuck on Running."""
    if group['progress'].get('state')!='running':return group
    with closing(db()) as conn,conn,conn.cursor() as cur:
        cur.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,))
        if not cur.fetchone()[0]:return group
        cur.execute("""UPDATE web.personal_groups SET progress=progress || %s
            WHERE id=%s AND owner_id=%s AND revision=%s AND progress->>'state'='running'""",
            (Json(dict(state='interrupted',phase='Preparation was interrupted. Completed periods are retained; start a refresh to rebuild.')),
             group['id'],group['owner_id'],group['revision']))
    return store.owned(group['id'],group['owner_id']) or group
