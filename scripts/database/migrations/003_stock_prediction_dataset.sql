ALTER TABLE stocks ADD COLUMN IF NOT EXISTS listed_date date;

CREATE TABLE IF NOT EXISTS stock_daily_prices (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    trade_date date NOT NULL,
    open numeric, high numeric, low numeric, close numeric, adjusted_close numeric,
    volume bigint, turnover numeric,
    available_at timestamptz NOT NULL,
    source text NOT NULL,
    quality_flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_stock_daily_prices_date ON stock_daily_prices(trade_date DESC);

CREATE TABLE IF NOT EXISTS stock_prediction_features (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    feature_date date NOT NULL,
    feature_version text NOT NULL,
    feature_available_cutoff timestamptz NOT NULL,
    target_date date,
    features jsonb NOT NULL,
    feature_availability jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, feature_date, feature_version)
);
CREATE INDEX IF NOT EXISTS idx_stock_prediction_features_target ON stock_prediction_features(target_date);

CREATE TABLE IF NOT EXISTS stock_prediction_targets (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    feature_date date NOT NULL,
    horizon smallint NOT NULL CHECK (horizon IN (1,3,5)),
    target_date date NOT NULL,
    target_return numeric NOT NULL,
    target_direction smallint NOT NULL CHECK (target_direction IN (0,1)),
    target_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, feature_date, horizon, target_version)
);
CREATE INDEX IF NOT EXISTS idx_stock_prediction_targets_date ON stock_prediction_targets(target_date);

CREATE TABLE IF NOT EXISTS stock_prediction_runs (
    run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mode text NOT NULL, status text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    stocks_total integer NOT NULL DEFAULT 0, stocks_succeeded integer NOT NULL DEFAULT 0,
    price_rows bigint NOT NULL DEFAULT 0, feature_rows bigint NOT NULL DEFAULT 0,
    target_rows bigint NOT NULL DEFAULT 0, error_message text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
