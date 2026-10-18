"""Fixed-quantity economic baskets from local zKill quotes; no writes or API calls."""
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import threading
import hashlib
import time
from .db import db

SHIP_CLASSES = {
    'Corvettes': {237}, 'Shuttles': {31},
    'Frigates': {25,324,830,831,834,893,1283,1527},
    'Destroyers': {420,541,1305,1534},
    'Cruisers': {26,358,832,833,894,906,963,1972},
    'Battlecruisers': {419,540,1201,4902}, 'Battleships': {27,898,900},
    'Haulers': {28}, 'Blockade runners': {1202}, 'Deep space transports': {380},
    'Mining barges / exhumers': {463,543},
    'Dreadnoughts': {485,4594}, 'Carriers': {547,5120}, 'Force auxiliaries': {1538},
    'Capital industrial ships': {883}, 'Freighters': {513}, 'Jump freighters': {902},
    'Supercarriers': {659}, 'Titans': {30},
}
CAPITAL = {485,4594,547,5120,1538,883,513,902}
SUPERCAPITAL = {659,30}
META = {1:'T1',2:'T2',14:'T3',3:'Storyline',4:'Faction',5:'Officer',6:'Deadspace'}
_CACHE = None
_CACHE_AT = 0
_CACHE_LOCK = threading.Lock()


def market_path(identifier, markets):
    result=[];seen=set()
    while identifier in markets and identifier not in seen:
        seen.add(identifier);name,parent=markets[identifier];result.append(name);identifier=parent
    return result


def classify(item, path):
    """Size/class first; Triglavian overrides technology and market ancestry."""
    if item['meta']==15 or 'abyssal' in item['name'].lower():return None
    category=item['category'];group=item['group']
    if category==6:
        if group==29 or 'Special Edition Ships' in path:return None
        size=next((name for name,ids in SHIP_CLASSES.items() if group in ids),None)
        if group==941:size=item['name'] # Orca and Porpoise have different hull sizes.
        if not size:return None
        family='Triglavian' if item['race']==135 or item['faction']==500026 else 'Supercapital' if group in SUPERCAPITAL else 'Capital' if group in CAPITAL else 'Subcapital'
        variant='Navy' if 'Navy Faction' in path else 'Pirate' if 'Pirate Faction' in path else META.get(item['meta'],'T1')
        return family,size,variant
    if category==7:return 'Modules',item['group_name'],META.get(item['meta'],'T1 / named')
    if category in (4,25,42,43):return 'Materials',item['group_name'],'All'
    return None


def catalog(conn):
    global _CACHE, _CACHE_AT
    with _CACHE_LOCK:
        if _CACHE is not None and time.monotonic()-_CACHE_AT<300:return _CACHE
    with conn.cursor() as cur:
        cur.execute("SELECT sde_key::bigint,data->'name'->>'en',NULLIF(data->>'parentGroupID','')::bigint FROM public.sde_marketgroups")
        markets={id:(name,parent) for id,name,parent in cur}
        cur.execute("""SELECT t.sde_key::bigint,t.data->'name'->>'en',
            (t.data->>'groupID')::int,(g.data->>'categoryID')::int,
            COALESCE((t.data->>'metaGroupID')::int,1),COALESCE((t.data->>'raceID')::int,0),
            COALESCE((t.data->>'factionID')::int,0),(t.data->>'marketGroupID')::bigint,
            g.data->'name'->>'en'
            FROM public.sde_types t JOIN public.sde_groups g ON g.sde_key=t.data->>'groupID'
            WHERE t.data->>'published'='true' AND t.data->>'marketGroupID' IS NOT NULL
            ORDER BY t.sde_key::bigint""")
        items={};baskets={};membership=defaultdict(list)
        for row in cur:
            item=dict(zip(('id','name','group','category','meta','race','faction','market','group_name'),row))
            if item['meta']==15 or 'abyssal' in item['name'].lower():continue
            items[item['id']]=item
            classification=classify(item,market_path(item['market'],markets))
            if not classification:continue
            family,size,variant=classification
            for label in (family,f'{family} / {size}',f'{family} / {size} / {variant}'):
                membership[label].append(item['id'])
        for index,(label,ids) in enumerate(sorted(membership.items())):
            key=str(index);baskets[key]={'key':key,'label':label,'ids':ids}
    # IDs derive from labels rather than a mutable ordinal: saved selections survive SDE refreshes.
    baskets={hashlib.sha256(b['label'].encode()).hexdigest()[:16]:b for b in baskets.values()}
    for key,basket in baskets.items():basket['key']=key
    result={'items':items,'baskets':baskets}
    with _CACHE_LOCK:_CACHE=result;_CACHE_AT=time.monotonic()
    return result


