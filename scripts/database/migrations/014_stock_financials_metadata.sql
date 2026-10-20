-- Preserve canonical Financials provenance metadata during historical imports.
-- source_date is the source statement period date; fetched_at records when the
-- canonical observation was retrieved and is timezone-aware.
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS fetched_at timestamptz;
