(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 2 });
  const whole = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 0 });
  const dataService = new StockDataService();
  const preferences = new StockPreferences();
  const sourceState = new Map();
  let searchSequence = 0;
  let searchTimer = null;
  let currentPeer = null;
  let peerSnapshot = null;
  let currentStock = null;
  let currentHistoryTab = 'revenue';
  const historyCache = new Map();

  const missing = (value) => value === null || value === undefined || value === '';
  const signed = (value, digits = 0) => missing(value) ? '資料不足' : `${value > 0 ? '+' : ''}${Number(value).toLocaleString('zh-TW', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
  const statusTone = (status) => status === 'pass' ? 'positive' : status === 'warning' ? 'warning' : status;
  function tone(node, value) {
    node.classList.remove('tone-positive', 'tone-negative', 'tone-neutral');
    node.classList.add(value > 0 ? 'tone-positive' : value < 0 ? 'tone-negative' : 'tone-neutral');
  }
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
  function renderWatchlist(items = preferences.getWatchlist()) {
    $('preferenceStorageStatus').textContent = preferences.statusMessage();
    $('watchlistItems').innerHTML = items.map((item) => `<span class="watchlist-item"><a href="./stock-analysis.html?symbol=${encodeURIComponent(item.symbol)}"><strong>${escapeHtml(item.symbol)}</strong> ${escapeHtml(item.name)}</a><button type="button" data-remove-watchlist="${escapeHtml(item.symbol)}" aria-label="從自選股移除 ${escapeHtml(item.symbol)} ${escapeHtml(item.name)}">移除</button></span>`).join('');
    $('watchlistEmpty').hidden = items.length > 0;
    const watched = currentStock && items.some((item) => item.symbol === currentStock.symbol);
    $('stockWatchlistToggle').classList.toggle('is-watched', Boolean(watched));
    $('stockWatchlistToggle').setAttribute('aria-pressed', String(Boolean(watched)));
    $('stockWatchlistToggle').textContent = watched ? '移除自選股' : '加入自選股';
  }
  function source(section, result) { sourceState.set(section, result.source); if (result.updatedAt) sourceState.set('updatedAt', result.updatedAt); }
  function renderSourceStatus() {
    $('dataSourceStatus').classList.remove('is-fallback');
    $('dataSourceStatus').textContent = `線上 API 資料${sourceState.get('updatedAt') ? `｜更新：${sourceState.get('updatedAt')}` : ''}`;
  }
  function results(items) {
    const box = $('searchResults');
    box.innerHTML = items.slice(0, 20).map((stock) => `<button class="search-result" type="button" data-symbol="${escapeHtml(stock.symbol)}" role="option"><span><strong>${escapeHtml(stock.symbol)}</strong> ${escapeHtml(stock.name)}</span><small>${stock.market === 'TWSE' ? '上市' : '上櫃'}｜${escapeHtml(stock.industry || '產業資料不足')}${stock.cached ? '' : '｜資料建置中'}</small></button>`).join('');
    box.hidden = !items.length;
    box.querySelectorAll('button').forEach((button) => { button.onclick = () => select(button.dataset.symbol); });
  }
  async function search() {
    const query = $('stockSearch').value.trim(), sequence = ++searchSequence;
    if (!query) { $('searchResults').hidden = true; return []; }
    try {
      const response = await dataService.searchStocks(query, { limit: 20 });
      if (sequence !== searchSequence) return [];
      results(response.data); return response.data;
    } catch { if (sequence === searchSequence) results([]); return []; }
  }
  function metricCard(label, item) {
    const value = missing(item?.value) ? '資料不足' : item.unit === 'thousand_TWD' ? `${fmt.format(item.value / 1000)} 百萬元` : `${fmt.format(item.value)}${item.unit === '%' || item.unit === 'percent' ? '%' : item.unit === 'TWD' ? ' 元' : ''}`;
    return `<article class="widget metric"><span>${label}</span><strong>${value}</strong><small>${item?.data_date || '日期不足'}${item?.note ? `｜${item.note}` : ''}</small></article>`;
  }
  function renderAnalysisSummary(summary) {
    const notice = $('analysisSummaryNotice'), grid = $('summarySectionGrid');
    const sectionLabels = { fundamentals: '基本面', valuation: '估值位置', growth: '成長性', financial_safety: '財務安全', chips: '籌碼', peer_position: '同業位置' };
    const statusLabels = { positive: '正向', neutral: '中性', warning: '警示', unavailable: '資料不足' };
    const evidenceValue = (item) => missing(item?.value) ? '資料不足' : `${Number(item.value).toLocaleString('zh-TW', { maximumFractionDigits: 2 })}${item.unit ? ` ${item.unit}` : ''}`;
    const evidenceMeta = (item) => `${evidenceValue(item)}｜門檻：${item.threshold || '不適用'}｜${item.date || '日期不足'}`;
    const list = (id, items, emptyText) => {
      $(id).innerHTML = items?.length ? items.map((item) => `<li><strong>${escapeHtml(item.label || item.summary)}</strong>${item.value !== undefined ? `<small>${escapeHtml(evidenceMeta(item))}</small>` : item.date ? `<small>${escapeHtml(item.date)}</small>` : ''}</li>`).join('') : `<li class="summary-empty">${emptyText}</li>`;
    };
    if (!summary?.overall_sections) {
      notice.textContent = '綜合摘要資料尚未建立，其他個股資料仍可正常使用。';
      notice.hidden = false; grid.innerHTML = '';
      list('summaryStrengths', [], '尚無可列示優勢'); list('summaryRisks', [], '尚無可列示風險'); list('summaryWatchItems', [], '等待摘要資料更新');
      $('analysisSummaryDate').textContent = '--'; return;
    }
    notice.hidden = true;
    $('analysisSummaryDate').textContent = summary.data_date || summary.generated_at || '日期不足';
    grid.innerHTML = Object.entries(summary.overall_sections).map(([key, section]) => {
      const evidence = (section.evidence || []).filter((item) => item.status !== 'unavailable').slice(0, 2);
      return `<article class="widget summary-section-card"><header><h3>${sectionLabels[key] || key}</h3><span class="summary-status status-${section.status}">${statusLabels[section.status] || section.status}</span></header><p>${escapeHtml(section.summary)}</p><small>${evidence.length ? evidence.map((item) => `${escapeHtml(item.label)}：${escapeHtml(evidenceValue(item))}（${escapeHtml(item.date || '日期不足')}）`).join('<br>') : '無可用證據'}</small></article>`;
    }).join('');
    list('summaryStrengths', summary.strengths, '目前沒有符合正向門檻的主要項目');
    list('summaryRisks', summary.risks, '目前沒有符合警示門檻的主要項目');
    list('summaryWatchItems', summary.watch_items, '目前沒有資料不足項目');
  }
  function renderHistorical(history) {
    const notice = $('valuationHistoryNotice');
    const grid = $('valuationHistoryGrid');
    if (!history || history.applicable === false) {
      notice.textContent = history?.reason || '歷史估值資料不足';
      notice.hidden = false;
      grid.innerHTML = '';
      return;
    }
    notice.hidden = true;
    const labels = { pe: '本益比 PE', pb: '股價淨值比 PB', dividend_yield: '殖利率' };
    grid.innerHTML = Object.entries(labels).map(([key, label]) => {
      const item = history[key];
      if (!item?.['3y']) return `<article class="widget valuation-history-card"><h3>${label}</h3><p>資料不足</p></article>`;
      const current = missing(item.current) ? '資料不足' : `${fmt.format(item.current)}${key === 'dividend_yield' ? '%' : ''}`;
      const periods = ['3y', '5y'].filter((period) => item[period]);
      return `<article class="widget valuation-history-card"><h3>${label}</h3><div class="valuation-current"><strong>${current}</strong><span>目前值</span></div>${periods.map((period) => { const stats = item[period]; return `<div class="valuation-period"><b>${period === '3y' ? '3 年' : '5 年'}｜${stats.sample_count} 筆（月頻）</b><div class="percentile-track"><i style="width:${Math.max(0, Math.min(100, stats.current_percentile))}%"></i></div><span>目前百分位 ${fmt.format(stats.current_percentile)}%</span><small>P25 ${fmt.format(stats.p25)}｜中位數 ${fmt.format(stats.median)}｜P75 ${fmt.format(stats.p75)}<br>低 ${fmt.format(stats.low)}｜高 ${fmt.format(stats.high)}</small></div>`; }).join('')}</article>`;
    }).join('');
  }
  function renderHealth(health) {
    const notice = $('healthNotice');
    const grid = $('healthGrid');
    if (!health?.applicable) {
      notice.textContent = health?.reason || '資料不足';
      notice.hidden = false;
      grid.innerHTML = '';
      return;
    }
    notice.hidden = true;
    const names = { growth: '成長股健檢', risk: '地雷股健檢', value: '便宜股健檢', quality: '績優股健檢' };
    const statuses = { pass: '通過', neutral: '中性', warning: '注意', unavailable: '資料不足' };
    const healthValue = (item) => missing(item.value) ? '資料不足' : item.unit === 'thousand_TWD' ? `${fmt.format(item.value / 1000)} 百萬元` : `${fmt.format(item.value)}${item.unit === 'percent' ? '%' : item.unit === 'TWD' ? ' 元' : item.unit === 'quarters' ? ' 季' : ''}`;
    grid.innerHTML = Object.entries(health.categories).map(([key, items]) => `<article class="widget health-card"><h3>${names[key] || key}</h3>${items.map((item) => `<div class="health-item"><span>${item.label}</span><strong class="status-${statusTone(item.status)}">${healthValue(item)}｜${statuses[item.status]}</strong><small>門檻：${item.threshold || '未設定'}<br>${item.data_date || '日期不足'}${item.note ? `｜${item.note}` : ''}</small></div>`).join('')}</article>`).join('');
  }
  function peerValue(key, value) {
    if (missing(value)) return '資料不足';
    if (key === 'ttm_operating_cash_flow' || key === 'ttm_free_cash_flow') return `${fmt.format(value / 1000)} 百萬元`;
    return `${fmt.format(value)}${['dividend_yield', 'revenue_yoy', 'eps_yoy', 'roe', 'gross_margin', 'operating_margin', 'net_margin', 'debt_ratio', 'current_ratio'].includes(key) ? '%' : ''}`;
  }
  function buildPeerFromSnapshot(symbol) {
    const rows = Array.isArray(peerSnapshot?.stocks) ? peerSnapshot.stocks : [];
    const current = rows.find((row) => row.symbol === symbol);
    if (!current) return { applicable: false, reason: '同業 Snapshot 尚未收錄' };
    const specs = {
      pe: ['valuation', 'lower', null], pb: ['valuation', 'lower', null], dividend_yield: ['valuation', 'higher', null],
      revenue_yoy: ['growth', 'higher', 'revenue_period'], eps_yoy: ['growth', 'higher', 'multi_period'],
      eps: ['profitability', 'higher', 'financial_period'], roe: ['profitability', 'higher', 'financial_period'],
      gross_margin: ['profitability', 'higher', 'financial_period'], operating_margin: ['profitability', 'higher', 'financial_period'],
      net_margin: ['profitability', 'higher', 'financial_period'], ttm_eps: ['profitability', 'higher', 'multi_period'],
      debt_ratio: ['safety', 'lower', 'financial_period'], current_ratio: ['safety', 'context', 'financial_period'],
      ttm_operating_cash_flow: ['safety', 'higher', 'multi_period'], ttm_free_cash_flow: ['safety', 'higher', 'multi_period']
    };
    const peers = rows.filter((row) => row.industry === current.industry);
    const categories = { valuation: {}, growth: {}, profitability: {}, safety: {} };
    const compare = (key) => {
      const [category, direction, periodKey] = specs[key], period = periodKey ? current[periodKey] : null;
      const valid = (row) => Number.isFinite(row[key]) && !(key === 'pe' && row[key] <= 0) && (!periodKey || row[periodKey] === period);
      const eligible = peers.filter(valid), values = eligible.map((row) => row[key]).sort((a, b) => a - b), value = current[key];
      const mismatch = periodKey ? peers.filter((row) => row[periodKey] !== period).length : 0;
      const item = { company_value: value, industry_sample_size: peers.length, industry_median: null, percentile: null, rank: null, total_ranked: eligible.length, relative_status: 'unavailable', comparison_direction: direction, comparison_period: period, data_date: category === 'valuation' ? current.valuation_date : key === 'revenue_yoy' ? current.revenue_period : current.financial_date, period_mismatch_excluded: mismatch };
      if (eligible.length < 5 || !valid(current)) return item;
      const middle = Math.floor(values.length / 2); item.industry_median = values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
      if (direction === 'higher') { item.rank = 1 + values.filter((number) => number > value).length; item.percentile = values.filter((number) => number <= value).length / values.length * 100; }
      else if (direction === 'lower') { item.rank = 1 + values.filter((number) => number < value).length; item.percentile = values.filter((number) => number >= value).length / values.length * 100; }
      else item.percentile = values.filter((number) => number <= value).length / values.length * 100;
      item.industry_median = Number(item.industry_median.toFixed(6)); item.percentile = Number(item.percentile.toFixed(4));
      if (direction !== 'context') item.relative_status = item.percentile >= 90 ? 'leading' : item.percentile >= 60 ? 'above_average' : item.percentile >= 40 ? 'average' : item.percentile >= 10 ? 'below_average' : 'lagging';
      return item;
    };
    Object.keys(specs).forEach((key) => { categories[specs[key][0]][key] = compare(key); });
    const rankings = {};
    for (const key of ['roe', 'eps', 'revenue_yoy', 'pe', 'pb']) {
      const metric = categories[specs[key][0]][key], periodKey = specs[key][2], period = periodKey ? current[periodKey] : null;
      const eligible = peers.filter((row) => Number.isFinite(row[key]) && !(key === 'pe' && row[key] <= 0) && (!periodKey || row[periodKey] === period));
      eligible.sort((a, b) => specs[key][1] === 'higher' ? b[key] - a[key] : a[key] - b[key]);
      const entry = (row) => ({ symbol: row.symbol, name: row.name, value: row[key], rank: specs[key][1] === 'higher' ? 1 + eligible.filter((peer) => peer[key] > row[key]).length : 1 + eligible.filter((peer) => peer[key] < row[key]).length, percentile: specs[key][1] === 'higher' ? eligible.filter((peer) => peer[key] <= row[key]).length / eligible.length * 100 : eligible.filter((peer) => peer[key] >= row[key]).length / eligible.length * 100 });
      rankings[key] = { metric: key, top10: eligible.slice(0, 10).map(entry), current_company: eligible.some((row) => row.symbol === symbol) ? entry(current) : null, sample_size: metric.total_ranked, comparison_period: period };
    }
    return { applicable: true, industry: current.industry, industry_company_count: peers.length, data_date: current.financial_date || current.valuation_date, minimum_sample_size: 5, categories, rankings };
  }
  function renderPeerRanking() {
    const key = $('peerRankingMetric').value, ranking = currentPeer?.rankings?.[key], notice = $('peerRankingNotice'), body = $('peerRankingRows');
    if (!ranking || ranking.sample_size < 5) { notice.textContent = '同期間有效樣本不足 5 家'; notice.hidden = false; body.innerHTML = ''; return; }
    notice.hidden = true; const entries = [...ranking.top10];
    if (ranking.current_company && !entries.some((entry) => entry.symbol === ranking.current_company.symbol)) entries.push({ ...ranking.current_company, separated: true });
    body.innerHTML = entries.map((entry) => `<tr class="${entry.symbol === $('stockSymbol').textContent ? 'is-current' : ''}${entry.separated ? ' is-separated' : ''}"><td>${entry.rank ?? '--'}</td><td><strong>${entry.symbol}</strong> ${entry.name}${entry.symbol === $('stockSymbol').textContent ? '｜目前公司' : ''}</td><td>${peerValue(key, entry.value)}</td><td>${missing(entry.percentile) ? '--' : `${fmt.format(entry.percentile)}%`}</td></tr>`).join('');
  }
  function renderIndustry(comparison) {
    const notice = $('industryNotice'), position = $('peerPosition'), groups = $('peerComparisonGroups'); currentPeer = comparison;
    $('industryName').textContent = comparison?.industry || '--';
    if (!comparison?.applicable) {
      notice.textContent = comparison?.reason || '同產業資料不足'; notice.hidden = false; position.innerHTML = ''; groups.innerHTML = ''; renderPeerRanking(); return;
    }
    notice.hidden = true;
    position.innerHTML = `<article class="widget"><span>產業名稱</span><strong>${comparison.industry}</strong></article><article class="widget"><span>同業公司數</span><strong>${whole.format(comparison.industry_company_count)} 家</strong></article><article class="widget"><span>資料日期</span><strong>${comparison.data_date || '日期不足'}</strong></article>`;
    const categoryNames = { valuation: '估值比較', growth: '成長比較', profitability: '獲利比較', safety: '財務安全' };
    const labels = { pe: 'PE', pb: 'PB', dividend_yield: '殖利率', revenue_yoy: '營收 YoY', eps_yoy: 'EPS YoY', eps: 'EPS', roe: 'ROE', gross_margin: '毛利率', operating_margin: '營業利益率', net_margin: '淨利率', debt_ratio: '負債比', current_ratio: '流動比率', ttm_operating_cash_flow: 'TTM OCF', ttm_free_cash_flow: 'TTM FCF', ttm_eps: 'TTM EPS' };
    const statuses = { leading: '領先', above_average: '高於平均', average: '平均', below_average: '低於平均', lagging: '落後', unavailable: '資料不足' };
    groups.innerHTML = Object.entries(comparison.categories).map(([category, metrics]) => `<section class="peer-category"><h3>${categoryNames[category]}</h3><div class="peer-metric-grid">${Object.entries(metrics).map(([key, metric]) => `<article class="widget peer-metric-card"><div><span>${labels[key] || key}</span><b class="peer-status status-${metric.relative_status}">${statuses[metric.relative_status]}</b></div><strong>${peerValue(key, metric.company_value)}</strong><small>同業中位數 ${peerValue(key, metric.industry_median)}<br>${metric.rank ? `排名 ${metric.rank} / ${metric.total_ranked}` : `有效樣本 ${metric.total_ranked}`}｜${metric.comparison_period || metric.data_date || '日期不足'}</small><div class="peer-percentile"><i style="width:${metric.percentile ?? 0}%"></i></div><em>${missing(metric.percentile) ? '百分位資料不足' : `百分位 ${fmt.format(metric.percentile)}%`}${metric.period_mismatch_excluded ? `｜排除不同期間 ${metric.period_mismatch_excluded} 家` : ''}</em></article>`).join('')}</div></section>`).join('');
    renderPeerRanking();
  }
  function streak(value) {
    if (!value?.direction) return '資料不足';
    return value.direction === 'buy' ? `連買 ${value.days} 日` : value.direction === 'sell' ? `連賣 ${value.days} 日` : `中性 ${value.days} 日`;
  }
  function sumText(values) { return `5日 ${signed(values?.['5d'])}｜20日 ${signed(values?.['20d'])}｜60日 ${signed(values?.['60d'])}`; }
  function svgChart(series, labels) {
    const all = series.flatMap((entry) => entry.values).filter(Number.isFinite);
    if (all.length < 2) return '<p>趨勢資料不足</p>';
    const chart = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    chart.setAttribute('role', 'img'); chart.setAttribute('aria-label', `${labels[0]} 至 ${labels.at(-1)}趨勢圖`); chart.setAttribute('preserveAspectRatio', 'none');
    PlatformXYChart.svg(chart, { width: 640, height: 220, compact: true, series, xValues: labels, xLabel: '期間', yLabel: '數值' });
    return chart.outerHTML;
  }
  const historyValue = (value, unit = '') => missing(value) ? '—' : `${Number(value).toLocaleString('zh-TW', { maximumFractionDigits: 2 })}${unit}`;
  const historyMoney = (value) => missing(value) ? '—' : `${Number(value / 1000).toLocaleString('zh-TW', { maximumFractionDigits: 1 })} 百萬元`;
  const historyMetric = (label, value, meta = '') => `<article class="widget metric"><span>${label}</span><strong>${value}</strong><small>${meta || '—'}</small></article>`;
  const historyChart = (title, rows, key, className, unit = '') => `<article class="widget chart-card"><h3>${title}</h3><div class="stock-chart">${svgChart([{ className, values: rows.map((row) => row[key]) }], rows.map((row) => row.revenue_month || row.valuation_date || row.trade_date || `${row.fiscal_year}Q${row.quarter}`))}</div><small class="history-chart-latest">最新：${historyValue(rows.at(-1)?.[key], unit)}</small></article>`;

  function renderRevenueHistory(rows) {
    if (!rows.length) return '<p class="health-notice">目前沒有月營收歷史資料。</p>';
    const latest = rows.at(-1);
    const millions = rows.map((row) => ({ ...row, revenue: missing(row.revenue) ? null : row.revenue / 1000 }));
    return `<div class="stock-grid stock-grid-3">${historyMetric('最新月營收', historyMoney(latest.revenue), latest.revenue_month)}${historyMetric('年增率 YoY', historyValue(latest.yoy, '%'), latest.revenue_month)}${historyMetric('月增率 MoM', historyValue(latest.mom, '%'), latest.revenue_month)}</div><div class="history-chart-grid">${historyChart('月營收趨勢（百萬元）', millions, 'revenue', 'line-total', ' 百萬元')}${`<article class="widget chart-card"><h3>YoY／MoM 趨勢</h3><div class="stock-chart">${svgChart([{ className: 'line-foreign', values: rows.map((row) => row.yoy) }, { className: 'line-trust', values: rows.map((row) => row.mom) }], rows.map((row) => row.revenue_month))}</div><div class="chart-legend"><span class="foreign">YoY</span><span class="trust">MoM</span></div></article>`}</div>`;
  }

  function renderFinancialHistory(rows) {
    if (!rows.length) return '<p class="health-notice">目前沒有財務歷史資料。</p>';
    const priority = [
      ['eps', 'EPS', ' 元'], ['revenue', '營業收入'], ['net_income', '稅後淨利'],
      ['gross_margin', '毛利率', '%'], ['operating_margin', '營業利益率', '%'], ['roe', 'ROE', '%']
    ];
    const more = [
      ['net_margin', '稅後淨利率', '%'], ['debt_ratio', '負債比', '%'], ['current_ratio', '流動比率', '%'],
      ['operating_cash_flow', '營業現金流'], ['free_cash_flow', '自由現金流'], ['total_assets', '總資產']
    ];
    const moneyKeys = new Set(['revenue', 'net_income', 'operating_cash_flow', 'free_cash_flow', 'total_assets']);
    const card = ([key, label, unit = '']) => moneyKeys.has(key)
      ? historyChart(`${label}（百萬元）`, rows.map((row) => ({ ...row, [key]: missing(row[key]) ? null : row[key] / 1000 })), key, 'line-total', ' 百萬元')
      : historyChart(label, rows, key, 'line-total', unit);
    return `<div class="financial-priority-grid">${priority.map(card).join('')}</div><details class="history-more"><summary>更多財務指標</summary><div class="financial-priority-grid">${more.map(card).join('')}</div></details>`;
  }

  function renderValuationHistory(rows) {
    if (!rows.length) return '<p class="health-notice">目前沒有估值歷史資料。</p>';
    const latest = rows.at(-1);
    return `<div class="stock-grid stock-grid-3">${historyMetric('本益比 PE', historyValue(latest.pe), latest.valuation_date)}${historyMetric('股價淨值比 PB', historyValue(latest.pb), latest.valuation_date)}${historyMetric('殖利率', historyValue(latest.dividend_yield, '%'), latest.valuation_date)}</div><div class="financial-priority-grid">${historyChart('PE 歷史趨勢', rows, 'pe', 'line-foreign')}${historyChart('PB 歷史趨勢', rows, 'pb', 'line-total')}${historyChart('殖利率歷史趨勢', rows, 'dividend_yield', 'line-trust', '%')}</div>`;
  }

  function renderChipsHistory(chips) {
    const rows = chips?.history || [];
    if (!rows.length) return '<p class="health-notice">目前沒有籌碼歷史資料。</p>';
    const latest = rows.at(-1);
    const tableRows = [...rows].reverse().slice(0, 60).map((row) => `<tr><td>${row.trade_date}</td>${['foreign_net', 'investment_trust_net', 'dealer_net'].map((key) => `<td class="${row[key] > 0 ? 'tone-positive' : row[key] < 0 ? 'tone-negative' : 'tone-neutral'}">${missing(row[key]) ? '—' : signed(row[key])}</td>`).join('')}<td>${historyValue(row.margin_balance)}</td><td>${historyValue(row.short_balance)}</td></tr>`).join('');
    return `<div class="stock-grid stock-grid-4">${historyMetric('外資買賣超', historyValue(latest.foreign_net), latest.trade_date)}${historyMetric('投信買賣超', historyValue(latest.investment_trust_net), latest.trade_date)}${historyMetric('自營商買賣超', historyValue(latest.dealer_net), latest.trade_date)}${historyMetric('融資／融券餘額', `${historyValue(latest.margin_balance)}／${historyValue(latest.short_balance)}`, latest.trade_date)}</div><div class="history-chart-grid"><article class="widget chart-card"><h3>法人買賣超</h3><div class="stock-chart">${svgChart([{ className: 'line-foreign', values: rows.map((row) => row.foreign_net) }, { className: 'line-trust', values: rows.map((row) => row.investment_trust_net) }, { className: 'line-total', values: rows.map((row) => row.dealer_net) }], rows.map((row) => row.trade_date))}</div><div class="chart-legend"><span class="foreign">外資</span><span class="trust">投信</span><span class="total">自營商</span></div></article><article class="widget chart-card"><h3>融資餘額</h3><div class="stock-chart">${svgChart([{ className: 'line-margin', values: rows.map((row) => row.margin_balance) }], rows.map((row) => row.trade_date))}</div></article><article class="widget chart-card"><h3>融券餘額</h3><div class="stock-chart">${svgChart([{ className: 'line-short', values: rows.map((row) => row.short_balance) }], rows.map((row) => row.trade_date))}</div></article></div><div class="table-wrap"><table><thead><tr><th>日期</th><th>外資</th><th>投信</th><th>自營商</th><th>融資</th><th>融券</th></tr></thead><tbody>${tableRows}</tbody></table></div>`;
  }

  function resetHistoryPanels() {
    document.querySelectorAll('[data-history-panel]').forEach((panel) => {
      panel.querySelector('.history-panel-state').textContent = `切換至${{ revenue: '月營收', financials: '財務', valuation: '估值', chips: '籌碼' }[panel.dataset.historyPanel]}後載入。`;
      panel.querySelector('.history-panel-state').hidden = false;
      panel.querySelector('.history-panel-content').innerHTML = '';
      delete panel.dataset.loadedSymbol;
    });
  }

  async function loadHistory(dataset) {
    if (!currentStock) return;
    const symbol = currentStock.symbol, panel = document.querySelector(`[data-history-panel="${dataset}"]`);
    if (!panel || panel.dataset.loadedSymbol === symbol) return;
    const state = panel.querySelector('.history-panel-state'), content = panel.querySelector('.history-panel-content');
    state.hidden = false; state.textContent = '載入中…'; content.innerHTML = '';
    const key = `${symbol}:${dataset}`;
    try {
      if (!historyCache.has(key)) {
        const request = dataset === 'revenue' ? dataService.getRevenueHistory(symbol, 60)
          : dataset === 'financials' ? dataService.getFinancials(symbol, 20)
            : dataset === 'valuation' ? dataService.getValuationHistory(symbol, 252)
              : dataService.getChips(symbol, 120);
        historyCache.set(key, request);
      }
      const result = await historyCache.get(key);
      if (currentStock?.symbol !== symbol) return;
      content.innerHTML = dataset === 'revenue' ? renderRevenueHistory(result.data)
        : dataset === 'financials' ? renderFinancialHistory(result.data)
          : dataset === 'valuation' ? renderValuationHistory(result.data)
            : renderChipsHistory(result.data);
      panel.dataset.loadedSymbol = symbol;
      state.hidden = true;
      source(`history:${dataset}`, result); renderSourceStatus();
    } catch (error) {
      historyCache.delete(key);
      state.textContent = '此歷史資料暫時無法取得，其他頁籤仍可使用。';
      content.innerHTML = '';
    }
  }

  function activateHistoryTab(dataset) {
    currentHistoryTab = dataset;
    document.querySelectorAll('[data-history-tab]').forEach((button) => {
      const active = button.dataset.historyTab === dataset;
      button.classList.toggle('is-active', active); button.setAttribute('aria-selected', String(active));
    });
    document.querySelectorAll('[data-history-panel]').forEach((panel) => { panel.hidden = panel.dataset.historyPanel !== dataset; });
    loadHistory(dataset);
  }
  function renderChipsTrend() {
    if (!currentChips?.history?.length) return;
    const rows = currentChips.history.slice(-chipsRange);
    $('chipsRows').innerHTML = [...rows].reverse().map((row) => `<tr><td>${row.trade_date}</td>${['foreign_net', 'investment_trust_net', 'dealer_net', 'institutional_total'].map((key) => `<td class="${row[key] > 0 ? 'tone-positive' : row[key] < 0 ? 'tone-negative' : 'tone-neutral'}">${signed(row[key])}</td>`).join('')}<td>${whole.format(row.margin_balance)}</td><td>${whole.format(row.short_balance)}</td></tr>`).join('');
    const labels = rows.map((row) => row.trade_date);
    $('institutionalTrendChart').innerHTML = svgChart([
      { className: 'line-foreign', values: rows.map((row) => row.foreign_net) },
      { className: 'line-trust', values: rows.map((row) => row.investment_trust_net) },
      { className: 'line-total', values: rows.map((row) => row.institutional_total) }
    ], labels);
    const indexed = (key) => { const base = rows.find((row) => Number.isFinite(row[key]) && row[key] !== 0)?.[key]; return base ? rows.map((row) => row[key] / base * 100) : []; };
    $('marginTrendChart').innerHTML = svgChart([
      { className: 'line-margin', values: indexed('margin_balance') },
      { className: 'line-short', values: indexed('short_balance') }
    ], labels);
  }
  function financialValue(value, unit) {
    if (missing(value)) return '資料不足';
    if (unit === 'thousand_TWD') return `${Number(value / 1000).toLocaleString('zh-TW', { maximumFractionDigits: 1 })} 百萬元`;
    return `${fmt.format(value)}${unit === 'percent' ? '%' : unit === 'TWD' ? ' 元' : ''}`;
  }
  function renderFinancialTrend() {
    const notice = $('financialNotice'), grid = $('financialTrendGrid');
    if (!currentFinancial?.length) {
      notice.textContent = $('stockMeta').textContent.includes('ETF') ? 'ETF 不適用公司財務分析' : '多期財報讀取失敗或資料不足';
      notice.hidden = false; grid.innerHTML = ''; $('financialDate').textContent = '--'; return;
    }
    notice.hidden = true;
    const rows = currentFinancial.slice(-financialRange);
    $('financialDate').textContent = `${rows[0].period_end}～${rows.at(-1).period_end}`;
    const definitions = [
      ['eps', 'EPS', 'TWD'], ['revenue', '營業收入', 'thousand_TWD'], ['gross_margin', '毛利率', 'percent'],
      ['operating_margin', '營業利益率', 'percent'], ['net_margin', '稅後淨利率', 'percent'], ['roe', 'ROE', 'percent'],
      ['debt_ratio', '負債比', 'percent'], ['operating_cash_flow', 'OCF', 'thousand_TWD'], ['free_cash_flow', 'FCF', 'thousand_TWD']
    ];
    grid.innerHTML = definitions.map(([key, label, unit]) => `<article class="widget financial-trend-card"><h3>${label}</h3><div class="financial-mini-chart">${svgChart([{ className: 'line-total', values: rows.map((row) => row[key]) }], rows.map((row) => `${row.fiscal_year}Q${row.quarter}`))}</div><div class="financial-quarter-list">${rows.map((row) => `<span><b>${row.fiscal_year}Q${row.quarter}</b>${financialValue(row[key], unit)}</span>`).join('')}</div></article>`).join('');
  }
  function render(payload) {
    const data = payload.data, profile = data.profile, quote = data.quote, valuation = data.valuation, fundamentals = data.fundamentals, chips = data.chips, analysis = chips.analysis || {};
    const buildState = data.build_status?.state;
    $('cacheStatus').hidden = buildState !== 'partial';
    $('cacheStatus').textContent = buildState === 'partial' ? '資料建置中／部分資料可用；尚未完成的區塊會個別顯示資料不足。' : '';
    $('stockSymbol').textContent = profile.symbol;
    $('stockName').textContent = profile.name;
    currentStock = { symbol: profile.symbol, name: profile.name || profile.symbol };
    renderWatchlist();
    $('stockMeta').textContent = `${profile.market === 'TWSE' ? '上市' : '上櫃'}｜${profile.industry}｜${profile.instrument_type === 'company' ? '一般公司' : 'ETF／其他證券'}`;
    $('quoteDate').textContent = quote.trade_date || '日期不足';
    $('closePrice').textContent = missing(quote.close) ? '資料不足' : `${fmt.format(quote.close)} 元`;
    $('priceChange').textContent = `${signed(quote.change, 2)}｜${signed(quote.change_percent, 2)}%`;
    tone($('priceChange'), quote.change || 0);
    $('volume').textContent = missing(quote.volume) ? '資料不足' : whole.format(quote.volume);
    for (const [id, key] of [['pe', 'pe'], ['pb', 'pb'], ['yield', 'dividend_yield']]) {
      const item = valuation[key];
      $(id).textContent = missing(item.value) ? '資料不足' : `${fmt.format(item.value)}${id === 'yield' ? '%' : ''}`;
      $(`${id}Date`).textContent = item.data_date || '日期不足';
    }
    renderAnalysisSummary(data.analysis_summary);
    $('reportPeriod').textContent = fundamentals.report_period ? `${fundamentals.report_period}｜${fundamentals.report_date}` : '不適用／資料不足';
    const labels = { eps: 'EPS', roe: 'ROE', revenue: '月營收', revenue_yoy: '營收 YoY', revenue_mom: '營收 MoM', gross_margin: '毛利率', operating_margin: '營業利益率', net_margin: '稅後淨利率', book_value_per_share: '每股淨值', debt_ratio: '負債比', current_ratio: '流動比率' };
    $('fundamentalsGrid').innerHTML = profile.instrument_type === 'company' ? Object.entries(labels).map(([key, label]) => metricCard(label, fundamentals[key])).join('') : '<p class="health-notice">ETF／非一般公司不套用公司財報指標。</p>';
    renderHealth(data.health_v2);
    renderIndustry(data.peer_analysis);
    $('pageState').hidden = true;
    $('stockContent').hidden = false;
  }
  async function select(symbol) {
    $('searchResults').hidden = true;
    $('stockSearch').value = symbol;
    $('pageState').hidden = false;
    $('pageState').textContent = `正在載入 ${symbol}…`;
    $('stockContent').hidden = true;
    sourceState.clear();
    try {
      const detail = await dataService.getStock(symbol), payload = detail.data;
      source('detail', detail);
      const profile = payload.data?.profile || {};
      const [peers] = await Promise.allSettled([dataService.getIndustryPeers(profile.industry, symbol)]);
      if (peers.status === 'fulfilled') { payload.data.peer_analysis = peers.value.data; source('peers', peers.value); }
      else payload.data.peer_analysis = { applicable: false, reason: '同業資料暫時無法使用' };
      resetHistoryPanels();
      render(payload);
      renderSourceStatus();
      activateHistoryTab(currentHistoryTab);
      history.replaceState(null, '', `?symbol=${encodeURIComponent(symbol)}`);
    } catch (error) {
      $('pageState').textContent = error.code === 'NOT_FOUND' ? `查無 ${symbol} 的個股資料` : '目前無法取得資料';
    }
  }
  async function init() {
    const symbol = new URLSearchParams(location.search).get('symbol')?.trim();
    if (symbol) {
      await select(symbol);
      return;
    }
    $('pageState').hidden = false;
    $('pageState').textContent = '請輸入股票代號或公司名稱開始查詢';
    $('stockContent').hidden = true;
  }
  const submitSearch = async () => { const query = $('stockSearch').value.trim(); if (/^[0-9A-Za-z]{2,10}$/.test(query)) select(query); else await search(); };
  $('stockSearch').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(search, 180); });
  $('stockSearch').addEventListener('keydown', (event) => { if (event.key === 'Enter') submitSearch(); });
  $('searchButton').onclick = submitSearch;
  document.querySelectorAll('[data-history-tab]').forEach((button) => { button.addEventListener('click', () => activateHistoryTab(button.dataset.historyTab)); });
  $('peerRankingMetric').addEventListener('change', renderPeerRanking);
  $('stockWatchlistToggle').addEventListener('click', () => { if (currentStock) preferences.toggle(currentStock); });
  $('watchlistItems').addEventListener('click', (event) => { const button = event.target.closest('[data-remove-watchlist]'); if (button) preferences.removeStock(button.dataset.removeWatchlist); });
  preferences.subscribe(renderWatchlist);
  document.addEventListener('click', (event) => { if (!event.target.closest('.search-section')) $('searchResults').hidden = true; });
  renderWatchlist();
  init();
})();
