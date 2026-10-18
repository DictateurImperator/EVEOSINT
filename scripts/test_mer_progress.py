"""Passive status, SQL wait reporting and sequential MER ALL; no live services."""
import json
from datetime import date
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.test_mer_economy_admin import routes,f

progress=routes.mer_progress
pipeline=routes.mer_pipeline
mer=pipeline.mer


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        patched=patch.object(progress,'PATH',Path(self.directory.name)/'state.json');patched.start();self.addCleanup(patched.stop)

    def test_months_counters_failure_and_completion_persist(self):
        progress.record('[KILL_MATCH] START STRICT_1TO1')
        progress.record('[KILL_MATCH] PLAN total=2')
        progress.record('[KILL_MATCH] partition=killmails_2026_08 START Searching unmatched rows')
        progress.record('[KILL_MATCH] partition=killmails_2026_08 rows=10 unique=7 ambiguous=1 unresolved=2')
        item=next(s for s in progress.snapshot()['operations'] if s['key']=='KILL_MATCH')
        self.assertEqual(item['processed'],1);self.assertEqual(item['total'],2);self.assertEqual(item['metrics']['unique'],7)
        progress.record('[KILL_MATCH] BUSY')
        self.assertEqual(progress._load()['KILL_MATCH']['processed'],1)
        progress.record('[KILL_MATCH] END rows=20 unique=15 ambiguous=1 unresolved=4')
        state=progress._load()['KILL_MATCH']
        self.assertEqual(state['status'],'completed');self.assertEqual(state['metrics']['rows'],20);self.assertIn('finished_at',state)
        progress.record('[KILL_MATCH] START')
        progress.record('[KILL_MATCH] FAILED=database error')
        self.assertEqual(progress._load()['KILL_MATCH']['status'],'failed')

    def test_file_progress_does_not_count_encoding_lines_or_month_start(self):
        progress.record('[KILL_IMPORT] START Preparing list')
        progress.record('[KILL_IMPORT] START files=2')
        progress.record('[KILL_IMPORT] 2026-08 source_encoding=utf8')
        self.assertEqual(progress._load()['KILL_IMPORT']['processed'],0)
        progress.record('[KILL_IMPORT] 2026-08 state=imported rows=100')
        self.assertEqual(progress._load()['KILL_IMPORT']['processed'],1)
        progress.record('[KILL_IMPORT] END processed=2 failed=1 rows=100')
        self.assertEqual(progress._load()['KILL_IMPORT']['status'],'completed_with_errors')

    def test_sql_wait_identifies_blocking_mer_operation_and_is_read_only(self):
        progress.record('[ALLIANCE_HISTORY] START');progress.bind_current(MagicMock(get_backend_pid=lambda:101))
        progress.record('[KILL_MATCH] START');progress.bind_current(MagicMock(get_backend_pid=lambda:102))
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
        cur.__iter__.return_value=iter([(101,'active',None,None,[]),(102,'active','Lock','relation',[101])])
        with patch.object(__import__(mer.__package__+'.db',fromlist=['db']),'db',return_value=conn):state=progress.snapshot()
        conn.set_session.assert_called_once_with(readonly=True)
        matching=next(s for s in state['operations'] if s['key']=='KILL_MATCH')
        self.assertTrue(matching['waiting_for_lock']);self.assertEqual(matching['waiting_for'],['Historical alliance IDs'])

    def test_interrupted_run_and_bounded_log_tail(self):
        progress.record('[ANALYZE] START')
        data=progress._load();data['ANALYZE']['pid']=999999999;progress._save(data)
        state=next(s for s in progress.snapshot()['operations'] if s['key']=='ANALYZE')
        self.assertEqual(state['status'],'interrupted')
        path=Path(self.directory.name)/'log';path.write_text('x'*100000+'\nlast\n')
        self.assertEqual(progress.log_tail(path,1),'last')

    def test_alliance_updates_report_partitions_and_commit_only_at_the_end(self):
        class Connection:
            commits=0;rowcount=5
            def __init__(self):self.updates=[]
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def cursor(self):return self
            def execute(self,query,params=None):
                self.last=str(query)
                if self.last.strip().startswith('UPDATE'):self.updates.append((self.last,params))
            def fetchone(self):return (date(2026,9,1),)
            def fetchall(self):return [('killmails_2026_08',),('killmails_2026_09',)]
            def commit(self):self.commits+=1
        conn=Connection()
        with patch.object(mer,'db',return_value=conn),patch.object(mer,'MER_LOG_FILE',Path(self.directory.name)/'mer.log'):
            result=mer.enrich_mer_entity_ids()
        self.assertEqual(result['rows'],5);self.assertEqual(conn.commits,1)
        self.assertEqual(len(conn.updates),1)
        query,params=conn.updates[0]
        self.assertIn('killmails_2026_09',query);self.assertNotIn('killmails_2026_08',query)
        self.assertIn('WHERE m.kill_datetime >= %s AND m.kill_datetime < %s',query)
        self.assertEqual(params,(date(2026,9,1),date(2026,10,1)))
        state=progress._load()['ALLIANCE_HISTORY']
        self.assertEqual(state['processed'],1);self.assertEqual(state['total'],1);self.assertEqual(state['status'],'completed')
        conn.updates.clear()
        with patch.object(mer,'db',return_value=conn),patch.object(mer,'MER_LOG_FILE',Path(self.directory.name)/'mer.log'):
            mer.enrich_mer_entity_ids(months=['2026-08'])
        self.assertEqual(len(conn.updates),1);self.assertIn('killmails_2026_08',conn.updates[0][0])
        self.assertEqual(conn.updates[0][1],(date(2026,8,1),date(2026,9,1)))


