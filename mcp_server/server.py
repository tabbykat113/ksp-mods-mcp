"""
CKAN mod index MCP server.

Exposes KSP mod search and detail lookup over stdio transport.

Tools:
  search_mods   — paginated search by name regex and/or tags
  get_mod       — full details for a specific mod identifier
  list_tags     — all tags in the index with mod counts
  index_status  — DB stats and harvest timestamps
"""

import json
import re
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from rich.console import Console

from harvester.ckan_cache import cache_dir_exists, cached_identifiers, is_cached
from harvester.parts import extract_parts
from harvester.db import (
    DB_PATH,
    RELATION_PRIORITY,
    count_search,
    get_mod,
    get_mod_count,
    get_mod_versions,
    get_recommendations,
    list_tags,
    open_db,
    search_mods,
)
from harvester.enrichment import get_github_cache, get_spacedock_cache
from harvester.harvest import run_harvest

mcp = FastMCP(
    "ckan-mod-index",
    instructions=(
        "Search and browse KSP (Kerbal Space Program) mods from the CKAN index. "
        "Use search_mods to find mods by name or tag, get_mod to read full details "
        "for a specific mod, list_tags to explore available categories, and "
        "index_status to check how fresh the data is."
    ),
)

# ---------------------------------------------------------------------------
# REGEXP support for SQLite (not built in by default)
# ---------------------------------------------------------------------------

def _add_regexp(conn: sqlite3.Connection) -> None:
    def regexp(pattern: str, value: str | None) -> bool:
        if value is None:
            return False
        try:
            return bool(re.search(pattern, value, re.IGNORECASE))
        except re.error:
            return False
    conn.create_function("REGEXP", 2, regexp)


def _get_conn() -> sqlite3.Connection:
    conn = open_db()
    _add_regexp(conn)
    return conn


