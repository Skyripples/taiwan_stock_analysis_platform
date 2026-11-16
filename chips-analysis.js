(() => {
  class DataError extends Error {}
  const integer = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 0 });

  function byId(id) { return document.getElementById(id); }
  function requireInteger(value, name) { if (!Number.isSafeInteger(value)) throw new DataError(`${name} 缺漏`); return value; }
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
  function setStatus(prefix, text, failed = false) { const node = byId(`${prefix}Status`); node.textContent = text; node.classList.toggle('tone-negative', failed); }
  function fail(prefix, error) { setStatus(prefix, error instanceof DataError ? '資料缺漏' : '讀取失敗', true); byId(`${prefix}Message`).textContent = error instanceof DataError ? '資料欄位缺漏或格式不正確。' : '目前無法讀取資料，其他區塊仍可正常使用。'; }

  function dailyRows(payload) {
    const rows = payload?.history?.last_60;
    if (!Array.isArray(rows) || rows.length < 10) throw new DataError('最近 10 日籌碼資料缺漏');
    return rows.slice(-10);
  }
  function tradeDate(row) {
    if (typeof row.trade_date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(row.trade_date)) throw new DataError('history.trade_date 缺漏');
    return row.trade_date;
  }
  function tableRow(values, tones = []) {
    const row = document.createElement('tr');
    values.forEach((value, index) => {
      const cell = document.createElement('td');
      cell.textContent = value;
      if (Number.isFinite(tones[index])) tone(cell, tones[index]);
      row.append(cell);
    });
    return row;
  }
  function newestFirst(rows) { return rows.slice().reverse(); }
  function reveal(prefix, latestDate) {
    byId(`${prefix}Content`).hidden = false;
    byId(`${prefix}Message`).hidden = true;
    setStatus(prefix, `更新至 ${latestDate}`);
  }
  function renderInstitutionalTable(rows) {
    byId('institutionalRows').replaceChildren(...newestFirst(rows).map(row => {
      const foreign = requireInteger(row.foreign_net, 'history.foreign_net');
      const trust = requireInteger(row.investment_trust_net, 'history.investment_trust_net');
      const dealer = requireInteger(row.dealer_net, 'history.dealer_net');
      const total = foreign + trust + dealer;
      if (!Number.isSafeInteger(total)) throw new DataError('history.institutional_total 缺漏');
      return tableRow(
        [tradeDate(row), billions(foreign), billions(trust), billions(dealer), billions(total)],
        [null, foreign, trust, dealer, total],
      );
    }));
  }
  function renderFuturesTable(rows) {
    byId('futuresRows').replaceChildren(...newestFirst(rows).map(row => {
      const long = requireInteger(row.foreign_futures_long, 'history.foreign_futures_long');
      const short = requireInteger(row.foreign_futures_short, 'history.foreign_futures_short');
      const net = requireInteger(row.foreign_futures_net, 'history.foreign_futures_net');
      return tableRow(
        [tradeDate(row), `${integer.format(long)} 口`, `${integer.format(short)} 口`, units(net, '口')],
        [null, null, null, net],
      );
    }));
  }
  function renderMarginTable(rows) {
    byId('marginRows').replaceChildren(...newestFirst(rows).map(row => {
      const balance = requireInteger(row.margin_balance, 'history.margin_balance');
      const change = requireInteger(row.margin_change, 'history.margin_change');
      const shortBalance = requireInteger(row.short_balance, 'history.short_balance');
      const shortChange = requireInteger(row.short_change, 'history.short_change');
      return tableRow(
        [tradeDate(row), `${(balance / 100000).toFixed(2)} 億元`, hundredMillionsFromThousands(change), `${integer.format(shortBalance)} 張`, units(shortChange)],
        [null, null, change, null, -shortChange],
      );
    }));
  }

  async function loadSummary() {
    let rows;
    try {
      const payload = await loadJson('./data/analysis/current/chips_summary.json');
      if (!payload || payload.version !== '1.0') throw new DataError('chips_summary 格式錯誤');
      rows = dailyRows(payload);
    } catch (error) {
      ['institutional', 'futures', 'margin'].forEach(prefix => fail(prefix, error));
      return;
    }
    const latestDate = tradeDate(rows[rows.length - 1]);
    for (const [prefix, renderer] of [
      ['institutional', renderInstitutionalTable],
      ['futures', renderFuturesTable],
      ['margin', renderMarginTable],
    ]) {
      try { renderer(rows); reveal(prefix, latestDate); } catch (error) { fail(prefix, error); }
    }
  }

  loadSummary();
})();
