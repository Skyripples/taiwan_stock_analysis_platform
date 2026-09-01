CREATE TABLE IF NOT EXISTS auth_feature_permissions (
    user_id uuid NOT NULL REFERENCES auth_users(user_id) ON DELETE CASCADE,
    feature_key text NOT NULL CHECK (feature_key IN (
        'calendar', 'prediction', 'market_overview',
        'chips_analysis', 'backtest', 'stock_analysis'
    )),
    allowed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, feature_key)
);
CREATE INDEX IF NOT EXISTS idx_auth_permissions_user_allowed
    ON auth_feature_permissions (user_id, allowed);
