CREATE TABLE IF NOT EXISTS test_results (
    id BIGSERIAL PRIMARY KEY,
    test_id BIGINT NOT NULL,
    user_id BIGINT,
    answers JSONB,                   
    good INTEGER DEFAULT 0,          
    bad INTEGER DEFAULT 0,           
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), 
	CONSTRAINT fk_test
        FOREIGN KEY (test_id) 
        REFERENCES tests(id)
        ON DELETE CASCADE 
);

-- How long the user took, in seconds (reported by the app; NULL for old results).
ALTER TABLE test_results ADD COLUMN IF NOT EXISTS duration_seconds INTEGER;
