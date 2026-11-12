(() => {
  class DataError extends Error {}
  const integer = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 0 });

  function byId(id) { return document.getElementById(id); }
  function requireObject(value, name) { if (!value || typeof value !== 'object' || Array.isArray(value)) throw new DataError(`${name} 格式錯誤`); return value; }
  function requireInteger(value, name) { if (!Number.isSafeInteger(value)) throw new DataError(`${name} 缺漏`); return value; }
  function latestRecord(payload, dataset) {
    if (payload?.dataset !== dataset || !Array.isArray(payload?.data?.records) || payload.data.records.length < 1) throw new DataError(`${dataset} 格式錯誤`);
    const record = requireObject(payload.data.records[payload.data.records.length - 1], dataset);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(record.trade_date || '')) throw new DataError(`${dataset}.trade_date 缺漏`);
    return record;
  }
  async function loadJson(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    try { return await response.json(); } catch { throw new DataError('JSON 格式錯誤'); }
  }
  function tone(node, value) {
    node.classList.remove('tone-positive', 'tone-negative', 'tone-neutral');
    node.classList.add(value > 0 ? 'tone-positive' : value < 0 ? 'tone-negative' : 'tone-neutral');
  }
  function signed(value, formatter) { return `${value > 0 ? '+' : ''}${formatter(value)}`; }
  function billions(value) { return `${signed(value / 100000000, number => number.toFixed(2))} 億`; }
  function hundredMillionsFromThousands(value) { return `${signed(value / 100000, number => number.toFixed(2))} 億元`; }
  function units(value, unit = '張') { return `${signed(value, number => integer.format(number))} ${unit}`; }
  function statusText(status) { return ({ bullish: '偏多', neutral: '中性', bearish: '偏空' })[status] || '資料缺漏'; }
  function setStatus(prefix, text, failed = false) { const node = byId(`${prefix}Status`); node.textContent = text; node.classList.toggle('tone-negative', failed); }
  function fail(prefix, error) { setStatus(prefix, error instanceof DataError ? '資料缺漏' : '讀取失敗', true); byId(`${prefix}Message`).textContent = error instanceof DataError ? '資料欄位缺漏或格式不正確。' : '目前無法讀取資料，其他區塊仍可正常使用。'; }

  async function loadInstitutional() {
    try {
      const record = latestRecord(await loadJson('./data/market/institutional_investors.json'), 'institutional_investors');
      const groups = {
        foreign: requireObject(record.foreign_and_mainland_investors, 'foreign'),
        trust: requireObject(record.investment_trust, 'trust'),
        dealer: requireObject(record.dealers, 'dealer'),
      };
      for (const [prefix, group] of Object.entries(groups)) {
        for (const field of ['buy', 'sell', 'net']) {
          const value = requireInteger(group[field], `${prefix}.${field}`);
          const node = byId(`${prefix}${field[0].toUpperCase()}${field.slice(1)}`);
          node.textContent = billions(value); if (field === 'net') tone(node, value);
        }
      }
      const proprietary = requireInteger(record.dealers?.breakdown?.proprietary?.net, 'dealers.proprietary.net');
      const hedging = requireInteger(record.dealers?.breakdown?.hedging?.net, 'dealers.hedging.net');
      const total = requireInteger(record.total?.net, 'total.net');
      for (const [id, value] of [['dealerProprietaryNet', proprietary], ['dealerHedgingNet', hedging], ['institutionalTotalNet', total]]) { const node = byId(id); node.textContent = billions(value); tone(node, value); }
      document.querySelectorAll('[data-date="institutional"]').forEach(node => { node.textContent = record.trade_date; });
      byId('institutionalContent').hidden = false; byId('institutionalMessage').hidden = true; setStatus('institutional', '已更新');
    } catch (error) { fail('institutional', error); }
  }

  async function loadFutures() {
    try {
      const record = latestRecord(await loadJson('./data/market/foreign_futures_position.json'), 'foreign_futures_position');
      const long = requireInteger(record.long_position?.open_interest, 'long_position.open_interest');
      const short = requireInteger(record.short_position?.open_interest, 'short_position.open_interest');
      const net = requireInteger(record.net_position?.open_interest, 'net_position.open_interest');
      const amount = requireInteger(record.net_position?.contract_amount, 'net_position.contract_amount');
      byId('futuresLong').textContent = `${integer.format(long)} 口`; byId('futuresShort').textContent = `${integer.format(short)} 口`;
      byId('futuresNet').textContent = units(net, '口'); tone(byId('futuresNet'), net);
      byId('futuresNetAmount').textContent = hundredMillionsFromThousands(amount); tone(byId('futuresNetAmount'), amount);
      byId('futuresTradeDate').textContent = record.trade_date;
      const metadata = requireObject(record.metadata, 'metadata');
      byId('futuresMeta').textContent = `${metadata.product_name || '臺股期貨'}｜${metadata.product_code || 'TXF'}｜${metadata.investor_type || '外資'}`;
      byId('futuresContent').hidden = false; byId('futuresMessage').hidden = true; setStatus('futures', '已更新');
    } catch (error) { fail('futures', error); }
  }

  async function loadMargin() {
    try {
      const record = latestRecord(await loadJson('./data/market/margin_trading.json'), 'margin_trading');
      const amount = requireObject(record.margin_financing?.amount, 'margin_financing.amount');
      const short = requireObject(record.short_selling?.trading_units, 'short_selling.trading_units');
      const marginBalance = requireInteger(amount.balance, 'amount.balance');
      const marginChange = requireInteger(amount.change, 'amount.change');
      const shortBalance = requireInteger(short.balance, 'short.balance');
      const shortChange = requireInteger(short.change, 'short.change');
      byId('marginBalance').textContent = `${(marginBalance / 100000).toFixed(2)} 億元`;
      byId('marginChange').textContent = hundredMillionsFromThousands(marginChange); tone(byId('marginChange'), marginChange);
      byId('shortBalance').textContent = `${integer.format(shortBalance)} 張`;
      byId('shortChange').textContent = units(shortChange); tone(byId('shortChange'), -shortChange);
      byId('marginTradeDate').textContent = record.trade_date;
      byId('marginContent').hidden = false; byId('marginMessage').hidden = true; setStatus('margin', '已更新');
    } catch (error) { fail('margin', error); }
  }

  function polyline(values, min, max, width = 800, height = 220) {
    const span = max - min || 1;
    return values.map((value, index) => {
      const x = values.length === 1 ? width / 2 : index * width / (values.length - 1);
      const y = 12 + (max - value) / span * (height - 24);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
  }
  function drawLines(container, series) {
    const all = series.flatMap(item => item.values);
    if (!all.length || all.some(value => !Number.isFinite(value))) throw new DataError('趨勢資料缺漏');
    const chart = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    chart.setAttribute('preserveAspectRatio', 'none'); chart.setAttribute('role', 'img'); chart.setAttribute('aria-label', '籌碼歷史趨勢');
    PlatformXYChart.svg(chart, { width: 800, height: 220, compact: true, series: series.map(item => ({ ...item, className: `chart-line ${item.className}` })), xLabel: '期間', yLabel: '數值', xFormatter: String });
    container.replaceChildren(chart);
  }
  function setToneValue(id, text, value) { const node = byId(id); node.textContent = text; tone(node, value); }
  function actionText(value) { return ({ increasing_shorts: '外資正在增加空單', covering_shorts: '外資正在回補空單', increasing_longs: '外資正在增加多單' })[value] || '狀態缺漏'; }

  let chipsSummary = null; let activeRange = 5;
  function dailyRows() {
    const rows = chipsSummary?.history?.last_60;
    if (!Array.isArray(rows) || rows.length < 5) throw new DataError('最近 5 日籌碼資料缺漏');
    return rows.slice(-5);
  }
  function renderDailyTable(rows) {
    byId('dailyChipsRows').replaceChildren(...rows.slice().reverse().map(row => {
      const date = typeof row.trade_date === 'string' ? row.trade_date : null;
      const foreign = requireInteger(row.foreign_net, 'history.foreign_net');
      const trust = requireInteger(row.investment_trust_net, 'history.investment_trust_net');
      const futuresNet = requireInteger(row.foreign_futures_net, 'history.foreign_futures_net');
      const marginChange = requireInteger(row.margin_change, 'history.margin_change');
      if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new DataError('history.trade_date 缺漏');
      const tr = document.createElement('tr');
      const values = [date, billions(foreign), billions(trust), units(futuresNet, '口'), hundredMillionsFromThousands(marginChange)];
      values.forEach((value, index) => { const cell = document.createElement('td'); cell.textContent = value; if (index > 0) tone(cell, [foreign, trust, futuresNet, marginChange][index - 1]); tr.append(cell); });
      return tr;
    }));
  }
  function renderTrends() {
    const history = chipsSummary?.history;
    const sourceRows = activeRange === 5 ? dailyRows() : history?.last_60;
    const rows = Array.isArray(sourceRows) ? sourceRows : null;
    if (!Array.isArray(rows) || !rows.length) throw new DataError('歷史趨勢缺漏');
    drawLines(byId('institutionalChart'), [
      { className: 'foreign', values: rows.map(row => row.foreign_net / 100000000) },
      { className: 'trust', values: rows.map(row => row.investment_trust_net / 100000000) },
      { className: 'dealer', values: rows.map(row => row.dealer_net / 100000000) },
    ]);
    drawLines(byId('futuresChart'), [{ className: 'primary', values: rows.map(row => row.foreign_futures_net) }]);
    drawLines(byId('marginChart'), [{ className: 'primary', values: rows.map(row => row.margin_balance / 100000) }]);
  }

  async function loadSummary() {
    try {
      const payload = await loadJson('./data/analysis/current/chips_summary.json');
      if (!payload || payload.version !== '1.0') throw new DataError('chips_summary 格式錯誤');
      const futures = requireObject(payload.futures, 'futures');
      chipsSummary = payload;
      byId('futuresAction').textContent = actionText(futures.position_action);
      renderDailyTable(dailyRows());
      renderTrends();
      byId('summaryContent').hidden = false; byId('summaryMessage').hidden = true; setStatus('summary', `更新至 ${payload.trade_date}`);
    } catch (error) { fail('summary', error); }
  }

  document.querySelectorAll('[data-range]').forEach(button => button.addEventListener('click', () => {
    activeRange = Number(button.dataset.range);
    document.querySelectorAll('[data-range]').forEach(item => item.classList.toggle('is-active', item === button));
    try { renderTrends(); } catch (error) { fail('summary', error); }
  }));

  Promise.allSettled([loadSummary(), loadInstitutional(), loadFutures(), loadMargin()]);
})();
