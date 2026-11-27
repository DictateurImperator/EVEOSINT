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