def validate(payload, available):
    try:
        base=date.fromisoformat(payload['base']);start=date.fromisoformat(payload['start']);end=date.fromisoformat(payload['end'])
    except (KeyError,TypeError,ValueError):raise ValueError('Choose valid reference, start and end dates.')
    if not date(2003,1,1)<=start<=base<=end<=date.today():raise ValueError('Dates must satisfy: start ≤ reference ≤ end ≤ today (UTC).')
    selections=payload.get('presets',[]);custom=payload.get('items',[])
    if not isinstance(selections,list) or len(selections)>6 or not isinstance(custom,list) or len(custom)>100:
        raise ValueError('Choose at most six indices and 100 custom items.')
    baskets=[]
    if any(not isinstance(key,str) for key in selections):raise ValueError('Invalid index selection.')
    for key in dict.fromkeys(selections):
        if not isinstance(key,str) or key not in available['baskets']:raise ValueError('Unknown index. Reload the SDE catalog.')
        basket=available['baskets'][key];baskets.append({'name':basket['label'],'quantities':{id:Decimal(1) for id in basket['ids']}})
    quantities={}
    for entry in custom:
        try:
            if not str(entry['id']).isdigit():raise ValueError()
            id=int(entry['id']);quantity=Decimal(str(entry['quantity']))
            if isinstance(entry['id'],bool) or id not in available['items'] or not quantity.is_finite() or not 0<quantity<=Decimal('1e12'):raise ValueError()
        except (KeyError,TypeError,ValueError,InvalidOperation):raise ValueError('Every basket item needs a valid object and a positive quantity (maximum 10¹²).')
        quantities[id]=quantities.get(id,Decimal(0))+quantity
        if quantities[id]>Decimal('1e12'):raise ValueError('Combined quantity exceeds 10¹² for one object.')
    if quantities:
        name=str(payload.get('name') or 'My basket').strip()[:80]
        baskets.append({'name':name,'quantities':quantities})
    if not baskets:raise ValueError('Select an index or add objects to your basket.')
    if len({id for basket in baskets for id in basket['quantities']})>6000:raise ValueError('Choose smaller sub-indices: maximum 6,000 distinct objects per comparison.')
    return start,base,end,baskets


def next_month(day):return date(day.year+(day.month==12),day.month%12+1,1)


def build_result(baskets, snapshots, base, end, items):
    reference=snapshots[base];latest=snapshots[end];series=[]
    for basket in baskets:
        quantities=basket['quantities']
        eligible={id:q for id,q in quantities.items() if id in reference}
        excluded=[{'id':id,'name':items[id]['name']} for id in quantities if id not in reference]
        base_cost=sum((q*reference[id][0] for id,q in eligible.items()),Decimal(0))
        points=[]
        for at,quotes in sorted(snapshots.items()):
            missing=[id for id in eligible if id not in quotes]
            cost=sum((q*quotes[id][0] for id,q in eligible.items() if id in quotes),Decimal(0))
            ready=bool(eligible) and not missing
            points.append({'date':at.isoformat(),'index':float(100*cost/base_cost) if ready else None,
                'cost':str(cost) if ready else None,'missing':len(missing),
                'carried':sum(quotes[id][1]<at for id in eligible if id in quotes)})
        details=[]
        for id,q in quantities.items():
            before=reference.get(id);after=latest.get(id)
            details.append({'id':id,'name':items[id]['name'],'quantity':str(q),'included':id in eligible,
                'base_price':str(before[0]) if before else None,'base_quote_date':before[1].isoformat() if before else None,
                'end_price':str(after[0]) if after else None,'end_quote_date':after[1].isoformat() if after else None,
                'change':float(100*(after[0]/before[0]-1)) if before and after else None})
        series.append({'name':basket['name'],'base_cost':str(base_cost) if eligible else None,
            'included':len(eligible),'requested':len(quantities),'excluded':excluded,'points':points,'items':details})
    return {'reference':base.isoformat(),'end':end.isoformat(),'series':series}


def calculate(payload, report, control):
    with closing(db()) as conn:
        control['connection']=conn
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn,conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='60s'; SET LOCAL timezone='UTC'; SET LOCAL jit=off; SET LOCAL work_mem='16MB'")
            available=catalog(conn);start,base,end,baskets=validate(payload,available)
            ids=sorted({str(id) for basket in baskets for id in basket['quantities']})
            cur.execute('SELECT min(price_date),max(price_date) FROM market.zkill_daily_prices')
            first,last=cur.fetchone()
            if not first:raise ValueError('No local zKill price history is available.')
            if end>last:raise ValueError(f'Prices are imported through {last}. Choose an end date on or before that date.')
            if base<first:raise ValueError(f'The first imported price date is {first}. Choose a later reference.')
            # Replay past quotes once. Never take a future quote to fill an earlier date.
            targets={start,base,end};month=start.replace(day=1)
            while month<=end:
                targets.add(min(next_month(month)-timedelta(days=1),end));month=next_month(month)
            targets=sorted(targets);snapshots={};quotes={};index=0
            month=first.replace(day=1);months=[]
            while month<=end:months.append(month);month=next_month(month)
            for number,month in enumerate(months):
                if control['stop'].is_set():raise ValueError('Calculation cancelled.')
                report(phase='Reading local prices',checked=number,total=len(months),month=month.isoformat()[:7])
                cur.execute("""SELECT d.price_date,p.key,p.value FROM market.zkill_daily_prices d
                    CROSS JOIN LATERAL jsonb_each_text(d.prices) p
                    WHERE d.price_date >= %s AND d.price_date < %s AND d.price_date <= %s
                    AND p.key=ANY(%s) ORDER BY d.price_date,p.key""",(month,next_month(month),end,ids))
                for at,key,value in cur:
                    while index<len(targets) and targets[index]<at:
                        snapshots[targets[index]]=dict(quotes);index+=1
                    try:price=Decimal(value)
                    except InvalidOperation:continue
                    if price.is_finite() and price>0:quotes[int(key)]=(price,at)
                until=min(next_month(month)-timedelta(days=1),end)
                while index<len(targets) and targets[index]<=until:
                    snapshots[targets[index]]=dict(quotes);index+=1
                report(phase='Reading local prices',checked=number+1,total=len(months),month=month.isoformat()[:7])
            return build_result(baskets,snapshots,base,end,available['items'])
