(() => {
  const $ = id => document.getElementById(id);
  const phases = {
    starting: 'Starting', queued: 'Months queued',
    initializing_cumulative_snapshots: 'Initializing cumulative totals from existing monthly results',
    reading_killmails: 'Reading this month’s killmails',
    aggregating_attackers: 'Calculating full-value and damage-weighted credit',
    writing_rankings: 'Saving monthly results and cumulative totals',
    analyzing_totals: 'Updating database statistics', completed: 'Completed',
    stopped: 'Stopped — completed months are saved', interrupted: 'Interrupted — completed months are saved',
    failed: 'Failed — see job log', purged: 'Rankings cleared — ready to build from scratch'
  };
  const number = value => Number(value || 0).toLocaleString();
  const duration = seconds => `${Math.floor(seconds / 3600)}h ${Math.floor(seconds / 60) % 60}m ${Math.floor(seconds) % 60}s`;
  let wasRunning = false;
  async function poll() {
    try {
      const r = await fetch('/admin/debug/rankings/status', {cache: 'no-store'});
      if (!r.ok) throw new Error('Job status unavailable');
      const data = await r.json(), p = data.progress || {};
      if ($('rankStart')) $('rankStart').disabled = data.running;
      if ($('rankStop')) $('rankStop').disabled = !data.running;
      const now = Date.now(), end = data.running ? now : Date.parse(p.updated_at);
      const elapsed = (end - Date.parse(p.started_at)) / 1000;
      const phaseElapsed = (end - Date.parse(p.phase_started_at)) / 1000;
      const heartbeat = (now - Date.parse(p.updated_at)) / 1000;
      $('rankDetail').textContent = [data.running ? 'Running' : '', phases[p.phase] || p.phase || 'Not started',
        p.current_month ? `Month: ${p.current_month}` : '',
        Number.isFinite(elapsed) ? `Elapsed: ${duration(Math.max(0, elapsed))}` : '',
        data.running && Number.isFinite(phaseElapsed) ? `This step: ${duration(Math.max(0, phaseElapsed))}` : '',
        data.running && Number.isFinite(heartbeat) ? (heartbeat > 15 ? 'No recent heartbeat — check job log' : 'Worker heartbeat received') : ''
      ].filter(Boolean).join(' · ');
      $('rankProgress').textContent = [
        p.months > 0 ? `Months saved: ${number(p.processed)} / ${number(p.months)}` : '',
        p.entity_rows !== undefined ? `Monthly entity rows saved: ${number(p.entity_rows)}` : '',
        p.current_month && p.killmails != null ? `Killmails in current month: ${number(p.killmails)}` : ''
      ].filter(Boolean).join(' · ');
      const percent = p.months ? Math.min(100, 100 * (p.processed || 0) / p.months) : 0;
      $('rankBar').style.width = `${percent}%`;
      $('rankBar').classList.toggle('progress-bar-striped', Boolean(data.running));
      $('rankBar').classList.toggle('progress-bar-animated', Boolean(data.running));
      $('rankError').textContent = p.error || '';
      if (wasRunning && !data.running && p.phase === 'completed') location.reload();
      wasRunning = data.running;
    } catch (e) { $('rankError').textContent = e.message; }
    finally { setTimeout(poll, 3000); }
  }
  for (const action of ['start', 'stop']) {
    const b = $('rank' + action[0].toUpperCase() + action.slice(1));
    if (b) b.onclick = async () => {
      b.disabled = true;
      $('rankError').textContent = '';
      try {
        const r = await fetch('/admin/debug/rankings/' + action, {method: 'POST', headers: {'Accept': 'application/json'}});
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Job action failed');
        if (action === 'start') wasRunning = true;
      } catch (e) { $('rankError').textContent = e.message; b.disabled = false; }
    };
  }
  poll();
})();
