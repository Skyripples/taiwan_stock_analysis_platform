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
    const width = 720, height = 300, left = 62, right = 25, top = 28, bottom = 48;
    const yields = values.map((item) => item.yield);
    let minimum = Math.min(...yields), maximum = Math.max(...yields);
    const padding = Math.max((maximum - minimum) * 0.18, 0.1);
    minimum -= padding; maximum += padding;
    const x = (index) => left + index * (width - left - right) / Math.max(values.length - 1, 1);
    const y = (value) => top + (maximum - value) * (height - top - bottom) / (maximum - minimum);
    const grid = [0, 1, 2, 3].map((step) => {
      const value = minimum + (maximum - minimum) * step / 3;
      const position = y(value);
      return `<line class="bond-grid-line" x1="${left}" y1="${position}" x2="${width - right}" y2="${position}"/><text class="bond-axis-label" x="${left - 9}" y="${position + 4}" text-anchor="end">${value.toFixed(2)}%</text>`;
    }).join("");
    const points = values.map((item, index) => `${x(index)},${y(item.yield)}`).join(" ");
    const labels = values.map((item, index) => `<circle class="bond-curve-point" cx="${x(index)}" cy="${y(item.yield)}" r="5"/><text class="bond-point-label" x="${x(index)}" y="${height - 18}" text-anchor="middle">${escapeHtml(item.tenor)}</text><text class="bond-point-label" x="${x(index)}" y="${y(item.yield) - 12}" text-anchor="middle">${item.yield.toFixed(3)}%</text>`).join("");
    svg.innerHTML = `${grid}<polyline class="bond-curve-line" points="${points}"/>${labels}`;
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
