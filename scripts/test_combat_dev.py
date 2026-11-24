"""016 integration tests against an explicitly selected, disposable local PostgreSQL.

EVEOSINT_COMBAT_TEST_DSN='host=/tmp/... dbname=postgres' python -m unittest scripts.test_combat_dev -v
Never loads production configuration; never connects without the explicit local socket.
"""
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime,timezone
import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import test_killmail_forensics as f

store=f.load('combat_timeline')
links=f.load('combat_killmail_links')
routes=f.load('routes_combat_timeline')
DSN=os.environ.get('EVEOSINT_COMBAT_TEST_DSN','')

def event(n=1,kind='shield_in'):
    return dict(version=2,event_id=hashlib.sha256(str(n).encode()).hexdigest(),occurred_at='2026-10-20T19:22:13Z',
      observer='Test Pilot',character_id=123,kind=kind,category='combat',raw='<b>Original</b>\ncomplete continuation',
      amount=100,source=dict(name='Test Logi',ship='Basilisk'))

def summary(n=1):
    return dict(version=2,summary_id=event(n)['event_id'],character_id=123,character_name='Test Pilot',
      death_signal_at='2026-10-20T19:22:13Z',window_seconds=1800,
      emitted=dict(damage_hp=42,healing_hp=15,capacitor_gj=7),
      received_healing=[dict(name='A',ship='Basilisk',hp=100),dict(name='B',ship='Guardian',hp=200)])

def encoded(records,summary=False):
    return json.dumps(records).encode() if summary else b'\n'.join(json.dumps(r).encode() for r in records)

def ingest(records,is_summary=False):
    body=encoded(records,is_summary)
    return store.receive(1,body,hashlib.sha256(body).hexdigest(),is_summary)

