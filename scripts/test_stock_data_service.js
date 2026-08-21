'use strict';

const assert = require('assert');
const { StockDataService } = require('../stock-data-service.js');

const profile = { symbol: '2330', name: '台積電', market: 'TWSE', industry: '半導體業', instrument_type: 'company', active: true };
const apiStock = { profile, quote: { trade_date: '2026-08-18', close: 2380, change: -20, change_percent: -0.8333, volume: 19997213 }, valuation: { valuation_date: '2026-08-18', pe: 27.59, pb: 9.59, dividend_yield: 0.92 }, valuation_history_observations: [], fundamentals: null, health_v2: { applicable: true, categories: {} }, peer_analysis: { applicable: true, metrics: [] }, chips: { summary: {} }, build_status: { state: 'complete', updated_at: '2026-08-19T10:03:11+00:00' } };
const fallbackStock = { updated_at: '2026-08-19T10:03:11+00:00', data: { profile, quote: apiStock.quote, valuation: {}, fundamentals: {}, chips: { history: [], analysis: {} }, health_v2: {}, peer_analysis: {}, build_status: {} } };
const fallbackIndex = { stocks: [profile, { symbol: '0050', name: '元大台灣50', market: 'TWSE', industry: 'ETF', instrument_type: 'ETF', active: true }, { symbol: '02001R', name: 'ETN測試', market: 'TWSE', industry: 'ETN', instrument_type: 'ETN', active: true }, { symbol: '03001P', name: '權證測試', market: 'TPEx', industry: '權證', instrument_type: 'warrant', active: true }] };

function response(status, payload, headers = {}) {
  return { status, ok: status >= 200 && status < 300, headers: { get: (key) => headers[key.toLowerCase()] || null }, json: async () => payload };
}

function service(fetchImpl, options = {}) {
  return new StockDataService({ apiBase: 'https://api.test', fallbackBase: 'https://fallback.test', fallbackConfigUrl: 'https://fallback.test/config/fallback_stocks.json', fetchImpl, timeoutMs: options.timeoutMs || 20, networkRetries: options.networkRetries ?? 1, cacheTtlMs: 100 });
}

const fallbackConfig = { version: '1.0', symbols: ['2330', '0050'] };
const fallbackPayload = (url, payload = fallbackStock) => response(200, url.endsWith('fallback_stocks.json') ? fallbackConfig : payload);

