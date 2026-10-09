(function (root, factory) {
  const exported = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = exported;
  if (root) root.StockPreferences = exported.StockPreferences;
})(typeof globalThis !== 'undefined' ? globalThis : this, (root) => {
  'use strict';

  const PREFIX = 'taiwan_stock_analysis_platform:v1';
  const WATCHLIST_KEY = `${PREFIX}:watchlist`;
  const SCREENER_KEY = `${PREFIX}:screener`;
  const SORTS = new Set(['symbol', 'close', 'change_percent', 'pe', 'pb', 'dividend_yield', 'revenue_yoy', 'roe', 'debt_ratio', 'foreign_5d']);
  const ORDERS = new Set(['asc', 'desc']);
  const FILTER_KEYS = new Set([
    'search', 'market', 'industry', 'instrument_type', 'price_min', 'price_max',
    'change_percent_min', 'change_percent_max', 'pe_min', 'pe_max', 'pb_min', 'pb_max',
    'dividend_yield_min', 'dividend_yield_max', 'revenue_yoy_min', 'revenue_yoy_max',
    'roe_min', 'roe_max', 'debt_ratio_min', 'debt_ratio_max', 'foreign_5d_min', 'foreign_5d_max'
  ]);

  const validStock = (item) => item && typeof item === 'object' && /^[0-9A-Z]{2,10}$/.test(String(item.symbol || '').toUpperCase()) && typeof item.name === 'string';
  const cleanWatchlist = (value) => {
    if (!value || value.schema !== 1 || !Array.isArray(value.items)) return [];
    const unique = new Map();
    value.items.filter(validStock).forEach((item) => {
      const symbol = String(item.symbol).toUpperCase();
      if (!unique.has(symbol)) unique.set(symbol, { symbol, name: item.name.trim() || symbol });
    });
    return [...unique.values()];
  };
  const cleanScreener = (value) => {
    if (!value || value.schema !== 1 || !value.filters || typeof value.filters !== 'object' || Array.isArray(value.filters)) return null;
    const filters = {};
    Object.entries(value.filters).forEach(([key, item]) => {
      if (FILTER_KEYS.has(key) && ['string', 'number'].includes(typeof item)) filters[key] = String(item);
    });
    return { filters, sort: SORTS.has(value.sort) ? value.sort : 'symbol', order: ORDERS.has(value.order) ? value.order : 'asc' };
  };

  class StockPreferences {
    constructor(options = {}) {
      if (Object.prototype.hasOwnProperty.call(options, 'storage')) this.storage = options.storage;
      else {
        try { this.storage = root?.localStorage; } catch { this.storage = null; }
      }
      this.eventTarget = options.eventTarget || root;
      this.memory = new Map();
      this.listeners = new Set();
      this.persistent = this.checkStorage();
      this.onStorage = (event) => {
        if (event.key !== WATCHLIST_KEY) return;
        const items = this.parseWatchlist(event.newValue);
        this.memory.set(WATCHLIST_KEY, JSON.stringify({ schema: 1, items }));
        this.notify(items);
      };
      this.eventTarget?.addEventListener?.('storage', this.onStorage);
    }

    checkStorage() {
      if (!this.storage) return false;
      const key = `${PREFIX}:probe`;
      try { this.storage.setItem(key, '1'); this.storage.removeItem(key); return true; } catch { return false; }
    }

    read(key) {
      if (this.persistent) {
        try { return this.storage.getItem(key); } catch { this.persistent = false; }
      }
      return this.memory.get(key) || null;
    }

    write(key, value) {
      this.memory.set(key, value);
      if (!this.persistent) return false;
      try { this.storage.setItem(key, value); return true; } catch { this.persistent = false; return false; }
    }

    remove(key) {
      this.memory.delete(key);
      if (!this.persistent) return false;
      try { this.storage.removeItem(key); return true; } catch { this.persistent = false; return false; }
    }

    parseWatchlist(raw) {
      try { return cleanWatchlist(JSON.parse(raw || 'null')); } catch { return []; }
    }

    getWatchlist() { return this.parseWatchlist(this.read(WATCHLIST_KEY)); }
    isWatched(symbol) { return this.getWatchlist().some((item) => item.symbol === String(symbol || '').toUpperCase()); }
    add(stock) {
      if (!validStock(stock)) return this.getWatchlist();
      const items = this.getWatchlist(), symbol = String(stock.symbol).toUpperCase();
      if (!items.some((item) => item.symbol === symbol)) items.push({ symbol, name: stock.name.trim() || symbol });
      this.write(WATCHLIST_KEY, JSON.stringify({ schema: 1, items }));
      this.notify(items); return items;
    }
    removeStock(symbol) {
      const items = this.getWatchlist().filter((item) => item.symbol !== String(symbol || '').toUpperCase());
      this.write(WATCHLIST_KEY, JSON.stringify({ schema: 1, items }));
      this.notify(items); return items;
    }
    toggle(stock) { return this.isWatched(stock.symbol) ? this.removeStock(stock.symbol) : this.add(stock); }
    subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); }
    notify(items = this.getWatchlist()) { this.listeners.forEach((listener) => listener([...items])); }
    getScreenerSettings() {
      try { return cleanScreener(JSON.parse(this.read(SCREENER_KEY) || 'null')); } catch { return null; }
    }
    saveScreenerSettings(filters, sort, order) {
      const clean = cleanScreener({ schema: 1, filters, sort, order });
      if (clean) this.write(SCREENER_KEY, JSON.stringify({ schema: 1, ...clean }));
      return clean;
    }
    clearScreenerSettings() { this.remove(SCREENER_KEY); }
    statusMessage() { return this.persistent ? '設定保存在此瀏覽器，不會跨裝置同步' : '目前僅於本頁暫存'; }
  }

  return { StockPreferences, WATCHLIST_KEY, SCREENER_KEY };
});
