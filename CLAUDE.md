# ksp-mods-mcp — notes for Claude

## Project overview

A uv Python project with two components:
- **`harvest`** CLI — downloads and indexes KSP mod metadata from CKAN-meta into a local SQLite DB
- **`ksp-mods-mcp`** — stdio MCP server that exposes search/lookup tools over that DB

## Architecture

```
harvester/
  db.py         — SQLite schema, all query/write functions, KSP version logic
  harvest.py    — tar.gz streaming, parsing loop, CLI entry point
  enrichment.py — lazy GitHub/SpaceDock fetchers, cache read/write, TTL logic
  ckan_cache.py — CKAN download cache detection (URL hashing, ZIP scanning)
  parts.py      — KSP part extraction from cached ZIPs (CFG parser, localization)
mcp_server/
  server.py     — FastMCP tool definitions, error handling wrapper
explore_mod.py  — standalone exploration script (not part of the package)
```

## Key design decisions

- **Single tar.gz download** for Pass 1 — one request gets all ~4000 mods, same approach as CKAN's own C# client. URL: `https://github.com/KSP-CKAN/CKAN-meta/archive/refs/heads/master.tar.gz`
- **ETag-based skip** — re-running harvest is a no-op if nothing changed upstream. ETag stored in the `meta` table.
- **Schema versioning** — `SCHEMA_VERSION` constant in `db.py`. `run_harvest()` auto-forces a re-harvest when the stored version in `meta` is stale. Bump `SCHEMA_VERSION` whenever a schema change requires re-harvest; no manual `--force` needed.
- **`mod_versions` table** — stores every `.ckan` file as a version row (~29k rows). The `mods` table stores one row per unique identifier, populated from the latest-by-`release_date` version entry (accumulated in Python during the tar pass, bulk-inserted after). Version history is used for KSP compatibility filtering.
- **`max_ksp_version`** — denormalized onto `mods` at harvest time. Normalized to `major.minor`, capped at `1.12` (KSP1 ceiling). Mods with no upper bound or no constraints default to `1.12`.
- **KSP version filter** — prefix-based tuple comparison in Python (`identifiers_supporting_ksp`), not SQL. Runs against the full `mod_versions` table then passes a set of identifiers into the WHERE clause.
- **Platform-aware DB path** — defaults to `~/.local/share/ksp-mods-mcp/ckan.db` (Linux) or `AppData/Local/ksp-mods-mcp/ckan.db` (Windows). `CKAN_DB` env var overrides.
- **Lazy harvest on first tool call** — the server triggers `run_harvest()` on the first tool invocation (not at startup), keeping MCP startup instant. ETag check makes subsequent calls a no-op.
- **Error handling in tools** — `@_tool` decorator catches all exceptions and returns them as `{"error": "..."}` JSON so the model gets a readable message rather than a traceback.
- **CKAN download cache detection** — `ckan_cache.py` scans the CKAN downloads directory once per session, builds a set of 8-char URL hashes from ZIP filenames, and answers `is_cached(url)` in O(1). CKAN hashes the **percent-decoded** URL (not the raw encoded form). `download_url` in `mods` stores all mirror URLs newline-separated so any mirror match counts. Only the latest version's URLs are checked — an older cached ZIP does not set `is_cached`.
- **Part extraction** — `parts.py` opens a cached ZIP, scans `GameData/{identifier}/Parts/**/*.cfg`, parses KSP's CFG format (brace-depth tracking for MODULE/RESOURCE sub-blocks), and resolves `#LOC_...` strings from `GameData/{identifier}/Localization/en-us.cfg`. Bundled deps under other `GameData/` subdirectories are ignored.

## DB schema

```sql
meta          (key PK, value)                          -- etag, schema_version
mods          (identifier PK, ckan_json, name,
               abstract, tags, authors,
               max_ksp_version, latest_version, last_updated_at,
               download_size, install_size,
               download_count, download_url, pass1_at)
mod_versions  (identifier, mod_version PK,
               ksp_version_exact, ksp_version_min,
               ksp_version_max, release_date,
               download_size, install_size)
github_cache  (identifier PK, fetched_at,
               stars, forks, open_issues, language, pushed_at,
               topics, readme_preview,
               latest_release_version, latest_release_date, latest_release_notes)
spacedock_cache (identifier PK, fetched_at,
                 spacedock_id, downloads, followers,
                 short_description, description,
                 latest_version, latest_version_date, version_count)
```

`tags`, `authors`, and `topics` are stored as comma-separated strings.
`download_url` stores one URL or multiple mirror URLs newline-separated.
`pass1_at`, `last_updated_at`, and `fetched_at` are ISO timestamps.
`last_updated_at` is the `release_date` of the latest version (null if no version had one).

`github_cache` and `spacedock_cache` are populated lazily on `get_mod` calls.
TTL: 7 days for GitHub, 3 days for SpaceDock. `force_refresh=True` on `get_mod` bypasses TTL.
Cache is NOT automatically invalidated when CKAN data changes — see TODO.md.

## MCP tools

- `search_mods_tool` — name/author regex, tags + tags_mode (and/or), ksp_versions, sort_by (`downloads`, `name`, `download_size`, `install_size`, `updated`), `cached_only`, limit/offset; results include `is_cached: true` when applicable
- `get_mod_tool` — details by identifier; selectable categories: metadata, relations, install, versions (includes per-version sizes), github, spacedock, raw; `force_refresh` bypasses enrichment TTL; metadata includes `is_cached: true` when applicable
- `get_recommendations_tool` — related mods for a list of identifiers; categories: `depends`, `supports`, `recommends`, `suggests` (forward) + `depends_by`, `supported_by`, `recommended_by`, `suggested_by` (reverse); default: depends/recommends/suggests; `["all"]` expands to all 8; deduplicates by highest-priority category; each result includes `category` + `related_mods` (source identifiers); `is_cached: true` when applicable; paginated
- `list_tags_tool` — all tags ranked by mod count
- `list_parts_tool` — parts in a mod's cached ZIP; detail levels: `summary` (category counts), `basic` (name+title+category), `long` (+cost, mass, tech_required, modules, resources, bulkhead_profiles); requires mod to be in CKAN download cache
- `index_status` — DB stats (mod count, download counts, tags, size coverage), last harvest time, etag, `ckan_cache_available`
- `refresh_index` — re-harvest CKAN-meta archive (ETag-aware, force option)

## Passes (see ROADMAP.md)

Only Pass 1 is implemented. Passes 2 (external enrichment) and 3 (LM synthesis + embeddings) are planned. Schema has nullable columns to support graceful degradation — tools work at whatever enrichment level is present.

## Common tasks

**Force re-harvest after schema change:**
```bash
uv run harvest --force
```

**Run MCP server locally for testing (harvests lazily on first tool call):**
```bash
uv run ksp-mods-mcp
```

**Test tools via stdin:**
```bash
printf '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}\n{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"index_status","arguments":{}}}\n' \
  | uv run ksp-mods-mcp 2>/dev/null
```
