-- V4.1 additive metadata for canonical stock history tables.
ALTER TABLE stock_valuations ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE stock_valuations ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE stock_valuations ADD COLUMN IF NOT EXISTS fetched_at timestamptz;

ALTER TABLE stock_monthly_revenue ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE stock_monthly_revenue ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE stock_monthly_revenue ADD COLUMN IF NOT EXISTS fetched_at timestamptz;

ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS net_income numeric;
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS fetched_at timestamptz;

ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS fetched_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_stock_valuations_stock_date_desc
    ON stock_valuations (stock_id, valuation_date DESC);
CREATE INDEX IF NOT EXISTS idx_stock_revenue_stock_month_desc
    ON stock_monthly_revenue (stock_id, revenue_month DESC);
CREATE INDEX IF NOT EXISTS idx_stock_financials_stock_period_desc
    ON stock_financials (stock_id, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_stock_chips_stock_date_desc
    ON stock_chips (stock_id, trade_date DESC);
