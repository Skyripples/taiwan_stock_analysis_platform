(() => {
  "use strict";
  const state = { payload: null, market: "taiwan", sort: { key: "tenor_months", direction: "asc" } };
  const status = document.getElementById("bondState");
  const tableBody = document.querySelector("#bondTable tbody");
  const svg = document.getElementById("yieldCurve");
  const marketName = document.getElementById("marketName");
  const dataDate = document.getElementById("dataDate");
  const spread = document.getElementById("yieldSpread");
  const fetchedAt = document.getElementById("fetchedAt");

  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const market = () => state.payload?.markets?.[state.market] || null;
  const sortedYields = () => {
    const values = [...(market()?.yields || [])];
    return values.sort((left, right) => {
      const a = left[state.sort.key], b = right[state.sort.key];
      const result = typeof a === "number" ? a - b : String(a).localeCompare(String(b));
      return state.sort.direction === "asc" ? result : -result;
    });
  };
  const renderCurve = (observations) => {
    if (!observations.length) { svg.innerHTML = ""; return; }
    const values = [...observations].sort((a, b) => a.tenor_months - b.tenor_months);
    PlatformXYChart.svg(svg, { width: 720, height: 300, series: [{ values: values.map((item) => item.yield), className: "bond-curve-line" }], xValues: values.map((item) => item.tenor), xFormatter: String, yFormatter: (value) => `${value.toFixed(2)}%`, xLabel: "期限", yLabel: "殖利率（%）" });
  };
  const render = () => {
    const current = market();
    if (!current) return;
    const rows = sortedYields();
    marketName.textContent = current.name;
    dataDate.textContent = current.data_date || "—";
    spread.textContent = Number.isFinite(current.spread_10y_2y) ? `${current.spread_10y_2y >= 0 ? "+" : ""}${current.spread_10y_2y.toFixed(3)}%` : "—";
    tableBody.innerHTML = rows.map((item) => `<tr><td>${escapeHtml(item.tenor)}</td><td>${item.yield.toFixed(4)}%</td><td>${escapeHtml(item.source_date || "—")}</td></tr>`).join("");
    renderCurve(current.yields);
    status.textContent = `顯示 ${rows.length} 個期限`;
  };
  document.querySelectorAll("[data-market]").forEach((button) => button.addEventListener("click", () => {
    state.market = button.dataset.market;
    document.querySelectorAll("[data-market]").forEach((item) => { const active = item === button; item.classList.toggle("is-active", active); item.setAttribute("aria-pressed", String(active)); });
    render();
  }));
  document.querySelectorAll("#bondTable th[data-sort]").forEach((header) => header.addEventListener("click", () => {
    const key = header.dataset.sort;
    state.sort = { key, direction: state.sort.key === key && state.sort.direction === "desc" ? "asc" : "desc" };
    render();
  }));
  fetch("./data/bonds/bond_yields.json", { cache: "no-cache" }).then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }).then((payload) => {
    if (!payload.markets?.taiwan || !payload.markets?.united_states) throw new Error("invalid schema");
    state.payload = payload;
    const updated = payload.fetched_at ? new Date(payload.fetched_at) : null;
    fetchedAt.textContent = updated && !Number.isNaN(updated.valueOf()) ? updated.toLocaleString("zh-TW") : "—";
    fetchedAt.dateTime = payload.fetched_at || "";
    render();
  }).catch(() => { status.textContent = "目前無法取得公債殖利率資料"; });
})();
