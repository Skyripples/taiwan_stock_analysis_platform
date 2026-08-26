'use strict';
const assert = require('assert');
const { StockDataService } = require('../stock-data-service.js');

const profile = { symbol: '2330', name: '台積電', market: 'TWSE', industry: '半導體業', instrument_type: 'company', active: true };
const etfProfile = { ...profile, symbol: '0050', name: '元大台灣50', industry: 'ETF', instrument_type: 'ETF' };
const response = (status, payload) => ({ status, ok: status >= 200 && status < 300, json: async () => payload });
const create = (fetchImpl, options = {}) => new StockDataService({
  apiBase: 'https://api.test', searchIndexUrl: 'https://pages.test/data/stocks/index.json',
  fetchImpl, timeoutMs: options.timeoutMs || 20, networkRetries: options.networkRetries ?? 0, cacheTtlMs: 10
});
const stock = (item) => ({ profile: item, quote: {}, valuation: {}, fundamentals: {}, health_v2: {}, peer_analysis: {}, build_status: {} });

async function run() {
  const normal = create(async (url) => {
    if (url.includes('/stocks?')) return response(200, { results: [profile] });
    if (url.endsWith('/stocks/2330')) return response(200, stock(profile));
    if (url.endsWith('/stocks/0050')) return response(200, stock(etfProfile));
    if (url.includes('/financials')) return response(200, { financials: [] });
    if (url.includes('/chips')) return response(200, { chips: [] });
    if (url.includes('/industries/')) return response(200, { stocks: [profile] });
    throw new Error(url);
  });
  assert.equal((await normal.getStock('2330')).source, 'api');
  assert.equal((await normal.getStock('0050')).data.data.profile.instrument_type, 'ETF');
  assert.equal((await normal.searchStocks('台積電')).source, 'api');

  for (const status of [429, 500]) {
    const unavailable = create(async () => response(status, {}));
    await assert.rejects(() => unavailable.getStock('2330'), (error) => error.code === 'SERVICE_UNAVAILABLE');
  }
  const offline = create(async () => { throw new TypeError('offline'); });
  await assert.rejects(() => offline.getStock('2330'), (error) => error.code === 'NETWORK_ERROR');
  const missing = create(async () => response(404, {}));
  await assert.rejects(() => missing.getStock('9999'), (error) => error.code === 'NOT_FOUND');

  const index = { stocks: [profile, etfProfile] };
  const indexSearch = create(async (url) => url.startsWith('https://api.test') ? response(500, {}) : response(200, index));
  const result = await indexSearch.searchStocks('0050');
  assert.equal(result.source, 'index');
  assert.equal(result.data[0].instrument_type, 'ETF');
  console.log(JSON.stringify({ status: 'passed', scenarios: ['api', 'offline', '429', '500', '404', 'index-search', 'ETF'] }));
}
run().catch((error) => { console.error(error); process.exit(1); });
