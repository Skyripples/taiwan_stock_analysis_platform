ALTER TABLE futures_intraday_prices
    ADD COLUMN IF NOT EXISTS trading_date date,
    ADD COLUMN IF NOT EXISTS days_to_expiry integer;

CREATE INDEX IF NOT EXISTS idx_futures_intraday_trading_date
    ON futures_intraday_prices(product, trading_date DESC, session, timestamp_utc DESC);

COMMENT ON TABLE futures_intraday_prices IS
    'Completed futures intraday OHLCV. Canonical timestamps are UTC; trading_date follows TAIFEX session rules.';
