"""Check sub-day activation delays across entity types without production access."""
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from scripts import test_killmail_forensics as f

population = f.load('population_intelligence')
UTC = timezone.utc


class ActivationPrecisionTests(unittest.TestCase):
    def calculate(self, entity_type, first_delay):
        anchor = date(2026, 10, 24)
        joined = datetime(2026, 10, 20, 23, 50, tzinfo=UTC)
        spells = {1: [(joined, datetime.max.replace(tzinfo=UTC))],
                  2: [(joined, datetime.max.replace(tzinfo=UTC))]}
        def first_activity(conn, candidates, mode):
            # Pilot 2 has no observed event and remains excluded.
            return {key: start + first_delay for key, cid, start, end in candidates if cid == 1}
        function = getattr(population, '_calculate_intelligence' if entity_type == 'alliance'
                           else f'_calculate_{entity_type}_intelligence')
        with ExitStack() as stack:
            for name, value in [('_table_exists', True),
                                ('_load_official_rows' if entity_type == 'alliance' else f'_load_{entity_type}_official_rows', ([], [])),
                                (f'_load_{entity_type}_membership_spells', (spells, []))]:
                stack.enter_context(patch.object(population, name, return_value=value))
            stack.enter_context(patch.object(population, '_compute_snapshot', return_value={'date': anchor.isoformat()}))
            stack.enter_context(patch.object(population, '_load_first_activity_for_anchor_candidates', side_effect=first_activity))
            return function(object(), 10, [anchor], 90, 'any', 6, scope='first_pvp')[0]['median_activation_delay_days']

    def test_midnight_crossing_measures_elapsed_time_not_calendar_days(self):
        for entity_type in ['alliance', 'corporation', 'coalition']:
            with self.subTest(entity_type=entity_type):
                self.assertAlmostEqual(self.calculate(entity_type, timedelta(minutes=20)), 20 / 1440, places=6)

    def test_fractional_days_survive_median_and_serialization(self):
        for entity_type in ['alliance', 'corporation', 'coalition']:
            with self.subTest(entity_type=entity_type):
                self.assertEqual(self.calculate(entity_type, timedelta(days=3, hours=3)), 3.125)

    def test_instant_activation_is_zero(self):
        self.assertEqual(self.calculate('alliance', timedelta(0)), 0)

    def test_sixteen_daily_candidates_share_one_spell_lookup(self):
        joined = datetime(2026, 10, 1, tzinfo=UTC)
        first_at = joined + timedelta(hours=12)
        conn = f.Connection(rows=[(1, joined, first_at, None)])
        candidates = [(str(day), 1, joined, joined + timedelta(days=day)) for day in range(1, 17)]
        result = population._load_first_activity_for_anchor_candidates(conn, candidates, 'attacker')
        self.assertEqual(result, {str(day): first_at for day in range(1, 17)})
        selects = [(sql, params) for sql, params in conn.queries if params is not None]
        self.assertEqual(len(selects), 1)
        self.assertEqual(selects[0][1], ([1], [joined], [joined + timedelta(days=16)]))
        self.assertNotIn('rawkm.killmails ', selects[0][0])

    def test_database_loader_preserves_event_timestamp_for_each_mode(self):
        first_at = datetime(2026, 10, 21, 0, 10, tzinfo=UTC)
        for mode in ['any', 'attacker', 'loss']:
            with self.subTest(mode=mode):
                joined = first_at - timedelta(minutes=20)
                conn = f.Connection(rows=[(1, joined, first_at if mode != 'loss' else None, first_at if mode != 'attacker' else None)])
                result = population._load_first_activity_for_anchor_candidates(conn, [
                    ('candidate', 1, first_at - timedelta(minutes=20), first_at + timedelta(days=1))
                ], mode)
                self.assertEqual(result['candidate'], first_at)


if __name__ == '__main__':
    unittest.main()