async function run() {
  const results = [];
  let fallbackCalls = 0;
  const normal = service(async (url) => {
    if (url.startsWith('https://fallback.test')) { fallbackCalls += 1; throw new Error('fallback should not load'); }
    if (url.includes('/stocks?')) return response(200, { results: [profile] });
    if (url.endsWith('/stocks/2330')) return response(200, apiStock);
    if (url.includes('/financials')) return response(200, { financials: [] });
    if (url.includes('/chips')) return response(200, { chips: [] });
    if (url.includes('/industries/')) return response(200, { stocks: [profile] });
    throw new Error(url);
  });
  assert.equal((await normal.getStock('2330')).source, 'api');
  assert.equal((await normal.getFinancials('2330')).source, 'api');
  assert.equal((await normal.getChips('2330')).source, 'api');
  assert.equal((await normal.getIndustryPeers('半導體業', '2330')).source, 'api');
  assert.equal((await normal.searchStocks('台積電')).source, 'api');
  assert.equal(fallbackCalls, 0); results.push('api_normal');

  let networkAttempts = 0, offlineFallbackFetches = 0;
  const offline = service(async (url) => {
    if (url.startsWith('https://api.test')) { networkAttempts += 1; throw new TypeError('offline'); }
    offlineFallbackFetches += 1;
    return fallbackPayload(url);
  });
  assert.equal((await offline.getStock('2330')).source, 'fallback');
  assert.equal(networkAttempts, 2); results.push('api_offline_retry_fallback');
  assert.equal((await offline.getChips('2330')).source, 'fallback');
  assert.equal(offlineFallbackFetches, 2); results.push('fallback_resource_deduplicated');

  const stock500 = service(async (url) => url.startsWith('https://api.test') ? response(500, {}) : fallbackPayload(url));
  assert.equal((await stock500.getStock('2330')).source, 'fallback'); results.push('stock_500_fallback');

  let notFoundFallbacks = 0;
  const notFound = service(async (url) => { if (url.startsWith('https://fallback.test')) notFoundFallbacks += 1; return response(404, {}); });
  await assert.rejects(() => notFound.getStock('999999'), (error) => error.code === 'NOT_FOUND');
  assert.equal(notFoundFallbacks, 0); results.push('stock_404_no_fallback');

  const financial500 = service(async (url) => {
    if (url.endsWith('/stocks/2330')) return response(200, apiStock);
    if (url.startsWith('https://api.test')) return response(500, {});
    if (url.endsWith('fallback_stocks.json')) return response(200, fallbackConfig);
    if (url.includes('/financials/')) return response(200, { data: { quarters: [] } });
    throw new Error(url);
  });
  assert.equal((await financial500.getStock('2330')).source, 'api');
  assert.equal((await financial500.getFinancials('2330')).source, 'fallback'); results.push('financial_500_isolated');

  const limited = service(async (url) => url.startsWith('https://api.test') ? response(429, {}) : fallbackPayload(url));
  assert.equal((await limited.getStock('2330')).source, 'fallback'); results.push('rate_limit_fallback');

  const timeout = service((url, options) => {
    if (url.startsWith('https://fallback.test')) return Promise.resolve(fallbackPayload(url));
    return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))));
  }, { timeoutMs: 5, networkRetries: 0 });
  assert.equal((await timeout.getStock('2330')).source, 'fallback'); results.push('timeout_fallback');

  const missingFinancial = service(async (url) => {
    if (url.endsWith('/stocks/2330')) return response(200, apiStock);
    if (url.includes('/financials') && url.startsWith('https://api.test')) return response(500, {});
    if (url.endsWith('fallback_stocks.json')) return response(200, fallbackConfig);
    if (url.includes('/financials/') && url.startsWith('https://fallback.test')) return response(404, {});
    throw new Error(url);
  });
  assert.equal((await missingFinancial.getStock('2330')).source, 'api');
  await assert.rejects(() => missingFinancial.getFinancials('2330'));
  results.push('missing_financial_isolated');

  const searchFallback = service(async (url) => url.startsWith('https://api.test') ? response(500, {}) : response(200, fallbackIndex));
  assert.equal((await searchFallback.searchStocks('元大')).data[0].instrument_type, 'ETF');
  assert.equal((await searchFallback.searchStocks('02001R')).data[0].instrument_type, 'ETN');
  assert.equal((await searchFallback.searchStocks('權證')).data[0].instrument_type, 'warrant');
  results.push('search_instruments_fallback');

  let nonAllowlistedStockFetches = 0;
  const nonAllowlisted = service(async (url) => {
    if (url.startsWith('https://api.test')) throw new TypeError('offline');
    if (url.endsWith('fallback_stocks.json')) return response(200, fallbackConfig);
    nonAllowlistedStockFetches += 1;
    return response(200, fallbackStock);
  });
  await assert.rejects(() => nonAllowlisted.getStock('9999'), (error) => error.code === 'SERVICE_UNAVAILABLE');
  assert.equal(nonAllowlistedStockFetches, 0);
  results.push('offline_non_allowlisted_unavailable');

  const etfApi = { ...apiStock, profile: { ...profile, symbol: '0050', instrument_type: 'ETF', industry: 'ETF' } };
  const etf = service(async () => response(200, etfApi));
  const etfResult = await etf.getStock('0050');
  assert.equal(etfResult.data.data.profile.instrument_type, 'ETF'); results.push('etf');

  console.log(JSON.stringify({ status: 'passed', scenarios: results, count: results.length }));
}

run().catch((error) => { console.error(error); process.exit(1); });
