(() => {
  const widget = document.querySelector(".institutional-widget");
  const status = document.getElementById("institutionalStatus");
  const message = document.getElementById("institutionalMessage");
  const values = document.getElementById("institutionalValues");
  const tradeDate = document.getElementById("institutionalTradeDate");
  const futuresWidget = document.querySelector(".futures-widget");
  const futuresStatus = document.getElementById("futuresStatus");
  const futuresMessage = document.getElementById("futuresMessage");
  const futuresValues = document.getElementById("futuresValues");
  const futuresTradeDate = document.getElementById("futuresTradeDate");
  const marketScoreWidget = document.querySelector(".market-score-widget");
  const marketScoreLoadStatus = document.getElementById("marketScoreLoadStatus");
  const marketScoreMessage = document.getElementById("marketScoreMessage");
  const marketScoreContent = document.getElementById("marketScoreContent");
  const marketScoreSummary = document.getElementById("marketScoreSummary");
  const marketScorePercentage = document.getElementById("marketScorePercentage");
  const marketScoreStatus = document.getElementById("marketScoreStatus");
  const marketScoreStatusZh = document.getElementById("marketScoreStatusZh");
  const marketScoreRaw = document.getElementById("marketScoreRaw");
  const marketScoreBar = document.getElementById("marketScoreBar");

  if (
    !widget || !status || !message || !values || !tradeDate ||
    !futuresWidget || !futuresStatus || !futuresMessage || !futuresValues || !futuresTradeDate
  ) return;

  const valueTargets = {
    foreign: document.getElementById("foreignNet"),
    trust: document.getElementById("trustNet"),
    dealer: document.getElementById("dealerNet"),
    total: document.getElementById("institutionalTotalNet"),
  };
  const futuresTargets = {
    long: document.getElementById("futuresLongPosition"),
    short: document.getElementById("futuresShortPosition"),
    net: document.getElementById("futuresNetPosition"),
    netAmount: document.getElementById("futuresNetAmount"),
  };
  const signalTargets = {
    foreignCashFlow: {
      value: document.getElementById("cashFlowSignalValue"),
      status: document.getElementById("cashFlowSignalStatus"),
      score: document.getElementById("cashFlowSignalScore"),
    },
    foreignFuturesPosition: {
      value: document.getElementById("futuresSignalValue"),
      status: document.getElementById("futuresSignalStatus"),
      score: document.getElementById("futuresSignalScore"),
    },
  };
  const integerFormatter = new Intl.NumberFormat("zh-TW", {
    maximumFractionDigits: 0,
  });
  const decimalFormatter = new Intl.NumberFormat("zh-TW", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const percentageFormatter = new Intl.NumberFormat("zh-TW", {
    maximumFractionDigits: 2,
  });

  class MissingDataError extends Error {}

  const MARKET_STATUS = {
    "Strong Bullish": { label: "強勢偏多", tone: "positive" },
    Bullish: { label: "偏多", tone: "positive" },
    Neutral: { label: "中性", tone: "neutral" },
    Bearish: { label: "偏空", tone: "negative" },
    "Strong Bearish": { label: "強勢偏空", tone: "negative" },
  };
  const SIGNAL_STATUS = {
    bullish: { label: "偏多", tone: "positive" },
    neutral: { label: "中性", tone: "neutral" },
    bearish: { label: "偏空", tone: "negative" },
  };

  function requireInteger(value, fieldName) {
    if (!Number.isSafeInteger(value)) {
      throw new MissingDataError(`缺少 ${fieldName}`);
    }
    return value;
  }

  function formatBillions(value) {
    const sign = value > 0 ? "+" : value < 0 ? "-" : "";
    return `${sign}${(Math.abs(value) / 100000000).toFixed(2)} 億`;
  }

  function valueClass(value) {
    if (value > 0) return "is-positive";
    if (value < 0) return "is-negative";
    return "is-neutral";
  }

  function renderValue(target, value) {
    if (!target) throw new MissingDataError("缺少數值顯示元件");
    target.textContent = formatBillions(value);
    target.classList.remove("is-positive", "is-negative", "is-neutral");
    target.classList.add(valueClass(value));
  }

  function formatContracts(value, showPositiveSign = false) {
    const sign = showPositiveSign && value > 0 ? "+" : "";
    return `${sign}${integerFormatter.format(value)} 口`;
  }

  function formatThousandsToBillions(value) {
    const sign = value > 0 ? "+" : value < 0 ? "-" : "";
    return `${sign}${decimalFormatter.format(Math.abs(value) / 100000)} 億`;
  }

  function renderFuturesValue(target, text, classificationValue) {
    if (!target) throw new MissingDataError("缺少期貨數值顯示元件");
    target.textContent = text;
    target.classList.remove("is-positive", "is-negative", "is-neutral");
    target.classList.add(valueClass(classificationValue));
  }

  function formatSignedInteger(value) {
    const sign = value > 0 ? "+" : "";
    return `${sign}${integerFormatter.format(value)}`;
  }

  function readMarketScore(payload) {
    const score = payload?.market_score;
    if (!score || typeof score !== "object") {
      throw new MissingDataError("缺少 market_score");
    }

    const rawScore = requireInteger(score.score, "市場原始分數");
    const maxScore = requireInteger(score.max_score, "市場最高分數");
    if (
      maxScore <= 0 || Math.abs(rawScore) > maxScore ||
      typeof score.percentage !== "number" || !Number.isFinite(score.percentage) ||
      score.percentage < 0 || score.percentage > 100 ||
      !Object.hasOwn(MARKET_STATUS, score.status)
    ) {
      throw new MissingDataError("市場評分欄位格式不完整");
    }

    return {
      score: rawScore,
      maxScore,
      percentage: score.percentage,
      status: score.status,
    };
  }

  function readSignal(signal) {
    if (!signal || typeof signal !== "object") {
      throw new MissingDataError("缺少市場訊號");
    }

    const value = requireInteger(signal.value, "訊號數值");
    const score = requireInteger(signal.score, "訊號分數");
    if (!Object.hasOwn(SIGNAL_STATUS, signal.status)) {
      throw new MissingDataError("缺少訊號狀態");
    }
    return { value, status: signal.status, score };
  }

  function renderSignal(targets, signal, valueFormatter) {
    if (!targets.value || !targets.status || !targets.score) {
      throw new MissingDataError("缺少訊號顯示元件");
    }

    const statusInfo = SIGNAL_STATUS[signal.status];
    targets.value.textContent = valueFormatter(signal.value);
    targets.status.textContent = `${signal.status}（${statusInfo.label}）`;
    targets.score.textContent = formatSignedInteger(signal.score);
    [targets.value, targets.status, targets.score].forEach((target) => {
      target.classList.remove("is-positive", "is-negative", "is-neutral", "is-missing");
      target.classList.add(`is-${statusInfo.tone}`);
    });
  }

  function renderMissingSignal(targets) {
    if (!targets.value || !targets.status || !targets.score) return;
    targets.value.textContent = "--";
    targets.status.textContent = "資料缺漏";
    targets.score.textContent = "--";
    [targets.value, targets.status, targets.score].forEach((target) => {
      target.classList.remove("is-positive", "is-negative", "is-neutral");
      target.classList.add("is-missing");
    });
  }

  function renderSignalSafely(signal, targets, valueFormatter) {
    try {
      renderSignal(targets, readSignal(signal), valueFormatter);
    } catch (error) {
      renderMissingSignal(targets);
    }
  }

  function showMarketScoreError(statusText, messageText) {
    marketScoreWidget.dataset.state = "error";
    marketScoreLoadStatus.textContent = statusText;
    marketScoreMessage.textContent = messageText;
    marketScoreMessage.hidden = false;
    marketScoreContent.hidden = true;
  }

  async function loadMarketScore() {
    if (
      !marketScoreWidget || !marketScoreLoadStatus || !marketScoreMessage ||
      !marketScoreContent || !marketScoreSummary || !marketScorePercentage ||
      !marketScoreStatus || !marketScoreStatusZh || !marketScoreRaw || !marketScoreBar
    ) return;

    try {
      const response = await fetch("./data/market/market_signals.json", {
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const payload = await response.json();
      const score = readMarketScore(payload);
      const statusInfo = MARKET_STATUS[score.status];

      marketScorePercentage.textContent = `${percentageFormatter.format(score.percentage)}%`;
      marketScoreStatus.textContent = score.status;
      marketScoreStatusZh.textContent = statusInfo.label;
      marketScoreRaw.textContent = `${formatSignedInteger(score.score)} / ${integerFormatter.format(score.maxScore)}`;
      marketScoreBar.style.setProperty("--score-percentage", `${score.percentage}%`);
      marketScoreBar.setAttribute("aria-valuenow", String(score.percentage));
      marketScoreSummary.dataset.tone = statusInfo.tone;
      marketScoreBar.dataset.tone = statusInfo.tone;

      renderSignalSafely(
        payload?.signals?.foreign_cash_flow,
        signalTargets.foreignCashFlow,
        formatBillions,
      );
      renderSignalSafely(
        payload?.signals?.foreign_futures_position,
        signalTargets.foreignFuturesPosition,
        (value) => formatContracts(value, true),
      );

      marketScoreWidget.dataset.state = "success";
      marketScoreLoadStatus.textContent = "資料已載入";
      marketScoreMessage.hidden = true;
      marketScoreContent.hidden = false;
    } catch (error) {
      if (error instanceof MissingDataError) {
        showMarketScoreError("資料缺漏", "市場評分資料格式不完整，請稍後再試。");
      } else {
        showMarketScoreError("讀取失敗", "目前無法讀取市場評分資料，請稍後再試。");
      }
    }
  }

  function readRecord(payload) {
    const record = payload?.data?.records?.[0];
    if (!record || typeof record !== "object") {
      throw new MissingDataError("缺少三大法人資料紀錄");
    }
    if (typeof record.trade_date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(record.trade_date)) {
      throw new MissingDataError("缺少交易日期");
    }
    return {
      date: record.trade_date,
      foreign: requireInteger(record.foreign_and_mainland_investors?.net, "外資及陸資買賣超"),
      trust: requireInteger(record.investment_trust?.net, "投信買賣超"),
      dealer: requireInteger(record.dealers?.net, "自營商買賣超"),
      total: requireInteger(record.total?.net, "三大法人合計買賣超"),
    };
  }

  function showError(statusText, messageText) {
    widget.dataset.state = "error";
    status.textContent = statusText;
    message.textContent = messageText;
    message.hidden = false;
    values.hidden = true;
    tradeDate.hidden = true;
  }

  function readFuturesRecord(payload) {
    const record = payload?.data?.records?.[0];
    if (!record || typeof record !== "object") {
      throw new MissingDataError("缺少外資台指期資料紀錄");
    }
    if (typeof record.trade_date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(record.trade_date)) {
      throw new MissingDataError("缺少期貨交易日期");
    }

    const metadata = record.metadata;
    if (
      !metadata || typeof metadata.product_name !== "string" ||
      typeof metadata.product_code !== "string" || typeof metadata.investor_type !== "string" ||
      metadata.position_unit !== "口" || metadata.contract_amount_unit !== "千元"
    ) {
      throw new MissingDataError("缺少期貨商品或單位資訊");
    }

    return {
      date: record.trade_date,
      productName: metadata.product_name,
      productCode: metadata.product_code,
      investorType: metadata.investor_type,
      long: requireInteger(record.long_position?.open_interest, "多方未平倉口數"),
      short: requireInteger(record.short_position?.open_interest, "空方未平倉口數"),
      net: requireInteger(record.net_position?.open_interest, "淨未平倉口數"),
      netAmount: requireInteger(record.net_position?.contract_amount, "淨契約金額"),
    };
  }

  function showFuturesError(statusText, messageText) {
    futuresWidget.dataset.state = "error";
    futuresStatus.textContent = statusText;
    futuresMessage.textContent = messageText;
    futuresMessage.hidden = false;
    futuresValues.hidden = true;
    futuresTradeDate.hidden = true;
  }

  async function loadInstitutionalInvestors() {
    try {
      const response = await fetch("./data/market/institutional_investors.json", {
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const payload = await response.json();
      const record = readRecord(payload);

      renderValue(valueTargets.foreign, record.foreign);
      renderValue(valueTargets.trust, record.trust);
      renderValue(valueTargets.dealer, record.dealer);
      renderValue(valueTargets.total, record.total);

      tradeDate.textContent = `交易日期：${record.date}`;
      widget.dataset.state = "success";
      status.textContent = "資料已載入";
      message.hidden = true;
      values.hidden = false;
      tradeDate.hidden = false;
    } catch (error) {
      if (error instanceof MissingDataError) {
        showError("資料缺漏", "三大法人資料格式不完整，請稍後再試。");
      } else {
        showError("讀取失敗", "目前無法讀取三大法人資料，請稍後再試。");
      }
    }
  }

  async function loadForeignFuturesPosition() {
    try {
      const response = await fetch("./data/market/foreign_futures_position.json", {
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const payload = await response.json();
      const record = readFuturesRecord(payload);

      renderFuturesValue(futuresTargets.long, formatContracts(record.long), 0);
      renderFuturesValue(futuresTargets.short, formatContracts(record.short), 0);
      renderFuturesValue(futuresTargets.net, formatContracts(record.net, true), record.net);
      renderFuturesValue(
        futuresTargets.netAmount,
        formatThousandsToBillions(record.netAmount),
        record.netAmount,
      );

      futuresMessage.textContent = `${record.productName}（${record.productCode}）｜投資人：${record.investorType}`;
      futuresTradeDate.textContent = `交易日期：${record.date}`;
      futuresWidget.dataset.state = "success";
      futuresStatus.textContent = "資料已載入";
      futuresMessage.hidden = false;
      futuresValues.hidden = false;
      futuresTradeDate.hidden = false;
    } catch (error) {
      if (error instanceof MissingDataError) {
        showFuturesError("資料缺漏", "外資台指期資料格式不完整，請稍後再試。");
      } else {
        showFuturesError("讀取失敗", "目前無法讀取外資台指期資料，請稍後再試。");
      }
    }
  }

  loadInstitutionalInvestors();
  loadForeignFuturesPosition();
  loadMarketScore();
})();
