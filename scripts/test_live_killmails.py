"""Offline lifecycle, cross-process coordination, quota and pricing tests."""
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from scripts import esi_global_quota as quota
from scripts import live_killmail_coordination as coordination
from scripts import stream_killmails as stream
from scripts import live_killmail_backend as backend
from scripts import calculate_killmail_values as values


def quota_child(root, queue):
    quota.wait_for_slot(280,root=root)
    queue.put(time.time())


def exclusive_child(root, ready, release):
    with coordination.exclusive('archive import',root):
        ready.set();release.wait(5)


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.backend=Mock();self.backend.cursor.return_value=(100,datetime.now(timezone.utc))
        self.raw=dict(sequence_id=100,killmail_id=123)
        self.consumer=stream.Consumer(self.backend,Mock(),self.root)
        self.backend.process.return_value=dict(killmail_id=123,inserted=True,valued=True)
    def tearDown(self):self.tmp.cleanup()

    def test_json_deleted_only_after_success_and_no_fetch_of_next_kill(self):
        with patch.object(stream,'get_json',return_value=self.raw) as fetch:
            def finish(raw,seq):
                self.assertTrue(self.consumer.pending.exists())
                self.assertEqual(seq,100)
                return dict(killmail_id=123,inserted=True,valued=True)
            self.backend.process.side_effect=finish
            self.consumer.tick()
            fetch.assert_called_once_with(self.consumer.session,'100.json')
            self.assertFalse(self.consumer.pending.exists())

    def test_failed_kill_keeps_json_and_retries_same_sequence_without_fetch(self):
        with patch.object(stream,'get_json',return_value=self.raw) as fetch:
            self.backend.process.side_effect=RuntimeError('failure')
            with self.assertRaises(RuntimeError):self.consumer.tick()
            self.assertTrue(self.consumer.pending.exists())
            self.backend.process.side_effect=None
            self.consumer.tick()
            self.assertEqual(fetch.call_count,1)
            self.assertFalse(self.consumer.pending.exists())

    def test_commit_before_unlink_crash_does_not_repeat_history_or_value(self):
        coordination.atomic_json(self.consumer.pending,self.raw)
        self.backend.cursor.return_value=(101,datetime.now(timezone.utc))
        with patch.object(stream,'get_json') as fetch:
            self.consumer.tick();fetch.assert_not_called()
        self.backend.process.assert_not_called()
        self.assertFalse(self.consumer.pending.exists())

    def test_404_never_advances_recent_cursor_and_waits_six_seconds(self):
        with patch.object(stream,'get_json',return_value=None):self.assertEqual(self.consumer.tick(),6)
        self.backend.skip_expired.assert_not_called()
        self.backend.process.assert_not_called()

    def test_expired_position_records_gap_for_daily_archives(self):
        self.backend.cursor.return_value=(100,datetime.now(timezone.utc)-timedelta(days=2))
        with patch.object(stream,'get_json',side_effect=[None,dict(sequence=500)]):self.consumer.tick()
        self.backend.skip_expired.assert_called_once_with(100,500)
        self.assertEqual(self.consumer.state['gap_from'],100)
        self.assertEqual(self.consumer.state['gap_to'],499)

    def test_invalid_sequence_not_processed_or_written_to_spool(self):
        with patch.object(stream,'get_json',return_value=dict(self.raw,sequence_id=101)):
            with self.assertRaises(ValueError):self.consumer.tick()
        self.backend.process.assert_not_called();self.assertFalse(self.consumer.pending.exists())


class CoordinationTests(unittest.TestCase):
    def test_import_waits_for_current_kill_then_blocks_next_and_resumes(self):
        with tempfile.TemporaryDirectory() as root:
            context=multiprocessing.get_context('spawn')
            ready=context.Event();release=context.Event()
            with coordination.live_boundary(root) as acquired:
                self.assertTrue(acquired)
                process=context.Process(target=exclusive_child,args=(root,ready,release));process.start()
                deadline=time.monotonic()+3
                while not coordination.paused(root) and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual(coordination.paused(root),'archive import')
                self.assertFalse(ready.is_set())
            self.assertTrue(ready.wait(3))
            with coordination.live_boundary(root) as acquired:self.assertFalse(acquired)
            release.set();process.join(3);self.assertEqual(process.exitcode,0)
            with coordination.live_boundary(root) as acquired:self.assertTrue(acquired)

    def test_quota_shared_between_processes(self):
        with tempfile.TemporaryDirectory() as root:
            queue=multiprocessing.Queue()
            jobs=[multiprocessing.Process(target=quota_child,args=(root,queue)) for _ in range(6)]
            for job in jobs:job.start()
            for job in jobs:job.join(3);self.assertEqual(job.exitcode,0)
            entries=json.loads((Path(root)/'quota.json').read_text())
            self.assertEqual(len(entries),6)

    def test_global_rolling_limit_and_lower_limit_apply_to_other_consumer(self):
        with tempfile.TemporaryDirectory() as root:
            now=[1000.0]
            def sleep(seconds):now[0]+=seconds
            quota.wait_for_slot(2,root=root,clock=lambda:now[0],sleep=sleep)
            quota.wait_for_slot(280,root=root,clock=lambda:now[0],sleep=sleep)
            quota.wait_for_slot(280,root=root,clock=lambda:now[0],sleep=sleep)
            self.assertGreaterEqual(now[0],1060)
            self.assertEqual(len(json.loads((Path(root)/'quota.json').read_text())),1)


class FormulaTests(unittest.TestCase):
    def test_live_uses_same_recipe_fallback_and_abyssal_policy(self):
        class Pricing(values.Pricing):
            def begin_kill(self):pass
        day=date(2026,10,24)
        pricing=Pricing({1:(25,6,'Hull'),2:(10,7,'Module'),3:(10,7,'Abyssal Module'),34:(18,4,'Mineral')},
                        {1:(1,{34:10})},{},valuation_mode='loss-day',abyssal_equivalents={3:(2,)})
        pricing.add_day(day,{'34':2,'2':50,'3':99999999})
        km=dict(victim=dict(ship_type_id=1,items=[dict(item_type_id=3,quantity_destroyed=2),dict(item_type_id=2,quantity_dropped=3,singleton=2)]))
        self.assertEqual(backend.kill_value(km,pricing,day),12003)

    def test_true_fraction_keeps_tiny_contributions_and_npc_damage(self):
        shares=backend.damage_credits([dict(damage_done=1),dict(damage_done=999999999)],100)
        self.assertEqual(shares[0],Decimal('0.0000001'))
        self.assertEqual(sum(shares),100)
        self.assertEqual(backend.damage_credits([dict(damage_done=0)],100),[0])

    def test_feed_envelope_keeps_hash_and_requires_valid_time(self):
        raw=dict(sequence_id=1,killmail_id=2,hash='a'*40,esi=dict(killmail_id=2,killmail_time='2026-10-24T00:00:00Z',victim={},attackers=[]))
        payload,instant=backend.decode_package(raw,1)
        self.assertEqual(payload['killmail_hash'],'a'*40)
        self.assertEqual(instant.tzinfo,timezone.utc)
        with self.assertRaises(ValueError):backend.decode_package(raw,2)


if __name__=='__main__':unittest.main()
