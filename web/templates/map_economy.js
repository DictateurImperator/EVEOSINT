// Included inside the map controller so both SVG and canvas use the same frames.
const economyControls = document.getElementById('eve2dEconomyControls');
const economyFrom = document.getElementById('eve2dEconomyFrom');
const economyTo = document.getElementById('eve2dEconomyTo');
const economyMode = document.getElementById('eve2dEconomyMode');
const economyMetricBox = document.getElementById('eve2dEconomyMetrics');
const economyCombined = document.getElementById('eve2dEconomyCombined');
const economyBasis = document.getElementById('eve2dEconomyBasis');
const economyTimeline = document.getElementById('eve2dEconomyTimeline');
const economySlider = document.getElementById('eve2dEconomyFrame');
const economyPlay = document.getElementById('eve2dEconomyPlay');
const economyFps = document.getElementById('eve2dEconomyFps');
const economyStatus = document.getElementById('eve2dEconomyStatus');
const economyLegend = document.getElementById('eve2dEconomyLegend');
const economyAnchors = new Map();
allNodes.forEach(node => {
  if (!node.region_id) return;
  const key = String(node.region_id);
  const group = economyAnchors.get(key) || {region_id: node.region_id, name: node.region_name, region_name: node.region_name, _x: 0, _y: 0, count: 0, url: '/map/region/' + node.region_id};
  group._x += node._x; group._y += node._y; group.count++;
  economyAnchors.set(key, group);
});
economyAnchors.forEach(group => {group._x /= group.count; group._y /= group.count;});
let economyOptions = null;
let economyPayload = null;
let economyFrameIndex = 0;
let economyRegionEntries = new Map();
let economyCap = 0;
let economyTimer = null;
let economyRequestToken = 0;
let economyAborter = null;

