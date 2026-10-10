const rankingTitle = document.getElementById('eve2dRankingTitle');
const rankingMetric = document.getElementById('eve2dRankingMetric');
const MAP_TOP_LIMIT = 20;

function rankingMessage(title, message) {
  if (!rankingPanel || activeMode === 'systems') return;
  rankingTitle.textContent = title;
  rankingMetric.hidden = true;
  rankingMeta.textContent = message;
  rankingList.replaceChildren();
}
function compactRankingValue(value) {
  return Number(value).toLocaleString(undefined, {notation: 'compact', maximumFractionDigits: 2});
}
function renderValueRanking(title, meta, entries, emptyMessage) {
  rankingTitle.textContent = title;
  const ordered = entries.filter(entry => entry.value !== null && Number.isFinite(Number(entry.value)))
    .sort((a, b) => Number(b.value) - Number(a.value) || a.name.localeCompare(b.name));
  const shown = ordered.slice(0, MAP_TOP_LIMIT);
  rankingMeta.textContent = meta + ' · Top ' + shown.length + ' / ' + ordered.length;
  rankingList.replaceChildren();
  if (!shown.length) {
    const empty = document.createElement('div');
    empty.className = 'eve2d-ranking-empty'; empty.textContent = emptyMessage;
    rankingList.appendChild(empty); return;
  }
  shown.forEach((entry, index) => {
    const row = document.createElement('div'); row.className = 'eve2d-ranking-row';
    row.dataset.rankingValue = String(entry.value);
    row.title = entry.tooltip;
    const rank = document.createElement('div'); rank.className = 'eve2d-ranking-rank'; rank.textContent = '#' + (index + 1);
    const color = document.createElement('div'); color.className = 'eve2d-ranking-color'; color.style.background = entry.color;
    const name = document.createElement('a'); name.className = 'eve2d-ranking-name';
    name.textContent = entry.name; name.href = entry.url; name.title = entry.tooltip;
    const stats = document.createElement('div'); stats.className = 'eve2d-ranking-stats';
    stats.textContent = entry.display; stats.title = entry.tooltip;
    row.append(rank, color, name, stats); rankingList.appendChild(row);
  });
}
function renderHeatRanking() {
  if (activeMode !== 'heat' || !currentHeatPayload) return;
  rankingMetric.hidden = true;
  const entries = [];
  const query = heatKillboardQuery();
  heatBySystem.forEach((entry, systemId) => {
    const node = nodeById.get(String(systemId));
    if (!node) return;
    entries.push({name: node.name, value: entry.value, color: '#ff4c28', url: heatKillboardUrl(node, query),
      display: compactRankingValue(entry.value) + (heatMetric === 'isk' ? ' ISK' : ' kills'),
      tooltip: node.name + ' · ' + (node.region_name || 'Anoikis') + ' · ' + formatHeatValue(entry.value)});
  });
  const from = heatInputValue(currentHeatPayload.from).replace('T', ' ');
  const to = heatInputValue(currentHeatPayload.to).replace('T', ' ');
  const source = {api: 'Killboard', total: 'Total', hidden: 'Hidden'}[currentHeatPayload.source || heatSource];
  renderValueRanking('Fight heat ranking', source + ' · ' + (heatMetric === 'isk' ? 'ISK' : 'Kills') + ' · ' + from + ' through ' + to + ' UTC',
    entries, 'No mapped systems with activity for this selection');
}
function renderEconomicRanking() {
  if (activeMode !== 'economy' || !economyPayload) return;
  const options = economyCombined.checked ? [{key: 'combined', label: 'Combined indicators'}] : economyPayload.metrics;
  const selected = options.some(option => option.key === rankingMetric.value) ? rankingMetric.value : options[0].key;
  // Preserve the selected ranking indicator while the animation changes frame.
  if (rankingMetric.dataset.options !== options.map(option => option.key).join(',')) {
    rankingMetric.replaceChildren();
    options.forEach(option => {
      const element = document.createElement('option'); element.value = option.key; element.textContent = 'Rank by ' + option.label;
      rankingMetric.appendChild(element);
    });
    rankingMetric.dataset.options = options.map(option => option.key).join(',');
  }
  rankingMetric.value = selected; rankingMetric.hidden = options.length < 2;
  const entries = [];
  economyRegionEntries.forEach(entry => {
    const anchor = economyAnchors.get(String(entry.region_id));
    if (!anchor) return;
    const item = economyCombined.checked ? entry.combined : entry.metrics.find(metric => metric.key === selected);
    const value = economicValue(item);
    entries.push({name: entry.region_name, value, color: economicColor(item), url: anchor.url,
      display: economyBasis.value === 'relative' ? economicPercent(value) : compactRankingValue(value) + ' ISK',
      tooltip: economicTooltip(entry)});
  });
  const label = options.find(option => option.key === selected).label;
  renderValueRanking('Economic ranking', label + ' · ' + (economyBasis.value === 'relative' ? '% of regional production' : 'ISK') + ' · ' + economicPeriod(),
    entries, 'No regional values for this indicator and period');
}
rankingMetric.addEventListener('change', renderEconomicRanking);
