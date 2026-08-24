'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

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

const root = path.resolve(__dirname, '..'), elements = new Map();
global.document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); },
  querySelectorAll() { return []; }, addEventListener() {}
};
global.location = { search: '?symbol=2330' };
global.history = { replaceState() {} };
global.fetch = async (url) => {
  const target = String(url);
  if (target.startsWith('https://172-238-20-217.ip.linodeusercontent.com')) throw new TypeError('simulated offline');
  const relative = target.replace(/^\.\//, '');
  const file = path.join(root, relative);
  if (!fs.existsSync(file)) return { status: 404, ok: false, json: async () => ({}) };
  return { status: 200, ok: true, json: async () => JSON.parse(fs.readFileSync(file, 'utf8')) };
};

require('../stock-data-service.js');
require('../stock-analysis.js');
const waitFor = async (predicate, timeout = 15000) => {
  const started = Date.now();
  while (!predicate()) { if (Date.now() - started > timeout) throw new Error('Fallback UI smoke test timed out'); await new Promise((resolve) => setTimeout(resolve, 50)); }
};

(async () => {
  await waitFor(() => elements.get('stockContent')?.hidden === false);
  assert.equal(elements.get('stockSymbol').textContent, '2330');
  assert(elements.get('dataSourceStatus').textContent.startsWith('備援資料'));
  assert(elements.get('summarySectionGrid').innerHTML.includes('基本面'));
  assert(elements.get('summaryStrengths').innerHTML.includes('ROE'));
  console.log(JSON.stringify({ status: 'passed', symbol: '2330', source: elements.get('dataSourceStatus').textContent }));
})().catch((error) => { console.error(error); process.exit(1); });
