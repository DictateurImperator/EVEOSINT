"""Ownership, three-slot quota and result-only public sharing for personal groups."""
from contextlib import closing
from datetime import date
from pathlib import Path
import re
import secrets

from psycopg2.extras import Json, RealDictCursor
from .db import db

TABS = ('killboard','stats','ships')
ENTITY_TABLES = {'character':('characters','character_id'),'corporation':('corporations','corporation_id'),
                 'alliance':('alliances','alliance_id'),'coalition':('coalitions','coalition_id')}
SECURITY_ZONES = {'highsec','lowsec','nullsec','wormhole','pochven'}


def day(value):
    if not value:
        return None
    try:
        result=date.fromisoformat(str(value))
    except (ValueError,TypeError):
        raise ValueError('Use dates in YYYY-MM-DD format.')
    if not 2003<=result.year<=2099:
        raise ValueError('Dates must be between 2003 and 2099.')
    return result


def definition(payload):
    if not isinstance(payload,dict):raise ValueError('Invalid group definition.')
    result={'from':str(day(payload.get('from'))) if payload.get('from') else None,
            'through':str(day(payload.get('through'))) if payload.get('through') else None}
    if result['from'] and result['through'] and result['from']>result['through']:
        raise ValueError('The end date must follow the start date.')
    members=payload.get('members',[]);zones=payload.get('zones',[])
    if not isinstance(members,list) or not 1<=len(members)<=60:
        raise ValueError('Choose between 1 and 60 membership rules.')
    if not isinstance(zones,list) or len(zones)>20:raise ValueError('Choose at most 20 geographical rules.')
    clean=[]
    for source in members:
        if not isinstance(source,dict) or source.get('kind') not in ENTITY_TABLES or source.get('op') not in ('include','exclude'):
            raise ValueError('Invalid membership rule.')
        try:id=int(source['id'])
        except (KeyError,TypeError,ValueError):raise ValueError('Invalid member ID.')
        if not 0<id<2**63:raise ValueError('Invalid member ID.')
        start=day(source.get('from'));end=day(source.get('through'))
        if start and end and start>end:raise ValueError('Invalid membership date range.')
        rule=dict(kind=source['kind'],id=id,op=source['op'],**{'from':str(start) if start else None,'through':str(end) if end else None})
        if rule not in clean:clean.append(rule)
    if not any(r['op']=='include' for r in clean):raise ValueError('Include at least one entity.')
    result['members']=clean
    clean=[]
    for source in zones:
        if not isinstance(source,dict) or source.get('kind') not in ('system','constellation','region','security') or source.get('op') not in ('include','exclude'):
            raise ValueError('Invalid geographical rule.')
        if source['kind']=='security':
            id=str(source.get('id',''))
            if id not in SECURITY_ZONES:raise ValueError('Invalid space type.')
        else:
            try:id=int(source['id'])
            except (KeyError,TypeError,ValueError):raise ValueError('Invalid location.')
            if not 0<id<2**63:raise ValueError('Invalid location.')
        rule=dict(kind=source['kind'],id=id,op=source['op'],label=str(source.get('label','')).strip()[:120])
        if rule not in clean:clean.append(rule)
    result['zones']=clean
    return result


def _row(cur):
    value=cur.fetchone()
    return dict(value) if value else None


def owned(group_id,owner_id):
    with closing(db()) as conn,conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM web.personal_groups WHERE id=%s AND owner_id=%s',(group_id,owner_id))
        return _row(cur)


def listed(owner_id):
    with closing(db()) as conn,conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM web.personal_groups WHERE owner_id=%s ORDER BY slot',(owner_id,))
        return [dict(r) for r in cur]


