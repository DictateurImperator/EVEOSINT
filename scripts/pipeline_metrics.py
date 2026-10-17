"""Small, versioned progress messages shared by workers and the daily runner.

Only named counters are emitted; configuration, errors and source payloads are excluded.
"""
import json
import logging
import time

MARKER = 'PIPELINE_METRICS '
FIELDS = {
    'killmail_archive_refresh': {
        'phase': 'current_phase', 'current_day': 'current_period',
        'scanned': 'archives_checked', 'archives_total': 'archives_total',
        'to_update': 'archives_to_update', 'processed': 'archives_processed',
        'updated': 'archives_updated', 'failed': 'archive_errors', 'added': 'archive_killmails_added',
        'bytes_downloaded': 'archive_bytes_downloaded', 'bytes_total': 'archive_bytes_total',
        'files_read': 'archive_files_read',
    },
    'killmail_missing_prices': {
        'phase': 'current_phase', 'current_day': 'current_period',
        'total_days': 'price_days_total', 'processed': 'price_days_processed',
        'downloaded': 'price_days_downloaded', 'skipped': 'price_days_skipped',
        'empty': 'price_days_empty', 'items': 'price_rows_imported',
        'errors': 'price_errors', 'requests': 'price_requests',
    },
    'killmail_missing_values': {
        'phase': 'current_phase', 'current_month': 'current_period',
        'current_price_day': 'valuation_price_day',
        'months_processed': 'valuation_months_done', 'months_total': 'valuation_months_total',
        'killmails_valued': 'killmails_valued', 'killmails_unvalued': 'killmails_unvalued',
        'fallback_price_pairs': 'valuation_price_fallbacks',
        'unavailable_recipe_price_pairs': 'valuation_recipe_gaps',
    },
    'killmail_price_recovery': {
        'phase': 'current_phase', 'current_day': 'current_period',
        'months_processed': 'recovery_months_done', 'months_total': 'recovery_months_total',
        'missing_items': 'recovery_missing_items', 'missing_item_days': 'recovery_pairs_total',
        'estimated_calls_max': 'recovery_calls_estimated', 'gaps_total': 'recovery_pairs_total',
        'gaps_processed': 'recovery_pairs_done', 'items_processed': 'recovery_items_done',
        'recovered': 'recovery_pairs_recovered', 'fallback_pairs': 'recovery_fallback_pairs',
        'unresolved': 'recovery_unresolved', 'requests': 'recovery_requests',
        'planned_calls_max': 'recovery_calls_estimated',
        'killmails_valued': 'killmails_valued', 'killmails_unvalued': 'killmails_unvalued',
        'valuation_phase': 'recovery_valuation_phase',
    },
    'killmail_rankings': {
        'phase': 'current_phase', 'current_month': 'current_period',
        'processed': 'ranking_months_done', 'months': 'ranking_months_total',
        'entity_rows': 'ranking_rows_saved', 'killmails': 'ranking_current_killmails',
        'valued': 'ranking_current_valued',
    },
}


class MetricEmitter:
    """Emit at most once a second, plus phase changes and terminal updates."""
    def __init__(self, step_id):
        self.step_id = step_id
        self.last = float('-inf')
        self.phase = None

    def __call__(self, state):
        now = time.monotonic()
        phase = state.get('phase')
        if phase == self.phase and now - self.last < 1:
            return
        self.last, self.phase = now, phase
        metrics = {label: state[key] for key, label in FIELDS[self.step_id].items()
                   if key in state and (state[key] is None or type(state[key]) in (str, int, float, bool))}
        logging.getLogger('pipeline_metrics').info('%s%s', MARKER, json.dumps(
            dict(version=1, step=self.step_id, metrics=metrics), ensure_ascii=True))
