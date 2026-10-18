"""Fixed, server-built group predicates. Parameters never come from public filters."""
from datetime import UTC,date,datetime,timedelta
from decimal import Decimal
from psycopg2 import sql
from . import entities


def ts(day):return datetime.combine(day,datetime.min.time(),tzinfo=UTC)
def next_month(day):return date(day.year+(day.month==12),day.month%12+1,1)


def load_rules(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT coalition_id,operation,member_type,member_id,valid_from,valid_to FROM entities.coalition_memberships ORDER BY id')
        result={}
        for parent,op,kind,id,start,end in cur:
            result.setdefault(str(parent),[]).append(dict(op=op,kind=kind,id=id,**{'from':start.isoformat() if start else None,'through':end.isoformat() if end else None}))
        return result


def actor_fields(alias,victim=False,history=True):
    prefix='victim_' if victim else ''
    at=f'{alias}.killmail_time';char=f'{alias}.{prefix}character_id';corp=f'{alias}.{prefix}corporation_id';ally=f'{alias}.{prefix}alliance_id'
    if history:
        corp=f'''COALESCE({corp},(SELECT h.corporation_id FROM entities.character_corporation_history h
             WHERE h.character_id={char} AND NOT h.is_deleted AND h.start_date<={at}
             AND (h.end_date IS NULL OR h.end_date>{at}) ORDER BY h.start_date DESC,h.record_id DESC LIMIT 1))'''
        ally=f'''COALESCE({ally},(SELECT h.alliance_id FROM entities.corporation_alliance_history h
             WHERE h.corporation_id={corp} AND NOT h.is_deleted AND h.start_date<={at}
             AND (h.end_date IS NULL OR h.end_date>{at}) ORDER BY h.start_date DESC,h.record_id DESC LIMIT 1))'''
    return dict(character=char,corporation=corp,alliance=ally)


def actor_predicate(config,rules,alias,victim=False,history=True,fields=None,at=None):
    at=at or f'{alias}.killmail_time'
    fields=fields or actor_fields(alias,victim,history)
    budget=[0]
    def combine(parts,operator,empty='false'):
        if not parts:return empty,[]
        return '('+f' {operator} '.join('('+q+')' for q,p in parts)+')',[x for q,p in parts for x in p]
    def rule(r,stack=()):
        budget[0]+=1
        if budget[0]>5000 or len(stack)>20:raise ValueError('Coalition nesting is too complex for this group.')
        kind=r['kind'];id=int(r['id'])
        if kind=='coalition':
            if id in stack:raise ValueError('Recursive coalition membership.')
            children=rules.get(str(id),[])
            positive=combine([rule(c,stack+(id,)) for c in children if c['op']=='include'],'OR')
            negative=combine([rule(c,stack+(id,)) for c in children if c['op']=='exclude'],'OR')
            query=f'({positive[0]}) AND NOT ({negative[0]})';params=positive[1]+negative[1]
        else:
            expression=fields[kind]
            query=f'{expression}=%s';params=[id]
        if r.get('from'):query=f'({query}) AND {at}>=%s::date';params.append(r['from'])
        if r.get('through'):query=f'({query}) AND {at}<(%s::date+1)';params.append(r['through'])
        return query,params
    positive=combine([rule(r) for r in config['members'] if r['op']=='include'],'OR')
    negative=combine([rule(r) for r in config['members'] if r['op']=='exclude'],'OR')
    return f'COALESCE(({positive[0]}) AND NOT ({negative[0]}),false)',positive[1]+negative[1]


def avoid_predicate(config,rules,alias,**options):
    if not config.get('avoids'):return 'false',[]
    avoid_config={'members':[dict(rule,op='include') for rule in config['avoids']]}
    return actor_predicate(avoid_config,rules,alias,victim=True,**options)


def zone_ids(conn,config):
    included=[];excluded=[]
    for r in config['zones']:
        term={'entity_type':r['kind'],'entity_id':r['id']}
        (included if r['op']=='include' else excluded).append(term)
    allow=set(entities._killmail_builder_zone_system_ids(conn,included)) if included else None
    deny=set(entities._killmail_builder_zone_system_ids(conn,excluded)) if excluded else set()
    return sorted(allow-deny) if allow is not None else None,sorted(deny) if allow is None else []


def relations(conn):
    attacks={name:(schema,name) for schema,name in entities._rawkm_partition_tables(conn,'killmail_attackers')}
    result=[]
    for schema,name in entities._rawkm_partition_tables(conn,'killmails'):
        suffix=name.removeprefix('killmails_')
        try:month=date.fromisoformat(suffix.replace('_','-')+'-01')
        except ValueError:raise ValueError('Personal groups require the monthly killmail partitions.')
        attack=attacks.get('killmail_attackers_'+suffix,('rawkm','killmail_attackers'))
        result.append((month,sql.Identifier(schema,name),sql.Identifier(*attack)))
    return sorted(result,key=lambda r:r[0],reverse=True)


def ship_terms(conn,config):
    result=[]
    for rule in config.get('ships',[]):
        if rule['kind']=='ship_group':
            with conn.cursor() as cur:
                cur.execute('SELECT entity_id::bigint FROM sde_work.ship_entities WHERE group_name=%s',(rule['id'],))
                ids=[row[0] for row in cur]
        else:ids=[rule['id']]
        result.append(dict(rule,type_ids=ids))
    return result


def match_sql(config,rules,kills,attackers,start,end,zones,cursor=None,ship_terms=()):
    victim,vp=actor_predicate(config,rules,'k',victim=True)
    actor,ap=actor_predicate(config,rules,'a')
    where='k.killmail_time >= %s AND k.killmail_time < %s';params=[ts(start),ts(end)]
    allow,deny=zones
    if allow is not None:where+=' AND k.solar_system_id=ANY(%s)';params.append(allow)
    if deny:where+=' AND NOT k.solar_system_id=ANY(%s)';params.append(deny)
    if cursor:
        where+=' AND (k.killmail_time,k.killmail_id)<(%s,%s)';params.extend(cursor)
    where_sql=sql.SQL(where)
    # OR within each role, AND across roles, matching the standard killboard builder.
    for role in ('both','attacker','victim'):
        includes=[];excludes=[];inc_params=[];exc_params=[]
        for rule in ship_terms:
            if rule['role']!=role:continue
            victim_ship=sql.SQL('k.victim_ship_type_id=ANY(%s)')
            attack_ship=sql.SQL('EXISTS(SELECT 1 FROM {} sa WHERE sa.killmail_id=k.killmail_id AND sa.killmail_time=k.killmail_time AND sa.ship_type_id=ANY(%s))').format(attackers)
            clause=victim_ship if role=='victim' else attack_ship if role=='attacker' else sql.SQL('({} OR {})').format(victim_ship,attack_ship)
            values=[rule['type_ids']]*(2 if role=='both' else 1)
            (includes if rule['op']=='include' else excludes).append(clause)
            (inc_params if rule['op']=='include' else exc_params).extend(values)
        if includes:
            where_sql+=sql.SQL(' AND ({})').format(sql.SQL(' OR ').join(includes));params+=inc_params
        if excludes:
            where_sql+=sql.SQL(' AND NOT COALESCE(({}),false)').format(sql.SQL(' OR ').join(excludes));params+=exc_params
    avoid_query,avoid_params=avoid_predicate(config,rules,'k')
    if config.get('avoids'):
        where_sql+=sql.SQL(' AND NOT (({avoid}) AND EXISTS(SELECT 1 FROM {attackers} a WHERE a.killmail_id=k.killmail_id AND a.killmail_time=k.killmail_time AND {actor}))').format(avoid=sql.SQL(avoid_query),attackers=attackers,actor=sql.SQL(actor))
        params+=avoid_params+ap
    query=sql.SQL('''WITH matches AS MATERIALIZED (
        SELECT k.killmail_id,k.killmail_time,k.estimated_value_cents,k.victim_character_id,
               {victim} AS is_loss
        FROM {kills} k WHERE {where} AND (({victim}) OR EXISTS(
            SELECT 1 FROM {attackers} a WHERE a.killmail_id=k.killmail_id
              AND a.killmail_time=k.killmail_time AND {actor})))''').format(
                  victim=sql.SQL(victim),where=where_sql,actor=sql.SQL(actor),kills=kills,attackers=attackers)
    return query,vp+params+vp+ap


def month_totals(conn,config,rules,relation,start,end,zones):
    month,kills,attackers=relation
    query,params=match_sql(config,rules,kills,attackers,start,end,zones,ship_terms=ship_terms(conn,config))
    actor,ap=actor_predicate(config,rules,'a')
    query+=sql.SQL(''', scored AS (
        SELECT k.*,d.* FROM matches k CROSS JOIN LATERAL (
            SELECT (COALESCE(bool_or({actor}),false) AND NOT k.is_loss) AS is_kill,
                   sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric) AS total_damage,
                   sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric) FILTER(WHERE {actor}) AS own_damage
            FROM {attackers} a WHERE a.killmail_id=k.killmail_id AND a.killmail_time=k.killmail_time) d)
        SELECT count(*) FILTER(WHERE is_kill),count(*) FILTER(WHERE is_loss),
          COALESCE(sum(COALESCE(estimated_value_cents,0)) FILTER(WHERE is_kill),0),
          COALESCE(sum(CASE WHEN total_damage>0 THEN COALESCE(estimated_value_cents,0)*own_damage/total_damage ELSE 0 END) FILTER(WHERE is_kill),0),
          COALESCE(sum(COALESCE(estimated_value_cents,0)) FILTER(WHERE is_loss),0),
          count(*) FILTER(WHERE is_kill AND estimated_value_cents IS NULL),
          count(*) FILTER(WHERE is_loss AND estimated_value_cents IS NULL),count(*) FROM scored
        ''').format(actor=sql.SQL(actor),attackers=attackers)
    with conn.cursor() as cur:
        cur.execute(query,params+ap+ap)
        row=cur.fetchone()
    names=('kills','losses','destroyed_cents','weighted_destroyed_cents','lost_cents','unvalued_kills','unvalued_losses','matched')
    return dict(zip(names,row))


def temporal_boundaries(config,rules,start,end):
    boundaries={start,end};visited=set()
    def visit(rule):
        for key,offset in [('from',0),('through',1)]:
            if rule.get(key):
                value=date.fromisoformat(rule[key])+timedelta(days=offset)
                if start<value<end:boundaries.add(value)
        if rule['kind']=='coalition' and rule['id'] not in visited:
            visited.add(rule['id'])
            for child in rules.get(str(rule['id']),[]):visit(child)
    for r in config['members']:visit(r)
    return sorted(boundaries)


def roster_query(config,rules,start,end,candidate_corps=None):
    """Known membership throughout an interval, including inactive pilots and alliance changes."""
    predicate,params=actor_predicate(config,rules,'r',history=False)
    direct=[r['id'] for r in config['members'] if r['kind']=='character' and r['op']=='include']
    candidate_sql=sql.SQL('') if candidate_corps is None else sql.SQL(' AND (h.character_id=ANY(%s) OR h.corporation_id=ANY(%s))')
    query=sql.SQL('''WITH histories AS MATERIALIZED (
        SELECT h.character_id,h.corporation_id,GREATEST(h.start_date,%s) AS start_at,
               LEAST(COALESCE(h.end_date,'infinity'::timestamptz),%s) AS end_at
        FROM entities.character_corporation_history h
        WHERE NOT h.is_deleted AND h.start_date<%s AND (h.end_date IS NULL OR h.end_date>%s) {candidates}
    ), stamps AS (
        SELECT h.*,s.at FROM histories h CROSS JOIN LATERAL (
            SELECT h.start_at AS at UNION
            SELECT a.start_date FROM entities.corporation_alliance_history a
              WHERE a.corporation_id=h.corporation_id AND NOT a.is_deleted AND a.start_date>h.start_at AND a.start_date<h.end_at UNION
            SELECT a.end_date FROM entities.corporation_alliance_history a
              WHERE a.corporation_id=h.corporation_id AND NOT a.is_deleted AND a.end_date>h.start_at AND a.end_date<h.end_at) s
    ), actors AS (
        SELECT s.character_id,s.corporation_id,s.at AS killmail_time,
          (SELECT a.alliance_id FROM entities.corporation_alliance_history a
            WHERE a.corporation_id=s.corporation_id AND NOT a.is_deleted AND a.start_date<=s.at
             AND (a.end_date IS NULL OR a.end_date>s.at) ORDER BY a.start_date DESC,a.record_id DESC LIMIT 1) AS alliance_id
        FROM stamps s WHERE s.corporation_id IS NOT DISTINCT FROM (
          SELECT h.corporation_id FROM entities.character_corporation_history h
          WHERE h.character_id=s.character_id AND NOT h.is_deleted AND h.start_date<=s.at
            AND (h.end_date IS NULL OR h.end_date>s.at)
          ORDER BY h.start_date DESC,h.record_id DESC LIMIT 1)
        UNION ALL
        SELECT e.character_id,NULL::bigint,%s,NULL::bigint FROM entities.characters e
          WHERE e.character_id=ANY(%s) AND NOT EXISTS(SELECT 1 FROM histories h WHERE h.character_id=e.character_id)
    ) SELECT DISTINCT r.character_id FROM actors r WHERE {predicate}
    AND r.character_id::text NOT IN (SELECT sde_key FROM public.sde_npccharacters WHERE sde_key IS NOT NULL)
    ''').format(predicate=sql.SQL(predicate),candidates=candidate_sql)
    candidate_params=[] if candidate_corps is None else [direct,candidate_corps]
    return query,[ts(start),ts(end),ts(end),ts(start)]+candidate_params+[ts(start),direct]+params


def pilotable_counts(conn,config,rules,start,end,check=lambda:None):
    """Deduplicate pilots in transient SQL; persist only small per-ship counts."""
    bounds=temporal_boundaries(config,rules,start,end)
    corps=set();alliances=set();visited=set()
    def potential(rule):
        if rule['kind']=='corporation':corps.add(rule['id'])
        if rule['kind']=='alliance':alliances.add(rule['id'])
        if rule['kind']=='coalition' and rule['id'] not in visited:
            visited.add(rule['id'])
            for child in rules.get(str(rule['id']),[]):potential(child)
    for rule in config['members']:potential(rule)
    with conn.cursor() as cur:
        if alliances:
            cur.execute('SELECT DISTINCT corporation_id FROM entities.corporation_alliance_history WHERE NOT is_deleted AND alliance_id=ANY(%s) AND start_date<%s AND (end_date IS NULL OR end_date>%s)',(sorted(alliances),ts(end),ts(start)))
            corps.update(row[0] for row in cur)
    unions=[];params=[]
    for a,b in zip(bounds,bounds[1:]):
        def active(rule):
            return (not rule.get('from') or rule['from']<=a.isoformat()) and (not rule.get('through') or rule['through']>=a.isoformat())
        segment=dict(config,members=[dict(r,**{'from':None,'through':None}) for r in config['members'] if active(r)])
        segment_rules={key:[dict(r,**{'from':None,'through':None}) for r in items if active(r)] for key,items in rules.items()}
        if not any(r['op']=='include' for r in segment['members']):continue
        query,values=roster_query(segment,segment_rules,a,b,sorted(corps))
        unions.append(sql.SQL('({})').format(query));params.extend(values)
    if not unions:return dict(counts={},roster_count=0,skills_count=0,ready=True)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('entities.character_pilotable_ships'),to_regclass('entities.pilotable_ship_index')")
        has_skills=all(cur.fetchone())
    if not has_skills:
        with conn.cursor() as cur:
            cur.execute(sql.SQL('SELECT count(*) FROM ({}) roster').format(sql.SQL(' UNION ').join(unions)),params)
            total=cur.fetchone()[0]
        return dict(counts={},roster_count=total,skills_count=0,ready=False)
    index=dict((bit,ship) for ship,bit in entities._pilotable_ship_index_rows(conn))
    query=sql.SQL('WITH roster AS ({}) SELECT r.character_id,s.ship_mask FROM roster r LEFT JOIN entities.character_pilotable_ships s USING(character_id)').format(sql.SQL(' UNION ').join(unions))
    counts={};total=known=0
    with conn.cursor(name='personal_group_roster') as cur:
        cur.execute(query,params)
        while True:
            rows=cur.fetchmany(2500)
            if not rows:break
            check()
            for character,mask in rows:
                total+=1
                if mask is None:continue
                known+=1
                for bit in entities._iter_mask_set_bits(bytes(mask)):
                    ship=index.get(bit)
                    if ship is not None:counts[str(ship)]=counts.get(str(ship),0)+1
    return dict(counts=counts,roster_count=total,skills_count=known,ready=True)


def killboard(conn,group,result,cursor='',limit=100,participation='both',mode='api',check=lambda:None):
    """Bounded fixed-scope pagination; at most three historical months per request."""
    if mode not in ('api','total','hidden'):raise ValueError('Invalid killboard mode.')
    if mode!='api':return mer_killboard(conn,group,result,cursor,limit,participation,mode,check)
    check()
    if participation not in ('both','kills','losses'):raise ValueError('Invalid participation.')
    start=result['from_date'];end=result['through_date']+timedelta(days=1)
    parts=[r for r in relations(conn) if r[0]<end and next_month(r[0])>start]
    index=0;before=None
    if cursor:
        if len(cursor)>180:raise ValueError('Invalid cursor.')
        items=cursor.split('|')
        if len(items) not in (1,3):raise ValueError('Invalid cursor.')
        index=int(items[0])
        if len(items)==3:
            at=datetime.fromisoformat(items[1]);id=int(items[2])
            if not at.tzinfo or not 0<id<2**63:raise ValueError('Invalid cursor.')
            before=(at,id)
        if not 0<=index<len(parts):raise ValueError('Invalid cursor.')
    rules=group.get('membership_rules')
    if rules is None:rules=load_rules(conn)
    zones=zone_ids(conn,group['definition']);entries=[];values={};credits={};scanned=0;next_cursor=''
    while index<len(parts) and len(entries)<limit and scanned<3:
        check()
        relation=parts[index];month,kills,attackers=relation
        query,params=match_sql(group['definition'],rules,kills,attackers,max(start,month),min(end,next_month(month)),zones,cursor=before,ship_terms=ship_terms(conn,group['definition']))
        if participation=='losses':
            where=sql.SQL(' WHERE is_loss')
        elif participation=='kills':
            actor,ap=actor_predicate(group['definition'],rules,'a')
            where=sql.SQL(' WHERE NOT is_loss AND EXISTS(SELECT 1 FROM {attackers} a WHERE a.killmail_id=matches.killmail_id AND a.killmail_time=matches.killmail_time AND {actor})').format(attackers=attackers,actor=sql.SQL(actor))
            params+=ap
        else:where=sql.SQL('')
        side=sql.SQL("'kill'" if participation=='kills' else "'loss'" if participation=='losses' else "CASE WHEN is_loss THEN 'loss' ELSE 'kill' END")
        query+=sql.SQL(" SELECT {side},killmail_id,killmail_time,estimated_value_cents FROM matches {where} ORDER BY killmail_time DESC,killmail_id DESC LIMIT %s").format(where=where,side=side)
        remaining=limit-len(entries)
        with conn.cursor() as cur:
            cur.execute(query,params+[remaining+1]);rows=cur.fetchall()
        page=rows[:remaining]
        for side,id,at,cents in page:
            entries.append((side,id,at));values[(id,at)]=cents
        if page:
            # Weighted credit is summed across the group, never once per selected rule.
            actor,ap=actor_predicate(group['definition'],rules,'a')
            placeholders=','.join(['(%s::bigint,%s::timestamptz)']*len(page))
            args=[v for side,id,at,cents in page for v in (id,at)]
            query=sql.SQL('''SELECT p.id,p.at,d.total,d.own FROM (VALUES {values}) p(id,at) CROSS JOIN LATERAL (
                SELECT sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric) AS total,
                 sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric) FILTER(WHERE {actor}) AS own
                FROM {attackers} a WHERE a.killmail_id=p.id AND a.killmail_time=p.at) d''').format(values=sql.SQL(placeholders),actor=sql.SQL(actor),attackers=attackers)
            with conn.cursor() as cur:
                cur.execute(query,args+ap)
                credits.update({(id,at):(total or 0,own or 0) for id,at,total,own in cur})
        scanned+=1
        if len(rows)>remaining:
            last=page[-1];next_cursor=f'{index}|{last[2].isoformat()}|{last[1]}'
            break
        index+=1;before=None
    if not next_cursor and index<len(parts):next_cursor=str(index)
    rows=entities._hydrate_global_killmail_entries(conn,entries)
    sides={(id,at):side for side,id,at in entries}
    for row in rows:
        key=row['killmail_id'],row['killmail_time'];cents=values.get(key)
        row.update(side=sides[key],is_loss=sides[key]=='loss',is_kill=sides[key]=='kill')
        if cents is not None:
            total,own=credits.get(key,(0,0))
            credit=Decimal(cents) if row['is_loss'] else Decimal(cents)*own/total if total else Decimal(0)
            row['true_value_label']=entities._killmail_value_fields(credit)['value_label']
            row['true_value_title']='Full loss value' if row['is_loss'] else 'Group damage share of this kill, including NPC damage in the total'
        total,own=credits.get(key,(0,0))
        row.update(pilot_damage_done=str(own),pilot_damage_percent=f'{100*own/total:.2f}%' if total else '—',
                   pilot_ship=None,pilot_weapon=None,pilot_final_blow=False)
    return dict(rows=rows,next_cursor=next_cursor,scanned=index,total=len(parts))


def month_top_kills(conn,group,relation,start,end,zones):
    """Keep at most 20 candidates per credit mode per month, never a full kill list."""
    month,kills,attackers=relation
    query,params=match_sql(group['definition'],group['membership_rules'],kills,attackers,start,end,zones,ship_terms=ship_terms(conn,group['definition']))
    actor,ap=actor_predicate(group['definition'],group['membership_rules'],'a')
    query+=sql.SQL(''' , scored AS MATERIALIZED (
       SELECT m.killmail_id,m.killmail_time,m.estimated_value_cents AS raw_cents,
        CASE WHEN d.total>0 THEN m.estimated_value_cents*d.own/d.total ELSE 0 END AS true_cents,
        100*d.own/NULLIF(d.total,0) AS damage_percent
       FROM matches m CROSS JOIN LATERAL (
         SELECT bool_or({actor}) AS is_kill,sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric) AS total,
          sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric) FILTER(WHERE {actor}) AS own
         FROM {attackers} a WHERE a.killmail_id=m.killmail_id AND a.killmail_time=m.killmail_time) d
       WHERE m.estimated_value_cents IS NOT NULL AND NOT m.is_loss AND d.is_kill)
       (SELECT 'raw' AS mode,* FROM scored ORDER BY raw_cents DESC,killmail_time DESC,killmail_id DESC LIMIT 20)
       UNION ALL
       (SELECT 'weighted' AS mode,* FROM scored ORDER BY true_cents DESC,killmail_time DESC,killmail_id DESC LIMIT 20)
    ''').format(actor=sql.SQL(actor),attackers=attackers)
    with conn.cursor() as cur:
        cur.execute(query,params+ap+ap)
        names=[c.name for c in cur.description]
        return [dict(zip(names,row)) for row in cur]


def hydrate_top_kills(conn,top):
    scores={(r['killmail_id'],r['killmail_time']):r for rows in top.values() for r in rows}
    hydrated=entities._hydrate_global_killmail_entries(conn,[('kill',id,at) for id,at in scores])
    details={}
    for row in hydrated:
        key=row['killmail_id'],row['killmail_time']
        victim=row.get('victim_character') or row.get('victim_corporation') or {}
        ship=row.get('victim_ship') or {};system=(row.get('location') or {}).get('system') or {}
        details[key]=dict(scores[key],ship_id=ship.get('type_id'),ship_name=ship.get('name','Unknown ship'),
            victim_id=(row.get('victim_character') or {}).get('entity_id'),victim_corporation_id=(row.get('victim_corporation') or {}).get('entity_id'),
            victim_name=victim.get('name','Unknown victim'),system_id=row.get('solar_system_id'),system_name=system.get('name','Unknown system'))
    return {mode:[details[(r['killmail_id'],r['killmail_time'])] for r in rows if (r['killmail_id'],r['killmail_time']) in details] for mode,rows in top.items()}


def mer_actor_predicate(group,side):
    """MER identifies whole corps/alliances, never individual characters."""
    fields=dict(character='NULL::bigint',corporation=f'm.{side}_corporation_id',alliance=f'm.{side}_alliance_id')
    return actor_predicate(group['definition'],group['membership_rules'],'m',fields=fields,at='m.kill_datetime')


def mer_killboard(conn,group,result,cursor,limit,participation,mode,check):
    filters={'date_from':result['from_date'].isoformat(),'date_to':result['through_date'].isoformat(),'participation':participation}
    if cursor:
        if len(cursor)>220 or not cursor.startswith('mer:'):raise ValueError('Invalid cursor.')
        pieces=cursor[4:].split('|')
        if len(pieces)!=3:raise ValueError('Invalid cursor.')
        filters.update(scan_before=pieces[0],scan_month=pieces[1],scan_row=pieces[2])
    try:normalized=entities._normalize_killmail_filters(filters)
    except entities.EntityError as exc:raise ValueError('Invalid MER cursor.') from exc
    if normalized['scan_row'] is not None and normalized['scan_row']<0:raise ValueError('Invalid MER cursor.')
    if normalized['scan_before'] and normalized['scan_before']>ts(result['through_date']+timedelta(days=1)):raise ValueError('Invalid MER cursor.')
    for op in ('include','exclude'):
        normalized['builder_ship_'+op]=[dict(role=r['role'],type_ids=r['type_ids']) for r in ship_terms(conn,group['definition']) if r['op']==op]
        normalized['builder_zone_'+op]=[dict(entity_type=r['kind'],entity_id=r['id']) for r in group['definition']['zones'] if r['op']==op]
    normalized['_scan_check']=check
    rows,pagination=entities._group_entity_mer_killmails(conn,'personal_group',group,per_page=limit,
        hidden_only=mode=='hidden',progressive=True,normalized_filters=normalized)
    check()
    following=''
    if pagination.get('scan_has_more'):
        following='mer:'+ '|'.join(str(pagination[key]) if pagination.get(key) is not None else '' for key in ('scan_before','scan_month','scan_row'))
    return dict(rows=rows,next_cursor=following,scanned=0,total=0,scan_end_label=pagination.get('scan_end_label'))
