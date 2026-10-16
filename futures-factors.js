(() => {
  "use strict";
  const container = document.getElementById("futuresFactors");
  if (!container) return;
  const category = { semiconductor: "半導體", equities: "美股", volatility: "波動", rates: "利率", fx: "外匯", macro_event: "總經" };
  const relation = { positive: "同向", inverse: "反向", regime_dependent: "依市場環境而異" };
  const number = (value, suffix = "") => value == null ? "—" : `${Number(value).toLocaleString("zh-TW", { maximumFractionDigits: 4 })}${suffix}`;
  const fetchJson = async (path) => { const response = await fetch(path, { cache: "no-store" }); if (!response.ok) throw new Error(String(response.status)); return response.json(); };
  const marketRecord = (payload) => payload?.data?.records?.[0] || null;
  async function resolve(factor) {
    if (!factor.data_source) return factor;
    try {
      const payload = await fetchJson(`./${factor.data_source}`);
      if (factor.factor_id === "US10Y") {
        const market = payload?.markets?.united_states, row = market?.yields?.find(item => item.tenor === "10Y");
        return { ...factor, status: row ? "available" : "unavailable", latest_value: row?.yield ?? null, latest_change: null, source_date: row?.source_date ?? market?.data_date ?? null, observed_at: row?.source_date ?? market?.data_date ?? null, available_at: null, fetched_at: payload?.fetched_at ?? null, source_name: "U.S. Treasury" };
      }
      const row = marketRecord(payload);
      return { ...factor, status: row ? "available" : "unavailable", latest_value: row?.close ?? null, latest_change: row?.change_percent ?? null, source_date: row?.trade_date ?? null, observed_at: row?.trade_date ?? null, available_at: null, fetched_at: payload?.updated_at ?? null, source_name: payload?.provider ?? row?.metadata?.source ?? "—" };
    } catch { return { ...factor, status: "unavailable" }; }
  }
  function render(rows) {
    const max = Math.max(...rows.map(item => item.effective_weight));
    container.innerHTML = rows.map(item => {
      const available = item.status === "available", change = item.latest_change;
      const tone = change > 0 ? "factor-positive" : change < 0 ? "factor-negative" : "";
      const value = item.factor_id === "US10Y" ? number(item.latest_value, "%") : number(item.latest_value);
      const delta = change == null ? "—" : `${change > 0 ? "+" : ""}${number(change, "%")}`;
      return `<article class="factor-row"><div class="factor-main"><div><strong>${item.label}</strong><span>${category[item.category] || item.category}</span></div><b>${Math.round(item.effective_weight * 100)}%</b></div><div class="factor-weight" role="img" aria-label="${item.label} 初始權重 ${Math.round(item.effective_weight * 100)}%"><i style="width:${item.effective_weight / max * 100}%"></i></div><dl><div><dt>目前值</dt><dd>${available ? value : "尚未提供"}</dd></div><div><dt>變化</dt><dd class="${tone}">${available ? delta : "—"}</dd></div><div><dt>關係</dt><dd>${relation[item.relation] || item.relation}</dd></div><div><dt>資料日期</dt><dd>${item.source_date || "—"}</dd></div><div><dt>來源</dt><dd>${item.source_name || "—"}</dd></div><div><dt>狀態</dt><dd>${available ? "資料可用" : "尚未提供"}</dd></div></dl></article>`;
    }).join("");
    document.getElementById("factorState").textContent = `現有資料 ${rows.filter(item => item.status === "available").length} / ${rows.length} 項`;
  }
  fetchJson("./config/futures_factor_config.json").then(async config => render(await Promise.all(config.factors.map(resolve)))).catch(() => { container.innerHTML = '<p class="factor-error">目前無法取得市場影響因子設定</p>'; document.getElementById("factorState").textContent = "載入失敗"; });
})();
