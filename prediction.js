(() => {
  'use strict';

  const SIGNALS = [
    ['foreign_cash_flow', value => `${formatSigned(value / 100000000, 2)} 億元`],
    ['foreign_futures_position', value => `${formatSigned(value, 0)} 口`],
    ['night_futures', value => `${formatSigned(value, 2)} 點`],
    ['tsm_adr', value => `${formatSigned(value, 2)} 美元`],
    ['sox_index', value => `${formatSigned(value, 2)}%`]
  ];
  const STATUS_LABELS = {
    'Strong Bullish': '強勢偏多', Bullish: '偏多', Neutral: '中性', Bearish: '偏空', 'Strong Bearish': '強勢偏空',
    bullish: '偏多', neutral: '中性', bearish: '偏空'
  };
  const modelVersions = { info: null, prediction: null };
  const byId = id => document.getElementById(id);

  class DataFormatError extends Error {}

  function formatSigned(value, digits) {
    if (!Number.isFinite(value)) return '資料缺漏';
    const formatted = Math.abs(value).toLocaleString('zh-TW', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return `${value > 0 ? '+' : value < 0 ? '-' : ''}${formatted}`;
  }

  function formatPercent(value) {
    return `${(value * 100).toFixed(2)}%`;
  }

  function formatDateTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-TW');
  }

  function tone(status) {
    const normalized = String(status || '').toLowerCase();
    if (normalized.includes('bullish') || normalized === 'up') return 'tone-bullish';
    if (normalized.includes('bearish') || normalized === 'down') return 'tone-bearish';
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
    if (!nonEmpty.length) throw new DataFormatError('CSV 沒有標頭');
    const headers = nonEmpty[0].map(header => header.replace(/^\uFEFF/, '').trim());
    return nonEmpty.slice(1).map(cells => Object.fromEntries(headers.map((header, index) => [header, (cells[index] || '').trim()])));
  }

  async function fetchText(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.text();
  }

  async function fetchOptionalJson(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (response.status === 404) return { state: 'missing', data: null };
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    try {
      return { state: 'ready', data: JSON.parse(await response.text()) };
    } catch (error) {
      throw new DataFormatError('JSON 格式錯誤');
    }
  }

  function requireNumber(payload, key, options = {}) {
    const value = Number(payload?.[key]);
    if (!Number.isFinite(value) || (options.probability && (value < 0 || value > 1))) {
      throw new DataFormatError(`${key} 欄位錯誤`);
    }
    return value;
  }

  function validateFactorConfig(config) {
    if (!config || typeof config !== 'object') throw new DataFormatError('因子設定格式錯誤');
    for (const [key] of SIGNALS) {
      const setting = config[key];
      if (!setting || typeof setting.enabled !== 'boolean' || !Number.isFinite(Number(setting.weight)) || Number(setting.weight) < 0 || !setting.display_name || !setting.description) {
        throw new DataFormatError(`因子設定缺漏：${key}`);
      }
    }
    return config;
  }

  function renderSignals(payload, factorConfig) {
    const score = payload.market_score;
    if (!score || !Number.isFinite(Number(score.score)) || !Number.isFinite(Number(score.max_score)) || !Number.isFinite(Number(score.percentage)) || !score.status) throw new DataFormatError('market_score 欄位缺漏');
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
    byId('signalGrid').replaceChildren(...SIGNALS.map(([key, formatter]) => {
      const item = document.createElement('div');
      item.className = 'signal-item';
      const signal = signals[key];
      const setting = factorConfig[key];
      if (!signal || !Number.isFinite(Number(signal.value)) || !Number.isFinite(Number(signal.score)) || !Number.isFinite(Number(signal.weight)) || !Number.isFinite(Number(signal.weighted_score)) || !signal.status || signal.enabled !== setting.enabled || Number(signal.weight) !== Number(setting.weight)) {
        item.innerHTML = `<span>${setting.display_name}</span><strong>資料缺漏</strong><small>--</small>`;
        return item;
      }
      const signalTone = tone(signal.status);
      const signedScore = Number(signal.score) > 0 ? `+${signal.score}` : String(signal.score);
      const weightedScore = Number(signal.weighted_score) > 0 ? `+${signal.weighted_score}` : String(signal.weighted_score);
      const stateText = setting.enabled ? `${STATUS_LABELS[signal.status] || signal.status} · score ${signedScore}` : '已停用';
      item.classList.toggle('is-disabled', !setting.enabled);
      item.title = setting.description;
      item.innerHTML = `<span>${setting.display_name}</span><strong class="${setting.enabled ? signalTone : 'tone-neutral'}">${formatter(Number(signal.value))}</strong><small>${stateText}<br>權重 ${signal.weight} · weighted ${weightedScore}</small>`;
      return item;
    }));
    byId('marketLoadStatus').textContent = '已載入';
    byId('marketStateWidget').dataset.state = 'success';
    const updated = new Date(payload.updated_at);
    byId('lastUpdatedAt').textContent = Number.isNaN(updated.getTime()) ? (payload.updated_at || '資料缺漏') : updated.toLocaleString('zh-TW');
  }

  async function loadSignals() {
    try {
      const [signalsPayload, factorConfig] = await Promise.all([
        fetchText('./data/market/market_signals.json').then(text => JSON.parse(text)),
        fetchText('./config/factor_config.json').then(text => JSON.parse(text))
      ]);
      renderSignals(signalsPayload, validateFactorConfig(factorConfig));
    } catch (error) {
      byId('marketLoadStatus').textContent = error instanceof DataFormatError || error instanceof SyntaxError ? '資料格式錯誤' : '讀取失敗';
      byId('marketStateWidget').dataset.state = 'error';
      byId('signalGrid').innerHTML = '<p class="widget-description">市場訊號目前無法顯示，其他資料仍可正常載入。</p>';
      byId('lastUpdatedAt').textContent = '讀取失敗';
    }
  }

  function renderModelMissing() {
    byId('modelFileStatus').textContent = '尚未建立';
    byId('modelStatusText').textContent = '模型狀態：尚未建立';
    byId('modelStatusReason').textContent = '原因：訓練樣本未達 200 筆';
    byId('modelInfoGrid').hidden = true;
    byId('modelStatusWidget').dataset.state = 'missing';
  }

  function validateModelInfo(data) {
    const requiredText = ['algorithm', 'model_version', 'trained_at'];
    if (requiredText.some(key => !data?.[key])) throw new DataFormatError('模型資訊欄位缺漏');
    ['sample_count', 'accuracy', 'baseline_accuracy', 'precision', 'recall', 'f1'].forEach(key => requireNumber(data, key));
    if (!Array.isArray(data.feature_names) || data.feature_names.length !== 15 || data.feature_names.includes('unchanged') || !data.feature_names.includes('vix_change_percent') || !data.feature_names.includes('kospi_change_percent')) throw new DataFormatError('feature_names 格式錯誤');
    if (data.calibration_method && data.calibration_method !== 'platt') throw new DataFormatError('calibration_method 格式錯誤');
    if (!Array.isArray(data.confusion_matrix) || data.confusion_matrix.length !== 2 || data.confusion_matrix.some(row => !Array.isArray(row) || row.length !== 2 || row.some(value => !Number.isFinite(Number(value))))) {
      throw new DataFormatError('confusion_matrix 格式錯誤');
    }
  }

  function renderModelInfo(data) {
    validateModelInfo(data);
    byId('modelFileStatus').textContent = '已建立';
    byId('modelStatusText').textContent = '模型狀態：可使用';
    byId('modelStatusReason').textContent = '已完成訓練與評估';
    byId('modelName').textContent = data.algorithm;
    byId('modelVersion').textContent = data.model_version;
    byId('modelSampleCount').textContent = `${Number(data.sample_count).toLocaleString('zh-TW')} 筆`;
    byId('modelFeatureCount').textContent = `${data.feature_names.length} Features`;
    byId('modelTrainedAt').textContent = formatDateTime(data.trained_at);
    byId('modelAccuracy').textContent = formatPercent(Number(data.accuracy));
    byId('modelBaselineAccuracy').textContent = formatPercent(Number(data.baseline_accuracy));
    byId('modelPrecision').textContent = formatPercent(Number(data.precision));
    byId('modelRecall').textContent = formatPercent(Number(data.recall));
    byId('modelF1').textContent = formatPercent(Number(data.f1));
    byId('modelCalibrationMethod').textContent = data.calibration_method === 'platt' ? 'Platt / Sigmoid' : '未校準';
    byId('modelCalibrationSamples').textContent = Number.isFinite(Number(data.calibration_fit_samples)) ? `${Number(data.calibration_fit_samples).toLocaleString('zh-TW')} 筆` : '--';
    byId('modelConfusionMatrix').textContent = `[[${data.confusion_matrix[0].join(', ')}], [${data.confusion_matrix[1].join(', ')}]]`;
    byId('modelFeatureList').textContent = data.feature_names.map(name => {
      if (name === 'vix_change_percent') return 'VIX 漲跌幅';
      if (name === 'kospi_change_percent') return 'KOSPI 漲跌幅';
      return name;
    }).join('、');
    byId('modelInfoGrid').hidden = false;
    byId('modelStatusWidget').dataset.state = 'success';
    modelVersions.info = String(data.model_version);
    updateVersionWarning();
  }

  async function loadModelInfo() {
    try {
      const result = await fetchOptionalJson('./models/model_info.json');
      if (result.state === 'missing') renderModelMissing();
      else renderModelInfo(result.data);
    } catch (error) {
      byId('modelFileStatus').textContent = error instanceof DataFormatError ? '資料格式錯誤' : '讀取失敗';
      byId('modelStatusText').textContent = '模型資訊無法顯示';
      byId('modelStatusReason').textContent = error instanceof DataFormatError ? '模型資訊 JSON 格式或欄位錯誤' : '模型資訊讀取失敗';
      byId('modelInfoGrid').hidden = true;
      byId('modelStatusWidget').dataset.state = 'error';
    }
  }

  function renderPredictionMissing() {
    byId('predictionFileStatus').textContent = '尚無模型';
    byId('upProbability').textContent = '尚未建立模型';
    byId('downProbability').textContent = '尚未建立模型';
    byId('rawUpProbability').textContent = '原始機率：--';
    byId('calibrationStatus').textContent = '校準狀態：尚未建立模型';
    byId('predictionDirection').textContent = '等待模型';
    byId('predictionConfidence').textContent = '--';
    byId('predictionFeatureDate').textContent = '尚未產生';
    byId('predictionTargetDate').textContent = '下一個交易日尚未確定';
    byId('predictionModelVersion').textContent = '--';
    byId('predictionGeneratedAt').textContent = '尚未產生';
    byId('confidenceBar').style.width = '0%';
    byId('predictionResultWidget').dataset.state = 'missing';
  }

  function validatePrediction(data) {
    const featureDate = data?.feature_date || data?.prediction_date;
    if (!featureDate || !data?.direction || !data?.model_version || !data?.generated_at) throw new DataFormatError('預測欄位缺漏');
    const rawUp = requireNumber(data, 'raw_up_probability', { probability: true });
    const isCalibrated = data.calibration_status === 'calibrated';
    const up = requireNumber(data, isCalibrated ? 'calibrated_up_probability' : 'up_probability', { probability: true });
    const down = requireNumber(data, isCalibrated ? 'calibrated_down_probability' : 'down_probability', { probability: true });
    const confidence = requireNumber(data, 'confidence', { probability: true });
    if (!['up', 'down'].includes(String(data.direction).toLowerCase()) || Math.abs(up + down - 1) > 0.001) throw new DataFormatError('預測機率或方向格式錯誤');
    if (isCalibrated && data.calibration_method !== 'platt') throw new DataFormatError('校準方法格式錯誤');
    return { featureDate, rawUp, up, down, confidence, isCalibrated };
  }

  function renderPrediction(data) {
    const values = validatePrediction(data);
    const direction = String(data.direction).toLowerCase();
    const directionTone = tone(direction);
    const confidencePercent = values.confidence * 100;
    byId('predictionFileStatus').textContent = '已產生';
    byId('upProbability').textContent = formatPercent(values.up);
    byId('upProbability').className = 'tone-bullish';
    byId('downProbability').textContent = formatPercent(values.down);
    byId('downProbability').className = 'tone-bearish';
    byId('rawUpProbability').textContent = `原始上漲機率：${formatPercent(values.rawUp)}`;
    byId('calibrationStatus').textContent = values.isCalibrated ? '校準狀態：Platt / Sigmoid' : '校準狀態：未校準（使用原始機率）';
    byId('predictionDirection').textContent = direction === 'up' ? '上漲' : '下跌';
    byId('predictionDirection').className = directionTone;
    byId('predictionConfidence').textContent = `${confidencePercent.toFixed(2)}%`;
    byId('confidenceBar').style.width = `${confidencePercent}%`;
    byId('confidenceBar').className = directionTone;
    byId('predictionFeatureDate').textContent = values.featureDate;
    byId('predictionTargetDate').textContent = data.target_date || data.prediction_target_date || '下一個交易日尚未確定';
    byId('predictionModelVersion').textContent = data.model_version;
    byId('predictionGeneratedAt').textContent = formatDateTime(data.generated_at);
    byId('lowConfidenceNote').hidden = values.confidence >= 0.6;
    byId('predictionResultWidget').dataset.state = 'success';
    modelVersions.prediction = String(data.model_version);
    updateVersionWarning();
  }

  async function loadPrediction() {
    try {
      const result = await fetchOptionalJson('./data/prediction/prediction.json');
      if (result.state === 'missing') renderPredictionMissing();
      else renderPrediction(result.data);
    } catch (error) {
      renderPredictionMissing();
      byId('predictionFileStatus').textContent = error instanceof DataFormatError ? '資料格式錯誤' : '讀取失敗';
      byId('predictionResultWidget').dataset.state = 'error';
    }
  }

  function updateVersionWarning() {
    byId('modelVersionWarning').hidden = !(modelVersions.info && modelVersions.prediction && modelVersions.info !== modelVersions.prediction);
  }

  async function loadPredictionDataset() {
    try {
      const rows = parseCsv(await fetchText('./data/history/prediction_dataset.csv'));
      const validRows = rows.filter(row => row.feature_date && row.target_date && row.next_taiex_close !== '' && row.next_taiex_return !== '' && row.target_direction !== '');
      if (validRows.length !== rows.length) throw new DataFormatError('CSV 含不完整資料列');
      const count = validRows.length;
      byId('dailyDatasetRowCount').textContent = `${count} 筆`;
      byId('latestFeatureDate').textContent = count ? validRows.map(row => row.feature_date).sort().at(-1) : '尚無可訓練特徵';
    } catch (error) {
      byId('dailyDatasetRowCount').textContent = error instanceof DataFormatError ? '資料格式錯誤' : '讀取失敗';
      byId('latestFeatureDate').textContent = '讀取失敗';
    }
  }

  async function loadMarketHistory() {
    try {
      const rows = parseCsv(await fetchText('./data/history/market_daily.csv'));
      if (rows.some(row => !row.trade_date)) throw new DataFormatError('trade_date 缺漏');
      byId('dailyHistoryRowCount').textContent = `${rows.length} 筆`;
      byId('latestMarketDate').textContent = rows.length ? rows.map(row => row.trade_date).sort().at(-1) : '尚無歷史資料';
    } catch (error) {
      byId('dailyHistoryRowCount').textContent = '讀取失敗';
      byId('latestMarketDate').textContent = '讀取失敗';
    }
  }

  function accuracyText(rows, minimumCount = 1) {
    if (rows.length < minimumCount) return '資料不足';
    const hits = rows.filter(row => row.hit === 'true').length;
    return `${(hits / rows.length * 100).toFixed(2)}%`;
  }

  async function loadHistoricalDataset() {
    try {
      const rows = parseCsv(await fetchText('./data/history/historical_prediction_dataset.csv'));
      if (rows.some(row => !row.feature_date || !row.target_date || row.target_direction === '')) throw new DataFormatError('歷史訓練 CSV 含不完整資料列');
      const count = rows.length;
      byId('historicalDatasetRowCount').textContent = `${count} 筆`;
      byId('historicalTrainingCount').textContent = `${count} 筆歷史樣本`;
      byId('historicalDatasetLoadStatus').textContent = count ? '已載入' : '0 筆資料';
    } catch (error) {
      const status = error instanceof DataFormatError ? '資料格式錯誤' : '讀取失敗';
      byId('historicalDatasetRowCount').textContent = status;
      byId('historicalTrainingCount').textContent = status;
      byId('historicalDatasetLoadStatus').textContent = status;
    }
  }

  function createHistoryCell(text, className = '') {
    const cell = document.createElement('td');
    cell.textContent = text;
    if (className) cell.className = className;
    return cell;
  }

  function renderPredictionHistory(rows) {
    const seenDates = new Set();
    rows.forEach((row, index) => {
      if (!row.feature_date || seenDates.has(row.feature_date) || !['', 'true', 'false'].includes(row.hit || '')) {
        throw new DataFormatError(`prediction_history 第 ${index + 2} 列格式錯誤`);
      }
      seenDates.add(row.feature_date);
      if (row.hit) {
        if (!row.target_date || !['up', 'down'].includes(row.actual_direction) || !Number.isFinite(Number(row.actual_return))) {
          throw new DataFormatError(`prediction_history 第 ${index + 2} 列驗證資料缺漏`);
        }
      }
    });
    const ordered = [...rows].sort((left, right) => left.feature_date.localeCompare(right.feature_date));
    const completed = ordered.filter(row => row.hit === 'true' || row.hit === 'false');
    byId('accuracySummary').textContent = accuracyText(completed);
    byId('accuracySampleCount').textContent = completed.length ? `${completed.length.toLocaleString('zh-TW')} 筆已驗證` : '尚無已驗證預測';
    byId('accuracy20').textContent = accuracyText(completed.slice(-20), 20);
    byId('accuracy60').textContent = accuracyText(completed.slice(-60), 60);
    byId('accuracyAll').textContent = accuracyText(completed);
    byId('predictionHistoryStatus').textContent = rows.length ? '已載入' : '資料不足';

    const recent = ordered.slice(-20).reverse();
    if (!recent.length) {
      const row = document.createElement('tr');
      const cell = createHistoryCell('資料不足');
      cell.colSpan = 7;
      row.append(cell);
      byId('predictionHistoryBody').replaceChildren(row);
      return;
    }
    byId('predictionHistoryBody').replaceChildren(...recent.map(item => {
      const row = document.createElement('tr');
      const completedRow = item.hit === 'true' || item.hit === 'false';
      const directionLabel = item.predicted_direction === 'up' ? '上漲' : item.predicted_direction === 'down' ? '下跌' : '--';
      const actualLabel = item.actual_direction === 'up' ? '上漲' : item.actual_direction === 'down' ? '下跌' : '等待收盤';
      const resultLabel = item.hit === 'true' ? '命中' : item.hit === 'false' ? '未命中' : '待驗證';
      const resultClass = item.hit === 'true' ? 'history-hit' : item.hit === 'false' ? 'history-miss' : 'history-pending';
      const confidence = Number(item.confidence);
      row.append(
        createHistoryCell(item.feature_date),
        createHistoryCell(item.target_date || '尚未確定'),
        createHistoryCell(directionLabel, item.predicted_direction === 'up' ? 'tone-bullish' : item.predicted_direction === 'down' ? 'tone-bearish' : ''),
        createHistoryCell(Number.isFinite(confidence) ? formatPercent(confidence) : '--'),
        createHistoryCell(completedRow ? `${formatSigned(Number(item.actual_return), 2)}%` : '等待收盤'),
        createHistoryCell(actualLabel),
        createHistoryCell(resultLabel, resultClass)
      );
      return row;
    }));
  }

  async function loadPredictionHistory() {
    try {
      const rows = parseCsv(await fetchText('./data/history/prediction_history.csv'));
      renderPredictionHistory(rows);
    } catch (error) {
      byId('predictionHistoryStatus').textContent = error instanceof DataFormatError ? '資料格式錯誤' : '讀取失敗';
      ['accuracySummary', 'accuracy20', 'accuracy60', 'accuracyAll'].forEach(id => { byId(id).textContent = '資料不足'; });
      byId('accuracySampleCount').textContent = '歷史資料無法讀取';
      const row = document.createElement('tr');
      const cell = createHistoryCell(error instanceof DataFormatError ? '資料格式錯誤' : '讀取失敗');
      cell.colSpan = 7;
      row.append(cell);
      byId('predictionHistoryBody').replaceChildren(row);
    }
  }

  Promise.allSettled([loadSignals(), loadModelInfo(), loadPrediction(), loadPredictionDataset(), loadHistoricalDataset(), loadMarketHistory(), loadPredictionHistory()]);
})();
