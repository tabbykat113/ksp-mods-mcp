"""
Lazy enrichment fetchers for GitHub and SpaceDock data.

Each fetcher checks the cache table first, re-fetching only on a cache miss
or when the TTL has expired (or force_refresh=True). Results are stored in
github_cache / spacedock_cache tables keyed by mod identifier.
"""

import base64
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------

GITHUB_TTL_DAYS    = 7
SPACEDOCK_TTL_DAYS = 3   # download counts change more frequently

PREVIEW_LIMIT = 2000
README_TRUNCATION_NOTE = "\n\n[README truncated. Use GitHub tools or visit the repository for the full content.]"
RELEASE_NOTES_TRUNCATION_NOTE = "\n\n[Release notes truncated. Use GitHub tools or visit the repository for the full content.]"

GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}
if token := os.environ.get("GITHUB_TOKEN"):
    GITHUB_HEADERS["Authorization"] = f"Bearer {token}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_stale(fetched_at: str | None, ttl_days: int) -> bool:
    if fetched_at is None:
        return True
    try:
        dt = datetime.fromisoformat(fetched_at)
        return datetime.now(timezone.utc) - dt > timedelta(days=ttl_days)
    except ValueError:
        return True


def _parse_github_url(url: str) -> tuple[str, str] | None:
    m = re.search(r"github\.com[/:]([^/]+)/([^/\s#?]+?)(?:\.git)?$", url)
    return (m.group(1), m.group(2)) if m else None


def _extract_spacedock_id(url: str) -> int | None:
    m = re.search(r"spacedock\.info/mod/(\d+)", url)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _fetch_github(client: httpx.Client, owner: str, repo: str) -> dict:
    """Fetch repo meta, README preview, and latest release from GitHub API."""
    result: dict = {}

    # Repo meta
    r = client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=GITHUB_HEADERS)
    if r.status_code == 200:
        meta = r.json()
        result["stars"]       = meta.get("stargazers_count")
        result["forks"]       = meta.get("forks_count")
        result["open_issues"] = meta.get("open_issues_count")
        result["language"]    = meta.get("language")
        result["pushed_at"]   = meta.get("pushed_at")
        topics = meta.get("topics") or []
        result["topics"]      = ",".join(topics) if topics else None

    # README
    r = client.get(f"https://api.github.com/repos/{owner}/{repo}/readme", headers=GITHUB_HEADERS)
    if r.status_code == 200:
        data    = r.json()
        content = data.get("content", "")
        if data.get("encoding") == "base64":
            content = base64.b64decode(content).decode("utf-8", errors="replace")
        if len(content) > PREVIEW_LIMIT:
            content = content[:PREVIEW_LIMIT] + README_TRUNCATION_NOTE
        result["readme_preview"] = content

    # Latest release
    r = client.get(
        f"https://api.github.com/repos/{owner}/{repo}/releases",
        headers=GITHUB_HEADERS,
        params={"per_page": 1},
    )
    if r.status_code == 200:
        releases = r.json()
        if releases:
            rel = releases[0]
            result["latest_release_version"] = rel.get("tag_name")
            result["latest_release_date"]    = rel.get("published_at")
            notes = rel.get("body") or ""
            if len(notes) > PREVIEW_LIMIT:
                notes = notes[:PREVIEW_LIMIT] + RELEASE_NOTES_TRUNCATION_NOTE
            result["latest_release_notes"]   = notes or None

    return result


def get_github_cache(
    conn: sqlite3.Connection,
    identifier: str,
    resources: dict,
    force_refresh: bool = False,
) -> dict | None:
    """
    Return GitHub enrichment data for a mod, fetching and caching if needed.
    Returns None if no GitHub URL is available.
    """
    gh_url  = (resources or {}).get("repository") or (resources or {}).get("homepage")
    gh_pair = _parse_github_url(gh_url) if gh_url else None
    if not gh_pair:
        return None

    row = conn.execute(
        "SELECT * FROM github_cache WHERE identifier = ?", (identifier,)
    ).fetchone()

    if row and not force_refresh and not _is_stale(row["fetched_at"], GITHUB_TTL_DAYS):
        return dict(row)

    # Fetch fresh data
    owner, repo = gh_pair
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            data = _fetch_github(client, owner, repo)
    except Exception as e:
        if row:
            return dict(row)  # return stale cache rather than failing
        return {"_fetch_error": str(e)}

    fetched_at = _now_iso()
    conn.execute(
        """
        INSERT OR REPLACE INTO github_cache
            (identifier, fetched_at, stars, forks, open_issues, language,
             pushed_at, topics, readme_preview,
             latest_release_version, latest_release_date, latest_release_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identifier, fetched_at,
            data.get("stars"), data.get("forks"), data.get("open_issues"),
            data.get("language"), data.get("pushed_at"), data.get("topics"),
            data.get("readme_preview"),
            data.get("latest_release_version"), data.get("latest_release_date"),
            data.get("latest_release_notes"),
        ),
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM github_cache WHERE identifier = ?", (identifier,)
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# SpaceDock
# ---------------------------------------------------------------------------

def _fetch_spacedock(client: httpx.Client, sd_id: int) -> dict:
    """Fetch mod info from the SpaceDock API."""
    result: dict = {}
    r = client.get(f"https://spacedock.info/api/mod/{sd_id}")
    if r.status_code != 200:
        return result

    data = r.json()
    result["downloads"]         = data.get("downloads")
    result["followers"]         = data.get("followers")
    result["short_description"] = data.get("short_description")
    result["description"]       = data.get("description") or None

    versions = data.get("versions") or []
    result["version_count"] = len(versions)
    if versions:
        latest = versions[0]
        result["latest_version"]      = latest.get("friendly_version")
        result["latest_version_date"] = latest.get("created")

    return result


def get_spacedock_cache(
    conn: sqlite3.Connection,
    identifier: str,
    resources: dict,
    force_refresh: bool = False,
) -> dict | None:
    """
    Return SpaceDock enrichment data for a mod, fetching and caching if needed.
    Returns None if no SpaceDock URL is available.
    """
    sd_url = (resources or {}).get("spacedock")
    sd_id  = _extract_spacedock_id(sd_url) if sd_url else None
    if sd_id is None:
        return None

    row = conn.execute(
        "SELECT * FROM spacedock_cache WHERE identifier = ?", (identifier,)
    ).fetchone()

    if row and not force_refresh and not _is_stale(row["fetched_at"], SPACEDOCK_TTL_DAYS):
        return dict(row)

    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            data = _fetch_spacedock(client, sd_id)
    except Exception as e:
        if row:
            return dict(row)
        return {"_fetch_error": str(e)}

    fetched_at = _now_iso()
    conn.execute(
        """
        INSERT OR REPLACE INTO spacedock_cache
            (identifier, fetched_at, spacedock_id,
             downloads, followers, short_description, description,
             latest_version, latest_version_date, version_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identifier, fetched_at, sd_id,
            data.get("downloads"), data.get("followers"),
            data.get("short_description"), data.get("description"),
            data.get("latest_version"), data.get("latest_version_date"),
            data.get("version_count"),
        ),
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM spacedock_cache WHERE identifier = ?", (identifier,)
    ).fetchone()
    return dict(row)
