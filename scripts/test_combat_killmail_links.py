"""Synthetic fixtures only: automatic website matching, no production access."""
from datetime import datetime, timezone
from unittest.mock import patch
import hashlib
import json
from scripts.test_combat_timeline import StorageTests, RouteTests, body, event, store, routes, ADMIN
links=routes.links
AT=datetime(2026,10,20,19,22,13,tzinfo=timezone.utc)


class MatchingTests(StorageTests):
    def ingest(self, items):
        raw=body(items);store.receive(1,raw,hashlib.sha256(raw).hexdigest())

    def test_kills_from_kb_and_ship_pod_losses_saved_only_in_sqlite(self):
        a=dict(event(1,kind='loss_signal'),character_id=123)
        b=dict(event(2,kind='loss_signal'),character_id=123,occurred_at='2026-10-20T19:22:25Z')
        self.ingest([a,b])
        kb=[(900,AT,'loss',587,'Rifter','25'),(901,AT.replace(second=25),'loss',670,'Capsule','29'),(902,AT,'kill',588,'Slasher','25')]
        with patch.object(links,'read_killboard',return_value=iter(kb)) as read:
            links.refresh()
            self.assertEqual(read.call_args.args[0],123)
        report=links.report()
        self.assertEqual(len(report['killmails']),3)
        self.assertFalse(report['unmatched_losses'])
        self.assertEqual({r['killmail_id']:r['is_pod'] for r in report['killmails']},{900:0,901:1,902:0})
        payload=links.attach_links(store.timeline(kind='loss_signal'))
        self.assertEqual({e['killmail_link']['killmail_id'] for e in payload['events']},{900,901})
        with patch.object(links,'read_killboard') as read:
            links.refresh();read.assert_not_called()
        self.assertEqual(store.timeline()['total'],2)

    def test_unknown_and_ambiguous_remain_unresolved(self):
        self.ingest([dict(event(1,kind='loss_signal'),character_id=123)])
        with patch.object(links,'read_killboard',return_value=iter([])): links.refresh()
        self.assertEqual(links.report()['unmatched_losses'][0]['status'],'unknown')
        candidates=[{'killmail_id':1,'occurred_at':'2026-10-20T19:22:12Z'},{'killmail_id':2,'occurred_at':'2026-10-20T19:22:14Z'}]
        self.assertEqual(links.choose_loss(AT,candidates),('ambiguous',None,[1,2]))

    def test_database_lookup_transaction_is_read_only_and_uses_both_kb_sides(self):
        class Conn:
            def __init__(self): self.sql='';self.readonly=False
            def set_session(self,readonly): self.readonly=readonly
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def cursor(self): return self
            def execute(self,sql,params): self.sql=sql;self.params=params
            def fetchmany(self,n): return []
            def close(self): pass
        conn=Conn()
        with patch.object(links,'db',return_value=conn): self.assertEqual(list(links.read_killboard(123,AT,AT)),[])
        self.assertTrue(conn.readonly)
        self.assertIn('victim_character_id=%s',conn.sql)
        self.assertIn('a.character_id=%s',conn.sql)
        self.assertIn('IS DISTINCT FROM',conn.sql)
        self.assertNotIn('INSERT',conn.sql)


