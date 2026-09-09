(() => {
  "use strict";
  const state = { banks: [], query: "", rateType: "fixed", sorts: {} };
  const search = document.getElementById("bankSearch");
  const status = document.getElementById("depositState");
  const sourceDate = document.getElementById("sourceDate");
  const formatRate = (value) => Number.isFinite(value) ? `${value.toFixed(3)}%` : "—";
  const valueFor = (bank, table, key) => {
    if (key === "bank_name") return bank.bank_name;
    if (table === "demandTable") return bank.demand?.[key] ?? null;
    const group = table === "timeDepositTable" ? bank.time_deposit : bank.time_savings_deposit;
    return group?.[key]?.[state.rateType] ?? null;
  };
  const visibleBanks = (table) => {
    const filtered = state.banks.filter((bank) => bank.bank_name.includes(state.query));
    const sort = state.sorts[table];
    if (!sort) return filtered;
    return [...filtered].sort((left, right) => {
      const a = valueFor(left, table, sort.key), b = valueFor(right, table, sort.key);
      if (a == null) return 1;
      if (b == null) return -1;
      const result = typeof a === "string" ? a.localeCompare(b, "zh-Hant") : a - b;
      return sort.direction === "asc" ? result : -result;
    });
  };
  const renderTable = (id) => {
    const table = document.getElementById(id);
    const keys = [...table.querySelectorAll("th")].map((header) => header.dataset.sort);
    table.querySelector("tbody").innerHTML = visibleBanks(id).map((bank) => `<tr>${keys.map((key) => `<td>${key === "bank_name" ? bank.bank_name : formatRate(valueFor(bank, id, key))}</td>`).join("")}</tr>`).join("");
  };
  const render = () => {
    ["demandTable", "timeDepositTable", "timeSavingsTable"].forEach(renderTable);
    status.textContent = `顯示 ${state.banks.filter((bank) => bank.bank_name.includes(state.query)).length} 家銀行`;
  };
  document.querySelectorAll("th[data-sort]").forEach((header) => header.addEventListener("click", () => {
    const table = header.closest("table").id;
    const current = state.sorts[table];
    state.sorts[table] = { key: header.dataset.sort, direction: current?.key === header.dataset.sort && current.direction === "desc" ? "asc" : "desc" };
    renderTable(table);
  }));
  search.addEventListener("input", () => { state.query = search.value.trim(); render(); });
  document.querySelectorAll('input[name="rateType"]').forEach((input) => input.addEventListener("change", () => { state.rateType = input.value; render(); }));
  fetch("./data/deposits/bank_rates.json", { cache: "no-cache" }).then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }).then((payload) => {
    if (!Array.isArray(payload.banks)) throw new Error("invalid schema");
    state.banks = payload.banks;
    sourceDate.textContent = payload.source_date || "—";
    sourceDate.dateTime = payload.source_date || "";
    render();
  }).catch(() => { status.textContent = "目前無法取得牌告利率資料"; });
})();