@unittest.skipUnless(DSN,'Requires explicit disposable PostgreSQL socket')
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options=psycopg2.extensions.parse_dsn(DSN)
        if not options.get('host','').startswith('/tmp/'):
            raise RuntimeError('Tests require an isolated /tmp Unix socket')

    def setUp(self):
        self.patches=[patch.object(store,'db',lambda:psycopg2.connect(DSN))]
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.patches.append(patch.object(store,'MAINTENANCE',Path(self.tmp.name)/'active'))
        for p in self.patches:p.start();self.addCleanup(p.stop)
        with store.connection() as conn,conn.cursor() as cur:cur.execute('DROP SCHEMA IF EXISTS devencour CASCADE')
        store.setup()

    def rows(self,sql,args=()):
        with store.connection() as conn,conn.cursor() as cur:cur.execute(sql,args);return cur.fetchall()

    def test_raw_retry_first_receipt_and_pagination(self):
        self.assertEqual(ingest([event(i) for i in range(120)])['inserted'],120)
        first=self.rows('SELECT received_at FROM devencour.raw_events ORDER BY event_id')
        self.assertEqual(ingest([event(i) for i in range(120)])['inserted'],0)
        self.assertEqual(first,self.rows('SELECT received_at FROM devencour.raw_events ORDER BY event_id'))
        a=store.timeline(limit=50);b=store.timeline(before=a['next'],limit=50);c=store.timeline(before=b['next'],limit=50)
        ids=[r['event_id'] for p in (a,b,c) for r in p['events']]
        self.assertEqual(len(ids),120);self.assertEqual(len(set(ids)),120);self.assertEqual(a['total'],120)
        self.assertIn('complete continuation',a['events'][0]['raw'])

    def test_summary_atomic_retry_and_conflict(self):
        self.assertEqual(ingest(summary(),True)['inserted'],1)
        received=self.rows('SELECT received_at FROM devencour.death_summaries')
        self.assertEqual(ingest(summary(),True)['inserted'],0)
        self.assertEqual(received,self.rows('SELECT received_at FROM devencour.death_summaries'))
        self.assertEqual(len(self.rows('SELECT * FROM devencour.received_healing')),2)
        altered=summary();altered['emitted']['damage_hp']=999
        with self.assertRaises(store.EventConflict):ingest(altered,True)
        # Force the child insert to fail: the parent summary must roll back too.
        with store.connection() as conn,conn.cursor() as cur:
            cur.execute('ALTER TABLE devencour.received_healing ADD CHECK (healer_index<1) NOT VALID')
        with self.assertRaises(psycopg2.IntegrityError):ingest(summary(2),True)
        self.assertEqual(len(self.rows('SELECT * FROM devencour.death_summaries')),1)

    def test_maintenance_stops_between_raw_rows_and_retry_fills_remainder(self):
        original=store.check_writing;checks=0
        def gate():
            nonlocal checks
            checks+=1
            if checks==3:store.MAINTENANCE.touch()
            original()
        with patch.object(store,'check_writing',gate):
            with self.assertRaises(store.Maintenance):ingest([event(1),event(2),event(3)])
        self.assertEqual(len(self.rows('SELECT * FROM devencour.raw_events')),1)
        store.MAINTENANCE.unlink()
        self.assertEqual(ingest([event(1),event(2),event(3)])['inserted'],2)
        self.assertEqual(store.timeline()['total'],3)

    def test_gzip_routes_write_only_and_maintenance_immediate(self):
        app=FastAPI();app.include_router(routes.router)
        body=encoded([event()]);headers={'Authorization':'Bearer test','Idempotency-Key':hashlib.sha256(body).hexdigest(),'Content-Encoding':'gzip'}
        with TestClient(app) as client,patch.object(routes,'active_collector',return_value=1),patch.object(links,'refresh',side_effect=AssertionError('No intake scan')):
            r=client.post('/collector/combat-events',content=gzip.compress(body),headers=headers)
            self.assertEqual(r.status_code,200,r.text)
            store.MAINTENANCE.touch()
            with patch.object(routes,'active_collector',side_effect=AssertionError('Maintenance must reject before DB/auth')):
                self.assertEqual(client.post('/collector/combat-events',content=b'{}').status_code,503)
                self.assertEqual(client.get('/collector/status').status_code,503)
        self.assertEqual(len(self.rows('SELECT * FROM devencour.raw_events')),1)

    def test_only_explicit_recalculation_links_summaries(self):
        ingest([event(1,'loss_signal')]);ingest(summary(),True)
        self.assertEqual(self.rows('SELECT * FROM devencour.summary_killmail_links'),[])
        at=datetime(2026,10,20,19,22,13,tzinfo=timezone.utc)
        with patch.object(links,'read_killboard',return_value=iter([(44,at,'loss',1,'Test ship','1')])):
            self.assertTrue(links.refresh(force=True))
        self.assertEqual(self.rows('SELECT killmail_id FROM devencour.summary_killmail_links'),[(44,)])
        report=links.report();self.assertEqual(report['killmails'][0]['killmail_id'],44)
        self.assertEqual(links.attach_links(store.timeline())['events'][0]['killmail_link']['killmail_id'],44)

    def test_healer_display_reads_stored_summary_only(self):
        ingest(summary(),True)
        with store.connection() as conn,conn.cursor() as cur:
            cur.execute('INSERT INTO devencour.summary_killmail_links(owner,summary_id,killmail_id) VALUES(%s,%s,%s)',(1,summary()['summary_id'],44))
        healing=f.load('combat_healing')
        mail=dict(killmail_id=44,killmail_time=datetime(2026,10,20,19,22,13,tzinfo=timezone.utc),victim=dict(character=dict(entity_id=123,name='Test Pilot')))
        with patch.object(healing,'enrich',side_effect=lambda km,rows:rows):rows=healing.get_healers(mail)
        self.assertEqual([r['total'] for r in rows],[200,100])
        self.assertEqual(mail['recorded_activity'],dict(damage_hp=42,healing_hp=15,capacitor_gj=7))
        self.assertEqual(self.rows('SELECT count(*) FROM devencour.raw_events'),[(0,)])

    def test_one_shot_purge_only_after_successful_postgresql_setup(self):
        import importlib.util
        import sys
        import types
        root=Path(self.tmp.name)/'legacy';root.mkdir()
        legacy=('timeline.sqlite3','timeline.sqlite3-wal','timeline.sqlite3-shm','timeline.sqlite3-journal')
        for name in legacy:(root/name).write_bytes(b'legacy test data')
        (root/'keep.txt').write_text('unrelated')
        app=types.ModuleType('app');app.combat_timeline=store
        with patch.dict(sys.modules,{'app':app}),patch.object(store,'ROOT',root):
            spec=importlib.util.spec_from_file_location('combat_setup_test',f.ROOT/'scripts/setup_combat_dev.py')
            script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
            argv=['setup_combat_dev.py','--owner','1','--token-sha256','a'*64,'--purge-legacy-sqlite']
            with patch.object(sys,'argv',argv),patch.object(store,'setup',side_effect=RuntimeError('PG unavailable')):
                with self.assertRaises(RuntimeError):script.main()
            self.assertTrue(all((root/name).exists() for name in legacy))
            with patch.object(sys,'argv',argv):script.main()
        self.assertFalse(any((root/name).exists() for name in legacy))
        self.assertTrue((root/'keep.txt').exists())
        self.assertEqual(self.rows('SELECT owner FROM devencour.collector_tokens'),[(1,)])

    def test_tokens_and_protocol_rejection(self):
        token=store.rotate_token(1);self.assertEqual(store.token_owner(token),1)
        other=store.rotate_token(1);self.assertIsNone(store.token_owner(token));self.assertEqual(store.token_owner(other),1)
        store.revoke_token(1);self.assertIsNone(store.token_owner(other))
        old=json.dumps(dict(version=1,events=[event()])).encode()
        with self.assertRaises(ValueError):store.receive(1,old,hashlib.sha256(old).hexdigest())

if __name__=='__main__':unittest.main()
