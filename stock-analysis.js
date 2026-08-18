(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 2 });
  const whole = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 0 });
  let stocks = [];
  let currentChips = null;
  let chipsRange = 20;

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
    const value = missing(item?.value) ? '資料不足' : `${fmt.format(item.value)}${item.unit === '%' ? '%' : item.unit === 'TWD' ? ' 元' : ''}`;
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
    grid.innerHTML = Object.entries(health.categories).map(([key, items]) => `<article class="widget health-card"><h3>${names[key] || key}</h3>${items.map((item) => `<div class="health-item"><span>${item.label}</span><strong class="status-${statusTone(item.status)}">${missing(item.value) ? '資料不足' : fmt.format(item.value)}｜${statuses[item.status]}</strong><small>門檻：${item.threshold || '未設定'}<br>${item.data_date || '日期不足'}${item.note ? `｜${item.note}` : ''}</small></div>`).join('')}</article>`).join('');
  }
  function renderIndustry(comparison) {
    const notice = $('industryNotice');
    const grid = $('industryGrid');
    $('industryName').textContent = comparison?.industry || '--';
    if (!comparison?.applicable) {
      notice.textContent = comparison?.reason || '同產業資料不足';
      notice.hidden = false;
      grid.innerHTML = '';
      return;
    }
    notice.hidden = true;
    const labels = { pe: 'PE', pb: 'PB', dividend_yield: '殖利率', roe: 'ROE', eps: 'EPS', revenue_yoy: '營收 YoY' };
    grid.innerHTML = Object.entries(labels).map(([key, label]) => {
      const metric = comparison.metrics?.[key];
      if (!metric || metric.status !== 'available') return `<article class="widget industry-card"><span>${label}</span><strong>資料不足</strong><small>同業樣本未達 ${comparison.minimum_samples} 家</small></article>`;
      return `<article class="widget industry-card"><span>${label}</span><strong>${fmt.format(metric.current)}</strong><small>同業中位數 ${fmt.format(metric.industry_median)}<br>百分位 ${fmt.format(metric.percentile)}%｜樣本 ${metric.sample_count} 家</small></article>`;
    }).join('');
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
  function render(payload) {
    const data = payload.data, profile = data.profile, quote = data.quote, valuation = data.valuation, fundamentals = data.fundamentals, chips = data.chips, analysis = chips.analysis || {};
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
    renderHealth(data.health_v2);
    renderIndustry(data.industry_comparison);
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
      render(await json(`./data/stocks/${encodeURIComponent(symbol)}.json`));
      history.replaceState(null, '', `?symbol=${encodeURIComponent(symbol)}`);
    } catch (error) {
      $('pageState').textContent = error.message === '尚未建立快取資料' ? `${symbol} 尚無快取資料。靜態網站需先執行 python scripts/update_stock_data.py --symbols ${symbol}` : `${symbol}：${error.message}`;
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
  document.addEventListener('click', (event) => { if (!event.target.closest('.search-section')) $('searchResults').hidden = true; });
  init();
})();
