(() => {
  'use strict';
  const api = globalThis.PlatformApiBase, $ = id => document.getElementById(id);
  const value = (v, d = 0) => v == null ? '—' : Number(v).toLocaleString('zh-TW', { maximumFractionDigits: d });
  const signed = (v, suffix = '') => v == null ? '—' : `${v > 0 ? '+' : ''}${value(v, 2)}${suffix}`;
  const tone = v => v > 0 ? 'tx-positive' : v < 0 ? 'tx-negative' : '';
  let selectedProduct = 'TMF', selectedRange = '3M', summary = null, requestId = 0;
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
    summary = data; ['TMF', 'MTX', 'TX'].forEach(renderCard);
    const rows = data.products || [], current = front(selectedProduct), tx = front('TX');
    $('spotIndex').textContent = value(data.taiex, 2); $('basisFuture').textContent = value(tx?.close, 2);
    $('txBasis').textContent = tx?.basis == null ? '資料日期無法可靠對齊' : `${signed(tx.basis)}（${signed(tx.basis_pct, '%')}）`;
    $('priceOiState').textContent = current?.price_oi_state || '資料不足';
    $('futuresTable').tBodies[0].innerHTML = rows.length ? rows.map(row => `<tr><td>${row.product}</td><td>${row.contract}</td><td>${row.contract_rank === 'front' ? '近月' : '次月'}</td><td>${value(row.close, 2)}</td><td>${value(row.settlement_price, 2)}</td><td>${value(row.volume)}</td><td>${value(row.open_interest)}</td><td>${row.expiry_date || '—'}</td><td>${row.days_to_expiry == null ? '—' : row.days_to_expiry}</td></tr>`).join('') : '<tr><td colspan="9">目前無法取得資料</td></tr>';
    $('futuresState').textContent = rows.length ? '資料已更新' : '目前無法取得資料'; $('futuresUpdated').textContent = `最近完成交易日：${data.latest_trade_date || '—'}`;
  }
  function draw(rows) {
    const canvas = $('futuresChart'), empty = $('chartEmpty'), context = canvas.getContext('2d'), scale = devicePixelRatio || 1, width = canvas.clientWidth || 800, height = 280;
    canvas.width = width * scale; canvas.height = height * scale; context.scale(scale, scale); context.clearRect(0, 0, width, height);
    const points = rows.filter(row => row.close != null); empty.hidden = points.length > 1; if (points.length < 2) return;
    const all = points.flatMap(row => [Number(row.close), Number(row.settlement_price ?? row.close)]), min = Math.min(...all), max = Math.max(...all), span = max - min || 1, pad = 24;
    const line = (field, color) => { context.beginPath(); points.forEach((row, i) => { const x = pad + i * (width - pad * 2) / (points.length - 1), y = pad + (max - Number(row[field] ?? row.close)) / span * (height - pad * 2); i ? context.lineTo(x, y) : context.moveTo(x, y); }); context.strokeStyle = color; context.lineWidth = 2; context.stroke(); };
    line('close', '#3b82f6'); line('settlement_price', '#f59e0b');
  }
  async function loadHistory() {
    const current = ++requestId; $('chartTitle').textContent = `${selectedProduct} 歷史價格`;
    try { const response = await fetch(`${api}/futures/history?product=${selectedProduct}&range=${selectedRange}&limit=1500`, { headers: { Accept: 'application/json' } }); if (!response.ok) throw Error(response.status); const data = await response.json(); if (current === requestId) draw(data.results || []); }
    catch { if (current === requestId) { draw([]); $('chartEmpty').textContent = '目前無法取得歷史資料'; $('chartEmpty').hidden = false; } }
  }
  async function load() { try { const response = await fetch(`${api}/futures/summary`, { headers: { Accept: 'application/json' }, cache: 'no-store' }); if (!response.ok) throw Error(response.status); renderSummary(await response.json()); await loadHistory(); } catch { $('futuresState').textContent = '目前無法取得資料'; $('futuresTable').tBodies[0].innerHTML = '<tr><td colspan="9">目前無法取得資料</td></tr>'; } }
  document.querySelectorAll('#productSelector button').forEach(button => button.addEventListener('click', () => { document.querySelectorAll('#productSelector button').forEach(x => x.classList.toggle('active', x === button)); selectedProduct = button.dataset.product; $('priceOiState').textContent = front(selectedProduct)?.price_oi_state || '資料不足'; loadHistory(); }));
  document.querySelectorAll('#rangeSelector button').forEach(button => button.addEventListener('click', () => { document.querySelectorAll('#rangeSelector button').forEach(x => x.classList.toggle('active', x === button)); selectedRange = button.dataset.range; loadHistory(); }));
  let resizeTimer; addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(loadHistory, 150); }); load();
})();
