ALTER TABLE stock_chips
    ADD COLUMN IF NOT EXISTS source text,
    ADD COLUMN IF NOT EXISTS source_date date,
    ADD COLUMN IF NOT EXISTS fetched_at timestamp with time zone;
