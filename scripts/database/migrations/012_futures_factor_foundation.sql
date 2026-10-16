BEGIN;

CREATE TABLE IF NOT EXISTS market_factor_series (
    factor_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    value numeric,
    change numeric,
    source text NOT NULL,
    source_date date,
    available_at timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL,
    PRIMARY KEY (factor_id, observed_at, source)
);
CREATE INDEX IF NOT EXISTS market_factor_series_factor_time_idx
    ON market_factor_series (factor_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS macro_events (
    event_type text NOT NULL,
    period text NOT NULL,
    actual numeric,
    consensus numeric,
    previous numeric,
    surprise numeric,
    release_time timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    source text NOT NULL,
    fetched_at timestamptz NOT NULL,
    PRIMARY KEY (event_type, period, source)
);
CREATE INDEX IF NOT EXISTS macro_events_type_available_idx
    ON macro_events (event_type, available_at DESC);

COMMIT;
