(function (root, factory) {
  const exported = factory();
  if (typeof module === 'object' && module.exports) module.exports = exported;
  if (root) root.StockDataService = exported.StockDataService;
})(typeof globalThis !== 'undefined' ? globalThis : this, () => {
  'use strict';

  const API_BASE_URL = 'https://172-238-20-217.ip.linodeusercontent.com/api/v1';
  const FALLBACK_BASE_URL = './data/stocks';
  const FALLBACK_CONFIG_URL = './config/fallback_stocks.json';
  const CACHE_TTL_MS = 45000;

  class DataServiceError extends Error {
    constructor(code, message, status = 0) { super(message); this.name = 'DataServiceError'; this.code = code; this.status = status; }
  }

  class StockDataService {
    constructor(options = {}) {
      this.apiBase = options.apiBase || API_BASE_URL;
      this.fallbackBase = options.fallbackBase || FALLBACK_BASE_URL;
      this.fallbackConfigUrl = options.fallbackConfigUrl || FALLBACK_CONFIG_URL;
      this.fetchImpl = options.fetchImpl || globalThis.fetch.bind(globalThis);
      this.timeoutMs = options.timeoutMs || 3000;
      this.networkRetries = options.networkRetries ?? 1;
      this.cacheTtlMs = options.cacheTtlMs || CACHE_TTL_MS;
      this.cache = new Map();
    }

    clone(value) { return typeof structuredClone === 'function' ? structuredClone(value) : JSON.parse(JSON.stringify(value)); }

    async cached(key, loader) {
      const hit = this.cache.get(key), now = Date.now();
      if (hit && hit.expires > now) return this.clone(await hit.value);
      const value = Promise.resolve().then(loader);
      this.cache.set(key, { expires: now + this.cacheTtlMs, value });
      try { return this.clone(await value); } catch (error) { this.cache.delete(key); throw error; }
    }

    async fetchJson(url, options = {}) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), options.timeoutMs || this.timeoutMs);
      try {
        const response = await this.fetchImpl(url, { cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' } });
        if (response.status === 404) throw new DataServiceError('NOT_FOUND', '查無此股票', 404);
        if (response.status === 429) throw new DataServiceError('FALLBACK_REQUIRED', 'API rate limited', 429);
        if (response.status >= 500) throw new DataServiceError('FALLBACK_REQUIRED', 'API unavailable', response.status);
        if (!response.ok) throw new DataServiceError('CLIENT_ERROR', '查詢條件無效', response.status);
        try { return await response.json(); } catch { throw new DataServiceError('INVALID_RESPONSE', '資料格式錯誤', response.status); }
      } catch (error) {
        if (error instanceof DataServiceError) throw error;
        const code = error?.name === 'AbortError' ? 'TIMEOUT' : 'NETWORK_ERROR';
        throw new DataServiceError(code, code === 'TIMEOUT' ? 'API timeout' : 'API network error');
      } finally { clearTimeout(timer); }
    }

    async api(path) {
      let attempt = 0;
      while (true) {
        try { return await this.fetchJson(`${this.apiBase}${path}`); }
        catch (error) {
          if (!['NETWORK_ERROR', 'TIMEOUT'].includes(error.code) || attempt >= this.networkRetries) throw error;
          attempt += 1;
        }
      }
    }

    async fallback(path) {
      return this.cached(`fallback-json:${path}`, () => this.fetchJson(`${this.fallbackBase}/${path}`, { timeoutMs: 5000 }));
    }

    async fallbackSymbols() {
      const config = await this.cached('fallback-config', () => this.fetchJson(this.fallbackConfigUrl, { timeoutMs: 5000 }));
      return new Set((config.symbols || []).map((symbol) => String(symbol).toUpperCase()));
    }

    async allowedFallback(symbol) {
      return (await this.fallbackSymbols()).has(symbol);
    }

    async stockFallback(symbol, path) {
      if (!await this.allowedFallback(symbol)) {
        throw new DataServiceError('SERVICE_UNAVAILABLE', '即時資料服務暫時無法使用', 503);
      }
      return this.fallback(path);
    }

    shouldFallback(error) { return ['NETWORK_ERROR', 'TIMEOUT', 'FALLBACK_REQUIRED', 'INVALID_RESPONSE'].includes(error?.code); }

    async readThrough(apiLoader, fallbackLoader) {
      try { return { data: await apiLoader(), source: 'api' }; }
      catch (error) {
        if (!this.shouldFallback(error)) throw error;
        return { data: await fallbackLoader(), source: 'fallback' };
      }
    }

    async searchStocks(search = '', filters = {}) {
      const query = String(search).trim();
      const key = `search:${query}:${filters.market || ''}:${filters.industry || ''}:${filters.limit || 20}`;
      return this.cached(key, () => this.readThrough(
        async () => {
          const params = new URLSearchParams();
          if (query) params.set('search', query);
          if (filters.market) params.set('market', filters.market);
          if (filters.industry) params.set('industry', filters.industry);
          params.set('limit', String(Math.min(100, filters.limit || 20)));
          const payload = await this.api(`/stocks?${params}`);
          return (payload.results || []).map((row) => ({ ...row, cached: true }));
        },
        async () => {
          const payload = await this.fallback('index.json');
          const lower = query.toLowerCase();
          return (payload.stocks || []).filter((row) => (!lower || row.symbol.toLowerCase().includes(lower) || row.name.toLowerCase().includes(lower))
            && (!filters.market || row.market === filters.market) && (!filters.industry || row.industry === filters.industry)).slice(0, filters.limit || 20);
        }
      ));
    }

    async getStock(symbol) {
      const normalized = this.symbol(symbol);
      return this.cached(`stock:${normalized}`, async () => {
        const result = await this.readThrough(
          () => this.api(`/stocks/${encodeURIComponent(normalized)}`).then((data) => this.normalizeApiStock(data)),
          () => this.stockFallback(normalized, `${encodeURIComponent(normalized)}.json`)
        );
        result.updatedAt = result.data.updated_at || result.data.data?.build_status?.updated_at || null;
        return result;
      });
    }

    async getFinancials(symbol, limit = 12) {
      const normalized = this.symbol(symbol), safeLimit = Math.max(1, Math.min(20, Number(limit) || 12));
      return this.cached(`financials:${normalized}:${safeLimit}`, () => this.readThrough(
        async () => [...((await this.api(`/stocks/${encodeURIComponent(normalized)}/financials?limit=${safeLimit}`)).financials || [])].reverse(),
        async () => ((await this.stockFallback(normalized, `financials/${encodeURIComponent(normalized)}.json`)).data?.quarters || []).slice(-safeLimit)
      ));
    }

    async getChips(symbol, limit = 60) {
      const normalized = this.symbol(symbol), safeLimit = Math.max(1, Math.min(250, Number(limit) || 60));
      return this.cached(`chips:${normalized}:${safeLimit}`, () => this.readThrough(
        async () => this.normalizeApiChips((await this.api(`/stocks/${encodeURIComponent(normalized)}/chips?limit=${safeLimit}`)).chips || []),
        async () => (await this.stockFallback(normalized, `${encodeURIComponent(normalized)}.json`)).data?.chips || this.emptyChips()
      ));
    }

    async getIndustryPeers(industry, symbol) {
      const normalized = this.symbol(symbol), name = String(industry || '').trim();
      if (!name) return { data: { applicable: false, reason: '產業資料不足' }, source: 'api' };
      return this.cached(`peers:${name}:${normalized}`, () => this.readThrough(
        async () => this.buildPeerAnalysis((await this.api(`/industries/${encodeURIComponent(name)}/peers`)).stocks || [], normalized),
        async () => this.buildPeerAnalysis((await this.fallback('industry_snapshot.json')).stocks || [], normalized)
      ));
    }

    symbol(value) {
      const symbol = String(value || '').trim().toUpperCase();
      if (!/^[0-9A-Z]{2,10}$/.test(symbol)) throw new DataServiceError('INVALID_SYMBOL', '請輸入有效股票代號', 422);
      return symbol;
    }

    metric(value, dataDate, unit) { return { value: value ?? null, data_date: dataDate || null, unit, note: null }; }

    normalizeApiStock(api) {
      const valuation = api.valuation || {}, financial = api.fundamentals || {}, monthly = financial.revenue || {};
      const reportDate = financial.report_date || null;
      const fundamentals = {
        report_period: financial.report_period || null, report_date: reportDate,
        source_updated_at: financial.available_date || null,
        eps: this.metric(financial.eps, reportDate, 'TWD'), roe: this.metric(financial.roe, reportDate, 'percent'),
        revenue: this.metric(monthly.revenue, monthly.revenue_month, monthly.unit || 'thousand_TWD'),
        revenue_yoy: this.metric(monthly.revenue_yoy, monthly.revenue_month, '%'),
        revenue_mom: this.metric(monthly.revenue_mom, monthly.revenue_month, '%'),
        gross_margin: this.metric(financial.gross_margin, reportDate, 'percent'),
        operating_margin: this.metric(financial.operating_margin, reportDate, 'percent'),
        net_margin: this.metric(financial.net_margin, reportDate, 'percent'),
        book_value_per_share: this.metric(null, reportDate, 'TWD'), debt_ratio: this.metric(financial.debt_ratio, reportDate, 'percent'),
        current_ratio: this.metric(financial.current_ratio, reportDate, 'percent'),
        operating_cash_flow: this.metric(financial.operating_cash_flow, reportDate, 'thousand_TWD'),
        free_cash_flow: this.metric(financial.free_cash_flow, reportDate, 'thousand_TWD')
      };
      return {
        updated_at: api.build_status?.updated_at || null, provider: 'PostgreSQL REST API', dataset: 'stock_analysis', version: '1.0',
        data: {
          profile: api.profile || {}, quote: api.quote || {},
          valuation: {
            pe: { value: valuation.pe ?? null, unit: 'ratio', data_date: valuation.pe_date || valuation.valuation_date || null, status: null, note: null },
            pb: { value: valuation.pb ?? null, unit: 'ratio', data_date: valuation.pb_date || valuation.valuation_date || null, status: null, note: null },
            dividend_yield: { value: valuation.dividend_yield ?? null, unit: '%', data_date: valuation.dividend_yield_date || valuation.valuation_date || null, status: null, note: null }
          },
          fundamentals,
          historical_valuation: this.historicalValuation(api.valuation_history_observations || [], valuation),
          financial_trends: { applicable: (api.profile || {}).instrument_type === 'company' },
          health_v2: api.health_v2 || { applicable: false, categories: {} },
          peer_analysis: api.peer_analysis || { applicable: false },
          chips: this.emptyChips(), build_status: api.build_status || {}
        }
      };
    }

    historicalValuation(rows, current) {
      if (!rows.length) return { applicable: false, reason: '歷史估值資料不足' };
      const output = {};
      for (const key of ['pe', 'pb', 'dividend_yield']) {
        const currentValue = current[key] ?? null;
        output[key] = { current: currentValue, '3y': this.distribution(rows, key, currentValue, 3), '5y': this.distribution(rows, key, currentValue, 5) };
      }
      return output;
    }

    distribution(rows, key, current, years) {
      if (current === null || !rows.length) return null;
      const end = new Date(rows.at(-1).trade_date), cutoff = new Date(end); cutoff.setFullYear(cutoff.getFullYear() - years);
      const values = rows.filter((row) => new Date(row.trade_date) >= cutoff && Number.isFinite(row[key])).map((row) => row[key]).sort((a, b) => a - b);
      if (!values.length) return null;
      const quantile = (p) => { const index = (values.length - 1) * p, lower = Math.floor(index), fraction = index - lower; return values[lower] + ((values[lower + 1] ?? values[lower]) - values[lower]) * fraction; };
      return { sample_count: values.length, low: values[0], p25: quantile(.25), median: quantile(.5), p75: quantile(.75), high: values.at(-1), current_percentile: values.filter((item) => item <= current).length / values.length * 100 };
    }

    emptyChips() { return { trade_date: null, history: [], analysis: {} }; }

    normalizeApiChips(descending) {
      const history = [...descending].reverse();
      const sums = (key) => Object.fromEntries([5, 20, 60].map((days) => [`${days}d`, history.slice(-days).reduce((sum, row) => sum + (Number.isFinite(row[key]) ? row[key] : 0), 0)]));
      const change = (key, days) => history.length > days && Number.isFinite(history.at(-1)?.[key]) && Number.isFinite(history.at(-(days + 1))?.[key]) ? history.at(-1)[key] - history.at(-(days + 1))[key] : null;
      const streak = (key) => { const latest = history.at(-1)?.[key]; if (!Number.isFinite(latest) || latest === 0) return { direction: null, days: 0 }; const sign = Math.sign(latest); let days = 0; for (let index = history.length - 1; index >= 0 && Math.sign(history[index][key]) === sign; index -= 1) days += 1; return { direction: sign > 0 ? 'buy' : 'sell', days }; };
      return { trade_date: history.at(-1)?.trade_date || null, history, analysis: {
        foreign_sum: sums('foreign_net'), investment_trust_sum: sums('investment_trust_net'), institutional_sum: sums('institutional_total'),
        foreign_streak: streak('foreign_net'), investment_trust_streak: streak('investment_trust_net'),
        margin_change: { '5d': change('margin_balance', 5), '20d': change('margin_balance', 20) }
      } };
    }

    buildPeerAnalysis(rows, symbol) {
      const current = rows.find((row) => row.symbol === symbol);
      if (!current || current.instrument_type !== 'company') return { applicable: false, reason: 'ETF 或同業資料不足' };
      const specs = {
        pe: ['valuation', 'lower', null], pb: ['valuation', 'lower', null], dividend_yield: ['valuation', 'higher', null],
        revenue_yoy: ['growth', 'higher', 'revenue_period'], eps_yoy: ['growth', 'higher', 'multi_period'],
        eps: ['profitability', 'higher', 'financial_period'], roe: ['profitability', 'higher', 'financial_period'],
        gross_margin: ['profitability', 'higher', 'financial_period'], operating_margin: ['profitability', 'higher', 'financial_period'],
        net_margin: ['profitability', 'higher', 'financial_period'], ttm_eps: ['profitability', 'higher', 'multi_period'],
        debt_ratio: ['safety', 'lower', 'financial_period'], current_ratio: ['safety', 'context', 'financial_period'],
        ttm_operating_cash_flow: ['safety', 'higher', 'multi_period'], ttm_free_cash_flow: ['safety', 'higher', 'multi_period']
      };
      const categories = { valuation: {}, growth: {}, profitability: {}, safety: {} };
      const compare = (key) => {
        const [category, direction, periodKey] = specs[key], period = periodKey ? current[periodKey] : null;
        const valid = (row) => Number.isFinite(row[key]) && !(key === 'pe' && row[key] <= 0) && (!periodKey || row[periodKey] === period);
        const eligible = rows.filter(valid), values = eligible.map((row) => row[key]).sort((a, b) => a - b), currentValue = current[key];
        const item = { company_value: currentValue ?? null, industry_sample_size: rows.length, industry_median: null, percentile: null, rank: null, total_ranked: eligible.length, relative_status: 'unavailable', comparison_direction: direction, comparison_period: period, data_date: category === 'valuation' ? current.valuation_date : key === 'revenue_yoy' ? current.revenue_period : current.financial_date, period_mismatch_excluded: periodKey ? rows.filter((row) => row[periodKey] !== period).length : 0 };
        if (eligible.length < 5 || !valid(current)) return item;
        const middle = Math.floor(values.length / 2); item.industry_median = values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
        if (direction === 'higher') { item.rank = 1 + values.filter((number) => number > currentValue).length; item.percentile = values.filter((number) => number <= currentValue).length / values.length * 100; }
        else if (direction === 'lower') { item.rank = 1 + values.filter((number) => number < currentValue).length; item.percentile = values.filter((number) => number >= currentValue).length / values.length * 100; }
        else item.percentile = values.filter((number) => number <= currentValue).length / values.length * 100;
        item.relative_status = direction === 'context' ? 'average' : item.percentile >= 90 ? 'leading' : item.percentile >= 60 ? 'above_average' : item.percentile >= 40 ? 'average' : item.percentile >= 10 ? 'below_average' : 'lagging';
        return item;
      };
      Object.keys(specs).forEach((key) => { categories[specs[key][0]][key] = compare(key); });
      const rankings = {};
      for (const key of ['roe', 'eps', 'revenue_yoy', 'pe', 'pb']) {
        const [category, direction, periodKey] = specs[key], period = periodKey ? current[periodKey] : null;
        const eligible = rows.filter((row) => Number.isFinite(row[key]) && !(key === 'pe' && row[key] <= 0) && (!periodKey || row[periodKey] === period)).sort((a, b) => direction === 'higher' ? b[key] - a[key] : a[key] - b[key]);
        const entry = (row) => ({ symbol: row.symbol, name: row.name, value: row[key], rank: direction === 'higher' ? 1 + eligible.filter((peer) => peer[key] > row[key]).length : 1 + eligible.filter((peer) => peer[key] < row[key]).length, percentile: direction === 'higher' ? eligible.filter((peer) => peer[key] <= row[key]).length / eligible.length * 100 : eligible.filter((peer) => peer[key] >= row[key]).length / eligible.length * 100 });
        rankings[key] = { metric: key, top10: eligible.slice(0, 10).map(entry), current_company: eligible.some((row) => row.symbol === symbol) ? entry(current) : null, sample_size: eligible.length, comparison_period: period };
      }
      return { applicable: true, industry: current.industry, industry_company_count: rows.length, data_date: current.financial_date || current.valuation_date, minimum_sample_size: 5, categories, rankings };
    }
  }

  return { StockDataService, DataServiceError, API_BASE_URL };
});
