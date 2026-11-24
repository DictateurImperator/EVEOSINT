"""Display recorded remote healing without changing killmail data."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import logging
from . import combat_timeline as store
from .entities import _entity_ref

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
    by_id,by_name=participants(killmail)
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


def enrich(killmail, rows):
    if not rows: return rows
    by_id,by_name=participants(killmail)
    for row in rows:
        source=row['source'];name=source['name'];existing=by_id.get(source.get('character_id')) or by_name.get(name.casefold()) or {}
        cid=source.get('character_id')
        row['character']=existing.get('character') or _entity_ref('character',cid,name) or dict(name=name)
        row['corporation']=existing.get('corporation')
        row['alliance']=existing.get('alliance')
        row['logged_corporation']=source.get('corporation','')
        row['ships']=[dict(name=name) for name in sorted(row['ship_names'])]
        if not row['ships'] and existing.get('ship'): row['ships']=[existing['ship']]
    return rows


def get_healers(killmail):
    if not killmail or not killmail.get('killmail_time'): return []
    # A KM without observations keeps its current presentation unchanged.
    path=store.ROOT/'timeline.sqlite3'
    if not path.is_file() or path.is_symlink(): return []
    end=killmail['killmail_time'].astimezone(timezone.utc)
    start=end-timedelta(minutes=30)
    try:
        with store.connection() as conn:
            events=(json.loads(r['payload']) for r in conn.execute('''SELECT payload FROM events
                WHERE kind IN ('shield_in','shield_out','armor_in','armor_out','hull_in','hull_out')
                  AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at,event_id''',
                (start.strftime('%Y-%m-%dT%H:%M:%SZ'),end.strftime('%Y-%m-%dT%H:%M:%SZ'))))
            rows=collect(killmail,events)
        return enrich(killmail,rows)
    except Exception:
        logger.warning('Recorded healing unavailable for killmail %s',killmail.get('killmail_id'),exc_info=True)
        return []
