"""Browser smoke test for the pipeline's per-job metric pills (no production calls)."""
import json
from pathlib import Path
import unittest

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from playwright.sync_api import sync_playwright, expect

from scripts import pipeline_metrics as messages


class PipelineBrowserTests(unittest.TestCase):
    def test_live_counters_render_for_all_new_workers_and_keep_zeroes(self):
        templates = Path(__file__).resolve().parents[1] / 'web/templates'
        env = Environment(loader=ChoiceLoader([
            DictLoader({'base.html':'{% block content %}{% endblock %}'}),
            FileSystemLoader(str(templates)),
        ]), autoescape=True)
        steps = []
        for step, fields in messages.FIELDS.items():
            metrics = {key:1234 for key in fields.values()}
            metrics.update(current_phase='completed', current_period='2026-10-16')
            steps.append(dict(id=step, label=step, status='done', group='market', command=[],
                              duration_seconds=12.3, metrics=metrics))
        steps[0]['metrics']['archive_errors'] = 0
        state = dict(pipeline_status='completed', running=False, current_step=None, timer={},
                     next_plan={}, history=[], summary={}, steps=steps, duration_seconds=100, run_id='test')
        html = env.get_template('admin_update_pipeline.html').render(pipeline=state, can_run=False)
        with sync_playwright() as p:
            browser = p.chromium.launch(args=['--no-sandbox'])
            page = browser.new_page(viewport={'width':1400, 'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def serve(route):
                url = route.request.url
                if '/api/status' in url:
                    route.fulfill(content_type='application/json', body=json.dumps(state))
                elif '/api/log' in url:
                    route.fulfill(body='Test log')
                else:
                    route.fulfill(content_type='text/html', body=html)
            page.route('**/*', serve)
            page.goto('http://pipeline.test/admin/update-pipeline', wait_until='networkidle')
            expect(page.locator('#stepsContainer .step-metrics')).to_have_count(5)
            expect(page.locator('#stepsContainer')).to_contain_text('Archives replaced')
            expect(page.locator('#stepsContainer')).to_contain_text('Item price rows imported')
            expect(page.locator('#stepsContainer')).to_contain_text('Killmails valued')
            expect(page.locator('#stepsContainer')).to_contain_text('Item/date prices recovered')
            expect(page.locator('#stepsContainer')).to_contain_text('Ranking months saved')
            expect(page.locator('.metric-pill').filter(has_text='Archive errors').locator('strong')).to_have_text('0')
            # Next status poll updates the counters rather than requiring a page reload.
            state['steps'][-1]['metrics']['ranking_months_done'] = 227
            page.evaluate('refreshStatus()')
            expect(page.locator('.metric-pill').filter(has_text='Ranking months saved').locator('strong')).to_have_text('227')
            page.set_viewport_size({'width':390,'height':844})
            expect(page.locator('#stepsContainer')).to_contain_text('Ranking months saved')
            self.assertFalse(errors, errors)
            browser.close()


if __name__ == '__main__':
    unittest.main()
