-- Restore the canonical Revenue provenance columns when migration 007 was
-- skipped on an older production database.
ALTER TABLE stock_monthly_revenue ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE stock_monthly_revenue ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE stock_monthly_revenue ADD COLUMN IF NOT EXISTS fetched_at timestamptz;