def _tool(fn):
    """Wrap a tool function to return errors as JSON rather than raising."""
    import functools
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return json.dumps({"error": str(e)})
    return wrapper


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
@_tool
def search_mods_tool(
    name: str | None = None,
    author: str | None = None,
    tags: list[str] | None = None,
    tags_mode: str = "and",
    ksp_versions: list[str] | None = None,
    sort_by: str = "downloads",
    cached_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Search KSP mods by name, author, tags, and/or KSP version compatibility, paginated.

    Args:
        name: Case-insensitive regex matched against mod identifier and display name.
              Examples: "engineer", "^MechJeb", "visual|scatter"
        author: Case-insensitive regex matched against mod author(s).
                A mod matches if any of its authors match.
                Examples: "sarbian", "squad|nertea"
        tags: List of tags to filter by.
              Examples: ["plugin"], ["parts", "resources"]
        tags_mode: How to combine multiple tags — "and" (mod must have all tags,
                   default) or "or" (mod must have at least one tag).
        ksp_versions: KSP game versions to filter by. A mod matches if any of its
                      released versions supports at least one of the given KSP versions.
                      Uses prefix matching, so "1.12" matches "1.12.0", "1.12.5", etc.
                      Examples: ["1.12"], ["1.11", "1.12"]
        sort_by: Sort order — "downloads" (default), "downloads asc", "name", "name desc",
                 "download_size", "download_size asc", "install_size", "install_size asc",
                 "updated", "updated asc".
        cached_only: If True, only return mods whose ZIP is present in the local CKAN
                     download cache. Requires CKAN to have been used to download mods.
        limit: Number of results per page (default 20, max 100).
        offset: Pagination offset (default 0).

    Returns JSON with keys: total, offset, limit, results.
    Each result has: identifier, name, abstract, tags, authors, max_ksp_version, latest_version,
    last_updated_at, download_count, download_size (bytes), install_size (bytes).
    Results include is_cached: true only for mods present in the CKAN download cache.
    """
    _ensure_harvested()
    if tags_mode not in ("and", "or"):
        tags_mode = "and"
    if sort_by.split()[0] not in ("downloads", "name", "download_size", "install_size", "updated"):
        sort_by = "downloads"
    limit = min(limit, 100)

    # Resolve cached identifiers if the filter is active or cache exists
    have_cache = cache_dir_exists()
    filter_ids: set[str] | None = None
    if cached_only:
        if not have_cache:
            return json.dumps({"error": "CKAN download cache directory not found. Set CKAN_DOWNLOAD_CACHE env var or ensure CKAN has been run."})
        # Load all download_urls to build the cached set
        conn = _get_conn()
        try:
            url_rows = conn.execute("SELECT identifier, download_url FROM mods WHERE download_url IS NOT NULL").fetchall()
        finally:
            conn.close()
        url_map = {r["identifier"]: r["download_url"] for r in url_rows}
        filter_ids = cached_identifiers(url_map)

    conn = _get_conn()
    try:
        rows  = search_mods(conn, name_pattern=name, tags=tags, tags_mode=tags_mode,
                            ksp_versions=ksp_versions, author_pattern=author,
                            sort_by=sort_by, limit=limit, offset=offset,
                            cached_ids=filter_ids)
        total = count_search(conn, name_pattern=name, tags=tags, tags_mode=tags_mode,
                             ksp_versions=ksp_versions, author_pattern=author,
                             cached_ids=filter_ids)
    finally:
        conn.close()

    def _row_dict(r) -> dict:
        d = {
            "identifier": r["identifier"],
            "name": r["name"],
            "abstract": r["abstract"],
            "tags": r["tags"].split(",") if r["tags"] else [],
            "authors": r["authors"].split(",") if r["authors"] else [],
            "max_ksp_version": r["max_ksp_version"],
            "latest_version": r["latest_version"],
            "last_updated_at": r["last_updated_at"],
            "download_count": r["download_count"],
            "download_size": r["download_size"],
            "install_size": r["install_size"],
        }
        if have_cache and is_cached(r["download_url"]):
            d["is_cached"] = True
        return d

    return json.dumps({
        "total": total,
        "offset": offset,
        "limit": limit,
        "results": [_row_dict(r) for r in rows],
    })


@mcp.tool()
@_tool
def get_mod_tool(
    identifier: str,
    categories: list[str] | None = None,
    force_refresh: bool = False,
) -> str:
    """Get details for a KSP mod by its CKAN identifier.

    Args:
        identifier: Exact CKAN identifier (e.g. "MechJeb2", "Trajectories").
                    Use search_mods to find identifiers first.
        categories: Which detail categories to include. Defaults to ["metadata"].
                    Available categories:
                    - "metadata": name, abstract, authors, tags, license, version info
                      (max_ksp_version, latest_version, last_updated_at), download_count, resources
                    - "relations": depends, recommends, suggests, conflicts, provides
                    - "install": install directives
                    - "versions": full version history with per-version KSP compatibility
                    - "github": GitHub repo stats, README preview, and latest release.
                      Lazily fetched and cached (TTL 7 days). Includes fetched_at timestamp.
                    - "spacedock": SpaceDock stats, descriptions, and latest version info.
                      Lazily fetched and cached (TTL 3 days). Includes fetched_at timestamp.
                    - "raw": full raw CKAN JSON (superset of all above)
        force_refresh: If True, bypass the cache TTL and re-fetch all requested
                       enrichment categories (github, spacedock) from their sources.

    Returns an error object if the mod is not found.
    Metadata results include is_cached: true if the mod's ZIP is in the CKAN download cache.
    """
    _ensure_harvested()
    if categories is None:
        categories = ["metadata"]
    cats = set(categories)

    conn = _get_conn()
    try:
        row = get_mod(conn, identifier)
        if row is None:
            return json.dumps({"error": f"Mod '{identifier}' not found."})
        versions = get_mod_versions(conn, identifier) if "versions" in cats or "raw" in cats else []

        raw = json.loads(row["ckan_json"])
        resources = raw.get("resources") or {}

        github_data    = get_github_cache(conn, identifier, resources, force_refresh) if "github" in cats else None
        spacedock_data = get_spacedock_cache(conn, identifier, resources, force_refresh) if "spacedock" in cats else None
    finally:
        conn.close()

    if "raw" in cats:
        data = json.loads(row["ckan_json"])
        data["_download_count"] = row["download_count"]
        data["_versions"] = [
            {
                "mod_version":       v["mod_version"],
                "ksp_version_exact": v["ksp_version_exact"],
                "ksp_version_max":   v["ksp_version_max"],
                "release_date":      v["release_date"],
                "download_size":     v["download_size"],
                "install_size":      v["install_size"],
            }
            for v in versions
        ]
        return json.dumps(data, indent=2)

    result: dict = {"identifier": identifier}

    if "metadata" in cats:
        result["name"]             = row["name"]
        result["abstract"]         = raw.get("abstract")
        result["authors"]          = row["authors"].split(",") if row["authors"] else []
        result["tags"]             = row["tags"].split(",") if row["tags"] else []
        result["license"]          = raw.get("license")
        result["max_ksp_version"]  = row["max_ksp_version"]
        result["latest_version"]   = row["latest_version"]
        result["last_updated_at"]  = row["last_updated_at"]
        result["download_count"]   = row["download_count"]
        result["resources"]        = resources
        if cache_dir_exists() and is_cached(row["download_url"]):
            result["is_cached"] = True

    if "relations" in cats:
        for key in ("depends", "recommends", "suggests", "conflicts", "provides"):
            if key in raw:
                result[key] = raw[key]

    if "install" in cats:
        if "install" in raw:
            result["install"] = raw["install"]

    if "versions" in cats:
        result["versions"] = [
            {
                "mod_version":       v["mod_version"],
                "ksp_version_exact": v["ksp_version_exact"],
                "ksp_version_max":   v["ksp_version_max"],
                "release_date":      v["release_date"],
                "download_size":     v["download_size"],
                "install_size":      v["install_size"],
            }
            for v in versions
        ]

    if "github" in cats:
        if github_data is None:
            result["github"] = {"error": "No GitHub URL found for this mod."}
        elif "_fetch_error" in github_data:
            result["github"] = {"error": github_data["_fetch_error"]}
        else:
            result["github"] = {
                "fetched_at":              github_data.get("fetched_at"),
                "stars":                   github_data.get("stars"),
                "forks":                   github_data.get("forks"),
                "open_issues":             github_data.get("open_issues"),
                "language":                github_data.get("language"),
                "pushed_at":               github_data.get("pushed_at"),
                "topics":                  github_data["topics"].split(",") if github_data.get("topics") else [],
                "readme_preview":          github_data.get("readme_preview"),
                "latest_release_version":  github_data.get("latest_release_version"),
                "latest_release_date":     github_data.get("latest_release_date"),
                "latest_release_notes":    github_data.get("latest_release_notes"),
            }

    if "spacedock" in cats:
        if spacedock_data is None:
            result["spacedock"] = {"error": "No SpaceDock URL found for this mod."}
        elif "_fetch_error" in spacedock_data:
            result["spacedock"] = {"error": spacedock_data["_fetch_error"]}
        else:
            result["spacedock"] = {
                "fetched_at":         spacedock_data.get("fetched_at"),
                "spacedock_id":       spacedock_data.get("spacedock_id"),
                "downloads":          spacedock_data.get("downloads"),
                "followers":          spacedock_data.get("followers"),
                "short_description":  spacedock_data.get("short_description"),
                "description":        spacedock_data.get("description"),
                "latest_version":     spacedock_data.get("latest_version"),
                "latest_version_date": spacedock_data.get("latest_version_date"),
                "version_count":      spacedock_data.get("version_count"),
            }

    return json.dumps(result, indent=2)


_ALL_RELATION_CATEGORIES = list(RELATION_PRIORITY.keys())
_DEFAULT_RELATION_CATEGORIES = ["depends", "recommends", "suggests"]


@mcp.tool()
@_tool
def get_recommendations_tool(
    identifiers: list[str],
    categories: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Get mods related to a given list of mods via CKAN dependency/recommendation relationships.

    For each result, shows the same fields as search_mods plus the relationship category
    and which input mods it was found through.

    If the same mod appears via multiple input mods or multiple relationship types, it is
    deduplicated: the highest-priority category wins, and all source mods are listed.

    Args:
        identifiers: List of CKAN mod identifiers to find recommendations for.
                     Use search_mods to find identifiers first.
        categories: Which relationship categories to include.
                    Defaults to ["depends", "recommends", "suggests"].
                    Available categories (in priority order):
                    - "depends":       mods that the input mods depend on
                    - "supports":      mods that the input mods declare support for
                    - "recommends":    mods recommended by the input mods
                    - "suggests":      mods suggested by the input mods
                    - "depends_by":    mods that depend on any of the input mods
                    - "supported_by":  mods that declare support for any of the input mods
                    - "recommended_by": mods that recommend any of the input mods
                    - "suggested_by":  mods that suggest any of the input mods
                    Pass ["all"] to include all categories.
        limit: Number of results per page (default 20, max 100).
        offset: Pagination offset (default 0).

    Returns JSON with keys: total, offset, limit, results.
    Each result has: identifier, name, abstract, tags, authors, max_ksp_version,
    latest_version, last_updated_at, download_count, download_size, install_size,
    category, related_mods.
    """
    _ensure_harvested()
    if not identifiers:
        return json.dumps({"error": "identifiers must be a non-empty list."})

    if categories is None:
        categories = _DEFAULT_RELATION_CATEGORIES
    elif categories == ["all"]:
        categories = _ALL_RELATION_CATEGORIES

    valid = set(_ALL_RELATION_CATEGORIES)
    categories = [c for c in categories if c in valid]
    if not categories:
        return json.dumps({"error": f"No valid categories specified. Valid: {_ALL_RELATION_CATEGORIES}"})

    limit = min(limit, 100)
    conn = _get_conn()
    try:
        results = get_recommendations(conn, identifiers, categories)
    finally:
        conn.close()

    total = len(results)
    page  = results[offset: offset + limit]

    have_cache = cache_dir_exists()

    def _rec_dict(r: dict) -> dict:
        d = {
            "identifier":      r["identifier"],
            "name":            r["name"],
            "abstract":        r["abstract"],
            "tags":            r["tags"].split(",") if r["tags"] else [],
            "authors":         r["authors"].split(",") if r["authors"] else [],
            "max_ksp_version": r["max_ksp_version"],
            "latest_version":  r["latest_version"],
            "last_updated_at": r["last_updated_at"],
            "download_count":  r["download_count"],
            "download_size":   r["download_size"],
            "install_size":    r["install_size"],
            "category":        r["category"],
            "related_mods":    r["related_mods"],
        }
        if have_cache and is_cached(r.get("download_url")):
            d["is_cached"] = True
        return d

    return json.dumps({
        "total":   total,
        "offset":  offset,
        "limit":   limit,
        "results": [_rec_dict(r) for r in page],
    })


@mcp.tool()
@_tool
def list_tags_tool(limit: int = 50) -> str:
    """List all tags used in the CKAN mod index, with mod counts.

    Args:
        limit: Maximum number of tags to return, ordered by popularity (default 50).

    Returns JSON list of {tag, count} objects.
    """
    _ensure_harvested()
    conn = _get_conn()
    try:
        tags = list_tags(conn)
    finally:
        conn.close()

    return json.dumps([
        {"tag": tag, "count": count}
        for tag, count in tags[:limit]
    ])


@mcp.tool()
@_tool
def index_status() -> str:
    """Return current status of the CKAN mod index.

    Reports total mod count, number with download data, number with tags,
    and when the index was last harvested.
    """
    _ensure_harvested()
    if not DB_PATH.exists():
        return json.dumps({"status": "not_initialized", "message": "No index yet. Call refresh_index to build it."})

    conn = _get_conn()
    try:
        total            = get_mod_count(conn)
        with_counts      = conn.execute("SELECT count(*) FROM mods WHERE download_count IS NOT NULL").fetchone()[0]
        with_tags        = conn.execute("SELECT count(*) FROM mods WHERE tags IS NOT NULL").fetchone()[0]
        with_dl_size     = conn.execute("SELECT count(*) FROM mods WHERE download_size IS NOT NULL").fetchone()[0]
        with_inst_size   = conn.execute("SELECT count(*) FROM mods WHERE install_size IS NOT NULL").fetchone()[0]
        latest_pass      = conn.execute("SELECT max(pass1_at) FROM mods").fetchone()[0]
        etag_row         = conn.execute("SELECT value FROM meta WHERE key='etag'").fetchone()
    finally:
        conn.close()

    return json.dumps({
        "status": "ready",
        "total_mods": total,
        "mods_with_download_count": with_counts,
        "mods_with_tags": with_tags,
        "mods_with_download_size": with_dl_size,
        "mods_with_install_size": with_inst_size,
        "last_harvested": latest_pass,
        "etag": etag_row[0] if etag_row else None,
        "ckan_cache_available": cache_dir_exists(),
    })


@mcp.tool()
@_tool
def list_parts_tool(
    identifier: str,
    detail: str = "basic",
) -> str:
    """List the KSP parts included in a mod's cached download ZIP.

    Only works for mods whose ZIP is present in the local CKAN download cache
    (i.e., mods you have downloaded via CKAN). Use index_status to check whether
    ckan_cache_available is true, and search_mods with cached_only=true to find
    mods that are cached.

    Parts are scanned from GameData/{identifier}/Parts/**/*.cfg inside the ZIP.
    Bundled dependencies under other GameData/ subdirectories are ignored.
    Titles are resolved from the mod's English localization file where available.

    Args:
        identifier: Exact CKAN mod identifier (e.g. "HeatControl", "NearFuturePropulsion").
        detail: Level of detail to return:
                - "summary": total part count and breakdown by category (cheapest)
                - "basic":   per-part name, resolved title, and category (default)
                - "long":    basic + cost, mass, tech_required, modules (what the part does),
                             resources (what it uses/carries), bulkhead_profiles

    Returns JSON. On success:
      summary: {total_parts, categories: {CategoryName: count}}
      basic/long: {total_parts, categories, parts: [...]}
    Returns an error object if the mod is not cached or the ZIP cannot be read.
    """
    _ensure_harvested()
    if detail not in ("summary", "basic", "long"):
        detail = "basic"

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT download_url FROM mods WHERE identifier = ?", (identifier,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return json.dumps({"error": f"Mod '{identifier}' not found in index."})

    result = extract_parts(identifier, row["download_url"], detail=detail)  # type: ignore[arg-type]
    return json.dumps(result)


_quiet_console = Console(quiet=True)
_harvest_done = False


def _ensure_harvested() -> None:
    global _harvest_done
    if not _harvest_done:
        run_harvest(console=_quiet_console)
        _harvest_done = True


@mcp.tool()
@_tool
def refresh_index(force: bool = False) -> str:
    """Re-harvest the CKAN-meta archive to update the mod index.

    By default, this is a no-op if nothing has changed upstream (ETag check).
    Use force=True to bypass the check and rebuild unconditionally.

    Args:
        force: Bypass ETag check and re-download/re-index everything.

    Returns JSON with harvest result: status ("skipped" or "updated") and stats.
    """
    global _harvest_done
    result = run_harvest(force=force, console=_quiet_console)
    _harvest_done = True
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
