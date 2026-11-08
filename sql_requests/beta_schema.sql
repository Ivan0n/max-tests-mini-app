-- Closed beta, managed from the admin panel (max-test-admin-app).
-- MAX user IDs (user.id from window.WebApp.initData) allowed in while the beta is on.
CREATE TABLE IF NOT EXISTS beta_invites (
    max_user_id BIGINT PRIMARY KEY CHECK (max_user_id > 0),
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Key/value settings. {"beta_enabled": true} closes the service to everyone without an invite.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
