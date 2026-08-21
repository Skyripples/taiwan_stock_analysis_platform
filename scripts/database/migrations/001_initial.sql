CREATE TABLE stocks (
    stock_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol text NOT NULL UNIQUE,
    name text NOT NULL,
    market text NOT NULL,
    industry text,
    instrument_type text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    cached boolean,
    cache_status text,
    source_updated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_stocks_market_active ON stocks (market, active);
CREATE INDEX idx_stocks_industry ON stocks (industry) WHERE instrument_type = 'company';
CREATE INDEX idx_stocks_name ON stocks (name);

CREATE TABLE stock_quotes (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    trade_date date NOT NULL,
    close numeric,
    change numeric,
    change_percent numeric,
    volume bigint,
    price_unit text,
    volume_unit text,
    source_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trade_date)
);
CREATE INDEX idx_stock_quotes_trade_date ON stock_quotes (trade_date DESC);

CREATE TABLE stock_valuations (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    valuation_date date NOT NULL,
    pe numeric,
    pb numeric,
    dividend_yield numeric,
    pe_date date,
    pb_date date,
    dividend_yield_date date,
    source_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, valuation_date)
);
CREATE INDEX idx_stock_valuations_date ON stock_valuations (valuation_date DESC);

CREATE TABLE stock_monthly_revenue (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    revenue_month date NOT NULL,
    revenue numeric,
    revenue_yoy numeric,
    revenue_mom numeric,
    unit text,
    source_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, revenue_month)
);
CREATE INDEX idx_stock_revenue_month ON stock_monthly_revenue (revenue_month DESC);

CREATE TABLE stock_financials (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    fiscal_year integer NOT NULL,
    quarter smallint NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    statement_scope text NOT NULL DEFAULT '',
    period_end date NOT NULL,
    published_date date,
    available_date date NOT NULL,
    eps numeric,
    revenue numeric,
    gross_margin numeric,
    operating_margin numeric,
    net_margin numeric,
    roe numeric,
    total_assets numeric,
    total_liabilities numeric,
    debt_ratio numeric,
    current_ratio numeric,
    operating_cash_flow numeric,
    investing_cash_flow numeric,
    capital_expenditure numeric,
    free_cash_flow numeric,
    monetary_unit text,
    source text,
    source_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, fiscal_year, quarter, statement_scope)
);
CREATE INDEX idx_stock_financials_available ON stock_financials (available_date DESC);
CREATE INDEX idx_stock_financials_period ON stock_financials (period_end DESC);

CREATE TABLE stock_chips (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    trade_date date NOT NULL,
    foreign_net bigint,
    investment_trust_net bigint,
    dealer_net bigint,
    institutional_total bigint,
    margin_balance bigint,
    margin_change bigint,
    short_balance bigint,
    short_change bigint,
    institutional_unit text,
    margin_unit text,
    source_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trade_date)
);
CREATE INDEX idx_stock_chips_trade_date ON stock_chips (trade_date DESC);

CREATE TABLE stock_health (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    as_of_date date NOT NULL,
    rules_version text NOT NULL DEFAULT '',
    category text NOT NULL,
    metric_key text NOT NULL,
    source_date date,
    value_numeric numeric,
    value_text text,
    threshold_text text,
    status text,
    unit text,
    note text,
    source_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, as_of_date, rules_version, category, metric_key)
);
CREATE INDEX idx_stock_health_status ON stock_health (category, status);

CREATE TABLE industry_rankings (
    industry text NOT NULL,
    metric_key text NOT NULL,
    comparison_period text NOT NULL DEFAULT '',
    stock_id bigint NOT NULL REFERENCES stocks(stock_id),
    company_value numeric,
    industry_median numeric,
    percentile numeric,
    rank integer,
    sample_size integer,
    relative_status text,
    source_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (industry, metric_key, comparison_period, stock_id)
);
CREATE INDEX idx_industry_rankings_lookup ON industry_rankings (industry, metric_key, rank);

CREATE TABLE pipeline_runs (
    run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mode text NOT NULL,
    status text NOT NULL,
    source_updated_at timestamptz,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    row_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_pipeline_runs_started ON pipeline_runs (started_at DESC);
