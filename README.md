# GitHub Crawler (GitHub Actions + Postgres)

This repo contains a GitHub Actions pipeline and Python crawler to collect repository star counts from GitHub using the GraphQL API and store them in Postgres.

## What is included
- `.github/workflows/main.yml` — GitHub Actions workflow that spins up a Postgres service container, runs schema creation, runs the crawler, and uploads CSV & DB dump artifacts.
- `schema.sql` — DB schema for `repositories` and `crawler_state`.
- `crawler.py` — Python script that uses GitHub GraphQL search + date-slice strategy to collect repositories and store their star counts.
- `requirements.txt` — Python dependencies.

## How it works
- The workflow uses the default `secrets.GITHUB_TOKEN` (no extra privileges needed).
- The crawler uses GitHub GraphQL search with `created:YYYY-MM-DD..YYYY-MM-DD` slices to avoid the 1,000-result search cap.
- The crawler paginates results, writes to Postgres with `ON CONFLICT` upserts, and stores progress so it can resume across runs.
- After the run, the pipeline exports a `repos.csv` artifact and a `db.dump`.
- **Note**: Due to GitHub API rate limits, the current configuration is set to collect **5,000 repositories** for testing.  
  The pipeline is designed to scale to **100,000 repositories**, but larger runs may take hours or multiple executions.

## Local development
You do not need Postgres installed locally to run in CI. To run locally for testing, you can run Postgres in Docker:

```bash
docker run --name postgres-dev -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=github_data -p 5432:5432 -d postgres:14
pip install -r requirements.txt
psql -h localhost -U postgres -d github_data -f schema.sql
export GITHUB_TOKEN=ghp_...    # your token (for local testing)
python crawler.py
```
# Design Questions
## 1. Schema Evolution (Issues, PRs, Comments, Reviews, CI checks)

The current schema tracks repositories and their star counts. To evolve it for more GitHub metadata:

Issues & PRs:
Create separate tables with foreign keys back to repositories.
```bash
CREATE TABLE issues (
    issue_id BIGINT UNIQUE PRIMARY KEY,
    repo_id BIGINT REFERENCES repositories(repo_id),
    title TEXT,
    state TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    last_synced TIMESTAMP DEFAULT now()
);

CREATE TABLE pull_requests (
    pr_id BIGINT UNIQUE PRIMARY KEY,
    repo_id BIGINT REFERENCES repositories(repo_id),
    title TEXT,
    state TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    last_synced TIMESTAMP DEFAULT now()
);
```

Comments & Reviews:
Use separate tables linked to issues or PRs.
```bash
CREATE TABLE comments (
    comment_id BIGINT UNIQUE PRIMARY KEY,
    parent_type TEXT CHECK (parent_type IN ('issue','pr')),
    parent_id BIGINT NOT NULL,
    body TEXT,
    author TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    last_synced TIMESTAMP DEFAULT now()
);

CREATE TABLE reviews (
    review_id BIGINT UNIQUE PRIMARY KEY,
    pr_id BIGINT REFERENCES pull_requests(pr_id),
    state TEXT,
    author TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    last_synced TIMESTAMP DEFAULT now()
);

```
Commits & CI checks:
Add tables for commits and link CI results to them.

Efficient updates:

Use ON CONFLICT DO UPDATE so only changed rows are updated.

Track last_synced timestamps to fetch only new/updated data from GitHub.

Append-only event tables (for comments, CI runs) allow incremental growth without rewriting history.

This ensures adding metadata does not require rewriting existing rows and keeps daily refreshes efficient.

## 2. Scaling to 500 Million Repositories

For 100k repositories, Postgres and a single crawler are sufficient.
At 500M scale, several things must change:

### Database

Postgres cannot handle hundreds of millions of rows efficiently.

Use a distributed data warehouse (BigQuery, Snowflake, Redshift) or a sharded NoSQL store (Cassandra, DynamoDB).

### Storage

Store raw GraphQL responses in object storage (S3/GCS/Azure Blob).

Process them into structured tables for analytics.

### Crawling

Replace the single crawler with a distributed system (many workers or Kubernetes jobs).

Use queues (Kafka, RabbitMQ, SQS) to distribute work.

### Rate limits

A single GitHub token (5k requests/hour) cannot cover 500M repos in realistic time.

### Strategies:

Use multiple tokens/accounts (token pools).

Partner with GitHub or use public datasets (GH Archive, GHTorrent).

Implement incremental updates (crawl only new/changed repos).

### Schema

Partition tables by repo_id or by date.

Use append-only event tables for issues, PRs, comments.

Update with upserts (ON CONFLICT) for minimal row changes.

### Processing

Switch to batch or streaming pipelines (Spark, Flink, Kafka).

Materialize views for queries instead of reading raw tables directly.

At this scale, the problem shifts from “writing a crawler” to designing a distributed data platform that can ingest, store, and query hundreds of millions of entities efficiently.