class DeferredWebsiteTests(RouteTests):
    def test_site_intake_commits_without_launching_a_lookup(self):
        raw=body([dict(event(kind='loss_signal'),character_id=123)])
        with patch.object(routes,'active_collector',return_value=1),patch.object(links,'refresh',side_effect=AssertionError('Intake must not scan')) as scan:
            response=self.client.post('/collector/combat-events',content=raw,headers={'Authorization':'Bearer test','Idempotency-Key':hashlib.sha256(raw).hexdigest()})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['inserted'],1)
        self.assertEqual(store.timeline()['total'],1)
        scan.assert_not_called()

    def test_explicit_recalculation_matches_received_events(self):
        self.ingest_for_recalculation()
        with patch.object(routes,'require_login',return_value=ADMIN),patch.object(routes,'require_permission_or_redirect',return_value=None),patch.object(links,'read_killboard',return_value=iter([(900,AT,'loss',670,'Capsule','29')])) as read:
            response=self.client.post('/admin/debug/combat/recalculate')
        self.assertEqual(response.status_code,200)
        read.assert_called_once()
        self.assertEqual(links.report()['killmails'][0]['killmail_id'],900)

    def ingest_for_recalculation(self):
        raw=body([dict(event(kind='loss_signal'),character_id=123)])
        store.receive(1,raw,hashlib.sha256(raw).hexdigest())

    def test_reading_killboard_does_not_launch_a_scan(self):
        links.report()
        with patch.object(routes,'require_login',return_value=ADMIN),patch.object(routes,'require_permission_or_redirect',return_value=None),patch.object(links,'refresh',side_effect=AssertionError('Viewing must not scan')) as scan,patch.object(links,'killboard_page',return_value={}):
            response=self.client.get('/admin/debug/combat/killmails')
        self.assertEqual(response.status_code,200)
        scan.assert_not_called()

    def test_explicit_scan_does_not_wait_for_an_existing_scan(self):
        with links._lock:
            with patch.object(routes,'require_login',return_value=ADMIN),patch.object(routes,'require_permission_or_redirect',return_value=None),patch.object(links,'read_killboard') as read:
                response=self.client.post('/admin/debug/combat/recalculate')
        self.assertEqual(response.status_code,409)
        read.assert_not_called()

    def test_report_is_admin_only(self):
        from fastapi.responses import RedirectResponse
        with patch.object(routes,'require_login',return_value=None),patch.object(routes,'require_permission_or_redirect',return_value=RedirectResponse('/login',302)):
            self.assertEqual(self.client.get('/admin/debug/combat/killmails',follow_redirects=False).status_code,302)
            self.assertEqual(self.client.post('/admin/debug/combat/recalculate',follow_redirects=False).status_code,302)


class DebugPaginationTests(StorageTests):
    def test_all_killboard_pages_and_date_filters_are_reachable(self):
        with store.connection() as conn:
            links.schema(conn)
            conn.executemany('INSERT INTO combat_killmails VALUES(?,?,?,?,?,?,?,?,?)',
                [(1,'Test Pilot',123,i,'2026-05-03T18:18:07Z','loss' if i%2 else 'kill',587,'Rifter',0) for i in range(1,206)])
            conn.commit()
        pages=[links.report(page=p) for p in (1,2,3)]
        ids=[r['killmail_id'] for p in pages for r in p['killmails']]
        self.assertEqual(len(ids),205);self.assertEqual(len(set(ids)),205)
        self.assertFalse(pages[-1]['pagination']['has_next'])
        self.assertEqual(links.report(date_to='2026-05-01T00:00:00Z')['pagination']['total'],0)
        self.assertEqual(links.report(side='losses')['pagination']['total'],103)

    def test_shared_kb_template_has_explicit_test_link_and_loss_color(self):
        from scripts import test_killmail_forensics as f
        from scripts.test_combat_timeline_browser import ReadConnection,hydrate
        report={'killmails':[dict(side='loss',killmail_id=900,occurred_at='2026-10-20T19:22:13Z')],
                'pagination':dict(page=1,has_prev=False,has_next=False)}
        with patch.object(links,'db',return_value=ReadConnection()),patch.object(f.entities,'_hydrate_global_killmail_entries',side_effect=hydrate):
            kb=links.killboard_page(report)
        html=f.templates.env.get_template('entity_killmails_fragment.html').render(killmail_page=kb)
        self.assertIn('killmail-row-loss',html)
        self.assertIn('/admin/debug/combat/kill/900',html)
        self.assertIn('TEST',html)


class DebugPreviewTests(RouteTests):
    def test_test_link_opens_existing_killmail_renderer_with_healing(self):
        import sys
        from types import SimpleNamespace
        from scripts.test_combat_healing import km, healing, event as heal_event
        mail=km();healers=healing.enrich(mail,healing.collect(mail,[heal_event()]))
        source=SimpleNamespace(kill_profile=lambda *args:SimpleNamespace(context=dict(killmail=mail,error=None,healers=healers)))
        with patch.dict(sys.modules,{routes.__package__+'.routes_entities':source}),patch.object(routes,'require_login',return_value=ADMIN),patch.object(routes,'require_permission_or_redirect',return_value=None):
            response=self.client.get('/admin/debug/combat/kill/123')
        self.assertEqual(response.status_code,200)
        self.assertIn('Top Healer',response.text)
        self.assertIn('100 HP',response.text)
        self.assertIn('TEST',response.text)
