"""Read-only killboard lookups, matching and shared debug renderer."""
from datetime import datetime,timezone
from unittest.mock import patch
import unittest
from scripts.test_combat_timeline import RouteTests,store,routes,ADMIN
links=routes.links
AT=datetime(2026,10,20,19,22,13,tzinfo=timezone.utc)

class MatchingTests(unittest.TestCase):
    def test_unknown_and_ambiguous_remain_unresolved(self):
        self.assertEqual(links.choose_loss(AT,[]),('unknown',None,[]))
        candidates=[dict(killmail_id=1,occurred_at='2026-10-20T19:22:12Z'),dict(killmail_id=2,occurred_at='2026-10-20T19:22:14Z')]
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

    def test_recalculation_does_not_queue(self):
        with links._lock:self.assertFalse(links.refresh(force=True))

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
