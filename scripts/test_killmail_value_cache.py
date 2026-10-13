"""Source-backed Price/Build cache behavior, independent of zKill totals."""

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from scripts.calculate_killmail_values import Pricing, QuoteCache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 1, 12, tzinfo=UTC)

    def engine(self, **kwargs):
        return Pricing(
            kwargs.pop("metadata", {}), kwargs.pop("recipes", {}),
            kwargs.pop("counts", {1: 100, 2: 100}),
            clock=lambda: self.now, **kwargs,
        )

    def test_first_quote_survives_history_refresh_and_new_items_use_new_history(self):
        engine = self.engine()
        day = date(2026, 8, 1)
        engine.add_day(day, {"1": "10", "2": "20"})
        self.assertEqual(engine.price(1, day, day), (Decimal("10"), False))
        engine.add_day(day, {"1": "30", "2": "40"})
        self.assertEqual(engine.price(1, day, day), (Decimal("10"), False))
        self.assertEqual(engine.price(2, day, day), (Decimal("40"), False))

    def test_recent_market_cache_expires_and_recomputes(self):
        engine = self.engine()
        day = date(2026, 8, 1)
        engine.add_day(day, {"1": "10"})
        engine.price(1, day, day)
        engine.add_day(day, {"1": "30"})
        self.now += timedelta(days=1)
        self.assertEqual(engine.price(1, day, day), (Decimal("30"), False))

    def test_old_market_cache_expires_after_one_hour(self):
        engine = self.engine()
        day = date(2026, 7, 1)
        engine.add_day(day, {"1": "10"})
        engine.price(1, day, day)
        engine.add_day(day, {"1": "30"})
        self.now += timedelta(minutes=59)
        self.assertEqual(engine.price(1, day, day)[0], Decimal("10"))
        self.now += timedelta(minutes=1)
        self.assertEqual(engine.price(1, day, day)[0], Decimal("30"))

    def test_shared_date_hash_expiry_is_extended_by_new_quote_not_read(self):
        cache = QuoteCache(clock=lambda: self.now)
        day = date(2026, 7, 1)
        cache.market_put(1, day, (Decimal("10"), False))
        self.now += timedelta(minutes=50)
        cache.market_put(2, day, (Decimal("20"), False))
        self.now += timedelta(minutes=20)
        self.assertEqual(cache.market_get(1, day)[0], Decimal("10"))
        self.now += timedelta(minutes=40)
        self.assertIsNone(cache.market_get(1, day))

    def test_disabled_legacy_cache_reads_refreshed_history(self):
        engine = self.engine(rules_at=datetime(2022, 5, 10, tzinfo=UTC))
        day = date(2021, 1, 2)
        engine.add_day(day, {"1": "10"})
        engine.price(1, day, day)
        engine.add_day(day, {"1": "30"})
        self.assertEqual(engine.price(1, day, day)[0], Decimal("30"))

    def test_source_revision_does_not_follow_kill_date(self):
        day = date(2009, 1, 1)
        current = self.engine()
        self.assertTrue(current.quotes.market_enabled())
        current.add_day(day, {"1": "10"})
        current.price(1, day, day)
        current.add_day(day, {"1": "30"})
        self.assertEqual(current.price(1, day, day)[0], Decimal("10"))

    def test_before_2025_old_date_cache_lasts_a_day(self):
        cache = QuoteCache(
            clock=lambda: self.now,
            rules_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        day = date(2009, 1, 1)
        cache.market_put(1, day, (Decimal("10"), False))
        self.now += timedelta(hours=2)
        self.assertEqual(cache.market_get(1, day)[0], Decimal("10"))

    def test_build_cache_is_separate_and_expires_after_a_day(self):
        cache = QuoteCache(clock=lambda: self.now)
        day = date(2009, 1, 1)
        cache.build_put(1, day, (Decimal("10"), False))
        self.now += timedelta(hours=2)
        self.assertEqual(cache.build_get(1, day)[0], Decimal("10"))
        self.now += timedelta(hours=22)
        self.assertIsNone(cache.build_get(1, day))

    def test_structure_build_recalc_bypasses_old_build_quote(self):
        engine = self.engine(metadata={1: (1415, 66, "Structure module")},
                             recipes={1: (1, {2: 3})})
        day = date(2026, 7, 2)
        material_day = day - timedelta(days=1)
        engine.add_day(material_day, {"2": "10"})
        self.assertEqual(engine.price(1, day, day)[0], Decimal("30"))
        engine.add_day(material_day, {"2": "20"})
        self.now += timedelta(hours=2)
        self.assertEqual(engine.price(1, day, day)[0], Decimal("60"))

    def test_legacy_build_ignores_recalc_flag_until_2024_revision(self):
        cache = QuoteCache(
            clock=lambda: self.now,
            rules_at=datetime(2023, 1, 1, tzinfo=UTC),
        )
        day = date(2026, 8, 1)
        cache.build_put(1, day, (Decimal("10"), False))
        self.assertEqual(cache.build_get(1, day, recalc=True)[0], Decimal("10"))
        cache.rules_at = datetime(2024, 4, 1, tzinfo=UTC)
        self.assertIsNone(cache.build_get(1, day, recalc=True))


if __name__ == "__main__":
    unittest.main()
