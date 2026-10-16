(() => {
  'use strict';
  const api = globalThis.PlatformApiBase, $ = id => document.getElementById(id);
  const value = (v, d = 0) => v == null ? '—' : Number(v).toLocaleString('zh-TW', { maximumFractionDigits: d });
  const signed = (v, suffix = '') => v == null ? '—' : `${v > 0 ? '+' : ''}${value(v, 2)}${suffix}`;
  const tone = v => v > 0 ? 'tx-positive' : v < 0 ? 'tx-negative' : '';
  const PRODUCT_ORDER = ['TX', 'MTX', 'TMF'];
  const productRank = product => { const rank = PRODUCT_ORDER.indexOf(product); return rank < 0 ? PRODUCT_ORDER.length : rank; };
  let selectedProduct = 'TX', selectedRange = '3M', summary = null, requestId = 0;
  let intradayProduct = 'TX', intradayRange = '1D', intradayRequestId = 0;
  const front = product => summary?.products?.find(row => row.product === product && row.contract_rank === 'front');

  function renderCard(product) {
    const row = front(product), card = document.querySelector(`[data-product="${product}"]`); if (!card || !row) return;
    card.querySelector('[data-price]').textContent = value(row.close, 2);
    card.querySelector('[data-contract]').textContent = `契約 ${row.contract || '—'}`;
    const change = card.querySelector('[data-change]'), pct = row.close && row.change != null ? row.change / (row.close - row.change) * 100 : null;
    change.textContent = `${signed(row.change)} / ${signed(pct, '%')}`; change.className = tone(row.change);
    card.querySelector('[data-metrics]').innerHTML = `<div><dt>成交量</dt><dd>${value(row.volume)}</dd></div><div><dt>未平倉量</dt><dd>${value(row.open_interest)}</dd></div><div><dt>結算價</dt><dd>${value(row.settlement_price, 2)}</dd></div><div><dt>到期</dt><dd>${row.days_to_expiry == null ? '—' : `${row.days_to_expiry} 天`}</dd></div>`;
  }
  function renderSummary(data) {
    summary = data; PRODUCT_ORDER.forEach(renderCard);
    const rows = [...(data.products || [])].sort((left, right) => productRank(left.product) - productRank(right.product) || String(left.contract).localeCompare(String(right.contract)));
    $('futuresTable').tBodies[0].innerHTML = rows.length ? rows.map(row => `<tr><td>${row.product}</td><td>${row.contract}</td><td>${row.contract_rank === 'front' ? '近月' : '次月'}</td><td>${value(row.close, 2)}</td><td>${value(row.settlement_price, 2)}</td><td>${value(row.volume)}</td><td>${value(row.open_interest)}</td><td>${row.expiry_date || '—'}</td><td>${row.days_to_expiry == null ? '—' : row.days_to_expiry}</td></tr>`).join('') : '<tr><td colspan="9">目前無法取得資料</td></tr>';
    $('futuresState').textContent = rows.length ? '資料已更新' : '目前無法取得資料'; $('futuresUpdated').textContent = `最近完成交易日：${data.latest_trade_date || '—'}`;
  }
  function draw(rows) {
    const canvas = $('futuresChart'), empty = $('chartEmpty');
    const points = rows.filter(row => row.close != null); empty.hidden = points.length > 1; if (points.length < 2) { PlatformXYChart.canvas(canvas,{series:[]}); return; }
    PlatformXYChart.canvas(canvas,{series:[{values:points.map(row=>Number(row.close)),color:'#3b82f6'},{values:points.map(row=>Number(row.settlement_price??row.close)),color:'#f59e0b'}],xValues:points.map(row=>row.trade_date),xLabel:'交易日期',yLabel:'指數點'});
  }
  async function loadHistory() {
    const current = ++requestId; $('chartTitle').textContent = `${selectedProduct} 歷史價格`;
    try { const response = await fetch(`${api}/futures/history?product=${selectedProduct}&range=${selectedRange}&limit=1500`, { headers: { Accept: 'application/json' } }); if (!response.ok) throw Error(response.status); const data = await response.json(); if (current === requestId) draw(data.results || []); }
    catch { if (current === requestId) { draw([]); $('chartEmpty').textContent = '目前無法取得歷史資料'; $('chartEmpty').hidden = false; } }
  }
  function drawIntraday(rows) {
    const canvas = $('intradayChart'), empty = $('intradayEmpty');
    const points = rows.filter(row => row.close != null); empty.hidden = points.length > 1;
    if (points.length < 2) { PlatformXYChart.canvas(canvas,{series:[]}); return; }
    PlatformXYChart.canvas(canvas,{series:[{values:points.map(row=>Number(row.close)),color:'#3b82f6'}],xValues:points.map(row=>row.timestamp),xLabel:'時間（台北）',yLabel:'指數點'});
  }
  async function loadIntraday() {
    const current = ++intradayRequestId;
    try {
      const response = await fetch(`${api}/futures/intraday?product=${intradayProduct}&range=${intradayRange}&interval=5m`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw Error(response.status);
      const data = await response.json(); if (current !== intradayRequestId) return;
      const rows = data.results || [], latest = rows.at(-1);
      drawIntraday(rows); $('intradayPrice').textContent = value(latest?.close, 2); $('intradayContract').textContent = latest?.contract || '—';
      $('intradaySession').textContent = data.latest_session === 'regular' ? '日盤' : data.latest_session === 'after_hours' ? '夜盤' : '—';
      $('intradayTimestamp').textContent = data.latest_timestamp ? new Date(data.latest_timestamp).toLocaleString('zh-TW', { timeZone: 'Asia/Taipei' }) : '—';
      $('intradayStatus').textContent = !rows.length ? '尚無官方 5 分鐘重建資料' : `${data.latest_trading_date || '—'}｜每日官方重建${data.stale ? '｜資料可能延遲' : ''}`;
      if (!rows.length) { $('intradayEmpty').textContent = '尚無官方 5 分鐘重建資料'; $('intradayEmpty').hidden = false; }
    } catch { if (current === intradayRequestId) { drawIntraday([]); $('intradayStatus').textContent = '目前無法取得資料'; $('intradayEmpty').textContent = '目前無法取得資料'; $('intradayEmpty').hidden = false; } }
  }
  async function load() { try { const url = `${api}/futures/summary`, data = globalThis.PlatformProductDataGet ? await globalThis.PlatformProductDataGet(url) : await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' }).then(response => { if (!response.ok) throw Error(response.status); return response.json(); }); renderSummary(data); await loadHistory(); } catch { $('futuresState').textContent = '目前無法取得資料'; $('futuresUpdated').textContent = '最近完成交易日行情：—'; $('futuresTable').tBodies[0].innerHTML = '<tr><td colspan="9">目前無法取得資料</td></tr>'; } }
  document.querySelectorAll('#productSelector button').forEach(button => button.addEventListener('click', () => { document.querySelectorAll('#productSelector button').forEach(x => x.classList.toggle('active', x === button)); selectedProduct = button.dataset.product; loadHistory(); }));
  document.querySelectorAll('#rangeSelector button').forEach(button => button.addEventListener('click', () => { document.querySelectorAll('#rangeSelector button').forEach(x => x.classList.toggle('active', x === button)); selectedRange = button.dataset.range; loadHistory(); }));
  document.querySelectorAll('#intradayProductSelector button').forEach(button => button.addEventListener('click', () => { document.querySelectorAll('#intradayProductSelector button').forEach(x => x.classList.toggle('active', x === button)); intradayProduct = button.dataset.product; loadIntraday(); }));
  document.querySelectorAll('#intradayRangeSelector button').forEach(button => button.addEventListener('click', () => { document.querySelectorAll('#intradayRangeSelector button').forEach(x => x.classList.toggle('active', x === button)); intradayRange = button.dataset.range; loadIntraday(); }));
  let resizeTimer; addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { loadHistory(); loadIntraday(); }, 150); }); load(); loadIntraday();
})();
