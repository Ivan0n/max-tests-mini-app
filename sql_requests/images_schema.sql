-- Task images uploaded by teachers. The file itself lives in S3 under object_key.
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,                -- 32 hex characters (uuid4)
    owner_id BIGINT NOT NULL,           -- accounts.id of the uploader
    object_key TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS images_owner_created_idx ON images (owner_id, created_at);
