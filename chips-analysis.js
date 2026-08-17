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
    const min = Math.min(...all, 0); const max = Math.max(...all, 0); const span = max - min || 1;
    const zeroY = 12 + (max / span) * 196;
    container.innerHTML = `<svg viewBox="0 0 800 220" preserveAspectRatio="none" aria-hidden="true"><line class="chart-grid" x1="0" y1="55" x2="800" y2="55"/><line class="chart-grid" x1="0" y1="110" x2="800" y2="110"/><line class="chart-grid" x1="0" y1="165" x2="800" y2="165"/><line class="chart-zero" x1="0" y1="${zeroY.toFixed(1)}" x2="800" y2="${zeroY.toFixed(1)}"/>${series.map(item => `<polyline class="chart-line ${item.className}" points="${polyline(item.values, min, max)}"/>`).join('')}</svg>`;
  }
  function setToneValue(id, text, value) { const node = byId(id); node.textContent = text; tone(node, value); }
  function actionText(value) { return ({ increasing_shorts: '外資正在增加空單', covering_shorts: '外資正在回補空單', increasing_longs: '外資正在增加多單' })[value] || '狀態缺漏'; }

  let chipsSummary = null; let activeRange = 20;
  function renderTrends() {
    const rows = chipsSummary?.history?.[`last_${activeRange}`];
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
      const payload = await loadJson('./data/analysis/chips_summary.json');
      if (!payload || payload.version !== '1.0') throw new DataError('chips_summary 格式錯誤');
      const institutional = requireObject(payload.institutional, 'institutional');
      const futures = requireObject(payload.futures, 'futures');
      const margin = requireObject(payload.margin, 'margin');
      const statuses = requireObject(payload.statuses, 'statuses');
      chipsSummary = payload;
      const streak = requireObject(institutional.foreign_streak, 'foreign_streak');
      const streakLabel = streak.direction === 'buy' ? `連買 ${streak.days} 日` : streak.direction === 'sell' ? `連賣 ${streak.days} 日` : `中性 ${streak.days} 日`;
      setToneValue('foreignStreak', streakLabel, streak.direction === 'buy' ? 1 : streak.direction === 'sell' ? -1 : 0);
      byId('foreignFlowSummary').textContent = `${billions(institutional.foreign_5d_sum)}／${billions(institutional.foreign_20d_sum)}`;
      tone(byId('foreignFlowSummary'), institutional.foreign_5d_sum);
      byId('trustFlowSummary').textContent = `${billions(institutional.investment_trust_5d_sum)}／${billions(institutional.investment_trust_20d_sum)}`;
      tone(byId('trustFlowSummary'), institutional.investment_trust_5d_sum);
      setToneValue('futures5dSummary', units(futures.net_change_5d, '口'), futures.net_change_5d);
      setToneValue('margin5dSummary', hundredMillionsFromThousands(margin.margin_change_5d), margin.margin_change_5d);
      for (const [prefix, key, zscore] of [['institutional', 'institutional_status', institutional.foreign_20d_zscore], ['futures', 'futures_status', futures.net_position_20d_zscore], ['margin', 'margin_status', margin.margin_balance_20d_zscore]]) {
        const status = statuses[key]; const signal = byId(`${prefix}Signal`);
        signal.textContent = statusText(status); tone(signal, status === 'bullish' ? 1 : status === 'bearish' ? -1 : 0);
        byId(`${prefix}Zscore`).textContent = `20 日 Z-score：${Number(zscore).toFixed(2)}`;
      }
      byId('futuresAction').textContent = actionText(futures.position_action);
      for (const [id, value] of [['futuresChange1d', futures.net_change_1d], ['futuresChange5d', futures.net_change_5d], ['futuresChange20d', futures.net_change_20d]]) setToneValue(id, units(value, '口'), value);
      byId('marginChanges').textContent = [margin.margin_change_1d, margin.margin_change_5d, margin.margin_change_20d].map(hundredMillionsFromThousands).join('／');
      byId('shortChanges').textContent = [margin.short_change_1d, margin.short_change_5d, margin.short_change_20d].map(value => units(value)).join('／');
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
