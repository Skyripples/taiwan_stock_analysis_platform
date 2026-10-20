-- Complete the canonical Financials importer contract on databases created
-- before net income was included in the historical table schema.
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS net_income numeric;
