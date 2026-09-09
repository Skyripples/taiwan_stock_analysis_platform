(() => {
  "use strict";
  const state = { rates: [], query: "", sort: { key: "currency", direction: "asc" } };
  const search = document.getElementById("currencySearch");
  const status = document.getElementById("forexState");
  const tableBody = document.querySelector("#forexTable tbody");
  const amount = document.getElementById("exchangeAmount");
  const from = document.getElementById("fromCurrency");
  const to = document.getElementById("toCurrency");
  const result = document.getElementById("exchangeResult");
  const sourceDate = document.getElementById("sourceDate");
  const fetchedAt = document.getElementById("fetchedAt");

  const formatNumber = (value, digits = 6) => Number.isFinite(value)
    ? new Intl.NumberFormat("zh-TW", { maximumFractionDigits: digits }).format(value) : "—";
  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
  const visibleRates = () => {
    const query = state.query.toLocaleLowerCase("zh-Hant");
    const filtered = state.rates.filter((item) => `${item.currency} ${item.name}`.toLocaleLowerCase("zh-Hant").includes(query));
    return [...filtered].sort((left, right) => {
      const a = left[state.sort.key], b = right[state.sort.key];
      if (a == null) return 1;
      if (b == null) return -1;
      const order = typeof a === "number" ? a - b : String(a).localeCompare(String(b), "zh-Hant");
      return state.sort.direction === "asc" ? order : -order;
    });
  };
  const renderTable = () => {
    const rows = visibleRates();
    tableBody.innerHTML = rows.map((item) => `<tr><td>${escapeHtml(item.currency)}</td><td>${escapeHtml(item.name)}</td><td>${formatNumber(item.twd_rate)}</td><td>${escapeHtml(item.source_date || "—")}</td></tr>`).join("");
    status.textContent = `顯示 ${rows.length} 種貨幣`;
  };
  const convert = () => {
    const value = Number(amount.value), fromRate = Number(from.value), toRate = Number(to.value);
    if (!Number.isFinite(value) || value < 0 || !Number.isFinite(fromRate) || !Number.isFinite(toRate) || toRate <= 0) {
      result.textContent = "請輸入有效金額";
      return;
    }
    const converted = value * fromRate / toRate;
    result.textContent = `${formatNumber(value)} ${from.selectedOptions[0].dataset.code} = ${formatNumber(converted)} ${to.selectedOptions[0].dataset.code}`;
  };
  const renderConverter = () => {
    const currencies = [{ currency: "TWD", name: "新台幣", twd_rate: 1 }, ...state.rates];
    const options = currencies.map((item) => `<option value="${item.twd_rate}" data-code="${item.currency}">${item.currency}｜${item.name}</option>`).join("");
    from.innerHTML = options;
    to.innerHTML = options;
    from.value = String(state.rates.find((item) => item.currency === "USD")?.twd_rate || 1);
    to.value = "1";
    convert();
  };

  document.querySelectorAll("#forexTable th[data-sort]").forEach((header) => header.addEventListener("click", () => {
    const key = header.dataset.sort;
    state.sort = { key, direction: state.sort.key === key && state.sort.direction === "desc" ? "asc" : "desc" };
    renderTable();
  }));
  search.addEventListener("input", () => { state.query = search.value.trim(); renderTable(); });
  [amount, from, to].forEach((element) => element.addEventListener(element === amount ? "input" : "change", convert));

  fetch("./data/forex/exchange_rates.json", { cache: "no-cache" }).then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }).then((payload) => {
    if (!Array.isArray(payload.rates)) throw new Error("invalid schema");
    state.rates = payload.rates;
    const displayDate = payload.data_date || payload.latest_source_date || "";
    sourceDate.textContent = displayDate || "—";
    sourceDate.dateTime = displayDate;
    const fetched = payload.fetched_at ? new Date(payload.fetched_at) : null;
    fetchedAt.textContent = fetched && !Number.isNaN(fetched.valueOf()) ? fetched.toLocaleString("zh-TW") : "—";
    fetchedAt.dateTime = payload.fetched_at || "";
    renderTable();
    renderConverter();
  }).catch(() => { status.textContent = "目前無法取得匯率資料"; });
})();
