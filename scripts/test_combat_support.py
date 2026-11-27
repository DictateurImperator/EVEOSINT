"""Synthetic support scores and isolated PostgreSQL tests; never production."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import test_combat_dev as base
from scripts.test_killmail_forensics import load,entities

support=load('combat_support')
T=datetime(2026,10,20,20,tzinfo=timezone.utc)
RECIPIENT=dict(id=100,name='Victim')


def e(n,kind='shield_in',value=100,recipient=100,at=None,observer=None,source='Logi'):
    incoming=kind.endswith('_in')
    return dict(version=2,event_id=hashlib.sha256(str(n).encode()).hexdigest(),
      occurred_at=(at or T-timedelta(seconds=30)).strftime('%Y-%m-%dT%H:%M:%SZ'),
      observer=observer or ('Victim' if recipient==100 else 'Attacker') if incoming else source,
      character_id=recipient if incoming else 300,category='combat',kind=kind,raw='synthetic event '+str(n),amount=value,
      source=dict(name=source,character_id=300,ship='Basilisk'),
      target=dict(name='Victim' if recipient==100 else 'Attacker',character_id=recipient))


class ScoreTests(unittest.TestCase):
    def test_30_minute_boundaries_and_maximum_of_sender_receiver_totals(self):
        events=[e(1,at=T-timedelta(minutes=30,seconds=1)),e(2,at=T-timedelta(minutes=30)),
                e(3,kind='shield_out',value=200),e(4),e(5,at=T+timedelta(seconds=1))]
        rows=list(support.rolling_scores([(9,T,'loss')],events,RECIPIENT))
        self.assertEqual(rows[0][2][('logi','healing')]['amount'],200)

    def test_healing_and_capacitor_are_separate_and_self_or_wrong_target_ignored(self):
        events=[e(1),e(2,kind='capacitor_in',value=700),e(3,kind='shield_out',recipient=200),e(4,source='Victim')]
        result=list(support.rolling_scores([(9,T,'loss')],events,RECIPIENT))[0][2]
        self.assertEqual({key[1]:r['amount'] for key,r in result.items()},{'healing':100,'capacitor':700})

    def test_window_expires_between_kills(self):
        entries=[(1,T,'kill'),(2,T+timedelta(minutes=31),'kill')]
        rows=list(support.rolling_scores(entries,[e(1)],RECIPIENT))
        self.assertTrue(rows[0][2]);self.assertFalse(rows[1][2])


@unittest.skipUnless(base.DSN,'Requires explicit disposable PostgreSQL socket')
class DatabaseTests(unittest.TestCase):
    setUpClass=classmethod(base.PostgreSQLTests.setUpClass.__func__)
    setUp=base.PostgreSQLTests.setUp
    def rows(self,sql,args=()):
        with base.store.connection() as conn,conn.cursor() as cur:
            cur.execute(sql,args)
            return cur.fetchall() if cur.description else []

    def ingest(self,events):
        body=b'\n'.join(json.dumps(event).encode() for event in events)
        return base.store.receive(1,body,hashlib.sha256(body).hexdigest())

    def test_support_recalculation_multiple_attackers_max_views_retry_and_no_death_signal(self):
        # Only recipient logs; the giver never uploaded. No death signal or summary.
        events=[e(1),e(2,recipient=200,value=80),e(3,recipient=201,value=20),
                e(4,kind='capacitor_in',value=50),e(5,kind='capacitor_in',recipient=200,value=90),
                e(6,kind='shield_out',recipient=200,value=100)]
        self.ingest(events)
        # Same physical log uploaded through another account must not double count.
        body=json.dumps(events[0]).encode();base.store.receive(2,body,hashlib.sha256(body).hexdigest())
        self.rows('CREATE SCHEMA IF NOT EXISTS entities')
        self.rows('CREATE TABLE IF NOT EXISTS entities.characters(character_id bigint PRIMARY KEY,name text)')
        self.rows("INSERT INTO entities.characters VALUES(100,'Victim'),(200,'Attacker'),(201,'Attacker Two'),(300,'Logi') ON CONFLICT DO NOTHING")
        def board(cid,first,last):
            return iter([(44,T,'loss' if cid==100 else 'kill',1,'Ship','1')]) if cid in (100,200,201) else iter([])
        with patch.object(support,'db',lambda:psycopg2.connect(base.DSN)),patch.object(support,'read_killboard',side_effect=board):
            result=support.recalculate()
            first=self.rows('SELECT side,resource,amount FROM devencour.killmail_support ORDER BY side,resource')
            self.assertEqual(first,[('attack','capacitor',90),('attack','healing',120),('defense','capacitor',50),('defense','healing',100)])
            self.assertEqual(result['scores'],4)
            support.recalculate()
            self.assertEqual(first,self.rows('SELECT side,resource,amount FROM devencour.killmail_support ORDER BY side,resource'))
            # Compact scores survive raw deletion; rerunning does not erase them.
            self.rows('DELETE FROM devencour.raw_events')
            support.recalculate()
            self.assertEqual(first,self.rows('SELECT side,resource,amount FROM devencour.killmail_support ORDER BY side,resource'))
        self.assertEqual(self.rows('SELECT count(*) FROM devencour.death_summaries'),[(0,)])

    def test_healer_only_history_finds_two_defenders_healers_without_victim_upload(self):
        first=e(41,kind='shield_out',value=100)
        second=e(42,kind='armor_out',value=200,source='Second Logi')
        second['character_id']=301;second['source']['character_id']=301
        self.ingest([first,second])
        self.rows('CREATE SCHEMA IF NOT EXISTS entities')
        self.rows('CREATE TABLE IF NOT EXISTS entities.characters(character_id bigint PRIMARY KEY,name text)')
        self.rows("INSERT INTO entities.characters VALUES(100,'Victim') ON CONFLICT DO NOTHING")
        def board(cid,first,last):
            return iter([(55,T,'loss',1,'Ship','1')]) if cid==100 else iter([])
        with patch.object(support,'db',lambda:psycopg2.connect(base.DSN)),patch.object(support,'read_killboard',side_effect=board):
            support.recalculate()
        self.assertEqual(self.rows('SELECT supporter_key,amount FROM devencour.killmail_support ORDER BY supporter_key'),[('logi',100),('second logi',200)])
        self.assertEqual(self.rows('SELECT count(*) FROM devencour.collector_streams WHERE character_id=100'),[(0,)])

    def run_at(self,at,board=None):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(support,'db',lambda:psycopg2.connect(base.DSN)))
            stack.enter_context(patch.object(support,'processing_time',return_value=at))
            lookup=stack.enter_context(patch.object(support,'read_killboard',side_effect=board or (lambda *_:iter([]))))
            result=support.recalculate()
            return result,lookup.call_count

    def test_each_run_before_three_days_then_final_retry_at_seven_days(self):
        self.ingest([e(70,at=T)])
        first,calls=self.run_at(T+timedelta(hours=2))
        self.assertGreater(calls,0)
        self.assertEqual((first['marked'],first['purged']),(1,0))
        mark=self.rows('SELECT processed_at FROM devencour.raw_events')[0][0]
        second,calls=self.run_at(T+timedelta(days=2))
        self.assertGreater(calls,0)
        self.assertEqual((second['marked'],second['purged']),(1,0))
        mark2=self.rows('SELECT processed_at FROM devencour.raw_events')[0][0]
        self.assertGreater(mark2,mark)
        for days in (3,6):
            result,calls=self.run_at(T+timedelta(days=days))
            self.assertEqual(calls,0)
            self.assertEqual((result['marked'],result['purged']),(0,0))
            self.assertEqual(self.rows('SELECT processed_at FROM devencour.raw_events')[0][0],mark2)
        result,calls=self.run_at(T+timedelta(days=7))
        self.assertGreater(calls,0)
        self.assertEqual((result['marked'],result['purged']),(1,1))
        self.assertEqual(self.rows('SELECT count(*) FROM devencour.raw_events'),[(0,)])

    def test_old_first_import_requires_second_success_and_failure_preserves_marker(self):
        self.ingest([e(71,at=T)])
        result,_=self.run_at(T+timedelta(days=20))
        self.assertEqual((result['marked'],result['purged']),(1,0))
        mark=self.rows('SELECT processed_at FROM devencour.raw_events')[0][0]
        def fail(*_): raise RuntimeError('Synthetic matching failure')
        with self.assertRaisesRegex(RuntimeError,'Synthetic'):
            self.run_at(T+timedelta(days=20,seconds=1),fail)
        self.assertEqual(self.rows('SELECT processed_at FROM devencour.raw_events'),[(mark,)])
        result,_=self.run_at(T+timedelta(days=20,seconds=2))
        self.assertEqual((result['marked'],result['purged']),(1,1))

    def test_new_upload_during_processing_is_not_marked_or_purged(self):
        self.ingest([e(72,at=T)])
        self.run_at(T+timedelta(days=1))
        first_save=support.save_scores;inserted=False
        def upload(scores):
            nonlocal inserted
            if not inserted:
                self.ingest([e(73,at=T)])
                inserted=True
            first_save(scores)
        with patch.object(support,'save_scores',side_effect=upload):
            result,_=self.run_at(T+timedelta(days=7))
        self.assertEqual(result['purged'],1)
        self.assertEqual(self.rows('SELECT event_id,processed_at FROM devencour.raw_events'),[(e(73)['event_id'],None)])

    def test_maintenance_between_mark_and_delete_rolls_back_batch(self):
        self.ingest([e(75,at=T)])
        self.run_at(T+timedelta(days=1))
        mark=self.rows('SELECT processed_at FROM devencour.raw_events')[0][0]
        finish=support.finish_run
        def interrupt(conn,started):
            calls=0
            def gate():
                nonlocal calls
                calls+=1
                if calls==2: raise base.store.Maintenance()
            with patch.object(base.store,'check_writing',side_effect=gate):
                return finish(conn,started)
        with patch.object(support,'finish_run',side_effect=interrupt):
            with self.assertRaises(base.store.Maintenance):
                self.run_at(T+timedelta(days=7))
        self.assertEqual(self.rows('SELECT processed_at FROM devencour.raw_events'),[(mark,)])

    def test_partial_killmail_period_does_not_mark_an_incomplete_thirty_minute_window(self):
        self.ingest([e(76,at=T)])
        with patch.object(support,'db',lambda:psycopg2.connect(base.DSN)),patch.object(support,'processing_time',return_value=T+timedelta(days=8)):
            result=support.recalculate(T,T+timedelta(minutes=15))
        self.assertEqual((result['marked'],result['purged']),(0,0))
        self.assertEqual(self.rows('SELECT processed_at FROM devencour.raw_events'),[(None,)])

    def test_purge_keeps_summaries_and_compact_scores_and_receipt_time_is_not_age(self):
        self.ingest([e(74,at=T)])
        base.ingest(base.summary(),True)
        support.save_scores({(44,'defense','logi','healing'):dict(amount=100,source=dict(name='Logi'))})
        # received_at is now, but event time is old: it is due immediately for
        # its first pass, then removable only after a successful second pass.
        self.run_at(T+timedelta(days=8))
        result,_=self.run_at(T+timedelta(days=8,seconds=1))
        self.assertEqual(result['purged'],1)
        self.assertEqual(self.rows('SELECT count(*) FROM devencour.death_summaries'),[(1,)])
        self.assertEqual(self.rows('SELECT amount FROM devencour.killmail_support'),[(100,)])

    def test_capacitor_summary_upgrade_does_not_change_original_totals_or_receipt(self):
        payload=base.summary();base.ingest(payload,True)
        receipt=self.rows('SELECT received_at FROM devencour.death_summaries')
        payload['received_capacitor']=[dict(name='Battery',gj=900)]
        base.ingest(payload,True)
        self.assertEqual(receipt,self.rows('SELECT received_at FROM devencour.death_summaries'))
        saved=self.rows('SELECT payload FROM devencour.death_summaries')[0][0]
        self.assertEqual(saved['received_capacitor'],payload['received_capacitor'])
        self.assertEqual(saved['emitted'],base.summary()['emitted'])
        base.ingest(base.summary(),True)
        self.assertEqual(self.rows('SELECT payload FROM devencour.death_summaries')[0][0],saved)

    def test_admin_replay_scoped_to_owner_and_collector_response(self):
        ident=base.store.request_replay(1,'2026-05-03T00:00:00Z','2026-05-04T00:00:00Z',100)
        self.assertEqual(base.store.replay_requests(2),[])
        app=FastAPI();app.include_router(base.routes.router)
        with TestClient(app) as client,patch.object(base.routes,'active_collector',return_value=1):
            r=client.get('/collector/status',headers={'Authorization':'Bearer test'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['replay_requests'][0]['id'],ident)
        base.store.cancel_replay(2,ident);self.assertTrue(base.store.replay_requests(1))
        base.store.cancel_replay(1,ident);self.assertEqual(base.store.replay_requests(1),[])


if __name__=='__main__':unittest.main()
