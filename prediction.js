(() => {
  'use strict';

  const TRAINING_THRESHOLD = 200;
  const SIGNALS = [
    ['foreign_cash_flow', '外資現貨', value => `${formatSigned(value / 100000000, 2)} 億元`],
    ['foreign_futures_position', '外資期貨', value => `${formatSigned(value, 0)} 口`],
    ['night_futures', '台指期夜盤', value => `${formatSigned(value, 2)} 點`],
    ['tsm_adr', '台積電 ADR', value => `${formatSigned(value, 2)} 美元`],
    ['sox_index', '費城半導體', value => `${formatSigned(value, 2)}%`]
  ];
  const STATUS_LABELS = {
    'Strong Bullish': '強勢偏多', Bullish: '偏多', Neutral: '中性', Bearish: '偏空', 'Strong Bearish': '強勢偏空',
    bullish: '偏多', neutral: '中性', bearish: '偏空'
  };

  const byId = id => document.getElementById(id);

  function formatSigned(value, digits) {
    if (!Number.isFinite(value)) return '資料缺漏';
    const formatted = Math.abs(value).toLocaleString('zh-TW', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return `${value > 0 ? '+' : value < 0 ? '-' : ''}${formatted}`;
  }

  function tone(status) {
    const normalized = String(status || '').toLowerCase();
    if (normalized.includes('bullish')) return 'tone-bullish';
    if (normalized.includes('bearish')) return 'tone-bearish';
    return 'tone-neutral';
  }

  function parseCsv(text) {
    const rows = [];
    let row = [], field = '', quoted = false;
    for (let index = 0; index < text.length; index += 1) {
      const char = text[index];
      if (quoted) {
        if (char === '"' && text[index + 1] === '"') { field += '"'; index += 1; }
        else if (char === '"') quoted = false;
        else field += char;
      } else if (char === '"') quoted = true;
      else if (char === ',') { row.push(field); field = ''; }
      else if (char === '\n') { row.push(field.replace(/\r$/, '')); rows.push(row); row = []; field = ''; }
      else field += char;
    }
    if (field || row.length) { row.push(field.replace(/\r$/, '')); rows.push(row); }
    const nonEmpty = rows.filter(cells => cells.some(cell => cell.trim() !== ''));
    if (!nonEmpty.length) throw new Error('CSV 沒有標頭');
    const headers = nonEmpty[0].map(header => header.trim());
    return nonEmpty.slice(1).map(cells => Object.fromEntries(headers.map((header, index) => [header, (cells[index] || '').trim()])));
  }

  async function fetchText(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.text();
  }

  function renderSignals(payload) {
    const score = payload.market_score;
    if (!score || !Number.isFinite(Number(score.score)) || !Number.isFinite(Number(score.max_score)) || !Number.isFinite(Number(score.percentage)) || !score.status) throw new Error('market_score 欄位缺漏');
    const percentage = Math.max(0, Math.min(100, Number(score.percentage)));
    const scoreTone = tone(score.status);
    byId('marketPercentage').textContent = `${percentage}%`;
    byId('marketPercentage').className = `score-percentage ${scoreTone}`;
    byId('marketStatus').textContent = `${score.status}（${STATUS_LABELS[score.status] || score.status}）`;
    byId('marketStatus').className = scoreTone;
    byId('marketRawScore').textContent = `${score.score} / ${score.max_score}`;
    byId('marketScoreBar').style.width = `${percentage}%`;
    byId('marketScoreBar').className = `score-fill ${scoreTone}`;

    const signals = payload.signals || {};
    byId('signalGrid').replaceChildren(...SIGNALS.map(([key, label, formatter]) => {
      const item = document.createElement('div');
      item.className = 'signal-item';
      const signal = signals[key];
      if (!signal || !Number.isFinite(Number(signal.value)) || !Number.isFinite(Number(signal.score)) || !signal.status) {
        item.innerHTML = `<span>${label}</span><strong>資料缺漏</strong><small>--</small>`;
        return item;
      }
      const signalTone = tone(signal.status);
      const signedScore = Number(signal.score) > 0 ? `+${signal.score}` : String(signal.score);
      item.innerHTML = `<span>${label}</span><strong class="${signalTone}">${formatter(Number(signal.value))}</strong><small>${STATUS_LABELS[signal.status] || signal.status} · score ${signedScore}</small>`;
      return item;
    }));
    byId('marketLoadStatus').textContent = '已載入';
    byId('marketStateWidget').dataset.state = 'success';
    const updated = new Date(payload.updated_at);
    byId('lastUpdatedAt').textContent = Number.isNaN(updated.getTime()) ? (payload.updated_at || '資料缺漏') : updated.toLocaleString('zh-TW');
  }

  async function loadSignals() {
    try {
      renderSignals(JSON.parse(await fetchText('./data/market/market_signals.json')));
    } catch (error) {
      byId('marketLoadStatus').textContent = error.message.includes('缺漏') ? '資料缺漏' : '讀取失敗';
      byId('marketStateWidget').dataset.state = 'error';
      byId('signalGrid').innerHTML = '<p class="widget-description">市場訊號目前無法顯示，其他資料仍可正常載入。</p>';
      byId('lastUpdatedAt').textContent = '讀取失敗';
    }
  }

  async function loadPredictionDataset() {
    try {
      const rows = parseCsv(await fetchText('./data/history/prediction_dataset.csv'));
      const validRows = rows.filter(row => row.feature_date && row.target_date && row.next_taiex_close !== '' && row.next_taiex_return !== '' && row.target_direction !== '');
      if (validRows.length !== rows.length) throw new Error('CSV 含不完整資料列');
      const count = validRows.length;
      const progress = Math.min(100, count / TRAINING_THRESHOLD * 100);
      byId('trainingProgressText').textContent = `${count} / ${TRAINING_THRESHOLD}`;
      byId('trainingProgressBar').style.width = `${progress}%`;
      byId('modelStatusText').textContent = count < TRAINING_THRESHOLD ? '資料累積中' : '模型尚未建立';
      byId('datasetLoadStatus').textContent = count === 0 ? '0 筆資料' : '已載入';
      byId('modelStatusWidget').dataset.state = 'success';
      byId('trainableRowCount').textContent = `${count} 筆`;
      byId('latestFeatureDate').textContent = count ? validRows.map(row => row.feature_date).sort().at(-1) : '尚無可訓練特徵';
    } catch (error) {
      byId('datasetLoadStatus').textContent = '讀取失敗';
      byId('modelStatusWidget').dataset.state = 'error';
      byId('trainingProgressText').textContent = '-- / 200';
      byId('modelStatusText').textContent = '訓練資料讀取失敗';
      byId('trainableRowCount').textContent = '讀取失敗';
      byId('latestFeatureDate').textContent = '讀取失敗';
    }
  }

  async function loadMarketHistory() {
    try {
      const rows = parseCsv(await fetchText('./data/history/market_daily.csv'));
      if (rows.some(row => !row.trade_date)) throw new Error('trade_date 缺漏');
      byId('historyRowCount').textContent = `${rows.length} 筆`;
      byId('latestMarketDate').textContent = rows.length ? rows.map(row => row.trade_date).sort().at(-1) : '尚無歷史資料';
    } catch (error) {
      byId('historyRowCount').textContent = '讀取失敗';
      byId('latestMarketDate').textContent = '讀取失敗';
    }
  }

  Promise.allSettled([loadSignals(), loadPredictionDataset(), loadMarketHistory()]);
})();
