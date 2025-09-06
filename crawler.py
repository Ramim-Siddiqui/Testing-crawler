#!/usr/bin/env python3
"""
crawler.py
Crawl GitHub repositories (stars) using GitHub GraphQL API, store into Postgres.

Environment variables used:
  GITHUB_TOKEN (required in Actions provided as secrets.GITHUB_TOKEN)
  PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
  TARGET_REPOS (default 100000)
  DAYS_PER_SLICE (default 1)
  PAGE_SIZE (default 100)
"""

import os
import sys
import time
import json
import math
import logging
from datetime import datetime, timedelta
from dateutil import parser as date_parser
import requests
import psycopg2
from psycopg2.extras import execute_values
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("github-crawler")

# Config from env
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    logger.error("GITHUB_TOKEN not provided. In GitHub Actions you can use secrets.GITHUB_TOKEN.")
    sys.exit(1)

PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = int(os.environ.get("PGPORT", 5432))
PGUSER = os.environ.get("PGUSER", "postgres")
PGPASSWORD = os.environ.get("PGPASSWORD", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "github_data")

TARGET_REPOS = int(os.environ.get("TARGET_REPOS", 100000))
DAYS_PER_SLICE = int(os.environ.get("DAYS_PER_SLICE", 1))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", 100))  # GraphQL max is 100

GQL_URL = "https://api.github.com/graphql"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v4+json",
    "User-Agent": "github-crawler-script"
}

# GraphQL query template:
GQL_QUERY = """
query ($q: String!, $pageSize: Int!, $after: String) {
  rateLimit {
    limit
    cost
    remaining
    resetAt
  }
  search(query: $q, type: REPOSITORY, first: $pageSize, after: $after) {
    repositoryCount
    pageInfo {
      endCursor
      hasNextPage
    }
    edges {
      node {
        ... on Repository {
          databaseId
          nameWithOwner
          stargazerCount
          description
          primaryLanguage { name }
          url
        }
      }
    }
  }
}
"""

# DB helpers
def get_conn():
    conn = psycopg2.connect(
        host=PGHOST, port=PGPORT, user=PGUSER, password=PGPASSWORD, dbname=PGDATABASE
    )
    return conn

def upsert_repos(conn, repo_rows):
    """
    repo_rows: list of tuples (repo_id (int), name_with_owner, stars, description, language, url)
    """
    sql = """
    INSERT INTO repositories (repo_id, name_with_owner, stars, description, language, url)
    VALUES %s
    ON CONFLICT (repo_id) DO UPDATE
      SET stars = EXCLUDED.stars,
          description = EXCLUDED.description,
          language = EXCLUDED.language,
          url = EXCLUDED.url,
          last_updated = now()
    """
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, repo_rows, template=None, page_size=100)
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise



