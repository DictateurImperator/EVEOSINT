"""Synthetic healing observations only. No game logs or production access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.test_combat_timeline import store, body
from scripts import test_killmail_forensics as f

healing=f.load('combat_healing')
TIME=datetime(2026,10,20,20,0,tzinfo=timezone.utc)


def char(cid,name):
    return f.entities._entity_ref('character',cid,name)


def km():
    ship=f.entities._killmail_type_ref(587,'Rifter','ship',64)
    return dict(killmail_id=123,killmail_time=TIME,killmail_hash='test',date_label='2026-10-20 20:00:00 UTC',
        victim=dict(character=char(100,'Victim'),damage_taken=5000),victim_ship=ship,
        attackers=[dict(character=char(200,'Attacker'),ship=ship,damage_done=4900,damage_percent_value=98,attacker_index=0),
                   dict(character=char(201,'Logi'),ship=ship,damage_done=100,damage_percent_value=2,attacker_index=1)],
        attackers_count=2,location={},fitting_sections=[],fitting_bands=[],slot_layout={},items=[],dropped_items=[],destroyed_items=[],
        total_attacker_damage=5000,final_blow=None,top_damage=None,value_label='1 M',value_title='1,000,000 ISK',inventory_value_totals={})


def event(n=1,at=None,kind='shield_in',amount=100,observer='Victim',source=None,target=None):
    return dict(event_id=hashlib.sha256(str(n).encode()).hexdigest(),occurred_at=(at or TIME-timedelta(seconds=30)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        kind=kind,amount=amount,observer=observer,source=source or dict(name='Logi',character_id=201),target=target or dict(name='Victim',character_id=100),weapon='Remote Shield Booster')


class HealingTests(unittest.TestCase):
    def test_exact_30_minute_window_and_no_post_death_heals(self):
        rows=healing.collect(km(),[event(1,TIME-timedelta(minutes=30)),event(2,TIME),event(3,TIME-timedelta(minutes=30,seconds=1)),event(4,TIME+timedelta(seconds=1))])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['total'],200)

    def test_sender_receiver_deduplicate_without_dropping_identical_cycles(self):
        a=event(1);b=event(2,kind='shield_out',observer='Logi');c=event(3)
        rows=healing.collect(km(),[a,b,c,deepcopy(a)])
        self.assertEqual(rows[0]['total'],200)
        self.assertEqual(rows[0]['percent'],100)

    def test_attacker_healing_layers_and_unrelated_zero_capacitor_excluded(self):
        rows=healing.collect(km(),[event(1,kind='armor_out',amount=80,observer='Logi',target=dict(name='Attacker',character_id=200)),
            event(2,kind='hull_in',amount=20),event(3,amount=0),event(4,kind='capacitor_in',amount=900),
            event(5,target=dict(name='Unrelated',character_id=999)),event(6,target=dict(name='Logi',character_id=201))])
        self.assertEqual(rows[0]['total'],100);self.assertEqual(rows[0]['armor'],80);self.assertEqual(rows[0]['hull'],20)
        self.assertEqual({r['side'] for r in rows[0]['recipients']},{'Victim','Attacker'})

    def test_lookup_by_name_when_peer_id_missing_and_never_substitute_conflicting_id(self):
        rows=healing.collect(km(),[event(1,target=dict(name='victim')),event(2,target=dict(name='Victim',character_id=999))])
        self.assertEqual(rows[0]['total'],100)

    def test_sqlite_read_does_not_modify_events_or_killmail(self):
        original=km();saved=deepcopy(original)
        with tempfile.TemporaryDirectory() as directory,patch.object(store,'ROOT',Path(directory)):
            self.assertEqual(healing.get_healers(original),[])
            raw=body([event()]);store.receive(1,raw,hashlib.sha256(raw).hexdigest())
            rows=healing.get_healers(original)
            self.assertEqual(rows[0]['character']['entity_id'],201)
            self.assertEqual(rows[0]['ships'][0]['name'],'Rifter')
            self.assertEqual(store.timeline()['total'],1)
        self.assertEqual(original,saved)

    def test_missing_observations_leave_original_km_template_unchanged(self):
        renderer=f.load('public_page_renderer')
        html=renderer.content('killmail_detail.html',dict(killmail=km(),error=None,healers=[]))
        self.assertNotIn('aria-label="Healing participants"',html)
        self.assertIn('id="attackers-list"',html)

    def test_healer_template_uses_attacker_style_english_badge_and_escapes_names(self):
        rows=healing.enrich(km(),healing.collect(km(),[event()]))
        rows[0]['character']=dict(name='<script>bad()</script>')
        renderer=f.load('public_page_renderer')
        html=renderer.content('killmail_detail.html',dict(killmail=km(),error=None,healers=rows))
        self.assertIn('attacker-row healer-row',html)
        self.assertIn('+ Healing',html)
        self.assertIn('30 minutes before destruction',html)
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;',html)
        self.assertIn('100 HP',html)
        self.assertLess(html.index('aria-label="Healing participants"'),html.index('Victim & fitting'))


if __name__=='__main__':unittest.main()
