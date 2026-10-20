-- V4.2 quarterly financial raw fields and point-in-time metadata.
-- The statement pages do not expose a reliable filing timestamp, so unknown
-- availability must remain NULL instead of using the quarter end/deadline.
ALTER TABLE stock_financials ALTER COLUMN available_date DROP NOT NULL;
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS available_at timestamptz;
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS gross_profit numeric;
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS operating_income numeric;
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS equity numeric;
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS financing_cash_flow numeric;
ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS roa numeric;
