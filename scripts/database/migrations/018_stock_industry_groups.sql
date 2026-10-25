CREATE TABLE IF NOT EXISTS industry_groups (
    group_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_key text NOT NULL UNIQUE,
    group_name text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    source text NOT NULL,
    source_url text NOT NULL,
    is_official boolean NOT NULL DEFAULT true,
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stock_industry_groups (
    stock_id bigint NOT NULL REFERENCES stocks(stock_id) ON DELETE CASCADE,
    group_id bigint NOT NULL REFERENCES industry_groups(group_id) ON DELETE CASCADE,
    source text NOT NULL,
    source_url text,
    provenance text NOT NULL,
    evidence text,
    source_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
    fetched_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_stock_industry_groups_group ON stock_industry_groups (group_id, stock_id);
