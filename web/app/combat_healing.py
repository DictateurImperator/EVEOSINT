"""Display recorded remote healing without changing killmail data."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import logging
from . import combat_timeline as store
from .entities import _entity_ref, _killmail_type_ref, _killmail_attacker_coalition_map
from .db import db

logger = logging.getLogger(__name__)
HEAL_KINDS = ('shield_in','shield_out','armor_in','armor_out','hull_in','hull_out')


def participants(killmail):
    rows=[dict(killmail.get('victim') or {},side='Victim')]
    rows.extend(dict(a,side='Attacker') for a in killmail.get('attackers',[]))
    by_id={};by_name={}
    for row in rows:
        char=row.get('character') or {}
        if char.get('entity_id'): by_id[char['entity_id']]=row
        if char.get('name'): by_name[char['name'].casefold()]=row
    return by_id,by_name


def collect(killmail, events):
    """Count each observed heal once across sender/receiver logs, retaining cycles."""
    by_id,by_name=participants(dict(victim=killmail.get('victim'),attackers=[]))
    end=killmail['killmail_time'].astimezone(timezone.utc)
    start=end-timedelta(minutes=30)
    observations={}; seen=set()
    for event in events:
        event_id=event.get('event_id')
        if event_id and event_id in seen: continue
        if event_id: seen.add(event_id)
        at=datetime.fromisoformat(event['occurred_at'].replace('Z','+00:00'))
        if not start<=at<=end or event.get('kind') not in HEAL_KINDS or event.get('amount',0)<=0: continue
        source=event.get('source') or {}; target=event.get('target') or {}
        recipient=(by_id.get(target['character_id']) if target.get('character_id') else by_name.get(target.get('name','').casefold()))
        if not recipient or not source.get('name'): continue
        recipient_char=recipient['character']
        if (source.get('character_id') and source.get('character_id')==recipient_char.get('entity_id')) or source['name'].casefold()==recipient_char['name'].casefold(): continue
        layer=event['kind'].split('_')[0]
        key=(event['occurred_at'],source['name'].casefold(),recipient_char['entity_id'],layer,event['amount'],event.get('weapon',''))
        item=observations.setdefault(key,dict(source=dict(source),recipient=recipient,layer=layer,amount=event['amount'],views=Counter()))
        # Incoming and outgoing recordings describe the same transfer. Separate
        # identical cycles within a single observer's log must still count.
        item['views'][event['observer'].casefold()]+=1
        for field,value in source.items():
            if value and not item['source'].get(field): item['source'][field]=value
    healers={}
    for item in observations.values():
        source=item['source'];key=source['name'].casefold()
        row=healers.setdefault(key,dict(source={},shield=0,armor=0,hull=0,total=0,recipients={},ship_names=set()))
        row['source'].update({k:v for k,v in source.items() if v and not row['source'].get(k)})
        amount=item['amount']*max(item['views'].values())
        row[item['layer']]+=amount;row['total']+=amount
        char=item['recipient']['character']
        row['recipients'][char['entity_id']]=dict(character=char,side=item['recipient']['side'])
        if source.get('ship'): row['ship_names'].add(source['ship'])
    result=sorted(healers.values(),key=lambda r:(-r['total'],r['source']['name'].casefold()))
    total=sum(row['total'] for row in result)
    for row in result:
        row['recipients']=list(row['recipients'].values())
        row['percent']=row['total']/total*100 if total else 0
    return result


def healing_refs(rows):
    refs={key:{} for key in ('character','ship','corporation','alliance')}
    names=sorted({r['source']['name'] for r in rows})
    ships=sorted({name for r in rows for name in r['ship_names']})
    corps=sorted({r['source']['corporation'] for r in rows if r['source'].get('corporation')})
    alliances=sorted({r['source']['alliance'] for r in rows if r['source'].get('alliance')})
    queries=[
        ('character',names,'SELECT character_id,name,NULL::text FROM entities.characters WHERE name=ANY(%s)',(names,)),
        ('ship',ships,"SELECT sde_key::bigint,data->'name'->>'en',NULL::text FROM public.sde_types WHERE data->'name'->>'en'=ANY(%s)",(ships,)),
        ('corporation',corps,'SELECT corporation_id,name,ticker FROM entities.corporations WHERE ticker=ANY(%s) OR name=ANY(%s)',(corps,corps)),
        ('alliance',alliances,'SELECT alliance_id,name,ticker FROM entities.alliances WHERE ticker=ANY(%s) OR name=ANY(%s)',(alliances,alliances))]
    try:
        with closing(db()) as conn:
            conn.set_session(readonly=True)
            for kind,values,sql,params in queries:
                if not values: continue
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql,params)
                        for entity_id,name,ticker in cur.fetchall():
                            ref=(_killmail_type_ref(entity_id,name,'ship',64) if kind=='ship' else _entity_ref(kind,entity_id,name,ticker))
                            refs[kind][name.casefold()]=ref
                            if ticker: refs[kind][ticker.casefold()]=ref
                except Exception:
                    conn.rollback()
                    logger.warning('Healing %s lookup unavailable; keeping recorded HP',kind)
    except Exception:
        logger.warning('Healing metadata unavailable; keeping recorded HP')
    return refs


def enrich(killmail, rows):
    if not rows: return rows
    by_id,by_name=participants(killmail)
    refs=healing_refs(rows)
    for row in rows:
        source=row['source'];name=source['name'];existing=by_id.get(source.get('character_id')) or by_name.get(name.casefold()) or {}
        cid=source.get('character_id')
        row['character']=existing.get('character') or _entity_ref('character',cid,name) or refs['character'].get(name.casefold()) or dict(name=name)
        row['corporation']=refs['corporation'].get(source.get('corporation','').casefold()) or existing.get('corporation')
        row['alliance']=refs['alliance'].get(source.get('alliance','').casefold()) or existing.get('alliance')
        row['coalition']=existing.get('coalition')
        row['logged_corporation']=source.get('corporation','')
        row['ships']=[refs['ship'].get(name.casefold()) or dict(name=name) for name in sorted(row['ship_names'])]
        if not row['ships'] and existing.get('ship'): row['ships']=[existing['ship']]
    coalition_rows=[(i,None,None,(r.get('corporation') or {}).get('entity_id'),None,None,
                     (r.get('alliance') or {}).get('entity_id')) for i,r in enumerate(rows)]
    if any(r[3] or r[6] for r in coalition_rows):
        try:
            with closing(db()) as conn:
                conn.set_session(readonly=True)
                coalitions=_killmail_attacker_coalition_map(conn,coalition_rows,killmail['killmail_time'])
            for i,row in enumerate(rows):
                if i in coalitions: row['coalition']=coalitions[i]
        except Exception:
            logger.warning('Support coalition metadata unavailable; keeping recorded amounts')
    return rows


def get_healers(killmail):
    if not killmail or not killmail.get('killmail_time'): return []
    try:
        with store.connection() as conn,conn.cursor() as cur:
            # Repeated ship/pod notifications must not multiply the same healing.
            cur.execute('''SELECT s.owner,s.summary_id,s.payload FROM devencour.death_summaries s
              JOIN devencour.summary_killmail_links l USING(owner,summary_id)
              WHERE l.killmail_id=%s AND s.character_id=%s
              ORDER BY abs(extract(epoch FROM (s.occurred_at-%s))),s.received_at,s.summary_id LIMIT 1''',
              (killmail['killmail_id'],(killmail.get('victim',{}).get('character') or {}).get('entity_id'),killmail['killmail_time']))
            summary=cur.fetchone()
            if not summary: return []
            killmail['recorded_activity']=summary[2]['emitted']
            cur.execute('SELECT payload FROM devencour.received_healing WHERE owner=%s AND summary_id=%s ORDER BY healer_index',summary[:2])
            healing=[r[0] for r in cur.fetchall()]
        total=sum(h['hp'] for h in healing)
        rows=[dict(source={k:v for k,v in h.items() if k!='hp'},shield=0,armor=0,hull=0,total=h['hp'],percent=h['hp']/total*100 if total else 0,
          recipients=[],ship_names={h['ship']} if h.get('ship') else set()) for h in healing if h['hp']>0]
        rows.sort(key=lambda r:(-r['total'],r['source']['name'].casefold()))
        return enrich(killmail,rows)
    except Exception:
        logger.warning('Recorded healing unavailable for killmail %s',killmail.get('killmail_id'),exc_info=True)
        return []


def get_support(killmail):
    """Page loads only read compact scores; no raw scan or calculation here."""
    groups={name:[] for name in ('healers','batteries','assists','battery_assists')}
    if not killmail: return groups
    summary_healers=get_healers(killmail)
    names={('defense','healing'):'healers',('defense','capacitor'):'batteries',
           ('attack','healing'):'assists',('attack','capacitor'):'battery_assists'}
    try:
        with store.connection() as conn,conn.cursor() as cur:
            cur.execute('SELECT side,resource,amount,source FROM devencour.killmail_support WHERE killmail_id=%s',(killmail['killmail_id'],))
            stored=cur.fetchall()
        for side,resource,value,source in stored:
            groups[names[(side,resource)]].append(dict(source=source,total=value,recipients=[],ship_names={source['ship']} if source.get('ship') else set()))
        rows=[r for values in groups.values() for r in values]
        enrich(killmail,rows)
        for values in groups.values():
            values.sort(key=lambda r:(-r['total'],r['source']['name'].casefold()))
            total=sum(r['total'] for r in values)
            for row in values: row['percent']=row['total']/total*100 if total else 0
    except Exception:
        logger.warning('Recorded support unavailable for killmail %s',killmail.get('killmail_id'),exc_info=True)
    # Existing stored summaries keep working before the first deferred run.
    if not groups['healers']: groups['healers']=summary_healers
    return groups
