(() => {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const compactNumber = (value) => new Intl.NumberFormat("zh-TW", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  const escape = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const ticks = (minimum, maximum, count = 4) => Array.from({ length: count + 1 }, (_, index) => minimum + (maximum - minimum) * index / count);
  const extent = (series) => {
    const values = series.flatMap((item) => item.values).filter(Number.isFinite);
    if (values.length < 2) return null;
    let minimum = Math.min(...values), maximum = Math.max(...values);
    if (minimum === maximum) { minimum -= 1; maximum += 1; }
    const padding = Math.max((maximum - minimum) * .06, Math.abs(maximum) * .001);
    return { minimum: minimum - padding, maximum: maximum + padding };
  };
  const defaultX = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value ?? "") : date.toLocaleString("zh-TW", { timeZone: "Asia/Taipei", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  };
  function svg(svg, options) {
    const series = options.series || [], range = extent(series);
    if (!range) { svg.replaceChildren(); return false; }
    const width = options.width || 800, height = options.height || 280, compact = Boolean(options.compact);
    const margin = compact ? { left: 54, right: 12, top: 12, bottom: 34 } : { left: 72, right: 20, top: 18, bottom: 52 };
    const plotWidth = width - margin.left - margin.right, plotHeight = height - margin.top - margin.bottom;
    const xValues = options.xValues || series[0].values.map((_, index) => index), count = Math.max(xValues.length - 1, 1);
    const x = (index) => margin.left + index * plotWidth / count;
    const y = (value) => margin.top + (range.maximum - value) * plotHeight / (range.maximum - range.minimum);
    const yFormat = options.yFormatter || compactNumber, xFormat = options.xFormatter || defaultX;
    const yGrid = ticks(range.minimum, range.maximum).map((value) => `<line class="xy-grid" x1="${margin.left}" y1="${y(value)}" x2="${width - margin.right}" y2="${y(value)}"/><text class="xy-tick" x="${margin.left - 9}" y="${y(value) + 4}" text-anchor="end">${escape(yFormat(value))}</text>`).join("");
    const xIndexes = [...new Set([0, Math.round(count / 2), count])];
    const xGrid = xIndexes.map((index) => `<line class="xy-grid" x1="${x(index)}" y1="${margin.top}" x2="${x(index)}" y2="${height - margin.bottom}"/><text class="xy-tick" x="${x(index)}" y="${height - margin.bottom + 18}" text-anchor="middle">${escape(xFormat(xValues[index]))}</text>`).join("");
    const lines = series.map((item, seriesIndex) => {
      const points = item.values.map((value, index) => Number.isFinite(value) ? `${x(index)},${y(value)}` : null).filter(Boolean).join(" ");
      return `<polyline${item.id ? ` id="${escape(item.id)}"` : ""} class="xy-series ${escape(item.className || `xy-series-${seriesIndex + 1}`)}" points="${points}"/>`;
    }).join("");
    const axisTitles = `<text class="xy-axis-title" x="${margin.left + plotWidth / 2}" y="${height - 5}" text-anchor="middle">${escape(options.xLabel || "時間")}</text><text class="xy-axis-title" transform="translate(15 ${margin.top + plotHeight / 2}) rotate(-90)" text-anchor="middle">${escape(options.yLabel || "數值")}</text>`;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.innerHTML = `${yGrid}${xGrid}<line class="xy-axis" x1="${margin.left}" y1="${height - margin.bottom}" x2="${width - margin.right}" y2="${height - margin.bottom}"/><line class="xy-axis" x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${height - margin.bottom}"/>${lines}${axisTitles}`;
    return true;
  }
  function canvas(canvas, options) {
    const series = options.series || [], range = extent(series), context = canvas.getContext("2d"), ratio = devicePixelRatio || 1;
    const width = canvas.clientWidth || 800, height = Number(canvas.getAttribute("height")) || 280;
    canvas.width = width * ratio; canvas.height = height * ratio; context.setTransform(ratio, 0, 0, ratio, 0, 0); context.clearRect(0, 0, width, height);
    if (!range) return false;
    const margin = { left: 72, right: 20, top: 18, bottom: 52 }, plotWidth = width - margin.left - margin.right, plotHeight = height - margin.top - margin.bottom;
    const xValues = options.xValues || series[0].values.map((_, index) => index), count = Math.max(xValues.length - 1, 1);
    const x = (index) => margin.left + index * plotWidth / count, y = (value) => margin.top + (range.maximum - value) * plotHeight / (range.maximum - range.minimum);
    const styles = getComputedStyle(canvas), grid = styles.getPropertyValue("--chart-grid").trim() || "rgba(148,163,184,.24)", text = styles.getPropertyValue("--chart-text").trim() || "#9aa7ba";
    context.font = "12px system-ui"; context.fillStyle = text; context.strokeStyle = grid; context.lineWidth = 1;
    ticks(range.minimum, range.maximum).forEach((value) => { context.beginPath(); context.moveTo(margin.left, y(value)); context.lineTo(width - margin.right, y(value)); context.stroke(); context.textAlign = "right"; context.fillText((options.yFormatter || compactNumber)(value), margin.left - 9, y(value) + 4); });
    [...new Set([0, Math.round(count / 2), count])].forEach((index) => { context.beginPath(); context.moveTo(x(index), margin.top); context.lineTo(x(index), height - margin.bottom); context.stroke(); context.textAlign = "center"; context.fillText((options.xFormatter || defaultX)(xValues[index]), x(index), height - margin.bottom + 18); });
    context.fillText(options.xLabel || "時間", margin.left + plotWidth / 2, height - 5); context.save(); context.translate(15, margin.top + plotHeight / 2); context.rotate(-Math.PI / 2); context.fillText(options.yLabel || "數值", 0, 0); context.restore();
    series.forEach((item, seriesIndex) => { context.beginPath(); item.values.forEach((value, index) => { if (!Number.isFinite(value)) return; index ? context.lineTo(x(index), y(value)) : context.moveTo(x(index), y(value)); }); context.strokeStyle = item.color || ["#4d91f7", "#f59e0b", "#10b981"][seriesIndex % 3]; context.lineWidth = 2.5; context.lineJoin = "round"; context.lineCap = "round"; context.stroke(); });
    return true;
  }
  globalThis.PlatformXYChart = { svg, canvas };
})();