function stopEconomicAnimation() {
  if (economyTimer !== null) clearInterval(economyTimer);
  economyTimer = null;
  economyPlay.textContent = 'Play';
}
function economicMeasurements(entry) {
  return economyCombined.checked ? [entry.combined] : entry.metrics;
}
function economicValue(item) {
  return economyBasis.value === 'relative' ? item.ratio : (item.value === null ? null : Number(item.value));
}
function economicMoney(value) {
  return value === null ? 'Not published' : Number(value).toLocaleString(undefined, {maximumFractionDigits: 0}) + ' ISK';
}
function economicPercent(value) {
  return value === null ? 'Undefined' : (100 * value).toLocaleString(undefined, {maximumFractionDigits: 2}) + '%';
}
function economicPeriod() {
  if (!economyPayload) return '';
  const frame = economyPayload.frames[economyFrameIndex];
  return frame.month || (economyPayload.from === economyPayload.to ? economyPayload.from : economyPayload.from + ' through ' + economyPayload.to);
}
function economicTooltip(entry) {
  const lines = [entry.region_name + ' · ' + economicPeriod(), 'Regional production: ' + economicMoney(entry.production)];
  economicMeasurements(entry).forEach(item => {
    const comparison = item.ratio === null ? 'Relative intensity unavailable' : economicPercent(item.ratio) + ' of production';
    lines.push(item.label + ': ' + economicMoney(item.value) + ' · ' + comparison);
    if (item.missing_reason) lines.push(item.missing_reason);
  });
  if (economyCombined.checked) lines.push('Combined = ' + economyPayload.metrics.map(m => m.label).join(' + '));
  return lines.join('\n');
}
function economicColor(item) {
  if (item.key === 'combined') return '#67e8f9';
  return economyPayload.metrics.find(m => m.key === item.key).color;
}
function economicLights() {
  const lights = [];
  if (activeMode !== 'economy' || !economyPayload) return lights;
  economyRegionEntries.forEach(entry => {
    const anchor = economyAnchors.get(String(entry.region_id));
    if (!anchor) return;
    const items = economicMeasurements(entry);
    items.forEach((item, index) => {
      const value = economicValue(item);
      const intensity = value === null || economyCap <= 0 ? 0 : Math.max(0, Math.min(1, value / economyCap));
      lights.push({entry, item, anchor, value, intensity, radius: 8 + Math.sqrt(intensity) * 28,
                   _x: anchor._x + (index - (items.length - 1) / 2) * 24, _y: anchor._y,
                   color: economicColor(item)});
    });
  });
  return lights;
}
function drawEconomy() {
  if (activeMode !== 'economy') return;
  ctx.save();
  economicLights().forEach(light => {
    const p = worldToScreen(light);
    const r = light.radius * scale;
    if (light.value !== null && light.value > 0) {
      const gradient = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 2);
      gradient.addColorStop(0, light.color); gradient.addColorStop(1, 'transparent');
      ctx.globalAlpha = .15 + .65 * light.intensity;
      ctx.fillStyle = gradient; ctx.beginPath(); ctx.arc(p.x, p.y, r * 2, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = light.value === null ? .65 : .3 + .7 * light.intensity;
    ctx.strokeStyle = light.value === null ? '#94a3b8' : light.color;
    ctx.setLineDash(light.value === null ? [3, 3] : []);
    ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(3, r / 2), 0, Math.PI * 2); ctx.stroke();
  });
  ctx.globalAlpha = 1; ctx.setLineDash([]); ctx.textAlign = 'center';
  ctx.font = '600 11px system-ui, sans-serif'; ctx.fillStyle = '#cbd5e1';
  economyRegionEntries.forEach(entry => {
    const anchor = economyAnchors.get(String(entry.region_id));
    if (!anchor) return;
    const p = worldToScreen(anchor);
    ctx.fillText(entry.region_name, p.x, p.y + Math.max(16, 45 * scale));
  });
  ctx.restore();
}
function nearestEconomyRegion(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left, y = clientY - rect.top;
  let best = null, distance = 24 * 24;
  economicLights().forEach(light => {
    const p = worldToScreen(light);
    const d = (p.x-x)**2 + (p.y-y)**2;
    if (d < distance) {distance = d; best = light.anchor;}
  });
  return best;
}
function renderEconomicSvg(svg) {
  let group = svg.querySelector('[data-economic-layer]');
  if (!group) {
    group = svgElement('g', {'data-economic-layer': '', 'aria-label': 'Regional economic lights'});
    svg.appendChild(group);
  }
  group.replaceChildren();
  const defs = svgElement('defs');
  const colors = economyCombined.checked ? [{key: 'combined', color: '#67e8f9'}] : (economyPayload ? economyPayload.metrics : []);
  colors.forEach(metric => {
    const gradient = svgElement('radialGradient', {id: 'economic-glow-' + metric.key});
    gradient.appendChild(svgElement('stop', {offset: '0%', 'stop-color': metric.color, 'stop-opacity': 1}));
    gradient.appendChild(svgElement('stop', {offset: '100%', 'stop-color': metric.color, 'stop-opacity': 0}));
    defs.appendChild(gradient);
  });
  group.appendChild(defs);
  economicLights().forEach(light => {
    const title = economicTooltip(light.entry);
    const link = svgElement('a', {href: light.anchor.url, 'data-economic-region': light.entry.region_id,
                                 'data-economic-metric': light.item.key, 'data-economic-value': light.value,
                                 'data-economic-intensity': light.intensity, 'aria-label': title});
    link.appendChild(svgElement('title', {}, title));
    if (light.value !== null && light.value > 0) {
      link.appendChild(svgElement('circle', {cx: light._x, cy: light._y, r: light.radius * 2, fill: 'url(#economic-glow-' + light.item.key + ')', opacity: .15 + .65 * light.intensity}));
      link.appendChild(svgElement('circle', {cx: light._x, cy: light._y, r: light.radius / 2, fill: light.color, opacity: .1 + .65 * light.intensity}));
    }
    link.appendChild(svgElement('circle', {cx: light._x, cy: light._y, r: Math.max(8, light.radius / 2),
      fill: 'transparent', stroke: light.value === null ? '#94a3b8' : light.color,
      'stroke-dasharray': light.value === null ? '3 3' : null, 'pointer-events': 'all'}));
    group.appendChild(link);
  });
  economyRegionEntries.forEach(entry => {
    const anchor = economyAnchors.get(String(entry.region_id));
    if (!anchor) return;
    group.appendChild(svgElement('text', {x: anchor._x, y: anchor._y + 50, fill: '#cbd5e1',
      'text-anchor': 'middle', 'font-size': 24, 'font-family': 'system-ui, sans-serif',
      stroke: '#050a11', 'stroke-width': 3, 'paint-order': 'stroke', 'pointer-events': 'none'}, entry.region_name));
  });
}
function refreshEconomyDisplay() {
  if (!economyPayload || activeMode !== 'economy') return;
  // A single cap for the entire series keeps light intensities comparable.
  economyCap = 0;
  economyPayload.frames.forEach(frame => frame.regions.forEach(entry => economicMeasurements(entry).forEach(item => {
    const value = economicValue(item);
    if (value !== null && Number.isFinite(value)) economyCap = Math.max(economyCap, value);
  })));
  economyLegend.replaceChildren();
  const legends = economyCombined.checked ? [{label: 'Combined indicators', color: '#67e8f9'}] : economyPayload.metrics;
  legends.forEach(metric => {
    const line = document.createElement('div');
    line.style.color = metric.color; line.textContent = '● ' + metric.label; economyLegend.appendChild(line);
  });
  const scaleLabel = document.createElement('div');
  scaleLabel.textContent = 'Full brightness: ' + (economyBasis.value === 'relative' ? economicPercent(economyCap) + ' of production' : economicMoney(economyCap)) + ' · fixed across months';
  economyLegend.appendChild(scaleLabel);
  showEconomyFrame(economyFrameIndex);
}
function showEconomyFrame(index) {
  if (!economyPayload || activeMode !== 'economy') return;
  economyFrameIndex = Math.max(0, Math.min(economyPayload.frames.length - 1, index));
  economySlider.value = String(economyFrameIndex);
  economySlider.setAttribute('aria-valuetext', economicPeriod());
  const frame = economyPayload.frames[economyFrameIndex];
  economyRegionEntries = new Map(frame.regions.map(entry => [String(entry.region_id), entry]));
  let available = 0, unavailable = 0;
  frame.regions.forEach(entry => {
    if (!economyAnchors.has(String(entry.region_id))) return;
    if (economicMeasurements(entry).some(item => economicValue(item) !== null)) available++; else unavailable++;
  });
  const unplaced = economyPayload.unplaced_scopes || [];
  economyStatus.textContent = economicPeriod() + ' · ' + available + ' regions with data' +
    (unavailable ? ' · ' + unavailable + ' unavailable in this view' : '') +
    (unplaced.length ? ' · Without regional placement: ' + unplaced.join(', ') : '');
  if (!available) economyStatus.textContent += ' · No usable regional data for this selection';
  renderEconomicRanking();
  renderCurrentView();
}
async function economicJson(url, signal) {
  const response = await fetch(url, {headers: {'Accept': 'application/json'}, cache: 'no-store', signal});
  if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('Economic data unavailable. Reload the page or check that economic data has been imported.');
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Economic data loading failed');
  return body;
}
async function loadEconomy() {
  stopEconomicAnimation();
  const token = ++economyRequestToken;
  if (economyAborter) economyAborter.abort();
  economyAborter = new AbortController();
  const signal = economyAborter.signal;
  economyPayload = null; economyRegionEntries.clear(); economyLegend.replaceChildren();
  economyPlay.disabled = true;
  economyStatus.textContent = 'Loading economic data…';
  rankingMessage('Economic ranking', 'Loading…');
  renderCurrentView();
  try {
    if (!economyOptions) {
      const options = await economicJson('/api/map/eve-2d/economy/options', signal);
      if (token !== economyRequestToken || activeMode !== 'economy') return;
      if (!options.months.length) throw new Error('No regional economic data imported yet. Run Import economic data in Admin MER.');
      economyOptions = options;
      const last = economyOptions.months.at(-1), first = economyOptions.months[0];
      [economyFrom, economyTo].forEach(input => {input.min = first; input.max = last; if (!input.value) input.value = last;});
      economyMetricBox.replaceChildren();
      economyOptions.metrics.forEach((metric, i) => {
        const label = document.createElement('label'); label.className = 'eve2d-check-label';
        const check = document.createElement('input'); check.type = 'checkbox'; check.value = metric.key;
        check.checked = metric.key === 'mining_isk' || (i === 0 && !economyOptions.metrics.some(m => m.key === 'mining_isk'));
        const text = document.createElement('span'); text.style.color = metric.color; text.textContent = metric.label;
        label.append(check, text); economyMetricBox.appendChild(label);
      });
    }
    const metrics = Array.from(economyMetricBox.querySelectorAll('input:checked')).map(input => input.value);
    if (!metrics.length) throw new Error('Select at least one economic indicator.');
    const params = new URLSearchParams({from: economyFrom.value, to: economyTo.value, evolution: String(economyMode.value === 'animated')});
    metrics.forEach(metric => params.append('metric', metric));
    const data = await economicJson('/api/map/eve-2d/economy?' + params, signal);
    if (token !== economyRequestToken || activeMode !== 'economy') return;
    economyPayload = data; economyFrameIndex = 0;
    economySlider.max = String(data.frames.length - 1);
    economyPlay.disabled = data.frames.length < 2;
    economyTimeline.hidden = economyMode.value !== 'animated';
    refreshEconomyDisplay();
  } catch (error) {
    if (token !== economyRequestToken || error.name === 'AbortError') return;
    economyStatus.textContent = error.message || String(error);
    rankingMessage('Economic ranking', 'Ranking unavailable — see economic status');
    renderCurrentView();
  }
}
document.getElementById('eve2dEconomyApply').addEventListener('click', loadEconomy);
[economyFrom, economyTo, economyMetricBox].forEach(element => element.addEventListener('change', loadEconomy));
[economyCombined, economyBasis].forEach(element => element.addEventListener('change', () => {stopEconomicAnimation(); refreshEconomyDisplay();}));
economyMode.addEventListener('change', () => {
  economyTimeline.hidden = economyMode.value !== 'animated';
  if (economyMode.value === 'animated' && economyFrom.value === economyTo.value && economyOptions) {
    const index = economyOptions.months.indexOf(economyTo.value);
    economyFrom.value = economyOptions.months[Math.max(0, index - 11)];
  }
  loadEconomy();
});
economySlider.addEventListener('input', () => {stopEconomicAnimation(); showEconomyFrame(Number(economySlider.value));});
economyFps.addEventListener('change', stopEconomicAnimation);
economyPlay.addEventListener('click', () => {
  if (economyTimer !== null) {stopEconomicAnimation(); return;}
  if (!economyPayload || economyPayload.frames.length < 2) return;
  if (economyFrameIndex === economyPayload.frames.length - 1) showEconomyFrame(0);
  economyPlay.textContent = 'Pause';
  economyTimer = setInterval(() => {
    if (activeMode !== 'economy' || economyFrameIndex >= economyPayload.frames.length - 1) {stopEconomicAnimation(); return;}
    showEconomyFrame(economyFrameIndex + 1);
    if (economyFrameIndex >= economyPayload.frames.length - 1) stopEconomicAnimation();
  }, 1000 / Math.max(1, Math.min(8, Number(economyFps.value) || 2)));
});
