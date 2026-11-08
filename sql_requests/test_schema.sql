CREATE TABLE IF NOT EXISTS tests (
    id BIGSERIAL PRIMARY KEY,
    url_path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    data JSONB NOT NULL,
    author_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- CREATE TABLE IF NOT EXISTS doesn't upgrade tables created by older versions.
CREATE UNIQUE INDEX IF NOT EXISTS tests_url_path_key ON tests (url_path);