class PipelineTests(unittest.TestCase):
    def test_affiliations_use_imported_mer_months_and_retry_latest_if_none(self):
        with patch.object(mer,'import_mer_kill_dumps',return_value={'imported_months':['2026-09']}),patch.object(mer,'enrich_mer_entity_ids') as enrich:
            selected={key:function for key,label,function in pipeline.steps()}
            selected['KILL_IMPORT']();selected['ALLIANCE_HISTORY']()
            enrich.assert_called_once_with(months=['2026-09'])
        with patch.object(mer,'import_mer_kill_dumps',return_value={'imported_months':[]}),patch.object(mer,'enrich_mer_entity_ids') as enrich:
            selected={key:function for key,label,function in pipeline.steps()}
            selected['KILL_IMPORT']();selected['ALLIANCE_HISTORY']()
            enrich.assert_called_once_with(months=None)

    def test_sequential_order_and_error_stops_remaining_steps(self):
        calls=[]
        selected=[('A','first',lambda:calls.append('first')),('B','second',lambda:calls.append('second')),('C','last',lambda:calls.append('last'))]
        with patch.object(pipeline,'steps',return_value=selected),patch.object(mer,'_mer_log') as log:
            pipeline._LOCK.acquire();pipeline.run()
            self.assertEqual(calls,['first','second','last']);self.assertFalse(pipeline.running())
            self.assertIn('END',log.call_args.args[0])
            calls.clear();selected[1]=('B','second',lambda:{'failed':1})
            pipeline._LOCK.acquire();pipeline.run()
            self.assertEqual(calls,['first']);self.assertIn('FAILED',log.call_args.args[0]);self.assertFalse(pipeline.running())

    def test_economy_waits_for_its_own_process_and_rejects_stale_completion(self):
        with patch.object(pipeline.jobs,'run_mer_economy_import_job') as launch,patch.object(pipeline.jobs,'_status_path',return_value=MagicMock(read_text=lambda:json.dumps({'pid':123}))),patch.object(pipeline.jobs,'read_mer_economy_progress',side_effect=[{'running':True,'progress':{}},{'running':False,'progress':{'pid':123,'phase':'completed'}}]),patch.object(pipeline.time,'sleep') as sleep:
            self.assertEqual(pipeline.economy(),{'failed':0});sleep.assert_called_once();launch.assert_called_once()
        with patch.object(pipeline.jobs,'run_mer_economy_import_job'),patch.object(pipeline.jobs,'_status_path',return_value=MagicMock(read_text=lambda:json.dumps({'pid':123}))),patch.object(pipeline.jobs,'read_mer_economy_progress',return_value={'running':False,'progress':{'pid':122,'phase':'completed'}}):
            with self.assertRaises(RuntimeError):pipeline.economy()

    def test_starts_only_explicitly_and_refuses_existing_work(self):
        with patch.object(pipeline.jobs,'read_mer_economy_progress',return_value={'running':False}),patch.object(pipeline.threading,'Thread') as thread,patch.object(mer,'_mer_log'):
            try:
                self.assertTrue(pipeline.start());thread.return_value.start.assert_called_once()
                self.assertFalse(pipeline.start())
            finally:
                if pipeline.running():pipeline._LOCK.release()
            mer._map_alliance_ids_lock.acquire()
            try:self.assertFalse(pipeline.start())
            finally:mer._map_alliance_ids_lock.release()


class RouteTests(unittest.TestCase):
    def test_status_log_and_all_are_permission_checked_and_gets_never_launch(self):
        app=FastAPI();app.include_router(routes.router);client=TestClient(app)
        user={'id':1,'username':'Admin','permissions':{'admin.mer.view'}}
        with patch.object(routes,'require_login',return_value=user),patch.object(progress,'snapshot',return_value={'operations':[]}) as read,patch.object(progress,'log_tail',return_value='tail'),patch.object(pipeline,'start',return_value=True) as start,patch.object(routes,'audit_log'):
            self.assertEqual(client.get('/admin/mer/status').json()['log'],'tail');self.assertEqual(client.get('/admin/mer/log').text,'tail');start.assert_not_called()
            response=client.post('/admin/mer/all',follow_redirects=False);self.assertIn('mer_all_started',response.headers['location']);start.assert_called_once()
            user['permissions']=set();read.reset_mock();start.reset_mock()
            for path in ['/admin/mer/status','/admin/mer/log']:self.assertEqual(client.get(path,follow_redirects=False).status_code,302)
            self.assertEqual(client.post('/admin/mer/all',follow_redirects=False).status_code,302);read.assert_not_called();start.assert_not_called()


if __name__=='__main__':unittest.main()
