(() => {
  'use strict';
  const params = new URLSearchParams(window.location.search);
  const sourceMode = params.get('source');
  const realMode = sourceMode === 'real' || sourceMode === 'day-real';
  const DAY_SHADOW_ENDPOINT = 'https://172-238-20-217.ip.linodeusercontent.com/api/v1/futures/day-shadow.json';
  const DATA_URL = sourceMode === 'day-real' ? DAY_SHADOW_ENDPOINT : realMode ? './preview-data/b4-forward-live.json' : './mock-data/b4-ml-preview.json';
  const $ = id => document.getElementById(id);
  const order = ['logistic', 'hist_gradient_boosting', 'dummy'];
  const state = $('pageState');
  const date = value => value ? new Intl.DateTimeFormat('zh-TW', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value)) : '\u2014';
  const dateTime = value => value ? new Intl.DateTimeFormat('zh-TW', { dateStyle: 'medium', timeStyle: 'short', hour12: false, timeZone: 'Asia/Taipei' }).format(new Date(value)) : '\u2014';
  const pct = value => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(2)}%` : '\u2014';
  const esc = value => String(value ?? '\u2014').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  const labels = { timely: '\u6e96\u6642', early: '\u63d0\u65e9\u7522\u751f', on_time: '\u6e96\u9ede\u7522\u751f', late: '\u9072\u5230', replay: '\u6b77\u53f2\u56de\u653e', unavailable: '\u7121\u6cd5\u7528\u65bc\u9810\u6e2c', expired: '\u5df2\u904e\u65e5\u671f' };
  const dayRealMode = sourceMode === 'day-real';
  const dayTaskCopy = {
    subtitle: '日盤 DAY 實驗：三個模型以同一份 08:15 前可用的 TX Core 特徵，預測當日日盤方向。',
    target: '08:15 前預測同日 08:45 開盤至正常日盤收盤方向；收盤高於開盤為 UP、低於開盤為 DOWN，平盤不納入二元評分。',
    label: 'DAY SESSION · FUTURES',
    recordsAria: 'DAY 08:15 期貨預測紀錄'
  };
  const modelName = label => `${label}\u6a21\u578b`;
  const modelDescription = Object.freeze({
    logistic: '用特徵的加權組合估算上漲機率，再依機率分類；適合當簡單基準。',
    hist_gradient_boosting: '逐步疊加決策樹，學習非線性門檻與特徵交互關係。',
    dummy: '不看特徵，只依訓練資料的漲跌比例預測，作為比較基準。',
  });
  let loadedData = null;
  const factorTemplates = {
    DAY: ['NQ', 'ES', 'TSM ADR', 'TX價量／OI'],
    NIGHT: ['NQ', 'ES', 'TSM ADR', 'TX價量／OI'],
    US_PRE: ['NQ', 'ES', 'TSM ADR', 'TX價量／OI'],
    US_POST: ['NQ', 'ES', 'TSM ADR', 'TX價量／OI']
  };
  const factorStatusLabel = { available: '可用', missing: '缺資料', stale: '資料過期', late: '晚於 cutoff', mock: '示範資料', unverified: '待驗證' };

  function normalize(raw) {
    if (!raw?.preview || !Array.isArray(raw.predictions) || !raw.dataset) throw Error('\u8cc7\u6599\u683c\u5f0f\u4e0d\u5b8c\u6574');
    const predictions = raw.predictions.map(row => ({ ...row, probability_up: Number.isFinite(Number(row.probability_up)) ? Number(row.probability_up) : null }));
    return { ...raw, predictions, latestDate: predictions.reduce((latest, row) => !latest || row.prediction_date > latest ? row.prediction_date : latest, '') };
  }

  function displayStatus(row, data) { return row.status === 'timely' && row.prediction_date < data.latestDate ? 'expired' : row.status; }

  function renderMetricRows(performance) {
    if (!performance || performance.status !== 'ready') return `<div class="ml-pending"><strong>\u5f85\u6536\u76e4\u8a55\u4f30</strong><br>${esc(performance?.message || '\u5c1a\u7121\u5df2\u63ed\u66c9\u7684 timely \u524d\u77bb\u7e3e\u6548\u3002')}</div>`;
    const period = performance.period ? `${date(performance.period.start)} - ${date(performance.period.end)}` : '\u2014';
    return `<div class="ml-backtest-row"><small>\u8cc7\u6599\u671f\u9593</small><strong>${period}</strong><small>\u50c5 timely</small></div>${(performance.models || []).map(model => {
      const insufficient = !Number.isFinite(Number(model.samples)) || Number(model.samples) < 2;
      return `<div class="ml-backtest-row"><span>${esc(modelName(model.label))}${model.baseline ? '\uff08\u57fa\u6e96\uff09' : ''}</span><strong>${insufficient ? '\u6a23\u672c\u4e0d\u8db3' : `\u6e96\u78ba\u7387 ${pct(model.accuracy)}`}</strong><small>${model.samples ?? 0} \u7b46${insufficient ? '' : ` / Brier ${Number(model.brier).toFixed(4)}`}</small></div>`;
    }).join('')}`;
  }

  function renderHistorical(data) {
    const historical = data.historical_backtest || data.backtest;
    if (!historical || historical.status === 'not_synced') return `<div class="ml-pending"><strong>\u6b77\u53f2\u56de\u6e2c\u672a\u540c\u6b65</strong><br>${esc(historical?.message || '\u672a\u63d0\u4f9b\u56de\u6e2c\u8cc7\u6599\u4f86\u6e90\u8207\u671f\u9593\u3002')}</div>`;
    const period = historical.period ? `${date(historical.period.start)} - ${date(historical.period.end)}` : '\u2014';
    return `<div class="ml-backtest-row"><small>\u4f86\u6e90\uff0f\u671f\u9593</small><strong>${esc(historical.source || '\u7814\u7a76\u56de\u6e2c')} ${period}</strong><small>\u6a23\u672c ${historical.samples ?? '\u2014'}</small></div>${(historical.models || []).map(model => `<div class="ml-backtest-row"><span>${esc(modelName(model.label))}${model.baseline ? '\uff08\u57fa\u6e96\uff09' : ''}</span><strong>\u6e96\u78ba\u7387 ${pct(model.accuracy)}</strong><small>Brier ${model.brier == null ? '\u2014' : Number(model.brier).toFixed(4)}</small></div>`).join('')}`;
  }

  function changeText(value) {
    if (value == null || value === '') return '\u2014';
    return Number.isFinite(Number(value)) ? pct(value) : esc(value);
  }

  function unavailableTask(key, real) {
    return {
      task: key,
      status: 'missing',
      message: real ? '真實因子資料尚未同步；不會改用示範資料。' : '尚未提供此任務的因子資料。',
      summary: { status: 'pending_rule', direction: '\u2014', score: null, coverage: null },
      factors: factorTemplates[key].map(name => ({ name, status: 'missing', observation_window: '\u2014', change: null, weight: null, contribution: null, observed_at: null }))
    };
  }

  function renderFactors(data) {
    const key = $('factorTaskSelect')?.value || 'DAY';
    const tasks = data.factor_scoring?.tasks;
    const task = tasks?.[key] || unavailableTask(key, data.data_mode === 'real');
    const factors = Array.isArray(task.factors) ? task.factors : [];
    const available = factors.filter(item => item.status === 'available' || item.status === 'mock').length;
    const summary = task.summary || {};
    const ready = summary.status === 'ready';
    $('factorSummary').innerHTML = [
      ['方向狀態', ready ? (summary.direction || '\u2014') : '待建立'],
      ['方向總分', ready && Number.isFinite(Number(summary.score)) ? Number(summary.score).toFixed(2) : '\u2014'],
      ['資料覆蓋率', `${available}/${factors.length || 0}`]
    ].map(([label, value]) => `<div><span>${label}</span><strong class="${ready ? '' : 'is-pending'}">${esc(value)}</strong></div>`).join('');
    $('factorRows').innerHTML = factors.map(item => {
      const status = item.status || 'missing';
      const weight = item.weight_status === 'verified' && Number.isFinite(Number(item.weight)) ? Number(item.weight).toFixed(2) : '待驗證';
      const contribution = ready && item.contribution != null ? esc(item.contribution) : '\u2014';
      return `<tr><td>${esc(item.name)}</td><td>${esc(item.observation_window || '\u2014')}</td><td>${changeText(item.change)}</td><td>${weight}</td><td>${contribution}</td><td>${dateTime(item.observed_at)}</td><td><span class="factor-status ${esc(status)}">${factorStatusLabel[status] || '未知'}</span></td></tr>`;
    }).join('');
    $('factorEmpty').hidden = Boolean(factors.length);
    $('factorEmpty').textContent = task.message || '尚無因子資料。';
  }

  function render(data) {
    if (dayRealMode) {
      $('experimentSubtitle').textContent = dayTaskCopy.subtitle;
      $('modelTaskLabel').textContent = dayTaskCopy.label;
      $('dayPredictionRecordsTable').setAttribute('aria-label', dayTaskCopy.recordsAria);
    }
    const sourceLabel = data.data_mode === 'real' ? `\u771f\u5be6\u8cc7\u6599\uff08\u540c\u6b65 ${dateTime(data.synced_at)}\uff09` : '\u4ecb\u9762\u9810\u89bd\uff0f\u793a\u7bc4\u8cc7\u6599';
    const badge = document.querySelector('.preview-badge');
    if (badge) badge.textContent = data.data_mode === 'real' ? '\u672c\u6a5f\u771f\u5be6\u8cc7\u6599\u9810\u89bd' : '\u4ecb\u9762\u9810\u89bd\uff0f\u793a\u7bc4\u8cc7\u6599';
    $('targetDefinition').textContent = dayRealMode ? dayTaskCopy.target : data.dataset.target; $('probabilityNotice').textContent = data.dataset.notice;
    const current = data.predictions.filter(row => row.prediction_date === data.latestDate);
    $('predictionMeta').textContent = current.length ? `\u9810\u6e2c\u65e5\uff1a${date(data.latestDate)} - ${current[0].contract || '\u2014'}` : '\u5c1a\u7121\u524d\u77bb\u9810\u6e2c\u7d00\u9304';
    $('modelCards').innerHTML = order.map(model => current.find(row => row.model === model)).filter(Boolean).map(row => {
      const status = displayStatus(row, data), direction = row.direction || '\u2014', directionClass = direction === 'UP' ? 'up' : direction === 'DOWN' ? 'down' : 'muted';
      const result = row.actual_direction ? `${row.actual_direction} / ${row.hit ? '\u547d\u4e2d' : '\u672a\u547d\u4e2d'}` : '\u5f85\u6536\u76e4\u8a55\u4f30';
      return `<article class="widget ml-model-card"><header><h3>${esc(modelName(row.label))}</h3>${row.baseline ? '<span class="model-baseline">\u57fa\u6e96</span>' : ''}<span class="status-pill ${status}">${labels[status] || status}</span></header><p class="ml-direction ${directionClass}">${direction}</p><p class="ml-probability">\u4e0a\u6f32\u6a5f\u7387\uff1a<strong>${pct(row.probability_up)}</strong></p><div class="ml-model-meta"><span>\u9810\u6e2c\u65e5\uff1a<b>${date(row.prediction_date)}</b></span><span>\u7522\u751f\u6642\u9593\uff1a<b>${dateTime(row.generated_at)}</b></span><span>\u6a21\u578b\u7248\u672c\uff1a<b>${esc(row.model_version)}</b></span><span>\u5408\u7d04\uff1a<b>${esc(row.contract)}</b></span><span>\u5be6\u969b\u7d50\u679c\uff1a<b>${result}</b></span></div></article>`;
    }).join('') || '<p class="ml-empty">\u5c1a\u7121\u6700\u65b0\u524d\u77bb\u9810\u6e2c\u3002</p>';
    document.querySelectorAll('#modelCards .ml-model-card').forEach((card, index) => {
      const row = order.map(model => current.find(item => item.model === model)).filter(Boolean)[index];
      const description = document.createElement('p');
      description.className = 'ml-model-description';
      description.textContent = modelDescription[row?.model] || '模型說明尚未提供。';
      card.querySelector('header')?.after(description);
    });
    renderFactors(data);
    const rows = [...data.predictions].sort((a, b) => `${b.prediction_date}${b.generated_at}`.localeCompare(`${a.prediction_date}${a.generated_at}`));
    const taskFor = row => String(row.task || row.prediction_task || row.dataset_task || 'DAY').toUpperCase();
    const renderRecords = (task, bodyId, emptyId) => {
      const taskRows = rows.filter(row => taskFor(row) === task);
      $(emptyId).hidden = Boolean(taskRows.length);
      $(bodyId).innerHTML = taskRows.map(row => {
        const status = displayStatus(row, data);
        const actual = row.actual_direction ? `${row.actual_direction} / ${row.hit ? '\u547d\u4e2d' : '\u672a\u547d\u4e2d'}` : '\u5f85\u8a55\u4f30';
        return `<tr class="${status === 'expired' ? 'record-old' : ''}"><td>${date(row.prediction_date)}</td><td>${esc(modelName(row.label))}${row.baseline ? '\uff08\u57fa\u6e96\uff09' : ''}</td><td>${esc(row.direction)}</td><td>${pct(row.probability_up)}</td><td>${actual}</td><td>${dateTime(row.generated_at)}</td><td><span class="status-pill ${status}">${labels[status] || status}</span></td><td>${esc(row.reason || '\u2014')}</td></tr>`;
      }).join('');
    };
    renderRecords('DAY', 'dayRecordsBody', 'dayRecordsEmpty');
    renderRecords('NIGHT', 'nightRecordsBody', 'nightRecordsEmpty');
    state.textContent = `\u5df2\u8f09\u5165${sourceLabel}`; state.className = 'ml-state is-ready';
  }

  $('factorTaskSelect')?.addEventListener('change', () => { if (loadedData) renderFactors(loadedData); });
  fetch(DATA_URL, { cache: 'no-store' }).then(response => { if (!response.ok) throw Error(`http_${response.status}`); return response.json(); }).then(normalize).then(data => { loadedData = data; render(data); }).catch(error => {
    const prefix = realMode ? '\u771f\u5be6\u8cc7\u6599\u8b80\u53d6\u5931\u6557\uff1b\u4e0d\u6703\u81ea\u52d5\u6539\u7528\u793a\u7bc4\u8cc7\u6599' : '\u793a\u7bc4\u8cc7\u6599\u8b80\u53d6\u5931\u6557';
    state.textContent = `${prefix}\uff1a${error.message}`; state.className = 'ml-state is-error'; $('modelCards').innerHTML = '<p class="ml-empty">\u7121\u6cd5\u986f\u793a\u9810\u6e2c\u3002</p>'; $('dayRecordsEmpty').hidden = false; $('nightRecordsEmpty').hidden = false;
  });
})();
