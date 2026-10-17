"""Killmail location thumbnails reuse existing map previews and tolerate missing SDE."""
import unittest
from unittest.mock import patch
from pathlib import Path
from jinja2 import Environment
from scripts.test_killmail_forensics import load
maps=load('map_data')


class LocationPreviewTests(unittest.TestCase):
    def setUp(self):
        maps._killmail_location_preview.cache_clear()

    def test_three_maps_reused_for_repeated_kills(self):
        location={kind:{'entity_id':n} for kind,n in [('system',1),('constellation',2),('region',3)]}
        with patch.object(maps,'get_location_preview',side_effect=lambda kind,entity_id:dict(kind=kind,nodes=[],edges=[])) as get,patch.object(maps,'monotonic',return_value=100):
            first=maps.get_killmail_location_previews(location)
            second=maps.get_killmail_location_previews(location)
            self.assertEqual(set(first),{'system:1','constellation:2','region:3'})
            self.assertEqual(first,second)
            self.assertEqual(get.call_count,3)

    def test_missing_system_does_not_break_other_cards(self):
        def preview(kind,entity_id):
            if kind=='system':raise maps.MapDataError('system_not_found')
            return dict(kind=kind,nodes=[],edges=[])
        with patch.object(maps,'get_location_preview',side_effect=preview),self.assertLogs(maps.__name__,level='WARNING'):
            result=maps.get_killmail_location_previews({'system':{'entity_id':1},'region':{'entity_id':3}})
            self.assertEqual(set(result),{'region:3'})
        self.assertEqual(maps.get_killmail_location_previews(None),{})

    def test_map_link_and_current_system_marker(self):
        path=Path(__file__).resolve().parents[1]/'web/templates/killmail_detail.html'
        source=path.read_text();start=source.index('{% macro location_map(');end=source.index('{%- endmacro %}',start)+len('{%- endmacro %}')
        template=Environment(autoescape=True).from_string(source[start:end]+"{{ location_map('region',ref) }}")
        html=template.render(ref={'entity_id':3,'name':'Test <region>'},killmail={'solar_system_id':42},location_previews={'region:3':{'kind':'region','edges':[],'nodes':[{'id':42,'x':30,'y':60}]}})
        self.assertIn('href="/map/region/3"',html)
        self.assertIn('thumb-marker',html)
        self.assertIn('Test &lt;region&gt;',html)
