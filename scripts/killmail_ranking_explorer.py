"""Rank lookup, membership predicates and visible-row affiliation enrichment."""
from datetime import UTC, datetime
from psycopg2 import sql


def month_end(month):
    return datetime(month.year+int(month.month==12),month.month%12+1,1,tzinfo=UTC)


def affiliations(kind,alias,as_of):
    """Affiliations as known now, or immediately before the next month's boundary."""
    root=sql.Identifier(alias)
    if kind=='alliance':
        return sql.SQL(''),sql.SQL('NULL::bigint'),sql.SQL('{}.entity_id').format(root),[]
    params=[]
    if kind=='character':
        if as_of is None:
            joins=sql.SQL('LEFT JOIN entities.character_current_affiliation ca ON ca.character_id={}.entity_id').format(root)
        else:
            joins=sql.SQL('''LEFT JOIN LATERAL (
                SELECT corporation_id FROM entities.character_corporation_history h
                WHERE h.character_id={}.entity_id AND NOT h.is_deleted
                  AND h.start_date<%s AND (h.end_date IS NULL OR h.end_date>=%s)
                ORDER BY h.start_date DESC,h.record_id DESC LIMIT 1) ca ON true''').format(root)
            params.extend((month_end(as_of),month_end(as_of)))
        corporation=sql.SQL('ca.corporation_id')
    else:
        joins=sql.SQL('')
        corporation=sql.SQL('{}.entity_id').format(root)
    if as_of is None:
        joins+=sql.SQL(' LEFT JOIN entities.corporation_current_affiliation aa ON aa.corporation_id={}').format(corporation)
        alliance=sql.SQL('aa.alliance_id')
        if kind=='character':
            alliance=sql.SQL('COALESCE(ca.alliance_id,aa.alliance_id)')
    else:
        joins+=sql.SQL(''' LEFT JOIN LATERAL (
            SELECT alliance_id,true AS affiliation_known FROM entities.corporation_alliance_history h
            WHERE h.corporation_id={} AND NOT h.is_deleted
              AND h.start_date<%s AND (h.end_date IS NULL OR h.end_date>=%s)
            ORDER BY h.start_date DESC,h.record_id DESC LIMIT 1) aa ON true''').format(corporation)
        params.extend((month_end(as_of),month_end(as_of)))
        alliance=sql.SQL('aa.alliance_id')
    return joins,corporation,alliance,params


def enrich(conn,kind,rows,as_of=None):
    if not rows or kind=='alliance':
        return rows
    joins,corp,alliance,params=affiliations(kind,'r',as_of)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='5s'")
        cur.execute(sql.SQL('''WITH r AS (SELECT unnest(%s::bigint[]) AS entity_id)
            SELECT r.entity_id,{corp} AS corporation_id,c.name,{alliance} AS alliance_id,a.name
            FROM r {joins} LEFT JOIN entities.corporations c ON c.corporation_id={corp}
            LEFT JOIN entities.alliances a ON a.alliance_id={alliance}''').format(
                corp=corp,alliance=alliance,joins=joins),([r['entity_id'] for r in rows],*params))
        affiliations_by_id={r[0]:dict(corporation_id=r[1],corporation_name=r[2],alliance_id=r[3],alliance_name=r[4]) for r in cur}
    return [dict(r,**affiliations_by_id.get(r['entity_id'],{})) for r in rows]


def scope_predicate(kind,includes,excludes,rules,day):
    def combine(items,operator,empty):
        if not items:
            return sql.SQL(empty),[]
        return sql.SQL(' '+operator+' ').join(sql.SQL('({})').format(item[0]) for item in items),[p for item in items for p in item[1]]
    def term(token,stack=frozenset()):
        entity_type,id=token.split(':');id=int(id)
        if entity_type=='universe' and id==0:
            return sql.SQL('true'),[]
        if entity_type=='alliance' and id==0:
            return (sql.SQL('false'),[]) if kind=='alliance' else (sql.SQL('alliance_known AND COALESCE(alliance_id,0)=0'),[])
        if entity_type in ('corporation','alliance'):
            return sql.SQL('COALESCE({}=%s,false)').format(sql.Identifier(entity_type+'_id')),[id]
        if id in stack:
            return sql.SQL('false'),[]
        inc=[];exc=[]
        for rule in rules.get(id,[]):
            if rule['valid_from'] and rule['valid_from']>day or rule['valid_to'] and rule['valid_to']<day:
                continue
            item=term(f"{rule['member_type']}:{rule['member_id']}",stack|{id})
            (exc if rule['operation']=='exclude' else inc).append(item)
        yes,yp=combine(inc,'OR','false');no,np=combine(exc,'OR','false')
        return sql.SQL('({}) AND NOT ({})').format(yes,no),yp+np
    inc,ip=combine([term(token) for token in includes],'OR','true')
    exc,ep=combine([term(token) for token in excludes],'OR','false')
    return sql.SQL('({}) AND NOT ({})').format(inc,exc),ip+ep


