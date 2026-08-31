'use strict';

const assert = require('assert');

class ClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  toggle(value, force) { if (force === undefined ? !this.values.has(value) : force) this.values.add(value); else this.values.delete(value); }
}
class Element {
  constructor(id) { this.id = id; this.hidden = false; this.value = ''; this.textContent = ''; this.innerHTML = ''; this.dataset = {}; this.classList = new ClassList(); }
  addEventListener() {}
  querySelectorAll() { return []; }
  closest() { return null; }
}
const elements = new Map();
global.document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); },
  querySelectorAll() { return []; },
  addEventListener() {}
};
global.location = { search: '?symbol=2330' };
global.history = { replaceState() {} };
require('../../stock-data-service.js');
require('../../stock-analysis.js');

const waitFor = async (predicate, timeout = 15000) => {
  const started = Date.now();
  while (!predicate()) { if (Date.now() - started > timeout) throw new Error('UI smoke test timed out'); await new Promise((resolve) => setTimeout(resolve, 50)); }
};

(async () => {
  await waitFor(() => elements.get('stockContent')?.hidden === false);
  assert.equal(elements.get('stockSymbol').textContent, '2330');
  assert.equal(elements.get('stockName').textContent, '台積電');
  assert(elements.get('dataSourceStatus').textContent.startsWith('即時 API'));
  assert(elements.get('summarySectionGrid').innerHTML.includes('基本面'));
  assert(elements.get('summaryStrengths').innerHTML.length > 0);
  const input = elements.get('stockSearch'); input.value = '0050';
  await elements.get('searchButton').onclick();
  await waitFor(() => elements.get('stockSymbol').textContent === '0050');
  assert(elements.get('stockMeta').textContent.includes('ETF'));
  assert(elements.get('summaryWatchItems').innerHTML.includes('ETF'));
  assert.equal(elements.get('pageState').hidden, true);
  console.log(JSON.stringify({ status: 'passed', symbols: ['2330', '0050'], source: elements.get('dataSourceStatus').textContent }));
})().catch((error) => { console.error(error); process.exit(1); });
