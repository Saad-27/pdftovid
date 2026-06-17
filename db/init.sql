
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY,
    state VARCHAR(20) NOT NULL DEFAULT 'queued',
    -- queued | validated | extracted | analysed | scripted
    --   | synthesised | timed | done | failed

    progress_percent FLOAT DEFAULT 0,
    current_stage VARCHAR(10),
    -- A | B | C | D | E | F | G

    filename VARCHAR(255),
    voice VARCHAR(50),
    page_count INT,

    video_url VARCHAR(500),
    video_size_bytes BIGINT,

    error_code VARCHAR(50),
    error_message TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,

    -- For Postgres-based queue (SELECT ... FOR UPDATE SKIP LOCKED)
    worker_id VARCHAR(50),
    locked_until TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jobs_queue
    ON jobs(state, locked_until)
    WHERE state = 'queued';

CREATE INDEX IF NOT EXISTS idx_jobs_expires
    ON jobs(expires_at);
