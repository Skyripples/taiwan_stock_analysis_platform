CREATE TABLE IF NOT EXISTS auth_users (
    user_id uuid PRIMARY KEY,
    username text NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'user',
    is_active boolean NOT NULL DEFAULT true,
    failed_attempts integer NOT NULL DEFAULT 0,
    locked_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_users_username_lower ON auth_users (lower(username));

CREATE TABLE IF NOT EXISTS auth_webauthn_credentials (
    credential_id bytea PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth_users(user_id) ON DELETE CASCADE,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL DEFAULT 0,
    transports text[] NOT NULL DEFAULT '{}',
    device_type text,
    backed_up boolean,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_auth_credentials_user ON auth_webauthn_credentials (user_id);

CREATE TABLE IF NOT EXISTS auth_challenges (
    flow_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth_users(user_id) ON DELETE CASCADE,
    ceremony text NOT NULL CHECK (ceremony IN ('registration', 'authentication')),
    challenge bytea NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auth_challenges_expiry ON auth_challenges (expires_at);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash bytea PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth_users(user_id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_expiry ON auth_sessions (user_id, expires_at DESC);

CREATE TABLE IF NOT EXISTS auth_audit_log (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id uuid REFERENCES auth_users(user_id) ON DELETE SET NULL,
    event text NOT NULL,
    success boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auth_audit_created ON auth_audit_log (created_at DESC);
