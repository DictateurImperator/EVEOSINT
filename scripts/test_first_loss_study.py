"""Unit checks and optional isolated PostgreSQL integration; no production connections."""

import json, os, signal, subprocess, sys, tempfile, time, unittest
from datetime import datetime, date, timedelta, UTC
from pathlib import Path
from scripts.first_loss_study import core, database as d


class CoreTests(unittest.TestCase):
    def test_no_future_leaks_into_encoded_predictors(self):
        f = {"age_days": 10, "space": "HS", "ship_role": "mining"}
        changed = dict(f, returned=True, return_days=1, cluster=7)
        self.assertEqual(core.encode(f), core.encode(changed))
        self.assertEqual(len(core.encode(f)), len(core.FEATURE_NAMES))

    def test_security_wh_and_missing_values(self):
        self.assertEqual(
            core.space({"regionID": 11000001, "securityStatus": -0.9}), "WH"
        )
        self.assertEqual(
            core.space({"regionID": 10000070, "securityStatus": -0.9}), "Pochven"
        )
        self.assertEqual(
            core.space(
                {"regionID": 10000070, "securityStatus": -0.9},
                datetime(2012, 1, 1, tzinfo=UTC),
            ),
            "unknown",
        )
        self.assertEqual(
            core.space({"regionID": 10000002, "securityStatus": 0.45}), "HS"
        )
        self.assertEqual(core.space({}), "unknown")
        self.assertEqual(core.ship_role("Mining Barge"), "mining")

    def test_return_without_pvp_and_ship_changes(self):
        t = datetime(2026, 1, 1, tzinfo=UTC)
        o = core.outcome(
            t,
            t + timedelta(days=2),
            None,
            None,
            t + timedelta(days=2),
            t + timedelta(days=100),
            [],
            initial_ship=1,
            next_ship=2,
        )
        self.assertTrue(o["30"]["returned"])
        self.assertFalse(o["30"]["pvp_returned"])
        self.assertEqual(o["30"]["trajectory"], "returned_without_observed_pvp")
        self.assertFalse(o["same_ship"])

    def test_missing_archives_and_recent_losses_are_not_false_nonreturns(self):
        t = datetime(2026, 1, 1, tzinfo=UTC)
        o = core.outcome(t, None, None, None, None, t + timedelta(days=10), [])
        self.assertIsNone(o["30"]["returned"])
        self.assertFalse(o["7"]["returned"])
        o = core.outcome(
            t, None, None, None, None, t + timedelta(days=100), [date(2026, 1, 4)]
        )
        self.assertIsNone(o["7"]["returned"])
        o = core.outcome(
            t, t + timedelta(days=2), None, None, None, t + timedelta(days=3), []
        )
        self.assertTrue(o["7"]["returned"])
        self.assertIsNone(o["7"]["pvp_returned"])

    def test_year_adjustment_avoids_simpsons_paradox(self):
        from scripts.first_loss_study.model import adjusted_association

        baseline = {"2012": [100, 200], "2026": [160, 200]}
        groups = {
            "A": {"2012": [80, 160], "2026": [32, 40]},
            "B": {"2012": [20, 40], "2026": [128, 160]},
        }
        result = adjusted_association(groups, baseline)
        self.assertTrue(
            all(abs(r["year_adjusted_difference_pp"]) < 1e-10 for r in result)
        )

    def test_evolution_preserves_non_pvp_transitions_and_known_denominators(self):
        from scripts.first_loss_study.model import evolution_bucket, add_evolution

        b = evolution_bucket()
        add_evolution(
            b,
            {"space": "HS", "ship_role": "mining"},
            {
                "same_zone": False,
                "same_ship": False,
                "same_ship_role": False,
                "return_zone": "LS",
                "return_ship_role": "combat",
                "losses_90": 2,
                "attacks_90": 0,
            },
            90,
        )
        self.assertEqual(b["zone_known"], 1)
        self.assertEqual(b["same_zone"], 0)
        self.assertEqual(b["system_known"], 0)
        self.assertEqual(b["repeat_losses"], 1)
        self.assertEqual(b["zone_transitions"], {"HS → LS": 1})


