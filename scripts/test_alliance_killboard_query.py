"""Indexed alliance lookup regressions. PostgreSQL tests execute VALUES-only SELECTs.
Set EVEOSINT_TEST_DSN to run the optional database tests; nothing is written.
"""
from datetime import timedelta
import os
import unittest
from unittest.mock import patch
import psycopg2
from scripts import test_killmail_forensics as f


class AlliancePageTests(unittest.TestCase):
    def test_first_page_stops_after_one_month_and_self_kill_is_a_loss(self):
        conn=f.Connection()
        attacks=[('kill',i,f.TIME) for i in range(1,102)]
        losses=[('loss',101,f.TIME)]
        timing={}
        with patch.object(f.entities,'_rawkm_partition_tables',return_value=[('rawkm','killmails_2026_10'),('rawkm','killmails_2026_09')]), \
             patch.object(f.entities,'_table_exists',return_value=True), \
             patch.object(f.entities,'_attacker_partition_lookup',return_value={'killmail_attackers_2026_10':('rawkm','killmail_attackers_2026_10')}), \
             patch.object(f.entities,'_alliance_partition_killmail_rows',side_effect=[losses,attacks]) as lookup:
            rows,has_next,page,limit=f.entities._entity_killmail_page_entries(conn,'alliance',42,timing_marks=timing)
        self.assertEqual(len(rows),100)
        self.assertEqual(rows[0],('loss',101,f.TIME))
        self.assertTrue(has_next)
        self.assertEqual(timing['ids_scanned_partitions'],1)
        self.assertEqual(lookup.call_count,2)
        self.assertEqual([r[1] for r in rows],list(range(101,1,-1)))

    def test_second_page_uses_global_offset_after_side_deduplication(self):
        conn=f.Connection()
        with patch.object(f.entities,'_rawkm_partition_tables',return_value=[('rawkm','killmails_2026_10')]), \
             patch.object(f.entities,'_table_exists',return_value=True), \
             patch.object(f.entities,'_attacker_partition_lookup',return_value={'killmail_attackers_2026_10':('rawkm','killmail_attackers_2026_10')}), \
             patch.object(f.entities,'_alliance_partition_killmail_rows',side_effect=[[],[('kill',i,f.TIME) for i in range(1,202)]]):
            rows,has_next,page,limit=f.entities._entity_killmail_page_entries(conn,'alliance',42,page=2)
        self.assertEqual([r[1] for r in rows],list(range(101,1,-1)))
        self.assertTrue(has_next)
        self.assertEqual(page,2)


@unittest.skipUnless(os.environ.get('EVEOSINT_TEST_DSN'),'Set EVEOSINT_TEST_DSN for SELECT-only PostgreSQL regressions')
class AllianceSQLTests(unittest.TestCase):
    def setUp(self):
        self.conn=psycopg2.connect(os.environ['EVEOSINT_TEST_DSN'],options='-c default_transaction_read_only=on -c statement_timeout=10000')
        self.addCleanup(self.conn.close)

    def query(self,kills,history,limit=101,loss=False):
        # Both data relations are CTEs: no real killmail/history table is accessed.
        prefix='WITH fixture_kills(killmail_id,killmail_time,corporation_id,alliance_id) AS (VALUES '
        prefix+=','.join(['(%s::bigint,%s::timestamptz,%s::bigint,%s::bigint)']*len(kills))
        prefix+='), fixture_history(corporation_id,alliance_id,start_date,end_date) AS (VALUES '
        prefix+=','.join(['(%s::bigint,%s::bigint,%s::timestamptz,%s::timestamptz)']*len(history))+') '
        fixture_params=[v for row in [*kills,*history] for v in row]
        with self.conn.cursor() as cur:
            class Cursor:
                def execute(self,sql,params):
                    sql=sql.replace('entities.corporation_alliance_history','fixture_history').replace('victim_corporation_id','corporation_id').replace('victim_alliance_id','alliance_id')
                    cur.execute(prefix+sql,fixture_params+params)
                def fetchall(self):return cur.fetchall()
            result=f.entities._alliance_partition_killmail_rows(Cursor(),'fixture_kills',
                'victim_corporation_id' if loss else 'corporation_id',
                'victim_alliance_id' if loss else 'alliance_id',42,'loss' if loss else 'kill',limit)
            oracle=f.entities._historical_alliance_scope_predicate('k','corporation_id','alliance_id','killmail_time').replace('entities.corporation_alliance_history','fixture_history')
            cur.execute(prefix+'SELECT DISTINCT k.killmail_id,k.killmail_time FROM fixture_kills k WHERE '+oracle+' ORDER BY k.killmail_time DESC,k.killmail_id DESC LIMIT %s',fixture_params+[42,42,limit])
            self.assertEqual([(r[1],r[2]) for r in result],cur.fetchall())
        return result

    def test_duplicate_attackers_do_not_hide_more_recent_distinct_kills(self):
        kills=[(200,f.TIME,1,42)]*500+[(i,f.TIME,1,42) for i in range(1,105)]
        result=self.query(kills,[(1,42,f.TIME-timedelta(days=1),None)])
        self.assertEqual(len(result),101)
        self.assertEqual([r[1] for r in result],[200,*range(104,4,-1)])

    def test_historical_dates_explicit_membership_and_overlaps(self):
        start=f.TIME-timedelta(days=10);end=f.TIME
        history=[(1,42,start,end),(1,42,start,end),(2,42,start,None)]
        kills=[(1,start,1,None),(2,end,1,None),(3,start-timedelta(seconds=1),1,None),
               (4,start,1,99),(5,end,2,None),(6,start-timedelta(days=1),99,42),
               (7,end,None,None)]
        for loss in (False,True):
            result=self.query(kills,history,loss=loss)
            self.assertEqual({r[1] for r in result},{1,5,6})
            self.assertEqual({r[0] for r in result},{'loss' if loss else 'kill'})

    def test_global_top_limit_merges_multiple_history_intervals_and_explicit_hits(self):
        history=[(1,42,f.TIME-timedelta(days=10),None),(2,42,f.TIME-timedelta(days=10),None)]
        kills=[(i,f.TIME-timedelta(minutes=100-i),i%2+1,None) for i in range(1,100)]
        kills+=[(200,f.TIME,5,42),(201,f.TIME-timedelta(minutes=3),6,42)]
        self.assertEqual([r[1] for r in self.query(kills,history,limit=4)],[200,99,98,201])


if __name__=='__main__':unittest.main()
