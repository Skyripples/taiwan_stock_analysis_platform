'use strict';

const assert = require('assert');

class ClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  toggle(value, force) { if (force === undefined ? !this.values.has(value) : force) this.values.add(value); else this.values.delete(value); }
}
class Element {
  constructor(id) { this.id = id; this.hidden = false; this.value = ''; this.textContent = ''; this.innerHTML = ''; this.outerHTML = '<svg></svg>'; this.dataset = {}; this.classList = new ClassList(); this.listeners = {}; this.children = new Map(); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this[name] = String(value); }
  querySelectorAll() { return []; }
  querySelector(selector) { if (!this.children.has(selector)) this.children.set(selector, new Element(`${this.id}:${selector}`)); return this.children.get(selector); }
  closest() { return null; }
}
const elements = new Map();
const datasets = ['revenue', 'financials', 'valuation', 'chips'];
const panels = datasets.map((dataset) => { const item = new Element(`historyPanel${dataset}`); item.dataset.historyPanel = dataset; return item; });
const tabs = datasets.map((dataset) => { const item = new Element(`historyTab${dataset}`); item.dataset.historyTab = dataset; return item; });
global.document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); },
  querySelectorAll(selector) { if (selector === '[data-history-panel]') return panels; if (selector === '[data-history-tab]') return tabs; return []; },
  querySelector(selector) { const match = selector.match(/^\[data-history-panel="(.+)"\]$/); return match ? panels.find((item) => item.dataset.historyPanel === match[1]) : null; },
  createElementNS() { return new Element('svg'); },
  addEventListener() {}
};
global.location = { search: '?symbol=2330' };
global.history = { replaceState() {} };
global.PlatformXYChart = { svg(node) { node.outerHTML = '<svg role="img"></svg>'; } };
require('../../stock-data-service.js');
require('../../stock-preferences.js');
require('../../stock-analysis.js');

const waitFor = async (predicate, timeout = 15000) => {
  const started = Date.now();
  while (!predicate()) { if (Date.now() - started > timeout) throw new Error('UI smoke test timed out'); await new Promise((resolve) => setTimeout(resolve, 50)); }
};

(async () => {
  await waitFor(() => elements.get('stockContent')?.hidden === false);
  assert.equal(elements.get('stockSymbol').textContent, '2330');
  assert.equal(elements.get('stockName').textContent, '台積電');
  assert(elements.get('dataSourceStatus').textContent.startsWith('線上 API 資料'));
  assert(elements.get('summarySectionGrid').innerHTML.includes('基本面'));
  assert(elements.get('summaryStrengths').innerHTML.length > 0);
  await waitFor(() => panels[0].dataset.loadedSymbol === '2330');
  assert(panels[0].querySelector('.history-panel-content').innerHTML.includes('月營收趨勢'));
  assert.equal(panels[1].dataset.loadedSymbol, undefined);
  tabs[1].listeners.click();
  await waitFor(() => panels[1].dataset.loadedSymbol === '2330');
  assert(panels[1].querySelector('.history-panel-content').innerHTML.includes('EPS'));
  tabs[0].listeners.click();
  assert.equal(panels[0].dataset.loadedSymbol, '2330');
  const input = elements.get('stockSearch'); input.value = '0050';
  await elements.get('searchButton').onclick();
  await waitFor(() => elements.get('stockSymbol').textContent === '0050');
  assert(elements.get('stockMeta').textContent.includes('ETF'));
  assert(elements.get('summaryWatchItems').innerHTML.includes('ETF'));
  assert.equal(elements.get('pageState').hidden, true);
  console.log(JSON.stringify({ status: 'passed', symbols: ['2330', '0050'], source: elements.get('dataSourceStatus').textContent }));
})().catch((error) => { console.error(error); process.exit(1); });
