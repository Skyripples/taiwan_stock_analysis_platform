'use strict';
const assert = require('assert');
const { StockDataService } = require('../../stock-data-service.js');

const profile = { symbol: '2330', name: '台積電', market: 'TWSE', industry: '半導體業', instrument_type: 'company', active: true };
const etfProfile = { ...profile, symbol: '0050', name: '元大台灣50', industry: 'ETF', instrument_type: 'ETF' };
const response = (status, payload) => ({ status, ok: status >= 200 && status < 300, json: async () => payload });
const create = (fetchImpl, options = {}) => new StockDataService({
  apiBase: 'https://api.test', searchIndexUrl: 'https://pages.test/data/stocks/index.json',
  fetchImpl, timeoutMs: options.timeoutMs || 20, networkRetries: options.networkRetries ?? 0, cacheTtlMs: 10_000
});
const stock = (item) => ({ profile: item, quote: {}, valuation: {}, fundamentals: {}, health_v2: {}, peer_analysis: {}, build_status: {} });

async function run() {
  const historyCalls = { revenue: 0, financials: 0, valuation: 0, chips: 0 };
  const normal = create(async (url) => {
    if (url.includes('/stocks?')) return response(200, { results: [profile] });
    if (url.includes('/stocks/screener/options')) return response(200, { markets: ['TWSE'], industries: ['半導體業'], instrument_types: ['company'] });
    if (url.includes('/stocks/screener?')) return response(200, { total: 1, count: 1, limit: 50, offset: 0, results: [profile] });
    if (url.endsWith('/stocks/2330')) return response(200, stock(profile));
    if (url.endsWith('/stocks/0050')) return response(200, stock(etfProfile));
    if (url.includes('/revenue')) { historyCalls.revenue += 1; return response(200, { revenue: [{ revenue_month: '2026-09-01', revenue: 1, revenue_yoy: null, revenue_mom: 2 }] }); }
    if (url.includes('/financials')) { historyCalls.financials += 1; return response(200, { financials: [{ fiscal_year: 2026, quarter: 2, eps: null }] }); }
    if (url.includes('/valuation')) { historyCalls.valuation += 1; return response(200, { valuations: [{ valuation_date: '2026-10-19', pe: 20, pb: null, dividend_yield: 2 }] }); }
    if (url.includes('/chips')) { historyCalls.chips += 1; return response(200, { chips: [{ trade_date: '2026-10-19', foreign_net: null }] }); }
    if (url.includes('/industries/')) return response(200, { stocks: [profile] });
    throw new Error(url);
  });
  assert.equal((await normal.getStock('2330')).source, 'api');
  assert.equal((await normal.getStock('0050')).data.data.profile.instrument_type, 'ETF');
  assert.equal((await normal.searchStocks('台積電')).source, 'api');
  assert.equal((await normal.screenStocks({ market: 'TWSE', pe_min: 10 })).data.total, 1);
  assert.deepEqual((await normal.getScreenerOptions()).data.markets, ['TWSE']);
  assert.equal((await normal.getRevenueHistory('2330')).data[0].mom, 2);
  assert.equal((await normal.getRevenueHistory('2330')).data[0].yoy, null);
  assert.equal((await normal.getFinancials('2330', 20)).data[0].eps, null);
  assert.equal((await normal.getValuationHistory('2330')).data[0].pb, null);
  assert.equal((await normal.getChips('2330', 120)).data.history[0].foreign_net, null);
  await normal.getRevenueHistory('2330');
  assert.deepEqual(historyCalls, { revenue: 1, financials: 1, valuation: 1, chips: 1 });

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
  console.log(JSON.stringify({ status: 'passed', scenarios: ['api', 'offline', '429', '500', '404', 'index-search', 'ETF', 'screener'] }));
}
run().catch((error) => { console.error(error); process.exit(1); });
