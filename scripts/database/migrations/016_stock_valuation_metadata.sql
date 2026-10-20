-- Preserve canonical Valuation provenance metadata during historical imports.
ALTER TABLE stock_valuations ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE stock_valuations ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE stock_valuations ADD COLUMN IF NOT EXISTS fetched_at timestamptz;
