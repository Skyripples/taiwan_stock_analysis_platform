'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { StockDataService } = require('../../stock-data-service.js');

const root = path.resolve(__dirname, '..', '..');
const index = JSON.parse(fs.readFileSync(path.join(root, 'data/stocks/index.json'), 'utf8'));
const fixed = ['2330', '2317', '6488', '2881', '1101', '0050'];
let seed = 311;
const random = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
const candidates = index.stocks.map((row) => row.symbol).filter((symbol) => !fixed.includes(symbol));
for (let index = candidates.length - 1; index > 0; index -= 1) {
  const swap = Math.floor(random() * (index + 1));
  [candidates[index], candidates[swap]] = [candidates[swap], candidates[index]];
}
const symbols = fixed.concat(candidates.slice(0, 20));

let apiRequests = 0, fallbackRequests = 0;
const countedFetch = async (url, options) => {
  if (url.startsWith('http://127.0.0.1:8000')) apiRequests += 1;
  else { fallbackRequests += 1; throw new Error(`Unexpected fallback request: ${url}`); }
  return fetch(url, options);
};
const service = new StockDataService({ apiBase: 'http://127.0.0.1:8000/api/v1', searchIndexUrl: 'https://index.invalid', fetchImpl: countedFetch });

(async () => {
  for (const symbol of symbols) {
    const detail = await service.getStock(symbol);
    assert.equal(detail.source, 'api');
    assert.equal(detail.data.data.profile.symbol, symbol);
    const financials = await service.getFinancials(symbol, 12);
    const chips = await service.getChips(symbol, 60);
    const peers = await service.getIndustryPeers(detail.data.data.profile.industry, symbol);
    assert.equal(financials.source, 'api'); assert.equal(chips.source, 'api'); assert.equal(peers.source, 'api');
    JSON.stringify(detail.data); // nulls are serializable and no cyclic shape exists.
  }
  for (const query of ['2330', '台積電', '0050', '020000', '01001T']) {
    const found = await service.searchStocks(query, { limit: 20 });
    assert.equal(found.source, 'api'); assert(found.data.length > 0, `No search result for ${query}`);
  }
  assert.equal(fallbackRequests, 0);
  console.log(JSON.stringify({ status: 'passed', symbols: symbols.length, fixed: fixed.length,
    random: 20, api_requests: apiRequests, fallback_requests: fallbackRequests }));
})().catch((error) => { console.error(error); process.exit(1); });
