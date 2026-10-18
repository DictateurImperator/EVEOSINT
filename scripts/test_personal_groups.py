"""Personal group ownership, historical membership, deduplication and public restrictions.

Database tests run only in the disposable killmail_values_test database.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
from datetime import UTC,date,datetime
from decimal import Decimal
import os
import threading
import time
import importlib
import unittest
from unittest.mock import patch

import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import test_killmail_rankings as base

f=base.fixture.f
routes=f.load('routes_personal_groups')
store=routes.store;jobs=routes.jobs;q=routes.queries
JAN=date(2026,1,1);FEB=date(2026,2,1);MAR=date(2026,3,1)


def config(members=None,**extra):
    return store.definition(dict(members=members or [dict(kind='corporation',id=10,op='include')],zones=[],**extra))


class DefinitionTests(unittest.TestCase):
    def test_dates_limits_types_and_exclusions(self):
        good=config();self.assertEqual(good['members'][0]['id'],10)
        for payload in [dict(members=[]),dict(members=[dict(kind='universe',id=1,op='include')]),
                        dict(members=[dict(kind='character',id=1,op='exclude')]),
                        dict(members=[dict(kind='character',id='1 OR true',op='include')]),
                        dict(members=[dict(kind='character',id=1,op='include')],**{'from':'2026-02-01','through':'2026-01-01'})]:
            with self.subTest(payload=payload),self.assertRaises(ValueError):store.definition(payload)
        with self.assertRaises(ValueError):store.definition(dict(members=[dict(kind='character',id=1,op='include')]*61))

    def test_mutations_are_covered_by_the_application_csrf_guard(self):
        source=ast.parse((Path(__file__).resolve().parents[1]/'web/app/main.py').read_text())
        function=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='_requires_csrf_check')
        scope={'Request':object}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'<csrf>','exec'),scope)
        check=scope['_requires_csrf_check']
        for suffix in ['save','9/prepare','9/stop','9/sharing','9/delete','9/managers']:
            self.assertTrue(check(SimpleNamespace(method='POST',url=SimpleNamespace(path='/account/groups/'+suffix))))
        self.assertFalse(check(SimpleNamespace(method='GET',url=SimpleNamespace(path='/account/groups/9'))))

    def test_coalition_cycles_are_rejected(self):
        c=config([dict(kind='coalition',id=1,op='include')])
        with self.assertRaises(ValueError):q.actor_predicate(c,{'1':[dict(kind='coalition',id=1,op='include')]},'a')


class LocalRankingStateTests(unittest.TestCase):
    def test_duplicate_polling_shares_one_worker_and_other_scans_wait(self):
        ranking=routes.local_rankings
        with ranking._LOCK:ranking._RUNS.clear()
        entered=threading.Event();release=threading.Event()
        group=dict(id=9,revision=1);result=dict(period='all',built_at=datetime.now(UTC))
        def scan(*args):
            entered.set();release.wait(2)
            args[-1](checked=1,total=1,rows=[dict(entity_id=1)])
            return [dict(entity_id=1)]
        with patch.object(ranking,'scan',side_effect=scan) as worker:
            try:
                self.assertEqual(ranking.state(group,result,'character','classic','destroyed')['status'],'running')
                self.assertTrue(entered.wait(1))
                self.assertEqual(ranking.state(group,result,'character','classic','destroyed')['status'],'running')
                self.assertEqual(ranking.state(group,result,'character','weighted','destroyed')['status'],'busy')
                worker.assert_called_once()
            finally:release.set()
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                data=ranking.state(group,result,'character','classic','destroyed')
                if data['status']=='completed':break
                time.sleep(.01)
            self.assertEqual(data['status'],'completed');self.assertEqual(data['progress']['checked'],1)
            worker.assert_called_once()


class RouteTests(unittest.TestCase):
    def setUp(self):
        app=FastAPI();app.include_router(routes.router);self.client=TestClient(app)
        self.user=dict(id=1,username='Owner',permissions={'entities.view'})
        self.auth=patch.object(routes,'require_login',return_value=self.user);self.login=self.auth.start();self.addCleanup(self.auth.stop)
        monthly=patch.object(store,'monthly_results',return_value=[]);self.monthly=monthly.start();self.addCleanup(monthly.stop)
        self.group=dict(id=9,owner_id=1,name='<Private group>',description='Description',definition=config(),
                        revision=2,prepared_revision=2,prepared_rules={},public_token='a'*32,public_tabs=['stats'],
                        progress={'state':'completed'},prepared_at=datetime.now(UTC))
        self.result=dict(group_id=9,revision=2,period='2026-01',from_date=JAN,through_date=date(2026,1,31),built_at=datetime.now(UTC),
                         totals=dict(kills=2,losses=1,destroyed_cents='10000',weighted_destroyed_cents='3000',lost_cents='5000',unvalued_kills=0,unvalued_losses=0),ship_counts={},roster_count=3,skills_count=2)

    def test_public_has_no_account_controls_and_enforces_page_blocks(self):
        with patch.object(store,'public',return_value=self.group),patch.object(store,'period_result',return_value=self.result),patch.object(store,'periods',return_value=[dict(period='2026-01'),dict(period='custom')]):
            response=self.client.get('/public/groups/'+'a'*32+'?view=stats&period=2026-01')
            self.assertEqual(response.status_code,200)
            self.assertIn('&lt;Private group&gt;',response.text)
            self.assertIn('30.000%',response.text)
            self.assertNotIn('groupEditor',response.text)
            self.assertNotIn('/account/groups',response.text)
            self.assertNotIn('Custom period (private)',response.text)
            self.assertNotIn('owner_id',response.text)
            self.assertIn('no-store',response.headers['cache-control'])
            for view in ['ships','killboard']:
                self.assertEqual(self.client.get('/public/groups/'+'a'*32+f'?view={view}').status_code,403)
                self.assertEqual(self.client.get('/public/groups/'+'a'*32+f'/data?view={view}').status_code,403)
            for query in ['period=custom','from=2007-01-01','include=character:42','view=stats&cursor=forged']:
                self.assertIn(self.client.get('/public/groups/'+'a'*32+'/data?'+query).status_code,(400,403))
            self.assertEqual(self.client.post('/public/groups/'+'a'*32+'/prepare').status_code,404)

    def test_public_custom_period_and_revoked_links_are_not_available(self):
        with patch.object(store,'public',return_value=None):
            self.assertEqual(self.client.get('/public/groups/'+'b'*32).status_code,404)
            self.assertEqual(self.client.get('/public/groups/'+'b'*32+'/data').status_code,404)

    def test_private_group_is_never_selected_without_owner_id(self):
        with patch.object(store,'accessible',return_value=None) as lookup:
            for path in ['/account/groups/9','/account/groups/9/edit','/account/groups/9/status','/account/groups/9/data']:
                self.assertEqual(self.client.get(path).status_code,404)
                lookup.assert_called_with(9,1)
            for path in ['prepare','stop','sharing','delete']:
                self.assertEqual(self.client.post('/account/groups/9/'+path).status_code,404)
        self.user['permissions']=set()
        self.assertEqual(self.client.get('/account/groups').status_code,403)
        self.login.return_value=None
        self.assertEqual(self.client.get('/account/groups').status_code,401)

    def test_monthly_history_shows_ratio_and_private_save_handles_invalid_definitions(self):
        self.monthly.return_value=[dict(period='2026-01',totals=self.result['totals'])]
        with patch.object(store,'accessible',return_value=self.group),patch.object(store,'period_result',return_value=self.result),patch.object(store,'periods',return_value=[]):
            page=self.client.get('/account/groups/9?view=stats&period=2026-01')
            self.assertIn('Monthly history',page.text)
            self.assertEqual(page.text.count('30.000%'),2)
        with patch.object(store,'save',side_effect=ValueError('Choose at least one member.')):
            response=self.client.post('/account/groups/save',data={'name':'Test','definition':'{}'})
            self.assertEqual(response.status_code,400)
            self.assertIn('Choose at least one member.',response.text)

    def test_local_ranking_page_can_be_published_or_blocked_and_has_only_fixed_options(self):
        with patch.object(store,'public',return_value=self.group),patch.object(store,'period_result',return_value=self.result),patch.object(store,'periods',return_value=[]),patch.object(routes.local_rankings,'state',return_value=dict(status='running',progress={'checked':1,'total':3},rows=[])) as scan:
            url='/public/groups/'+'a'*32
            self.assertEqual(self.client.get(url+'/data?view=ranking').status_code,403)
            scan.assert_not_called()
            self.group['public_tabs']=['ranking']
            page=self.client.get(url+'?view=ranking')
            self.assertEqual(page.status_code,200)
            self.assertIn('Local ranking',page.text);self.assertNotIn('Management</a>',page.text)
            data=self.client.get(url+'/data?view=ranking')
            self.assertEqual(data.status_code,202)
            self.assertEqual(data.json()['progress']['checked'],1)
            for query in ['kind=coalition','page=999','mode=arbitrary','from=2026-01-01']:
                self.assertEqual(self.client.get(url+'/data?view=ranking&'+query).status_code,400)
            self.group['public_tabs']=[]
            self.assertEqual(self.client.get(url+'/data?view=ranking').status_code,403)

    def test_delegates_cannot_delete_or_delegate_and_owners_get_management_tab(self):
        with patch.object(store,'accessible',return_value=self.group),patch.object(store,'period_result',return_value=None),patch.object(store,'periods',return_value=[]),patch.object(store,'managers',return_value=[]):
            response=self.client.get('/account/groups/9?view=management')
            self.assertEqual(response.status_code,200);self.assertIn('Add a manager',response.text)
            self.user['id']=2
            response=self.client.get('/account/groups/9?view=management')
            self.assertEqual(response.status_code,200);self.assertNotIn('Delete group</button>',response.text)
            self.assertNotIn('id="managerForm"',response.text)
            self.assertEqual(self.client.post('/account/groups/9/delete').status_code,403)
            self.assertEqual(self.client.post('/account/groups/9/managers',data={'action':'add','user_id':'3'}).status_code,403)
            self.assertEqual(self.client.get('/account/groups/9/managers/lookup?q=Owner').status_code,403)

    def test_delegated_preparation_does_not_increase_owner_ship_permissions(self):
        self.user['id']=2;self.user['permissions'].add('superintel.view')
        with patch.object(store,'accessible',return_value=self.group),patch.object(f.auth,'get_user_permissions',return_value={'entities.view'}),patch.object(jobs,'start') as start:
            response=self.client.post('/account/groups/9/prepare')
            self.assertEqual(response.status_code,200)
            start.assert_called_once_with(self.group,False,custom=None,actor_id=2)

    def test_member_search_returns_portraits_affiliations_and_an_entity_type_filter(self):
        coalitions=importlib.import_module(store.__package__+'.coalitions')
        pilot=dict(entity_type='character',entity_id=42,name='Pilot',corporation_name='Corp',alliance_name='Alliance')
        corp=dict(entity_type='corporation',entity_id=10,name='Corp')
        with patch.object(base.rankings,'lookup',return_value=[pilot]) as find,patch.object(coalitions,'search_member_entities',return_value=[corp]):
            response=self.client.get('/account/groups/lookup?kind=member&q=Pi')
            data=response.json()['results'];self.assertEqual(response.status_code,200)
            self.assertEqual([r['section'] for r in data],['CORPORATIONS','CHARACTERS'])
            by_kind={r['kind']:r for r in data}
            self.assertIn('/characters/42/portrait?',by_kind['character']['image_url'])
            self.assertIn('Corp · Alliance',by_kind['character']['subtitle'])
            self.assertIn('/corporations/10/logo?',by_kind['corporation']['image_url'])
            find.reset_mock()
            response=self.client.get('/account/groups/lookup?kind=member&entity_type=corporation&q=Co')
            find.assert_not_called();self.assertEqual([r['kind'] for r in response.json()['results']],['corporation'])

    def test_private_editor_and_result_controls_render(self):
        with patch.object(store,'accessible',return_value=self.group),patch.object(store,'period_result',return_value=self.result),patch.object(store,'periods',return_value=[dict(period='2026-01')]):
            self.assertEqual(self.client.get('/account/groups/9/edit').status_code,200)
            response=self.client.get('/account/groups/9?period=2026-01')
            self.assertEqual(response.status_code,200)
            self.assertIn('Pages available through this link',response.text)
            self.assertIn('name="tabs" value="ships"',response.text)


@unittest.skipUnless(os.environ.get('KILLMAIL_VALUES_TEST_DSN'),'Disposable PostgreSQL required')
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        base.DatabaseTests.setUp(self)
        with self.conn.cursor() as cur:
            cur.execute('''DROP SCHEMA IF EXISTS web CASCADE;CREATE SCHEMA web;
              CREATE TABLE web.users(id bigint PRIMARY KEY,username text,is_active boolean DEFAULT true);INSERT INTO web.users(id,username) VALUES(1,'Owner'),(2,'Manager'),(3,'Outsider');
              CREATE TABLE entities.coalitions(coalition_id bigint PRIMARY KEY,name text);
              INSERT INTO entities.coalitions VALUES(1,'Changing bloc'),(2,'Nested bloc');
              CREATE TABLE entities.coalition_memberships(id bigserial,coalition_id bigint,operation text,member_type text,member_id bigint,valid_from date,valid_to date);
              INSERT INTO entities.coalition_memberships(coalition_id,operation,member_type,member_id,valid_from,valid_to) VALUES
                (1,'include','corporation',10,'2026-01-01','2026-01-31'),
                (1,'include','corporation',11,'2026-02-01',NULL),
                (2,'include','coalition',1,NULL,NULL);
              INSERT INTO entities.characters VALUES(3,'Pilot Three'),(4,'Inactive member'),(9,'Victim');
              INSERT INTO entities.character_corporation_history VALUES(4,1,10,'2025-01-01',NULL,false);
              CREATE TABLE entities.pilotable_ship_index(ship_type_id bigint,bit_index integer,enabled boolean);
              INSERT INTO entities.pilotable_ship_index VALUES(638,0,true),(587,1,true);
              CREATE TABLE entities.character_pilotable_ships(character_id bigint PRIMARY KEY,ship_mask bytea);
              INSERT INTO entities.character_pilotable_ships VALUES(1,'\x01'::bytea),(2,'\x03'::bytea),(4,'\x02'::bytea);
              CREATE SCHEMA sde_work;CREATE TABLE sde_work.ship_entities(entity_id bigint,group_name text);
              INSERT INTO sde_work.ship_entities VALUES(638,'Battleship'),(587,'Frigate');
              ALTER TABLE rawkm.killmail_attackers ADD COLUMN ship_type_id bigint DEFAULT 587;
              ALTER TABLE rawkm.killmails ADD COLUMN solar_system_id bigint DEFAULT 7;
              ALTER TABLE rawkm.killmails ADD COLUMN victim_ship_type_id bigint DEFAULT 638;
            ''')
            cur.execute(store.schema_sql())
        self.conn.commit()
        self.patches=[]
        for module in [store,jobs,routes,routes.local_rankings]:
            patcher=patch.object(module,'db',side_effect=lambda:psycopg2.connect(self.dsn));patcher.start();self.patches.append(patcher)

    def tearDown(self):
        for patcher in self.patches:patcher.stop()
        self.conn.rollback()
        with self.conn.cursor() as cur:cur.execute('DROP SCHEMA IF EXISTS web CASCADE;DROP SCHEMA IF EXISTS sde_work CASCADE')
        self.conn.commit();base.DatabaseTests.tearDown(self)

    def make_group(self,payload=None):
        id=store.save(1,'Test group','Description',payload or config())
        return store.owned(id,1)

    def test_quota_ownership_delete_and_link_revocation(self):
        groups=[self.make_group() for _ in range(3)]
        with self.assertRaises(ValueError):self.make_group()
        self.assertIsNone(store.owned(groups[0]['id'],2))
        with self.assertRaises(LookupError):store.remove(groups[0]['id'],2)
        group=groups[0]
        with self.conn.cursor() as cur:cur.execute('UPDATE web.personal_groups SET prepared_revision=revision WHERE id=%s',(group['id'],))
        self.conn.commit()
        token=store.sharing(group['id'],1,True,['stats'])
        self.assertIsNotNone(store.public(token))
        store.sharing(group['id'],1,False,[])
        self.assertIsNone(store.public(token))
        new_token=store.sharing(group['id'],1,True,['stats'])
        self.assertNotEqual(token,new_token)
        store.save(1,'Edited','',config(),group['id'])
        self.assertIsNone(store.public(new_token))
        store.remove(group['id'],1)
        self.make_group()
        self.assertEqual(len(store.listed(1)),3)

    def test_concurrent_creations_cannot_exceed_three_groups(self):
        def create(_):
            try:return self.make_group()['id']
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(create,range(4)))
        self.assertEqual(sum(id is not None for id in results),3)
        self.assertEqual(len(store.listed(1)),3)

    def test_killboard_pagination_preserves_losses_values_and_scope(self):
        group=self.make_group()
        result=dict(from_date=JAN,through_date=date(2026,1,31))
        def hydrate(conn,entries):
            # The shared global hydrator labels all entries as kills; groups must restore the side.
            return [dict(killmail_id=id,killmail_time=at,is_loss=False,is_kill=True) for side,id,at in entries]
        with patch.object(q.entities,'_hydrate_global_killmail_entries',side_effect=hydrate):
            pages=[];cursor=''
            for _ in range(5):
                data=q.killboard(self.conn,group,result,cursor,limit=1)
                pages.extend(data['rows']);cursor=data['next_cursor']
                if not cursor:break
            self.assertEqual([r['killmail_id'] for r in pages],[102,101,100])
            self.assertTrue(pages[0]['is_loss']);self.assertNotIn('true_value_label',pages[0])
            self.assertTrue(pages[1]['is_loss']);self.assertEqual(pages[1]['true_value_label'],'200')
            self.assertFalse(pages[2]['is_loss']);self.assertEqual(pages[2]['true_value_label'],'50')
            for bad in ['-1','999','0|2026-01-02T12:00:00|100','0|bad|100','0|2026-01-02T12:00:00+00:00|-1']:
                with self.assertRaises(ValueError):q.killboard(self.conn,group,result,bad)

    def test_direct_inactive_member_and_dated_exclusion(self):
        c=config([dict(kind='character',id=9,op='include')])
        data=q.pilotable_counts(self.conn,c,{},JAN,FEB)
        self.assertEqual(data['roster_count'],1) # Explicit member with no affiliation history.
        c=config([dict(kind='corporation',id=10,op='include'),dict(kind='character',id=4,op='exclude',**{'from':'2026-01-16'})])
        data=q.pilotable_counts(self.conn,c,{},JAN,FEB)
        self.assertEqual(data['roster_count'],3) # Member before the exclusion still belongs to this interval.
        data=q.pilotable_counts(self.conn,c,{},date(2026,1,16),FEB)
        self.assertEqual(data['roster_count'],2)

    def test_interrupted_preparation_can_be_restarted_after_a_process_exit(self):
        group=self.make_group()
        with self.conn.cursor() as cur:
            cur.execute("UPDATE web.personal_groups SET progress=%s WHERE id=%s",(psycopg2.extras.Json(dict(state='running')),group['id']))
        self.conn.commit()
        stale=store.owned(group['id'],1)
        # An actual worker's advisory lock prevents incorrectly marking it interrupted.
        with self.conn.cursor() as cur:cur.execute('SELECT pg_advisory_lock(%s)',(jobs.LOCK_ID,))
        self.assertEqual(jobs.recover_interrupted(stale)['progress']['state'],'running')
        with self.conn.cursor() as cur:cur.execute('SELECT pg_advisory_unlock(%s)',(jobs.LOCK_ID,))
        self.assertEqual(jobs.recover_interrupted(stale)['progress']['state'],'interrupted')

    def test_union_dedup_exclusions_damage_and_geographical_scope(self):
        c=config([dict(kind='corporation',id=10,op='include'),dict(kind='character',id=1,op='include')])
        relations=q.relations(self.conn);jan=next(r for r in relations if r[0]==JAN)
        result=q.month_totals(self.conn,c,{},jan,JAN,FEB,(None,[]))
        self.assertEqual(result['kills'],2)
        self.assertEqual(result['losses'],2)
        self.assertEqual(result['destroyed_cents'],10000)
        self.assertEqual(result['weighted_destroyed_cents'],5000)
        c['members'].append(dict(kind='character',id=2,op='exclude'))
        result=q.month_totals(self.conn,c,{},jan,JAN,FEB,(None,[]))
        self.assertEqual(result['weighted_destroyed_cents'],3000)
        self.assertEqual(result['losses'],1)
        self.assertEqual(q.month_totals(self.conn,c,{},jan,JAN,FEB,([999],[]))['matched'],0)
        self.assertEqual(q.month_totals(self.conn,c,{},jan,JAN,FEB,([7],[]))['matched'],3)

    def test_historical_nested_coalitions_and_inactive_roster(self):
        c=config([dict(kind='coalition',id=2,op='include')]);rules=q.load_rules(self.conn)
        data=q.pilotable_counts(self.conn,c,rules,JAN,FEB)
        self.assertEqual(data['roster_count'],3) # Includes inactive character 4.
        self.assertEqual(data['counts'],{'638':2,'587':2})
        later=q.pilotable_counts(self.conn,c,rules,FEB,MAR)
        self.assertEqual(later['roster_count'],1)
        self.assertEqual(later['counts'],{'638':1})
        whole=q.pilotable_counts(self.conn,c,rules,JAN,MAR)
        self.assertEqual(whole['roster_count'],3) # Character 1 is not counted twice.
        self.assertEqual(whole['counts'],data['counts'])

    def test_ship_model_group_role_and_exclusions_filter_the_same_killmails(self):
        jan=next(r for r in q.relations(self.conn) if r[0]==JAN)
        c=config();c['ships']=[dict(kind='ship_group',id='Battleship',op='include',role='victim')]
        self.assertEqual(q.month_totals(self.conn,c,{},jan,JAN,FEB,(None,[]))['matched'],3)
        c['ships'][0]['id']='Frigate'
        self.assertEqual(q.month_totals(self.conn,c,{},jan,JAN,FEB,(None,[]))['matched'],0)
        c['ships'][0]['role']='attacker'
        self.assertEqual(q.month_totals(self.conn,c,{},jan,JAN,FEB,(None,[]))['matched'],3)
        c['ships'].append(dict(kind='ship',id=638,op='exclude',role='victim'))
        self.assertEqual(q.month_totals(self.conn,c,{},jan,JAN,FEB,(None,[]))['matched'],0)

    def test_local_rankings_use_exact_own_damage_and_deduplicate_corporations(self):
        group=self.make_group();result=dict(period='2026-01',from_date=JAN,through_date=date(2026,1,31),built_at=datetime.now(UTC))
        progress=[]
        rows=routes.local_rankings.scan(group,result,'character','weighted','destroyed',lambda **values:progress.append(values))
        by_id={r['entity_id']:r for r in rows}
        self.assertEqual(set(by_id),{1,2})
        self.assertEqual(by_id[1]['raw'],10000);self.assertEqual(by_id[1]['weighted'],3000)
        self.assertEqual(by_id[2]['weighted'],2000)
        self.assertEqual(by_id[1]['lost'],20000)
        self.assertEqual(by_id[2]['unvalued_losses'],1)
        self.assertEqual(progress[-1]['checked'],1)
        corps=routes.local_rankings.scan(group,result,'corporation','classic','destroyed',lambda **values:None)
        self.assertEqual(len(corps),1)
        self.assertEqual(corps[0]['raw'],10000) # Full kill value once for corp, not once per pilot.
        self.assertEqual(corps[0]['weighted'],5000)
        group['definition']['members'].append(dict(kind='character',id=2,op='exclude'))
        fewer=routes.local_rankings.scan(group,result,'corporation','weighted','destroyed',lambda **values:None)
        self.assertEqual(fewer[0]['weighted'],3000)
        self.assertEqual(fewer[0]['losses'],1)

    def test_local_rankings_follow_membership_dates_and_never_include_outside_pilots(self):
        c=config([dict(kind='character',id=1,op='include',**{'from':'2026-02-01','through':'2026-02-02'})])
        group=self.make_group(c);result=dict(period='all',from_date=JAN,through_date=date(2026,2,28),built_at=datetime.now(UTC))
        rows=routes.local_rankings.scan(group,result,'character','weighted','destroyed',lambda **values:None)
        self.assertEqual([r['entity_id'] for r in rows],[1])
        self.assertEqual(rows[0]['raw'],30000);self.assertEqual(rows[0]['weighted'],30000)
        self.assertEqual(rows[0]['kills'],1);self.assertEqual(rows[0]['losses'],0)

    def test_delegation_access_edit_revocation_and_independent_quotas(self):
        group=self.make_group()
        self.assertIsNone(store.accessible(group['id'],2))
        store.set_manager(group['id'],1,2)
        self.assertEqual(store.accessible(group['id'],2)['owner_id'],1)
        self.assertEqual(len(store.delegated(2)),1);self.assertEqual(len(store.listed(2)),0)
        self.assertEqual(store.managers(group['id'],1)[0]['username'],'Manager')
        self.assertEqual(store.manager_accounts(group['id'],1,'Out')[0]['id'],3)
        with self.assertRaises(LookupError):store.set_manager(group['id'],2,3)
        with self.assertRaises(LookupError):store.remove(group['id'],2)
        store.save(1,'Managed edit','',config(),group['id'],actor_id=2)
        self.assertEqual(store.owned(group['id'],1)['name'],'Managed edit')
        store.set_manager(group['id'],1,2,remove=True)
        self.assertIsNone(store.accessible(group['id'],2))
        with self.assertRaises(LookupError):store.save(1,'Forbidden','',config(),group['id'],actor_id=2)

    def test_preparation_writes_only_aggregate_periods_and_publishes_after_completion(self):
        group=self.make_group(config([dict(kind='coalition',id=2,op='include')]))
        with self.assertRaises(ValueError):store.sharing(group['id'],1,True,['stats'])
        jobs.prepare(psycopg2.connect(self.dsn),group,True)
        group=store.owned(group['id'],1)
        self.assertEqual(group['progress']['state'],'completed',group['progress'])
        self.assertEqual(group['prepared_revision'],group['revision'])
        periods=store.periods(group)
        self.assertEqual({p['period'] for p in periods},{'all','2026','2026-01','2026-02'})
        whole=store.period_result(group,'all')
        self.assertEqual(whole['roster_count'],3)
        self.assertEqual(whole['ship_counts'],{'638':2,'587':2})
        self.assertEqual(Decimal(whole['totals']['destroyed_cents']),40000)
        token=store.sharing(group['id'],1,True,['stats'])
        self.assertIsNotNone(store.public(token))
        jobs.prepare(psycopg2.connect(self.dsn),group,True,custom=(JAN,date(2026,1,31)))
        custom=store.period_result(group,'custom')
        self.assertEqual(Decimal(custom['totals']['destroyed_cents']),10000)
        self.assertEqual(store.public(token)['prepared_revision'],group['revision'])
        with self.conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM rawkm.killmails')
            self.assertEqual(cur.fetchone()[0],5)
