"""Exercise actual worker messages and their persistence in the daily runner."""
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import pipeline_metrics as messages
from scripts import update_pipeline as pipeline


class PipelineMetricsTests(unittest.TestCase):
    def parse(self, step, *lines):
        item = {'id': step, 'metrics': {}}
        for line in lines:
            pipeline.parse_step_metrics(item, line)
        return item['metrics']

    def test_every_structured_worker_counter_has_a_visible_label(self):
        template = (Path(__file__).resolve().parents[1] / 'web/templates/admin_update_pipeline.html').read_text()
        for step, fields in messages.FIELDS.items():
            with self.subTest(step=step):
                state = {key: '2026-10' if key.startswith('current_') else 42 for key in fields}
                state['phase'] = 'running'
                with self.assertLogs('pipeline_metrics', level=logging.INFO) as captured:
                    messages.MetricEmitter(step)(state)
                parsed = self.parse(step, captured.output[0])
                self.assertEqual(set(parsed), set(fields.values()))
                for key in parsed:
                    self.assertIn(f'  {key}:', template)

    def test_throttle_phase_changes_final_snapshot_and_sensitive_exclusion(self):
        emitter = messages.MetricEmitter('killmail_missing_prices')
        with patch.object(messages.time, 'monotonic', side_effect=[0, .2, .3, .4]), self.assertLogs('pipeline_metrics', level=logging.INFO) as captured:
            emitter({'phase':'running', 'processed':1, 'db_password':'never-publish'})
            emitter({'phase':'running', 'processed':2})
            emitter({'phase':'completed', 'processed':3})
            emitter({'phase':'completed', 'processed':3})
        self.assertEqual(len(captured.output), 2)
        self.assertNotIn('never-publish', ''.join(captured.output))
        self.assertEqual(self.parse('killmail_missing_prices', *captured.output)['price_days_processed'], 3)

    def test_malformed_and_nested_worker_messages_are_ignored(self):
        self.assertEqual(self.parse('killmail_price_recovery',
            'PIPELINE_METRICS broken', 'PIPELINE_METRICS []',
            'PIPELINE_METRICS ' + json.dumps({'version':1, 'step':'killmail_missing_values', 'metrics':{'killmails_valued':123}})), {})
        item = {'id':'killmail_rankings', 'metrics':{'ranking_current_killmails':100}}
        pipeline.parse_step_metrics(item, 'PIPELINE_METRICS ' + json.dumps(dict(version=1, step='killmail_rankings',
            metrics={'ranking_current_killmails':None,'ranking_months_done':0,'not_allowed':'ignored'})))
        self.assertEqual(item['metrics'], {'ranking_months_done':0})

    def test_import_workers_can_finish_out_of_order_without_losing_counts(self):
        metrics = self.parse('superintel_sync_killmails',
            '2026-10-15 files=100', '2026-10-16 files=200',
            '2026-10-16 committed 50/200', 'SUCCESS 2026-10-15',
            'SUCCESS 2026-10-16', 'SKIP 2026-10-14 already success')
        self.assertEqual(metrics['import_files_total'], 300)
        self.assertEqual(metrics['import_files_processed'], 300)
        self.assertEqual(metrics['killmail_days_loaded'], 2)
        self.assertEqual(metrics['killmail_days_skipped'], 1)

    def test_population_sovereignty_skill_and_mer_counters(self):
        population = self.parse('population_alliances_daily',
            '[DISCOVERY] complete pages=3 unique_alliances=123 advertised_total=123',
            '[SNAPSHOT] saved=120 unresolved=3 backfill_pending=2',
            '[RESOLVE 1/3] Example', '[RESOLVE 1/3] OK Example alliance_id=99 initialized=yes',
            '[BACKFILL PROGRESS] 1/2 completed=1 skipped=0 errors=0 http_calls=4',
            '=== Population live alliance update finished snapshot_saved=123 new_or_renamed=3 resolve_errors=0 backfill_completed=2 backfill_skipped=0 backfill_errors=0 http_calls=5 ===')
        self.assertEqual(population['population_snapshots'], 123)
        self.assertEqual(population['population_resolved'], 1)
        self.assertEqual(population['population_backfill_completed'], 2)
        self.assertEqual(population['population_requests'], 5)
        sov = self.parse('sovereignty_esi', 'SOV_ESI status=updated systems=5432 changes=3 scope=global_systems_2026')
        self.assertEqual(sov, {'current_phase':'updated', 'sovereignty_systems':5432, 'sovereignty_changes':3})
        skill = self.parse('character_skill_inference', 'MONTHS available=227', 'MONTH 2017_02 start',
            'PROGRESS month=2017_02 processed_batch=500 elapsed_batch_sec=1.1 total_processed=2000',
            'MONTH 2017_02 done processed=2000 elapsed_sec=1.1')
        self.assertEqual(skill['skill_months_done'], 1)
        self.assertEqual(skill['skill_killmails_processed'], 2000)
        mer = self.parse('killmail_archive_refresh', 'MER_MATCH_DONE {"matched_unique":4,"unresolved":2,"ambiguous":1}')
        self.assertEqual(mer, {'mer_matched':4, 'mer_unresolved':2, 'mer_ambiguous':1})

    def test_real_child_stdout_is_parsed_and_saved_in_final_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = str(Path(__file__).resolve().parents[1])
            code = (f'import sys; sys.path.insert(0, {source!r}); import logging; logging.basicConfig(level=logging.INFO); '
                    'from scripts.pipeline_metrics import MetricEmitter; '
                    'MetricEmitter("killmail_rankings")({"phase":"completed","processed":2,"months":2,"entity_rows":345})')
            state = dict(pipeline_status='running', steps=[pipeline.step('killmail_rankings', 'Ranking', 'rankings', [sys.executable,'-c',code])])
            with patch.object(pipeline, 'BASE_DIR', root), patch.object(pipeline, 'STATUS_PATH', root/'status.json'):
                self.assertTrue(pipeline.run_step(state, 0, io.StringIO()))
            saved = json.loads((root/'status.json').read_text())['steps'][0]
            self.assertEqual(saved['status'], 'done')
            self.assertEqual(saved['metrics']['ranking_rows_saved'], 345)
            self.assertEqual(saved['metrics']['ranking_months_done'], 2)


if __name__ == '__main__':
    unittest.main()