def public(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{32}',token):return None
    with closing(db()) as conn,conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('''SELECT * FROM web.personal_groups WHERE public_token=%s
                       AND prepared_revision=revision''',(token,))
        return _row(cur)


def save(owner_id,name,description,payload,group_id=None):
    name=str(name or '').strip();description=str(description or '').strip()
    if not 1<=len(name)<=80 or len(description)>1000:raise ValueError('Use a name up to 80 characters and a description up to 1,000 characters.')
    config=definition(payload)
    with closing(db()) as conn,conn,conn.cursor() as cur:
        # Lock the account, so concurrent creations cannot allocate a fourth slot.
        cur.execute('SELECT id FROM web.users WHERE id=%s FOR UPDATE',(owner_id,))
        if not cur.fetchone():raise ValueError('Account not found.')
        for rule in config['members']:
            table,column=ENTITY_TABLES[rule['kind']]
            cur.execute(f'SELECT name FROM entities.{table} WHERE {column}=%s',(rule['id'],))
            entity=cur.fetchone()
            if not entity:raise ValueError('One of the selected entities no longer exists.')
            rule['label']=entity[0] or f"{rule['kind']} {rule['id']}"
        if group_id is None:
            cur.execute('SELECT slot FROM web.personal_groups WHERE owner_id=%s',(owner_id,))
            occupied={r[0] for r in cur}
            available=next((s for s in range(1,4) if s not in occupied),None)
            if available is None:raise ValueError('You can keep up to three groups. Delete a group to free a slot.')
            cur.execute('''INSERT INTO web.personal_groups(owner_id,slot,name,description,definition)
                           VALUES(%s,%s,%s,%s,%s) RETURNING id''',(owner_id,available,name,description,Json(config)))
        else:
            cur.execute('''UPDATE web.personal_groups SET name=%s,description=%s,definition=%s,revision=revision+1,
                           prepared_revision=NULL,prepared_rules='{}',public_token=NULL,prepared_at=NULL,
                           progress='{"state":"idle"}',updated_at=now()
                           WHERE id=%s AND owner_id=%s RETURNING id''',(name,description,Json(config),group_id,owner_id))
        result=cur.fetchone()
        if not result:raise LookupError('Group not found.')
        if group_id is not None:cur.execute('DELETE FROM web.personal_group_periods WHERE group_id=%s',(group_id,))
        return result[0]


def remove(group_id,owner_id):
    with closing(db()) as conn,conn,conn.cursor() as cur:
        cur.execute('DELETE FROM web.personal_groups WHERE id=%s AND owner_id=%s RETURNING id',(group_id,owner_id))
        if not cur.fetchone():raise LookupError('Group not found.')


def sharing(group_id,owner_id,enabled,tabs):
    tabs=list(dict.fromkeys(tabs))
    if any(t not in TABS for t in tabs) or (enabled and not tabs):raise ValueError('Select at least one public page.')
    with closing(db()) as conn,conn,conn.cursor() as cur:
        cur.execute('SELECT revision,prepared_revision,public_token FROM web.personal_groups WHERE id=%s AND owner_id=%s FOR UPDATE',(group_id,owner_id))
        row=cur.fetchone()
        if not row:raise LookupError('Group not found.')
        if enabled and row[0]!=row[1]:raise ValueError('Prepare the complete group before publishing it.')
        token=(row[2] or secrets.token_urlsafe(24)) if enabled else None
        cur.execute('UPDATE web.personal_groups SET public_token=%s,public_tabs=%s,updated_at=now() WHERE id=%s',(token,Json(tabs),group_id))
        return token


def periods(group):
    with closing(db()) as conn,conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('''SELECT period,from_date,through_date,built_at FROM web.personal_group_periods
                       WHERE group_id=%s AND revision=%s ORDER BY from_date DESC,period''',(group['id'],group['revision']))
        return [dict(r) for r in cur]


def period_result(group,period):
    if period!='all' and period!='custom' and not re.fullmatch(r'\d{4}(-\d{2})?',period):raise ValueError('Choose a prepared year or month.')
    with closing(db()) as conn,conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM web.personal_group_periods WHERE group_id=%s AND revision=%s AND period=%s',(group['id'],group['revision'],period))
        return _row(cur)


def schema_sql():
    return Path(__file__).with_name('personal_groups_schema.sql').read_text()


def monthly_results(group,start,end):
    with closing(db()) as conn,conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(r'''SELECT period,totals FROM web.personal_group_periods
          WHERE group_id=%s AND revision=%s AND period ~ '^\d{4}-\d{2}$'
            AND from_date>=%s AND through_date<=%s ORDER BY period DESC''',(group['id'],group['revision'],start,end))
        return [dict(row) for row in cur]
