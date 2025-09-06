-- schema.sql
-- Primary table for repository star counts and minimal metadata

CREATE TABLE IF NOT EXISTS repositories (
    id BIGSERIAL PRIMARY KEY,
    repo_id BIGINT UNIQUE NOT NULL,             -- GitHub repository node id (database ID)
    name_with_owner TEXT NOT NULL,              -- e.g. "torvalds/linux"
    stars INT NOT NULL,
    description TEXT,
    language TEXT,
    url TEXT,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Small metadata table for resuming progress and storing offsets / slices
CREATE TABLE IF NOT EXISTS crawler_state (
    id BIGSERIAL PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    value TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- index to look up repo quickly by repo_id
CREATE UNIQUE INDEX IF NOT EXISTS idx_repositories_repo_id ON repositories (repo_id);
