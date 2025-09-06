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

## Local development
You do not need Postgres installed locally to run in CI. To run locally for testing, you can run Postgres in Docker:

```bash
docker run --name postgres-dev -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=github_data -p 5432:5432 -d postgres:14
pip install -r requirements.txt
psql -h localhost -U postgres -d github_data -f schema.sql
export GITHUB_TOKEN=ghp_...    # your token (for local testing)
python crawler.py
