"""Check daily orchestration without starting workers or touching production data."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scripts import update_pipeline as pipeline


class DailyPipelineTests(unittest.TestCase):
    def run_mock_pipeline(self, *, ready=False, fail=None):
        executed = []
        snapshots = []

        def run_step(state, index, log):
            item = state['steps'][index]
            executed.append(item['id'])
            item['status'] = 'failed' if item['id'] == fail else 'done'
            return item['id'] != fail

        def super_commands(day):
            self.assertEqual(executed, ['sync_sde', 'killmail_archive_refresh'])
            return (None if ready else [['python', f'super-{i}.py'] for i in range(4)]), {}

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            for key, value in {
                'RUN_DIR': root / 'run', 'HISTORY_DIR': root / 'history',
                'LOG_DIR': root / 'logs', 'LOCK_PATH': root / 'run/lock',
            }.items():
                stack.enter_context(patch.object(pipeline, key, value))
            stack.enter_context(patch.object(pipeline, 'validate_scripts'))
            stack.enter_context(patch.object(pipeline, 'write_state', side_effect=lambda state: snapshots.append(state.copy())))
            stack.enter_context(patch.object(pipeline, 'archive_state'))
            stack.enter_context(patch.object(pipeline, 'run_step', side_effect=run_step))
            stack.enter_context(patch.object(pipeline, 'superintel_commands', side_effect=super_commands))
            result = pipeline.run_pipeline('scheduled', 'test')
        return result, executed, snapshots[-1]

    def test_full_order_and_price_options(self):
        result, executed, state = self.run_mock_pipeline()
        steps = pipeline.weekly_steps()
        self.assertEqual(result, 0)
        self.assertEqual(executed, [step['id'] for step in steps])
        self.assertEqual(executed[-3:], ['killmail_missing_prices', 'killmail_missing_values', 'killmail_price_recovery'])
        self.assertEqual(steps[-3]['command'][-2:], ['--refresh-days', '0'])
        self.assertEqual(len(steps[1]['command']), 2)  # All imported history; no date limits.
        self.assertEqual(state['scheduled_time'], '14:30')

    def test_superintel_ready_still_runs_other_jobs(self):
        result, executed, state = self.run_mock_pipeline(ready=True)
        self.assertEqual(result, 0)
        self.assertEqual(executed, [s['id'] for s in pipeline.weekly_steps() if s['group'] != 'superintel'])
        self.assertTrue(all(s['status'] == 'skipped' for s in state['steps'] if s['group'] == 'superintel'))

    def test_archive_failure_stops_before_superintel(self):
        result, executed, _ = self.run_mock_pipeline(fail='killmail_archive_refresh')
        self.assertEqual(result, 1)
        self.assertEqual(executed, ['sync_sde', 'killmail_archive_refresh'])

    def test_price_failure_does_not_calculate_values(self):
        result, executed, _ = self.run_mock_pipeline(fail='killmail_missing_prices')
        self.assertEqual(result, 1)
        self.assertEqual(executed[-1], 'killmail_missing_prices')
        self.assertNotIn('killmail_missing_values', executed)

    def test_valuation_failure_does_not_start_recovery(self):
        result, executed, _ = self.run_mock_pipeline(fail='killmail_missing_values')
        self.assertEqual(result, 1)
        self.assertEqual(executed[-1], 'killmail_missing_values')
        self.assertNotIn('killmail_price_recovery', executed)

    def test_timer_uses_paris_timezone(self):
        timer = (Path(__file__).resolve().parents[1] / 'deploy/eveosint-update.timer').read_text()
        self.assertIn('OnCalendar=*-*-* 14:30:00 Europe/Paris', timer)


if __name__ == '__main__':
    unittest.main()