def explore(conn,engine,kind,mode,sort,first,last,*,as_of=None,includes=(),excludes=(),rules=None,
            position=1,target_id=None,around=False,limit=50,scope_day=None):
    """Read sparse snapshots, then rank within the chosen membership scope."""
    conn.set_session(isolation_level='REPEATABLE READ',readonly=True)
    kind_id,table,id_column=engine.KINDS[kind]
    destroyed='weighted_destroyed_cents' if mode=='weighted' else 'destroyed_cents'
    fields={'destroyed':destroyed,'lost':'lost_cents','kills':'kills','losses':'losses',
            'unvalued_kills':'unvalued_kills','unvalued_losses':'unvalued_losses'}
    params=[]
    if as_of is None:
        values=sql.SQL(',').join(sql.SQL('c.{c} AS {a}').format(c=sql.Identifier(c),a=sql.Identifier(a)) for a,c in fields.items())
        snapshot=sql.SQL('')
    else:
        values=sql.SQL(',').join(sql.SQL('CASE WHEN c.last_month<=%s THEN c.{c} ELSE p.{p} END AS {a}').format(
            c=sql.Identifier(c),p=sql.Identifier('cumulative_'+c),a=sql.Identifier(a)) for a,c in fields.items())
        params.extend([last]*len(fields))
        snapshot=sql.SQL('''LEFT JOIN LATERAL (SELECT * FROM analytics.killmail_rankings_monthly m
            WHERE m.entity_kind=c.entity_kind AND m.entity_id=c.entity_id
              AND m.month<=%s AND c.last_month>%s ORDER BY month DESC LIMIT 1) p ON true''')
        params.extend((last,last))
    params.append(kind_id)
    scope_join=sql.SQL('')
    predicate=sql.SQL('true')
    if includes or excludes:
        joins,corp,alliance,join_params=affiliations(kind,'f',as_of)
        known = ('true' if kind=='alliance' else 'COALESCE(aa.affiliation_known,false)' if as_of is not None
                 else 'ca.corporation_id IS NOT NULL' if kind=='character' else 'aa.corporation_id IS NOT NULL')
        scope_join=sql.SQL('''scoped AS (SELECT f.*,{corp} AS corporation_id,{alliance} AS alliance_id,{known} AS alliance_known
                             FROM facts f {joins}),''').format(corp=corp,alliance=alliance,joins=joins,known=sql.SQL(known))
        params.extend(join_params)
        predicate,scope_params=scope_predicate(kind,includes,excludes,rules or {},scope_day)
        params.extend(scope_params)
    metric={'net':sql.SQL('destroyed-lost'),
            'efficiency':sql.SQL('100*destroyed/NULLIF(destroyed+lost,0)')}.get(sort,sql.Identifier(sort))
    base=sql.SQL('''WITH facts AS (SELECT c.entity_id,{values} FROM analytics.killmail_rankings_current c
            {snapshot} WHERE c.entity_kind=%s AND {players}),{scope_join}
        candidates AS (SELECT *,{metric} AS score FROM {source}
                        WHERE (kills>0 OR losses>0) AND ({predicate})) ''').format(
        values=values,snapshot=snapshot,scope_join=scope_join,metric=metric,
        players=engine.player_character_filter(kind,sql.SQL('c.entity_id')),
        source=sql.Identifier('scoped' if includes or excludes else 'facts'),predicate=predicate)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='25s'")
        cur.execute("SET LOCAL work_mem='32MB'")
        cur.execute('SET LOCAL max_parallel_workers_per_gather=2')
        if target_id:
            cur.execute(base+sql.SQL('SELECT score FROM candidates WHERE entity_id=%s'),(*params,target_id))
            target=cur.fetchone()
            if target is None:
                raise ValueError('This entity is not ranked at this date under the selected filters.')
            score=target[0]
            cur.execute(base+sql.SQL('''SELECT count(*)+1 FROM candidates
                WHERE score>%s::numeric OR (score IS NOT NULL AND %s::numeric IS NULL)
                  OR (score IS NOT DISTINCT FROM %s::numeric AND entity_id<%s)'''),(*params,score,score,score,target_id))
            position=int(cur.fetchone()[0])
        start=max(1,position-5) if around else position
        cur.execute(base+sql.SQL(''', top_rows AS (
            SELECT * FROM candidates ORDER BY score DESC NULLS LAST,entity_id OFFSET %s LIMIT %s)
            SELECT r.entity_id,r.destroyed,r.lost,r.kills,r.losses,r.unvalued_kills,r.unvalued_losses,
                   r.destroyed-r.lost AS net,100*r.destroyed/NULLIF(r.destroyed+r.lost,0) AS efficiency,e.name
            FROM top_rows r LEFT JOIN entities.{table} e ON e.{id_column}=r.entity_id
            ORDER BY r.score DESC NULLS LAST,r.entity_id''').format(table=sql.Identifier(table),id_column=sql.Identifier(id_column)),
            (*params,start-1,11 if around else limit+1))
        names=[c.name for c in cur.description]
        result=[dict(zip(names,r),rank=start+i,highlight=(r[0]==target_id or start+i==position)) for i,r in enumerate(cur)]
    if not result and around:
        raise ValueError('This rank is outside the selected ranking.')
    more=False if around else len(result)>limit
    return dict(rows=result if around else result[:limit],position=position,offset=start-1,more=more)
