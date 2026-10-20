-- V4.2 additive stock chips history fields. Existing PK/table remain canonical.
ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS foreign_buy bigint;
ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS foreign_sell bigint;
ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS investment_trust_buy bigint;
ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS investment_trust_sell bigint;
ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS dealer_buy bigint;
ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS dealer_sell bigint;
ALTER TABLE stock_chips ADD COLUMN IF NOT EXISTS available_at timestamptz;
