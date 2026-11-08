-- Activity log shown in the admin panel ("Логи"): logins, closed-beta denials, bot starts,
-- tests created/completed (source app/bot) and every change made in the admin panel (source admin).
-- Names are a snapshot: people refused by the closed beta never get an account.
CREATE TABLE IF NOT EXISTS activity_log (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source TEXT NOT NULL CHECK (source IN ('app', 'bot', 'admin')),
    event TEXT NOT NULL,
    max_user_id BIGINT,
    account_id BIGINT,
    first_name TEXT,
    last_name TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS activity_log_created_at_idx ON activity_log (created_at DESC);
CREATE INDEX IF NOT EXISTS activity_log_event_idx ON activity_log (event, created_at DESC);
CREATE INDEX IF NOT EXISTS activity_log_max_user_idx ON activity_log (max_user_id);
