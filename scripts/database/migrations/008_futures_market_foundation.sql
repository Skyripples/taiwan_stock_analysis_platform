CREATE TABLE IF NOT EXISTS futures_daily_prices (
    trade_date date NOT NULL,
    product text NOT NULL CHECK (product IN ('TX','MTX','TMF')),
    contract text NOT NULL,
    session text NOT NULL CHECK (session IN ('regular','after_hours')),
    open numeric,
    high numeric,
    low numeric,
    close numeric,
    settlement_price numeric,
    volume bigint,
    open_interest bigint,
    expiry_date date,
    days_to_expiry integer,
    contract_rank text CHECK (contract_rank IN ('front','next','quarter') OR contract_rank IS NULL),
    roll_date date,
    is_expiry_week boolean,
    spot_index numeric,
    basis numeric,
    basis_pct numeric,
    source text NOT NULL,
    source_date date NOT NULL,
    available_at timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, product, contract, session)
);
CREATE INDEX IF NOT EXISTS idx_futures_daily_product_date
    ON futures_daily_prices(product, session, trade_date DESC, contract);

CREATE TABLE IF NOT EXISTS futures_intraday_prices (
    timestamp_utc timestamptz NOT NULL,
    interval_seconds integer NOT NULL CHECK (interval_seconds > 0),
    product text NOT NULL CHECK (product IN ('TX','MTX','TMF')),
    contract text NOT NULL,
    session text NOT NULL CHECK (session IN ('regular','after_hours')),
    open numeric,
    high numeric,
    low numeric,
    close numeric,
    volume bigint,
    source text NOT NULL,
    available_at timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (product, contract, session, timestamp_utc, interval_seconds)
);
CREATE INDEX IF NOT EXISTS idx_futures_intraday_lookup
    ON futures_intraday_prices(product, contract, session, interval_seconds, timestamp_utc DESC);

CREATE TABLE IF NOT EXISTS futures_institutional_positions (
    trade_date date NOT NULL,
    product text NOT NULL,
    investor_type text NOT NULL CHECK (investor_type IN ('foreign','dealer','investment_trust')),
    long_oi bigint,
    short_oi bigint,
    net_oi bigint,
    source text NOT NULL,
    source_date date NOT NULL,
    available_at timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, product, investor_type)
);
CREATE INDEX IF NOT EXISTS idx_futures_positions_lookup
    ON futures_institutional_positions(product, investor_type, trade_date DESC);

CREATE TABLE IF NOT EXISTS futures_options_context (
    trade_date date NOT NULL,
    product text NOT NULL DEFAULT 'TXO',
    put_volume bigint,
    call_volume bigint,
    put_call_volume_ratio numeric,
    put_oi bigint,
    call_oi bigint,
    put_call_oi_ratio numeric,
    source text NOT NULL,
    source_date date NOT NULL,
    available_at timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, product)
);
CREATE INDEX IF NOT EXISTS idx_futures_options_context_date
    ON futures_options_context(product, trade_date DESC);

COMMENT ON TABLE futures_intraday_prices IS
    'V4.3 schema foundation only; no claim of free official five-minute history availability.';