def get_state(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM crawler_state WHERE key = %s", (key,))
        r = cur.fetchone()
        return r[0] if r else None

def set_state(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO crawler_state (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """, (key, value))
    conn.commit()

# GraphQL helper with basic retries and rate-limit handling
def run_graphql(query, variables, max_retries=6):
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(GQL_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    # if rate-limit error or abuse, handle gracefully
                    logger.warning("GraphQL returned errors: %s", data["errors"])
                return data
            elif resp.status_code == 502 or resp.status_code == 503:
                # transient server error
                sleep_time = 2 ** attempt
                logger.warning("Server error %s; sleeping %ss (attempt %d/%d)", resp.status_code, sleep_time, attempt, max_retries)
                time.sleep(sleep_time)
            elif resp.status_code == 401 or resp.status_code == 403:
                logger.error("Auth or permissions error: status_code=%s, body=%s", resp.status_code, resp.text)
                raise Exception(f"Auth/permission issue: {resp.status_code}")
            else:
                logger.warning("Unexpected status %s; body=%s", resp.status_code, resp.text)
                time.sleep(2 ** attempt)
        except requests.exceptions.RequestException as e:
            sleep_time = 2 ** attempt
            logger.warning("Request exception: %s; sleeping %ss", e, sleep_time)
            time.sleep(sleep_time)

    raise Exception("Max retries exceeded for GraphQL request")

def to_date(dt):
    return dt.strftime("%Y-%m-%d")

def parse_repo_node(node):
    dbid = node.get("databaseId")
    name = node.get("nameWithOwner")
    stars = node.get("stargazerCount", 0)
    desc = node.get("description")
    lang = None
    if node.get("primaryLanguage"):
        lang = node["primaryLanguage"].get("name")
    url = node.get("url")
    return (dbid, name, stars, desc, lang, url)

def sleep_until(resetAt_iso):
    """resetAt_iso is ISO time string"""
    reset_time = date_parser.isoparse(resetAt_iso)
    now = datetime.utcnow().replace(tzinfo=reset_time.tzinfo)
    secs = (reset_time - now).total_seconds()
    if secs < 0:
        return
    logger.info("Rate-limit reset at %s (in %d seconds). Sleeping...", resetAt_iso, int(secs) + 2)
    time.sleep(max(0, int(secs) + 2))

def main():
    conn = get_conn()
    logger.info("Connected to DB %s@%s:%s/%s", PGUSER, PGHOST, PGPORT, PGDATABASE)

    # Load progress
    total_collected = int(get_state(conn, "collected_count") or 0)
    logger.info("Previously collected repositories: %d", total_collected)

    # Determine the date window to slice over
    # We'll iterate from some earlier date -> until today, slicing by DAYS_PER_SLICE.
    # To avoid missing old repos, start from 2008-01-01 (GitHub launch ~2008).
    start_date = datetime(2008, 1, 1)
    end_date = datetime.utcnow()

    # Try to resume last slice processed
    last_slice_start_str = get_state(conn, "last_slice_start")
    if last_slice_start_str:
        try:
            start_date = date_parser.parse(last_slice_start_str)
            logger.info("Resuming from last slice start: %s", start_date.isoformat())
        except Exception:
            logger.warning("Failed to parse last_slice_start: %s", last_slice_start_str)

    slice_delta = timedelta(days=DAYS_PER_SLICE)
    current_slice_start = start_date

    # We'll iterate slices until we collect TARGET_REPOS or reach end_date
    pbar = tqdm(total=TARGET_REPOS, desc="collecting repos", unit="repo")
    pbar.update(total_collected)

    while total_collected < TARGET_REPOS and current_slice_start < (end_date + slice_delta):
        # build slice
        slice_end = current_slice_start + slice_delta - timedelta(seconds=1)
        if slice_end > end_date:
            slice_end = end_date

        q = f"created:{to_date(current_slice_start)}..{to_date(slice_end)}"
        logger.info("Searching slice: %s (from %s to %s). collected=%d/%d",
                    q, current_slice_start.date(), slice_end.date(), total_collected, TARGET_REPOS)

        # save progress (we can resume at this slice if interrupted)
        set_state(conn, "last_slice_start", current_slice_start.isoformat())

        # Paginate within the slice
        has_next = True
        after = None
        while has_next and total_collected < TARGET_REPOS:
            variables = {"q": q, "pageSize": PAGE_SIZE, "after": after}
            payload = run_graphql(GQL_QUERY, variables)
            if payload is None:
                logger.error("No payload returned for query; aborting slice")
                break

            # handle errors
            if "errors" in payload:
                logger.warning("GraphQL errors: %s", payload["errors"])
                # If errors indicate excessive result or rate, break/pause
                time.sleep(5)
                # continue attempt; but to avoid loop, break
                break

            # inspect rateLimit
            rate = payload.get("data", {}).get("rateLimit")
            if rate:
                remaining = rate.get("remaining", 0)
                resetAt = rate.get("resetAt")
                logger.debug("Rate limit: remaining=%s resetAt=%s", remaining, resetAt)
                # If remaining is very low, sleep until reset
                if remaining is not None and remaining < 50:
                    # safety margin for other operations in workflow
                    sleep_until(resetAt)

            search = payload.get("data", {}).get("search")
            if not search:
                logger.warning("No search data returned. Response: %s", payload)
                break

            edges = search.get("edges", [])
            repo_nodes = []
            for e in edges:
                node = e.get("node")
                if not node:
                    continue
                parsed = parse_repo_node(node)
                if parsed[0] is None:
                    # skip repositories without databaseId (shouldn't happen)
                    continue
                repo_nodes.append(parsed)

            if repo_nodes:
                # upsert into DB
                try:
                    upsert_repos(conn, repo_nodes)
                    total_collected += len(repo_nodes)
                    set_state(conn, "collected_count", str(total_collected))
                    pbar.update(len(repo_nodes))
                    logger.info("Inserted/updated %d repos (total collected %d)", len(repo_nodes), total_collected)
                except Exception as e:
                    logger.exception("Failed to upsert repos: %s", e)
                    # try to continue after some sleep
                    time.sleep(5)

            page_info = search.get("pageInfo", {})
            has_next = page_info.get("hasNextPage", False)
            after = page_info.get("endCursor")
            # small sleep to be polite and avoid hitting rate limit too fast
            time.sleep(0.5)

        # next slice
        current_slice_start = current_slice_start + slice_delta

    pbar.close()
    logger.info("Done. Total collected: %d", total_collected)
    conn.close()

if __name__ == "__main__":
    main()
