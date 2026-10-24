"""Disposable PostgreSQL integration. No production DB, credentials or ESI calls."""
from contextlib import ExitStack
from datetime import date, datetime, timezone
from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

import psycopg2
from scripts import live_killmail_backend as live
from scripts import calculate_killmail_values as values

PG=Path('/usr/lib/postgresql/16/bin')


@unittest.skipUnless((PG/'initdb').exists(),'Local PostgreSQL binaries required')
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='eveosint-live-db-');cls.root=Path(cls.tmp.name)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));cls.port=sock.getsockname()[1]
        subprocess.run([str(PG/'initdb'),'-D',str(cls.root/'db'),'-A','trust','-U','test','--no-locale'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        subprocess.run([str(PG/'pg_ctl'),'-D',str(cls.root/'db'),'-l',str(cls.root/'server.log'),'-o',f"-F -k {cls.root} -h '' -p {cls.port}",'-w','start'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        cls.kwargs=dict(dbname='postgres',user='test',host=str(cls.root),port=cls.port)

    @classmethod
    def tearDownClass(cls):
        subprocess.run([str(PG/'pg_ctl'),'-D',str(cls.root/'db'),'-m','immediate','-w','stop'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        cls.tmp.cleanup()

    def connect(self):return psycopg2.connect(**self.kwargs)

    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(live.importer,'db',side_effect=self.connect))
        self.stack.enter_context(patch.object(live.affiliations,'db_connect',side_effect=self.connect))
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute('DROP SCHEMA IF EXISTS rawkm CASCADE; DROP SCHEMA IF EXISTS market CASCADE; DROP SCHEMA IF EXISTS entities CASCADE; DROP SCHEMA IF EXISTS esi CASCADE')
                cur.execute('DROP TABLE IF EXISTS public.sde_types,public.sde_groups CASCADE; CREATE TABLE public.sde_types(sde_key text PRIMARY KEY,data jsonb); CREATE TABLE public.sde_groups(sde_key text PRIMARY KEY,data jsonb)')
                cur.execute('''INSERT INTO public.sde_groups VALUES ('25','{"categoryID":6}'),('10','{"categoryID":7}');
                    INSERT INTO public.sde_types VALUES ('10','{"groupID":25,"name":{"en":"Hull"}}'),('20','{"groupID":10,"name":{"en":"Module"}}');''')
            live.importer.ensure_tables(conn)
        self.backend=live.Backend();self.addCleanup(self.backend.close)
        with self.backend.conn.cursor() as cur:
            cur.execute('''CREATE TABLE market.zkill_daily_prices(price_date date PRIMARY KEY,prices jsonb);
                CREATE TABLE market.zkill_recovered_prices(item_type_id bigint,price_date date,unit_price_isk numeric,PRIMARY KEY(item_type_id,price_date));
                INSERT INTO market.zkill_daily_prices VALUES ('2026-10-23','{"10":100,"20":10}');''')
        self.backend.conn.commit()
        self.backend.initialize(100)
        self.calls=[]
        self.stack.enter_context(patch.object(live.affiliations,'esi_request',side_effect=self.esi))

    def esi(self,**kw):
        endpoint=kw['endpoint'];self.calls.append(endpoint)
        if endpoint.endswith('/corporationhistory/'):payload=[dict(record_id=1,corporation_id=201,start_date='2020-01-01T00:00:00Z')]
        elif endpoint.endswith('/alliancehistory/'):payload=[dict(record_id=1,alliance_id=301,start_date='2020-01-01T00:00:00Z')]
        elif endpoint.startswith('/characters/'):payload=dict(name='Pilot',corporation_id=201)
        elif endpoint.startswith('/corporations/'):payload=dict(name='Corporation',ticker='CORP',alliance_id=301)
        elif endpoint.startswith('/alliances/'):payload=dict(name='Alliance',ticker='ALLY')
        else:raise AssertionError(endpoint)
        return live.affiliations.HttpResult(200,payload,None,None,None,None,None)

    def package(self,ident=141000001,sequence=100):
        return dict(sequence_id=sequence,killmail_id=ident,hash='a'*40,esi=dict(killmail_id=ident,
            killmail_time='2026-10-24T10:00:00Z',solar_system_id=30000142,
            victim=dict(character_id=101,corporation_id=201,alliance_id=301,ship_type_id=10,damage_taken=100,items=[dict(item_type_id=20,quantity_destroyed=1,flag=27)]),
            attackers=[dict(character_id=102,corporation_id=201,alliance_id=301,damage_done=25,final_blow=True),dict(damage_done=75)]))

    def test_full_kill_history_values_next_kill_and_replay(self):
        first=self.backend.process(self.package(),100)
        self.assertEqual(first['killmail_id'],141000001)
        self.assertTrue(first['valued'])
        self.assertIn('/characters/101/corporationhistory/',self.calls)
        self.assertIn('/corporations/201/alliancehistory/',self.calls)
        with self.backend.conn.cursor() as cur:
            cur.execute('SELECT estimated_value_cents,live_price_date FROM rawkm.killmails')
            self.assertEqual(cur.fetchone(),(11000,date(2026,10,24)))
            cur.execute('SELECT corporation_id,alliance_id FROM entities.character_current_affiliation WHERE character_id=101')
            self.assertEqual(cur.fetchone(),(201,301))
        self.backend.conn.commit();self.calls.clear()
        self.backend.process(self.package(141000002,101),101)
        self.assertEqual(self.calls,[])  # Sequential: the next KM reuses the completed histories.
        self.backend.process(self.package(141000001,102),102)
        with self.backend.conn.cursor() as cur:
            cur.execute('SELECT count(*),min(estimated_value_cents) FROM rawkm.killmails')
            self.assertEqual(cur.fetchone(),(2,11000))
            cur.execute('SELECT count(*) FROM rawkm.killmail_attackers')
            self.assertEqual(cur.fetchone()[0],4)
            cur.execute('SELECT next_sequence FROM rawkm.live_killmail_cursor')
            self.assertEqual(cur.fetchone()[0],103)

    def test_pending_daily_pilot_is_left_to_daily_job(self):
        run=str(uuid.uuid4());live.affiliations.create_run_row(self.backend.conn,run,'rawkm.killmails','victim_character_id')
        with self.backend.conn.cursor() as cur:
            cur.execute('INSERT INTO entities.esi_affiliation_character_work(run_id,character_id) VALUES(%s,101)',(run,))
        self.backend.conn.commit()
        self.backend.process(self.package(),100)
        self.assertNotIn('/characters/101/corporationhistory/',self.calls)
        self.assertIn('/characters/102/corporationhistory/',self.calls)
        self.assertGreaterEqual(self.backend.metrics['affiliations_deferred'],1)

    def test_dead_daily_work_does_not_suppress_live_refresh(self):
        run=str(uuid.uuid4());live.affiliations.create_run_row(self.backend.conn,run,'rawkm.killmails','victim_character_id')
        with self.backend.conn.cursor() as cur:
            cur.execute('UPDATE entities.esi_affiliation_run SET worker_pid=2147483647 WHERE run_id=%s',(run,))
            cur.execute('INSERT INTO entities.esi_affiliation_character_work(run_id,character_id) VALUES(%s,101)',(run,))
        self.backend.conn.commit();self.backend.process(self.package(),100)
        self.assertIn('/characters/101/corporationhistory/',self.calls)

    def test_pending_daily_corporation_is_left_to_daily_job(self):
        run=str(uuid.uuid4());live.affiliations.create_run_row(self.backend.conn,run,'rawkm.killmails','victim_character_id')
        with self.backend.conn.cursor() as cur:
            cur.execute('INSERT INTO entities.esi_affiliation_corporation_work(run_id,corporation_id) VALUES(%s,201)',(run,))
        self.backend.conn.commit();self.backend.process(self.package(),100)
        self.assertNotIn('/corporations/201/',self.calls)
        self.assertNotIn('/corporations/201/alliancehistory/',self.calls)

    def test_failed_history_does_not_advance_cursor_or_destroy_imported_kill(self):
        with patch.object(live.affiliations,'esi_request',side_effect=RuntimeError('ESI unavailable')):
            with self.assertRaises(RuntimeError):self.backend.process(self.package(),100)
        self.assertEqual(self.backend.cursor()[0],100)
        with self.backend.conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM rawkm.killmails');self.assertEqual(cur.fetchone()[0],1)
        self.backend.conn.commit()
        self.backend.process(self.package(),100)
        self.assertEqual(self.backend.cursor()[0],101)

    def test_daily_claim_defers_live_price_and_daily_updates_only_its_list(self):
        # Create the kill as if the archive importer had just loaded it.
        raw=self.package();stamp=datetime(2026,10,24,10,tzinfo=timezone.utc);month=date(2026,10,1)
        live.importer.ensure_month_partitions(self.backend.conn,month,month)
        payload,_=live.decode_package(raw,100)
        live.importer.insert_killmail(self.backend.conn,payload,skip_existing=True)
        with self.backend.conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_lock(%s)',(values.LOCK,))
            cur.execute('INSERT INTO market.live_valuation_claims VALUES(%s,pg_backend_pid(),clock_timestamp())',(month,))
        self.backend.conn.commit()
        result=self.backend.process(raw,100)
        self.assertFalse(result['valued']);self.assertEqual(result['valuation_deferred'],1)
        self.backend.process(self.package(141000002,101),101)
        rows=[(stamp.date(),stamp.date(),10,120,False),(stamp.date(),stamp.date(),20,15,False)]
        updated,_=values.update_month(self.backend.conn,month,rows,'loss-day',claimed=True)
        self.backend.conn.commit();self.assertEqual(updated,1)
        with self.backend.conn.cursor() as cur:
            cur.execute('SELECT estimated_value_cents,live_price_date FROM rawkm.killmails ORDER BY killmail_id')
            self.assertEqual(cur.fetchall(),[(13500,None),(11000,stamp.date())])

    def test_nearest_quote_precedence_and_recovered_quote(self):
        pricing=live.LivePricing(self.backend.conn)
        with self.backend.conn.cursor() as cur:
            cur.execute('''INSERT INTO market.zkill_daily_prices VALUES ('2026-10-01','{}'),('2026-10-25','{"20":99}');
                INSERT INTO market.zkill_recovered_prices VALUES (20,'2026-10-01',5);''')
        self.backend.conn.commit()
        self.assertEqual(pricing.nearest_market(20,date(2026,10,24))[0],10)
        self.assertEqual(pricing.nearest_market(20,date(2026,10,10))[0],5)
        self.assertEqual(pricing.nearest_market(20,date(2020,1,1))[0],5)
        self.assertEqual(pricing.nearest_market(20,date(2026,10,25))[0],99)


if __name__=='__main__':unittest.main()
