(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 2 });
  const whole = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 0 });
  let stocks = [];
  let currentChips = null;
  let chipsRange = 20;
  let currentFinancial = null;
  let financialRange = 8;
  let currentPeer = null;
  let peerSnapshot = null;

  const missing = (value) => value === null || value === undefined || value === '';
  const signed = (value, digits = 0) => missing(value) ? '資料不足' : `${value > 0 ? '+' : ''}${Number(value).toLocaleString('zh-TW', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
  const statusTone = (status) => status === 'pass' ? 'positive' : status === 'warning' ? 'warning' : status;
  function tone(node, value) {
    node.classList.remove('tone-positive', 'tone-negative', 'tone-neutral');
    node.classList.add(value > 0 ? 'tone-positive' : value < 0 ? 'tone-negative' : 'tone-neutral');
  }
  async function json(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error(response.status === 404 ? '尚未建立快取資料' : '讀取失敗');
    try { return await response.json(); } catch { throw new Error('資料格式錯誤'); }
  }
  function results(items) {
    const box = $('searchResults');
    box.innerHTML = items.slice(0, 20).map((stock) => `<button class="search-result" type="button" data-symbol="${stock.symbol}" role="option"><span><strong>${stock.symbol}</strong> ${stock.name}</span><small>${stock.market === 'TWSE' ? '上市' : '上櫃'}｜${stock.industry || '產業資料不足'}${stock.cached ? '' : '｜尚未快取'}</small></button>`).join('');
    box.hidden = !items.length;
    box.querySelectorAll('button').forEach((button) => { button.onclick = () => select(button.dataset.symbol); });
  }
  function search() {
    const query = $('stockSearch').value.trim().toLowerCase();
    results(query ? stocks.filter((stock) => stock.symbol.toLowerCase().includes(query) || stock.name.toLowerCase().includes(query)) : stocks.filter((stock) => stock.cached));
  }
  function metricCard(label, item) {
    const value = missing(item?.value) ? '資料不足' : item.unit === 'thousand_TWD' ? `${fmt.format(item.value / 1000)} 百萬元` : `${fmt.format(item.value)}${item.unit === '%' || item.unit === 'percent' ? '%' : item.unit === 'TWD' ? ' 元' : ''}`;
    return `<article class="widget metric"><span>${label}</span><strong>${value}</strong><small>${item?.data_date || '日期不足'}${item?.note ? `｜${item.note}` : ''}</small></article>`;
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
    const width = 640, height = 220, pad = 22;
    const all = series.flatMap((entry) => entry.values).filter(Number.isFinite);
    if (all.length < 2) return '<p>趨勢資料不足</p>';
    let min = Math.min(...all), max = Math.max(...all);
    if (min === max) { min -= 1; max += 1; }
    const points = (values) => values.map((value, index) => `${pad + index * (width - pad * 2) / Math.max(1, values.length - 1)},${height - pad - (value - min) * (height - pad * 2) / (max - min)}`).join(' ');
    const zero = min <= 0 && max >= 0 ? height - pad - (0 - min) * (height - pad * 2) / (max - min) : null;
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${labels[0]} 至 ${labels.at(-1)}趨勢圖" preserveAspectRatio="none">${zero === null ? '' : `<line class="chart-zero" x1="${pad}" x2="${width - pad}" y1="${zero}" y2="${zero}"/>`}${series.map((entry) => `<polyline class="${entry.className}" points="${points(entry.values)}"/>`).join('')}</svg>`;
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
    renderHistorical(data.historical_valuation);
    $('reportPeriod').textContent = fundamentals.report_period ? `${fundamentals.report_period}｜${fundamentals.report_date}` : '不適用／資料不足';
    const labels = { eps: 'EPS', roe: 'ROE', revenue: '月營收', revenue_yoy: '營收 YoY', revenue_mom: '營收 MoM', gross_margin: '毛利率', operating_margin: '營業利益率', net_margin: '稅後淨利率', book_value_per_share: '每股淨值', debt_ratio: '負債比', current_ratio: '流動比率' };
    $('fundamentalsGrid').innerHTML = profile.instrument_type === 'company' ? Object.entries(labels).map(([key, label]) => metricCard(label, fundamentals[key])).join('') : '<p class="health-notice">ETF／非一般公司不套用公司財報指標。</p>';
    renderFinancialTrend();
    renderHealth(data.health_v2);
    renderIndustry(data.peer_analysis);
    $('chipsDate').textContent = chips.trade_date || '日期不足';
    $('foreignFlow').textContent = sumText(analysis.foreign_sum);
    $('foreignStreak').textContent = streak(analysis.foreign_streak);
    $('trustFlow').textContent = sumText(analysis.investment_trust_sum);
    $('trustStreak').textContent = streak(analysis.investment_trust_streak);
    $('totalFlow').textContent = sumText(analysis.institutional_sum);
    $('marginChanges').textContent = `5日 ${signed(analysis.margin_change?.['5d'])}｜20日 ${signed(analysis.margin_change?.['20d'])}`;
    const last = chips.history.at(-1);
    $('stockMargin').textContent = last && !missing(last.margin_balance) ? whole.format(last.margin_balance) : '資料不足';
    $('stockShort').textContent = last && !missing(last.short_balance) ? whole.format(last.short_balance) : '資料不足';
    currentChips = chips;
    renderChipsTrend();
    $('pageState').hidden = true;
    $('stockContent').hidden = false;
  }
  async function select(symbol) {
    $('searchResults').hidden = true;
    $('stockSearch').value = symbol;
    $('pageState').hidden = false;
    $('pageState').textContent = `正在載入 ${symbol}…`;
    $('stockContent').hidden = true;
    try {
      const payload = await json(`./data/stocks/${encodeURIComponent(symbol)}.json`);
      currentFinancial = null;
      if (payload.data?.profile?.instrument_type === 'company') {
        try { currentFinancial = (await json(`./data/stocks/financials/${encodeURIComponent(symbol)}.json`)).data?.quarters || null; } catch { currentFinancial = null; }
        if (payload.data.peer_analysis?.snapshot) {
          try {
            peerSnapshot ||= await json(payload.data.peer_analysis.snapshot);
            payload.data.peer_analysis = buildPeerFromSnapshot(symbol);
          } catch { payload.data.peer_analysis = { applicable: false, reason: '同業共用資料讀取失敗' }; }
        }
      }
      render(payload);
      history.replaceState(null, '', `?symbol=${encodeURIComponent(symbol)}`);
    } catch (error) {
      $('pageState').textContent = error.message === '尚未建立快取資料' ? `${symbol} 資料建置中，目前尚無個股快取；其他股票與搜尋功能仍可使用。` : `${symbol}：${error.message}`;
    }
  }
  async function init() {
    try {
      const index = await json('./data/stocks/index.json');
      stocks = Array.isArray(index.stocks) ? index.stocks : [];
      $('pageState').textContent = `已載入 ${whole.format(stocks.length)} 檔證券清單`;
      await select(new URLSearchParams(location.search).get('symbol') || '2330');
    } catch (error) { $('pageState').textContent = `初始化失敗：${error.message}`; }
  }
  $('stockSearch').addEventListener('input', search);
  $('stockSearch').addEventListener('keydown', (event) => { if (event.key === 'Enter') select($('stockSearch').value.trim()); });
  $('searchButton').onclick = () => select($('stockSearch').value.trim());
  document.querySelectorAll('[data-chips-range]').forEach((button) => { button.addEventListener('click', () => { chipsRange = Number(button.dataset.chipsRange); document.querySelectorAll('[data-chips-range]').forEach((item) => item.classList.toggle('is-active', item === button)); renderChipsTrend(); }); });
  document.querySelectorAll('[data-financial-range]').forEach((button) => { button.addEventListener('click', () => { financialRange = Number(button.dataset.financialRange); document.querySelectorAll('[data-financial-range]').forEach((item) => item.classList.toggle('is-active', item === button)); renderFinancialTrend(); }); });
  $('peerRankingMetric').addEventListener('change', renderPeerRanking);
  document.addEventListener('click', (event) => { if (!event.target.closest('.search-section')) $('searchResults').hidden = true; });
  init();
})();
