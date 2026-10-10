"""Offline tools navigation and access checks. Never connects to the database."""
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import test_killmail_forensics as f

menus=f.load('menus')
routes=f.load('routes_home')

class ToolsTests(unittest.TestCase):
    def test_navigation_filters_tools_by_existing_permissions(self):
        self.assertEqual(menus.visible_tools({'permissions':set()}),[])
        intel=menus.visible_tools({'permissions':{'superintel.view'}})
        self.assertEqual({t['menu_key'] for t in intel},{'tools.super_evolution'})
        entities=menus.visible_tools({'permissions':{'entities.view'}})
        self.assertIn('tools.economics',{t['menu_key'] for t in entities})
        self.assertIn('tools.ship_analysis',{t['menu_key'] for t in entities})
        self.assertNotIn('tools.super_evolution',{t['menu_key'] for t in entities})

    def test_top_menu_and_tools_sidebar_need_no_database_menu_registration(self):
        user={'permissions':{'entities.view'}}
        with patch.object(menus,'fetch_visible_menu_items',return_value=[]):
            top=menus.build_top_menu(user,'tools')
        self.assertEqual(sum(t['menu_key']=='tools' for t in top),1)
        self.assertTrue(next(t for t in top if t['menu_key']=='tools')['active'])
        context=menus.build_context_menu(user,'tools','tools.economics')
        self.assertTrue(next(t for t in context['items'] if t['menu_key']=='tools.economics')['active'])

    def test_hub_and_comparators_render_with_permission(self):
        app=FastAPI();app.include_router(routes.router)
        user={'username':'test','permissions':{'entities.view'}}
        with patch.object(routes,'require_login',return_value=user),TestClient(app) as client:
            for path in ['/tools','/tools/population','/tools/economics']:
                response=client.get(path)
                self.assertEqual(response.status_code,200)
                self.assertNotIn('Super Evolution',response.text)
            self.assertIn('Economics comparison',client.get('/tools').text)

    def test_search_includes_coalitions_and_filters_corporations(self):
        app=FastAPI();app.include_router(routes.router)
        user={'username':'test','permissions':{'entities.view'}}
        matches=[{'entity_type':'coalition','entity_id':58,'name':'Imperium'},
                 {'entity_type':'alliance','entity_id':1,'name':'Example'},
                 {'entity_type':'corporation','entity_id':2,'name':'Other'}]
        with patch.object(routes,'require_login',return_value=user),patch.object(routes,'search_member_entities',return_value=matches),TestClient(app) as client:
            response=client.get('/api/tools/entity-search?q=Example')
            self.assertEqual(response.status_code,200)
            self.assertEqual(len(response.json()['results']),2)
            self.assertEqual(response.json()['results'][0]['entity_id'],58)

    def test_comparator_denies_users_without_entities_permission(self):
        app=FastAPI();app.include_router(routes.router)
        with patch.object(routes,'require_login',return_value={'username':'test','permissions':set()}),TestClient(app) as client:
            self.assertEqual(client.get('/tools/population',follow_redirects=False).status_code,302)
            self.assertEqual(client.get('/tools/economics',follow_redirects=False).status_code,302)

if __name__=='__main__':unittest.main()
