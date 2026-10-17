"""Inventory multiplication follows the NULL-only worker's per-line rules."""
import unittest
from decimal import Decimal
from scripts.test_killmail_forensics import load
values = load('killmail_item_values')


def item(index,type_id,dropped=0,destroyed=0,flag=5,singleton=0):
    return dict(item_index=index,type_id=type_id,quantity_dropped=dropped,
                quantity_destroyed=destroyed,flag=flag,singleton=singleton)


class InventoryValueTests(unittest.TestCase):
    def test_quantities_and_hull_total_without_early_rounding(self):
        result=values.breakdown([item(1,2,dropped=3,destroyed=7)],1,{1:Decimal('100'),2:Decimal('1.23456')})
        self.assertEqual(result['lines'][1]['unit'],Decimal('1.23456'))
        self.assertEqual(result['dropped'],Decimal('3.70368'))
        self.assertEqual(result['destroyed'],Decimal('108.64192'))
        self.assertEqual(result['total'],Decimal('112.34560'))

    def test_blueprint_copy_frigate_bay_and_special_implant_are_pennies(self):
        items=[item(1,2,destroyed=3,singleton=2),item(2,3,dropped=1,flag=179),item(3,33329,destroyed=1,flag=89)]
        result=values.breakdown(items,1,{1:Decimal('100'),2:Decimal('999999'),3:Decimal('999999'),33329:Decimal('999999')})
        self.assertEqual(result['total'],Decimal('100.05'))
        self.assertTrue(all(line['unit']==Decimal('.01') for line in result['lines'].values()))

    def test_missing_destroyed_price_does_not_hide_known_dropped_subtotal(self):
        result=values.breakdown([item(1,2,destroyed=1),item(2,3,dropped=2)],1,{1:Decimal(100),2:None,3:Decimal(3)})
        self.assertEqual(result['dropped'],Decimal(6))
        self.assertIsNone(result['destroyed'])
        self.assertIsNone(result['total'])

    def test_missing_prices_stay_unavailable_instead_of_looking_like_free_items(self):
        result=values.breakdown([item(1,2,dropped=1)],1,None)
        self.assertIsNone(result['lines'][1]['unit'])
        self.assertIsNone(result['total'])

    def test_calculator_loaded_from_original_script(self):
        calc=values.calculator()
        self.assertEqual(calc.FORMULA_VERSION,'loss-day-v7-abyssal-equivalents')
        self.assertEqual(calc.cents(Decimal('112.34560')),11235)


import os
from scripts import test_killmail_values as fixtures
from scripts import recover_killmail_prices as recovery
from datetime import date


@unittest.skipUnless(os.environ.get('KILLMAIL_VALUES_TEST_DSN'),'Isolated valuation test database required')
class ItemPriceDatabaseTests(unittest.TestCase):
    def setUp(self):
        fixtures.DatabaseTests.setUp(self)
        with values._PRICE_LOCK:
            values._PRICE_CACHE.clear()
        with self.conn.cursor() as cur:
            cur.execute('CREATE SCHEMA market; CREATE TABLE market.zkill_daily_prices(price_date date PRIMARY KEY,prices jsonb)')
            cur.execute(recovery.SCHEMA.read_text())
            cur.execute("CREATE TABLE public.sde_groups(sde_key text,data jsonb); INSERT INTO public.sde_groups VALUES('7','{\"categoryID\":7}')")
            cur.execute("INSERT INTO public.sde_types VALUES('999999901','{\"groupID\":7,\"name\":{\"en\":\"Test module\"}}')")
            cur.execute("INSERT INTO market.zkill_daily_prices VALUES('2026-01-01','{\"999999901\":10}'),('2026-01-02','{}'),('2026-01-03','{\"999999901\":20}')")

    def tearDown(self):
        fixtures.DatabaseTests.tearDown(self)

    def test_nearest_previous_quote_precedes_later_quote(self):
        self.assertEqual(values.load_prices(self.conn,date(2026,1,2),{999999901}),{999999901:Decimal(10)})

    def test_earliest_future_quote_used_when_no_earlier_price(self):
        self.assertEqual(values.load_prices(self.conn,date(2025,12,31),{999999901}),{999999901:Decimal(10)})

    def test_supplement_exact_day_and_official_exact_day_precedence(self):
        day=date(2026,1,2)
        recovery.save_quote(self.conn,day,999999901,Decimal('12.34'),source_kind='item_current')
        self.assertEqual(values._load_prices(self.conn,day,{999999901})[999999901],Decimal('12.34'))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE market.zkill_daily_prices SET prices='{\"999999901\":25}' WHERE price_date='2026-01-02'")
        self.assertEqual(values._load_prices(self.conn,day,{999999901})[999999901],Decimal(25))


    def test_previous_official_quote_wins_over_same_day_supplement(self):
        recovery.save_quote(self.conn,date(2026,1,1),999999901,Decimal('3'),source_kind='item_current')
        # save_quote preserves an already positive daily price, so seed the
        # pre-existing supplement that can remain after a later daily refresh.
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO market.zkill_recovered_prices(price_date,item_type_id,unit_price_isk,source_url) VALUES('2026-01-01',999999901,3,'https://zkillboard.com/item/999999901/')")
        self.assertEqual(values._load_prices(self.conn,date(2026,1,2),{999999901})[999999901],Decimal(10))


    def test_abyssal_inventory_price_uses_t2_even_with_a_positive_abyssal_quote(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO public.sde_groups VALUES('60','{\"categoryID\":7}')")
            cur.execute("INSERT INTO public.sde_types VALUES('52227','{\"groupID\":60,\"name\":{\"en\":\"Abyssal Damage Control\"},\"metaGroupID\":15,\"techLevel\":1}'),('2048','{\"groupID\":60,\"name\":{\"en\":\"Damage Control II\"},\"metaGroupID\":2,\"techLevel\":2}')")
            cur.execute("CREATE TABLE public.sde_dynamicitemattributes(data jsonb); INSERT INTO public.sde_dynamicitemattributes VALUES('{\"inputOutputMapping\":[{\"resultingType\":52227,\"applicableTypes\":[2048]}]}')")
            cur.execute("UPDATE market.zkill_daily_prices SET prices=prices || '{\"52227\":999999999,\"2048\":650000}'::jsonb WHERE price_date='2026-01-01'")
        self.assertEqual(values._load_prices(self.conn,date(2026,1,1),{52227})[52227],Decimal(650000))