@unittest.skipUnless(
    os.environ.get("FIRST_LOSS_TEST_DSN"),
    "Set FIRST_LOSS_TEST_DSN to a disposable PostgreSQL database.",
)
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2

        cls.conn = psycopg2.connect(os.environ["FIRST_LOSS_TEST_DSN"])
        with cls.conn.cursor() as c:
            c.execute("SELECT current_database()")
            if not c.fetchone()[0].startswith("first_loss_test"):
                raise RuntimeError(
                    "Integration DB name must start with first_loss_test."
                )
            c.execute(
                "DROP SCHEMA IF EXISTS research CASCADE; DROP SCHEMA IF EXISTS rawkm CASCADE; DROP SCHEMA IF EXISTS entities CASCADE; DROP SCHEMA IF EXISTS population CASCADE; DROP SCHEMA IF EXISTS sde_work CASCADE; CREATE SCHEMA rawkm; CREATE SCHEMA entities; CREATE SCHEMA population; CREATE SCHEMA sde_work;"
            )
            c.execute(
                "DROP TABLE IF EXISTS public.sde_types,public.sde_groups,public.sde_mapsolarsystems CASCADE"
            )
            c.execute(d.SCHEMA.read_text())
            c.execute(
                """CREATE TABLE rawkm.killmail_import_days(day date PRIMARY KEY,status text);
                INSERT INTO rawkm.killmail_import_days SELECT day,'success' FROM generate_series('2012-01-01'::date,'2026-04-01','1 day') day;
                CREATE TABLE entities.characters(character_id bigint PRIMARY KEY,birthday timestamptz);
                CREATE TABLE entities.corporations(corporation_id bigint PRIMARY KEY,is_npc boolean);
                CREATE TABLE entities.character_corporation_history(character_id bigint,corporation_id bigint,start_date timestamptz,end_date timestamptz);
                CREATE TABLE population.corporation_daily(corporation_id bigint,snapshot_date date,member_count integer);
                CREATE TABLE population.alliance_daily(alliance_id bigint,snapshot_date date,member_count integer);
                CREATE TABLE public.sde_types(sde_key text PRIMARY KEY,data jsonb);
                CREATE TABLE public.sde_groups(sde_key text PRIMARY KEY,data jsonb);
                CREATE TABLE public.sde_mapsolarsystems(sde_key text PRIMARY KEY,data jsonb);
                CREATE TABLE sde_work.killmail_item_flags(flag_id integer PRIMARY KEY,is_fitted_slot boolean,is_hold boolean,flag_category text);
                INSERT INTO sde_work.killmail_item_flags VALUES(5,FALSE,TRUE,'hold'),(89,FALSE,FALSE,'implant'),(27,TRUE,FALSE,'slot');
                INSERT INTO entities.corporations VALUES(1,FALSE),(2,TRUE);
                INSERT INTO public.sde_types VALUES('1','{"groupID":1}'),('2','{"groupID":2}'),('670','{"groupID":29}');
                INSERT INTO public.sde_groups VALUES('1','{"name":{"en":"Mining Barge"}}'),('2','{"name":{"en":"Cruiser"}}'),('29','{"name":{"en":"Capsule"}}');
                INSERT INTO public.sde_mapsolarsystems VALUES('10','{"regionID":10000001,"securityStatus":0.8}'),('20','{"regionID":10000002,"securityStatus":0.2}');"""
            )
            for month in ["2012_01", "2012_02", "2026_01", "2026_02"]:
                c.execute(
                    f"""CREATE TABLE rawkm.killmails_{month}(killmail_id bigint,killmail_time timestamptz,victim_character_id bigint,victim_corporation_id bigint,victim_alliance_id bigint,victim_ship_type_id bigint,solar_system_id bigint);
                CREATE TABLE rawkm.killmail_attackers_{month}(killmail_id bigint,killmail_time timestamptz,character_id bigint,corporation_id bigint,alliance_id bigint,ship_type_id bigint);
                CREATE TABLE rawkm.killmail_items_{month}(killmail_id bigint,killmail_time timestamptz,flag integer);"""
                )
            for cid in range(1, 601):
                year = 2012 if cid <= 300 else 2026
                typ = 1 if cid % 3 == 0 else 2
                system = 10 if typ == 1 else 20
                loss = datetime(year, 1, 2, tzinfo=UTC)
                future = datetime(year, 2, 2, tzinfo=UTC)
                c.execute(
                    "INSERT INTO entities.characters VALUES(%s,%s)",
                    (cid, loss - timedelta(days=cid % 90 + 1)),
                )
                c.execute(
                    "INSERT INTO entities.character_corporation_history VALUES(%s,1,%s,NULL)",
                    (cid, loss - timedelta(days=30)),
                )
                c.execute(
                    f"INSERT INTO rawkm.killmails_{year}_01 VALUES(%s,%s,%s,1,100,%s,%s)",
                    (cid, loss, cid, typ, system),
                )
                c.execute(
                    f"INSERT INTO rawkm.killmail_attackers_{year}_01 VALUES(%s,%s,%s,%s,NULL,2)",
                    (
                        cid,
                        loss,
                        None if cid % 5 == 0 else 900001,
                        2 if cid % 5 == 0 else 3,
                    ),
                )
                c.execute(
                    f"INSERT INTO rawkm.killmail_items_{year}_01 VALUES(%s,%s,27)",
                    (cid, loss),
                )
                # A capsule 5 minutes later is part of the episode, not a return.
                c.execute(
                    f"INSERT INTO rawkm.killmails_{year}_01 VALUES(%s,%s,%s,1,100,670,%s)",
                    (cid + 10000, loss + timedelta(minutes=5), cid, system),
                )
                if cid % 3 == 0:
                    # Return only through an NPC loss, in a different zone and ship.
                    c.execute(
                        f"INSERT INTO rawkm.killmails_{year}_02 VALUES(%s,%s,%s,1,100,2,20)",
                        (cid + 20000, future, cid),
                    )
                    c.execute(
                        f"INSERT INTO rawkm.killmail_attackers_{year}_02 VALUES(%s,%s,NULL,NULL,NULL,2)",
                        (cid + 20000, future),
                    )
                elif cid % 3 == 1:
                    c.execute(
                        f"INSERT INTO rawkm.killmails_{year}_02 VALUES(%s,%s,888888,3,NULL,2,20)",
                        (cid + 20000, future),
                    )
                    c.execute(
                        f"INSERT INTO rawkm.killmail_attackers_{year}_02 VALUES(%s,%s,%s,1,100,2)",
                        (cid + 20000, future, cid),
                    )
            c.execute(
                "INSERT INTO research.first_loss_runs(version,status,cutoff,history_start,config) VALUES(1,'running','2026-04-02','2012-01-01','{}') RETURNING id"
            )
            cls.run_id = c.fetchone()[0]
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_full_population_features_followup_and_models(self):
        from scripts.first_loss_study.model import train
        import psycopg2

        months, cutoff, start, missing = d.months(self.conn)
        fingerprint = d.source_fingerprint(self.conn, cutoff)
        d.verify_sources(self.conn, cutoff, fingerprint)
        with self.conn.cursor() as c:
            c.execute(
                "UPDATE rawkm.killmail_import_days SET status='failed' WHERE day='2012-01-01'"
            )
        with self.assertRaisesRegex(ValueError, "Imported history changed"):
            d.verify_sources(self.conn, cutoff, fingerprint)
        self.conn.rollback()
        for phase, fn in [
            ("losses", d.collect_losses),
            ("groups", d.collect_groups),
            ("followup", d.collect_followup),
        ]:
            for month in months:
                if d.completed(self.conn, self.run_id, phase, month):
                    continue
                fn(
                    self.conn,
                    self.run_id,
                    month,
                    cutoff,
                    *([30] if phase == "followup" else []),
                )
                d.checkpoint(self.conn, self.run_id, phase, month)
        # PostgreSQL SUM(bigint) returns Decimal: cover populated group context,
        # not just empty sums or the censored beginning of imported history.
        with self.conn.cursor() as c:
            c.execute(
                "INSERT INTO research.first_loss_group_days VALUES(%s,'2026-01-01','corporation',1,10,4,2)",
                (self.run_id,),
            )
        self.conn.commit()
        roles, systems = d.load_sde(self.conn)
        for month in months:
            after = 0
            while True:
                batch = d.feature_batch(self.conn, self.run_id, month, after, 100)
                if not batch:
                    break
                a, i = d.loss_details(
                    self.conn, self.run_id, month, [r["character_id"] for r in batch]
                )
                output = []
                for row in batch:
                    f, o = d.build_features(
                        row, a, i, roles, systems, cutoff, missing, start
                    )
                    output.append((row["character_id"], f, o, row["loss_time"].year))
                d.store_features(self.conn, self.run_id, output)
                after = batch[-1]["character_id"]
        with self.conn.cursor() as c:
            c.execute(
                "SELECT count(*) FROM research.first_loss_players WHERE run_id=%s",
                (self.run_id,),
            )
            n = c.fetchone()[0]
            self.assertEqual(
                n, 601
            )  # 600 targets plus an adversary with a known loss; attacker-only 900001 excluded.
            c.execute(
                "SELECT outcomes FROM research.first_loss_players WHERE run_id=%s AND character_id=3",
                (self.run_id,),
            )
            o = c.fetchone()[0]
            self.assertEqual(o["return_days"], 31)
            self.assertFalse(o["same_zone"])
            self.assertFalse(o["same_ship"])
            self.assertEqual(o["90"]["trajectory"], "returned_without_observed_pvp")
            c.execute(
                "SELECT outcomes FROM research.first_loss_players WHERE run_id=%s AND character_id=1",
                (self.run_id,),
            )
            o = c.fetchone()[0]
            self.assertTrue(o["90"]["pvp_returned"])
            self.assertEqual(o["first_attack_days"], 31)
            c.execute(
                "SELECT features FROM research.first_loss_players WHERE run_id=%s AND character_id=301",
                (self.run_id,),
            )
            f = c.fetchone()[0]
            self.assertEqual(f["corp_industry_loss_fraction"], 0.5)
            self.assertEqual(f["corp_attacks_30"], 10)
            c.execute(
                "SELECT features,outcomes FROM research.first_loss_players WHERE run_id=%s AND character_id=5",
                (self.run_id,),
            )
            f, o = c.fetchone()
            self.assertEqual(f["opponent"], "NPC/object")
            self.assertFalse(o["initial_pvp"])
            self.assertFalse(
                o["90"]["returned"]
            )  # First NPC loss is included even with no further event.
        self.conn.commit()
        writer = psycopg2.connect(os.environ["FIRST_LOSS_TEST_DSN"])
        try:
            model, report = train(
                self.conn,
                writer,
                self.run_id,
                3,
                lambda *a, **kw: None,
                lambda: None,
                100,
            )
        finally:
            writer.close()
        self.assertEqual(model["training_players"], 601)
        self.assertFalse(model["outcomes_used_for_training"])
        self.assertEqual(report["players"], 601)
        self.assertEqual(sum(v["n"] for v in report["years"].values()), 601)
        h = report["overall"]["horizons"]["90"]
        self.assertEqual(h["evolution"]["without_pvp"]["returned"], 200)
        self.assertEqual(
            h["evolution"]["without_pvp"]["zone_transitions"], {"HS → LS": 200}
        )
        self.assertEqual(h["evolution"]["pvp"]["attacks"], 200)
        with self.conn.cursor() as c:
            c.execute(
                "SELECT count(*) FROM research.first_loss_players WHERE run_id=%s AND cluster IS NOT NULL",
                (self.run_id,),
            )
            self.assertEqual(c.fetchone()[0], 601)
        # Cancelled, uncommitted work must not double-count when a month resumes.
        month = months[1]
        with self.conn.cursor() as c:
            c.execute(
                "SELECT sum(prior_attacks),sum(losses_90),sum(attacks_90) FROM research.first_loss_players WHERE run_id=%s",
                (self.run_id,),
            )
            before = c.fetchone()
        d.collect_followup(self.conn, self.run_id, month, cutoff, 30)
        self.conn.rollback()
        with self.conn.cursor() as c:
            c.execute(
                "SELECT sum(prior_attacks),sum(losses_90),sum(attacks_90) FROM research.first_loss_players WHERE run_id=%s",
                (self.run_id,),
            )
            self.assertEqual(c.fetchone(), before)
        self.assertTrue(d.completed(self.conn, self.run_id, "followup", month))
        self.conn.commit()
        # Exercise the actual worker, including cancellation during monthly SQL
        # and resumption. The only connection is the guarded disposable DB.
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            params = self.conn.get_dsn_parameters()
            cfg = {
                "db_name": params["dbname"],
                "db_user": params["user"],
                "db_password": "",
                "db_host": params["host"],
                "db_port": int(params["port"]),
            }
            (directory / "db.json").write_text(json.dumps(cfg))
            with self.conn.cursor() as c:
                c.execute(
                    """CREATE FUNCTION research.test_slow_insert() RETURNS trigger LANGUAGE plpgsql AS
                  $$BEGIN PERFORM pg_sleep(.005); RETURN NEW; END;$$;
                  CREATE TRIGGER test_slow_insert BEFORE INSERT ON research.first_loss_players
                  FOR EACH ROW EXECUTE FUNCTION research.test_slow_insert()"""
                )
            self.conn.commit()
            command = [
                sys.executable,
                str(Path(__file__).with_name("analyze_first_losses.py")),
                "--db-config",
                str(directory / "db.json"),
                "--progress",
                str(directory / "progress.json"),
                "--clusters",
                "3",
                "--batch-size",
                "100",
            ]
            try:
                with (directory / "job.log").open("w") as log:
                    worker = subprocess.Popen(
                        command + ["--rebuild"], stdout=log, stderr=log
                    )
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if (directory / "progress.json").exists() and json.loads(
                            (directory / "progress.json").read_text()
                        ).get("phase") == "losses":
                            break
                        if worker.poll() is not None:
                            self.fail("Worker exited before loss collection.")
                        time.sleep(0.02)
                    else:
                        worker.terminate()
                        worker.wait(timeout=10)
                        self.fail("Worker did not start.")
                    time.sleep(0.1)
                    worker.send_signal(signal.SIGTERM)
                    self.assertEqual(worker.wait(timeout=10), 0)
                self.assertEqual(
                    json.loads((directory / "progress.json").read_text())["phase"],
                    "paused",
                )
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=10)
                with self.conn.cursor() as c:
                    c.execute(
                        "DROP TRIGGER test_slow_insert ON research.first_loss_players; DROP FUNCTION research.test_slow_insert()"
                    )
                self.conn.commit()
            with self.conn.cursor() as c:
                c.execute("SELECT count(*) FROM research.first_loss_months")
                self.assertEqual(c.fetchone()[0], 0)
                c.execute("SELECT count(*) FROM research.first_loss_players")
                self.assertEqual(c.fetchone()[0], 0)
            self.conn.commit()
            resumed = subprocess.run(
                command, capture_output=True, text=True, timeout=20
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            state = json.loads((directory / "progress.json").read_text())
            self.assertEqual(state["phase"], "completed")
            self.assertEqual(state["players"], 601)
            with self.conn.cursor() as c:
                c.execute(
                    "SELECT summary->>'players',status FROM research.first_loss_runs ORDER BY id DESC LIMIT 1"
                )
                self.assertEqual(c.fetchone(), ("601", "completed"))


if __name__ == "__main__":
    unittest.main()
