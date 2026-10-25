"""Activation batching against disposable PostgreSQL, with an independent oracle."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import socket
import subprocess
import tempfile
import unittest
import psycopg2
from scripts.test_population_activation import population

PG = Path('/usr/lib/postgresql/16/bin')
UTC = timezone.utc


@unittest.skipUnless((PG/'initdb').exists(), 'Local PostgreSQL required')
class ActivationDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='eveosint-activation-test-')
        cls.root = Path(cls.tmp.name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); cls.port = sock.getsockname()[1]
        subprocess.run([str(PG/'initdb'), '-D', str(cls.root/'db'), '-A', 'trust', '-U', 'test', '--no-locale'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run([str(PG/'pg_ctl'), '-D', str(cls.root/'db'), '-l', str(cls.root/'server.log'), '-o', f"-F -k {cls.root} -h '' -p {cls.port}", '-w', 'start'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls):
        subprocess.run([str(PG/'pg_ctl'), '-D', str(cls.root/'db'), '-m', 'immediate', '-w', 'stop'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.tmp.cleanup()

    def setUp(self):
        self.conn = psycopg2.connect(dbname='postgres', user='test', host=str(self.root), port=self.port)
        self.addCleanup(self.conn.close)
        self.events = {'attacker': [], 'loss': []}
        with self.conn.cursor() as cur:
            cur.execute('''DROP SCHEMA IF EXISTS rawkm CASCADE;
                DROP SCHEMA IF EXISTS entities CASCADE;
                CREATE SCHEMA rawkm; CREATE SCHEMA entities;
                CREATE TABLE rawkm.killmails(victim_character_id bigint, killmail_time timestamptz);
                CREATE TABLE rawkm.killmail_attackers(character_id bigint, killmail_time timestamptz);
                CREATE TABLE entities.character_corporation_history(character_id bigint, corporation_id bigint, start_date timestamptz, end_date timestamptz, is_deleted boolean DEFAULT false);
                CREATE TABLE entities.corporation_alliance_history(corporation_id bigint, alliance_id bigint, start_date timestamptz, end_date timestamptz, is_deleted boolean DEFAULT false);''')
        self.start = datetime(2026, 10, 1, tzinfo=UTC)

    def event(self, cid, when, kind):
        self.events[kind].append((cid, when))
        table, column = ('killmail_attackers', 'character_id') if kind == 'attacker' else ('killmails', 'victim_character_id')
        with self.conn.cursor() as cur:
            cur.execute(f'INSERT INTO rawkm.{table} VALUES(%s,%s)', (cid, when))

    def reference(self, candidates, mode):
        result = {}
        for key, cid, start, end in candidates:
            attacks = [at for who, at in self.events['attacker'] if who == cid and start <= at < end]
            losses = [at for who, at in self.events['loss'] if who == cid and start <= at < end]
            hits = attacks if mode == 'attacker' else ([] if attacks else losses) if mode == 'loss' else attacks + losses
            if hits:
                result[key] = min(hits)
        return result

    def test_future_attack_does_not_rewrite_earlier_loss_only_point(self):
        self.event(1, self.start + timedelta(hours=2), 'loss')
        self.event(1, self.start + timedelta(days=2), 'attacker')
        candidates = [('early', 1, self.start, self.start + timedelta(days=1)),
                      ('boundary', 1, self.start, self.start + timedelta(days=2)),
                      ('later', 1, self.start, self.start + timedelta(days=3))]
        for mode in ['attacker', 'any', 'loss']:
            with self.subTest(mode=mode):
                self.assertEqual(population._load_first_activity_for_anchor_candidates(self.conn, candidates, mode), self.reference(candidates, mode))

    def test_generated_histories_match_oracle_for_all_daily_points_and_modes(self):
        rng = random.Random(42)
        candidates = []
        for cid in range(1, 26):
            # Two independent membership spells, each with 16 daily anchors.
            for offset in [0, 30]:
                joined = self.start + timedelta(days=offset, hours=cid % 24)
                for day in range(1, 17):
                    candidates.append((f'{cid}:{offset}:{day}', cid, joined, joined + timedelta(days=day)))
                for _ in range(rng.randrange(0, 12)):
                    self.event(cid, joined + timedelta(hours=rng.randrange(-24, 408)), rng.choice(['attacker', 'loss']))
        for mode in ['attacker', 'any', 'loss']:
            with self.subTest(mode=mode):
                self.assertEqual(population._load_first_activity_for_anchor_candidates(self.conn, candidates, mode), self.reference(candidates, mode))

    def test_leave_rejoin_and_internal_corporation_transfer_preserve_spells(self):
        with self.conn.cursor() as cur:
            cur.execute('''INSERT INTO entities.corporation_alliance_history VALUES
                (10,100,'2020-01-01',NULL,false),(20,100,'2020-01-01',NULL,false);
                INSERT INTO entities.character_corporation_history VALUES
                (1,10,'2026-10-01','2026-10-03',false),
                (1,20,'2026-10-03','2026-10-06',false),
                (1,10,'2026-10-10',NULL,false);''')
        spells, _ = population._load_alliance_membership_spells(self.conn, 100, (self.start + timedelta(days=20)).date())
        self.assertEqual(len(spells[1]), 2)
        self.assertEqual(spells[1][0], (self.start, self.start + timedelta(days=5)))
        self.event(1, self.start + timedelta(days=1), 'attacker')
        self.event(1, self.start + timedelta(days=10), 'attacker')
        candidates = [(str(i), 1, start, min(end, self.start + timedelta(days=15))) for i, (start, end) in enumerate(spells[1])]
        result = population._load_first_activity_for_anchor_candidates(self.conn, candidates, 'attacker')
        self.assertEqual(result['0'], self.start + timedelta(days=1))
        self.assertEqual(result['1'], self.start + timedelta(days=10))

    def test_empty_invalid_and_multiple_sql_chunks(self):
        self.assertEqual(population._load_first_activity_for_anchor_candidates(self.conn, [], 'attacker'), {})
        self.assertEqual(population._load_first_activity_for_anchor_candidates(self.conn, [('invalid', 1, self.start, self.start)], 'attacker'), {})
        candidates = [(str(cid), cid, self.start, self.start + timedelta(days=1)) for cid in range(1, 503)]
        self.event(501, self.start + timedelta(minutes=1), 'attacker')
        self.assertEqual(population._load_first_activity_for_anchor_candidates(self.conn, candidates, 'attacker'), self.reference(candidates, 'attacker'))


if __name__ == '__main__':
    unittest.main()
