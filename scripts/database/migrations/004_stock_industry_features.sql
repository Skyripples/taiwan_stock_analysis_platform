CREATE TABLE IF NOT EXISTS stock_industry_daily_features (
    industry text NOT NULL,
    trade_date date NOT NULL,
    feature_version text NOT NULL,
    industry_return_1d numeric,
    industry_return_5d numeric,
    industry_advancing_ratio numeric,
    sample_size integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(industry,trade_date,feature_version)
);
CREATE INDEX IF NOT EXISTS idx_stock_industry_daily_date ON stock_industry_daily_features(trade_date DESC);
