CREATE TABLE IF NOT EXISTS data_sources (
    source_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_key text NOT NULL UNIQUE,
    name text NOT NULL,
    official boolean NOT NULL,
    base_url text,
    transport text NOT NULL,
    authentication_type text NOT NULL DEFAULT 'none',
    license_notes text,
    rate_limit_notes text,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market_instruments (
    instrument_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id bigint NOT NULL REFERENCES data_sources(source_id),
    canonical_symbol text NOT NULL UNIQUE,
    source_symbol text NOT NULL,
    name text NOT NULL,
    category text NOT NULL,
    market text,
    country text,
    exchange text,
    currency text,
    timezone text NOT NULL,
    native_frequency text NOT NULL,
    trading_hours text,
    adjusted_policy text,
    inception_date date,
    active boolean NOT NULL DEFAULT true,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_symbol)
);
CREATE INDEX IF NOT EXISTS idx_market_instruments_category ON market_instruments(category, active);
CREATE INDEX IF NOT EXISTS idx_market_instruments_market ON market_instruments(market, country);

CREATE TABLE IF NOT EXISTS market_daily_prices (
    instrument_id bigint NOT NULL REFERENCES market_instruments(instrument_id),
    trade_date date NOT NULL,
    open numeric,
    high numeric,
    low numeric,
    close numeric,
    adjusted_close numeric,
    volume numeric,
    currency text,
    session text NOT NULL DEFAULT 'regular',
    timezone text NOT NULL,
    available_at timestamptz NOT NULL,
    source_updated_at timestamptz,
    quality_flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_market_daily_lookup ON market_daily_prices(instrument_id, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_market_daily_available ON market_daily_prices(available_at);

CREATE TABLE IF NOT EXISTS market_intraday_prices (
    instrument_id bigint NOT NULL REFERENCES market_instruments(instrument_id),
    timestamp_utc timestamptz NOT NULL,
    interval_seconds integer NOT NULL CHECK (interval_seconds > 0),
    open numeric,
    high numeric,
    low numeric,
    close numeric,
    volume numeric,
    timezone text NOT NULL DEFAULT 'UTC',
    available_at timestamptz NOT NULL,
    quality_flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, timestamp_utc, interval_seconds)
);
CREATE INDEX IF NOT EXISTS idx_market_intraday_lookup ON market_intraday_prices(instrument_id, interval_seconds, timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_market_intraday_available ON market_intraday_prices(available_at);

CREATE TABLE IF NOT EXISTS macro_series (
    macro_series_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id bigint NOT NULL REFERENCES data_sources(source_id),
    series_key text NOT NULL UNIQUE,
    source_series_id text NOT NULL,
    name text NOT NULL,
    country text,
    frequency text NOT NULL,
    unit text NOT NULL,
    seasonal_adjustment text,
    timezone text NOT NULL,
    release_rule text,
    inception_date date,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_series_id)
);

CREATE TABLE IF NOT EXISTS macro_observations (
    macro_series_id bigint NOT NULL REFERENCES macro_series(macro_series_id),
    observation_date date NOT NULL,
    available_at timestamptz NOT NULL,
    value numeric,
    value_text text,
    unit text NOT NULL,
    vintage_date date,
    preliminary boolean,
    source_updated_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (macro_series_id, observation_date, available_at)
);
CREATE INDEX IF NOT EXISTS idx_macro_observation_lookup ON macro_observations(macro_series_id, observation_date DESC);
CREATE INDEX IF NOT EXISTS idx_macro_observation_available ON macro_observations(macro_series_id, available_at DESC);

CREATE TABLE IF NOT EXISTS market_features (
    feature_key text NOT NULL,
    target_date date NOT NULL,
    target_scope text NOT NULL DEFAULT 'TAIEX',
    cutoff_at timestamptz NOT NULL,
    value numeric,
    available_at timestamptz NOT NULL,
    source_instrument_id bigint REFERENCES market_instruments(instrument_id),
    transform_version text NOT NULL,
    quality_flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feature_key, target_date, target_scope, transform_version)
);
CREATE INDEX IF NOT EXISTS idx_market_features_target ON market_features(target_scope, target_date DESC);
CREATE INDEX IF NOT EXISTS idx_market_features_available ON market_features(available_at);

CREATE TABLE IF NOT EXISTS global_backfill_checkpoints (
    task_key text PRIMARY KEY,
    status text NOT NULL,
    last_cursor text,
    rows_written bigint NOT NULL DEFAULT 0,
    started_at timestamptz,
    finished_at timestamptz,
    error_message text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
