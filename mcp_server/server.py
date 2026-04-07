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

from harvester.db import (
    DB_PATH,
    count_search,
    get_mod,
    get_mod_count,
    get_mod_versions,
    list_tags,
    open_db,
    search_mods,
)
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
        limit: Number of results per page (default 20, max 100).
        offset: Pagination offset (default 0).

    Returns JSON with keys: total, offset, limit, results.
    Each result has: identifier, name, abstract, tags, authors, max_ksp_version, latest_version,
    last_updated_at, download_count, download_size (bytes), install_size (bytes).
    """
    _ensure_harvested()
    if tags_mode not in ("and", "or"):
        tags_mode = "and"
    if sort_by.split()[0] not in ("downloads", "name", "download_size", "install_size", "updated"):
        sort_by = "downloads"
    limit = min(limit, 100)
    conn = _get_conn()
    try:
        rows  = search_mods(conn, name_pattern=name, tags=tags, tags_mode=tags_mode,
                            ksp_versions=ksp_versions, author_pattern=author,
                            sort_by=sort_by, limit=limit, offset=offset)
        total = count_search(conn, name_pattern=name, tags=tags, tags_mode=tags_mode,
                             ksp_versions=ksp_versions, author_pattern=author)
    finally:
        conn.close()

    return json.dumps({
        "total": total,
        "offset": offset,
        "limit": limit,
        "results": [
            {
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
            for r in rows
        ],
    })


@mcp.tool()
@_tool
def get_mod_tool(
    identifier: str,
    categories: list[str] | None = None,
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
                    - "raw": full raw CKAN JSON (superset of all above)

    Returns an error object if the mod is not found.
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

    raw = json.loads(row["ckan_json"])
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
        result["resources"]        = raw.get("resources")

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

    return json.dumps(result, indent=2)


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
    })


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
