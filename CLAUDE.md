# ckan-indexer — notes for Claude

## Project overview

A uv Python project with two components:
- **`harvest`** CLI — downloads and indexes KSP mod metadata from CKAN-meta into a local SQLite DB
- **`ckan-mcp-server`** — stdio MCP server that exposes search/lookup tools over that DB

## Architecture

```
harvester/
  db.py       — SQLite schema, all query/write functions, KSP version logic
  harvest.py  — tar.gz streaming, parsing loop, CLI entry point
mcp_server/
  server.py   — FastMCP tool definitions, error handling wrapper
explore_mod.py — standalone exploration script (not part of the package)
```

## Key design decisions

- **Single tar.gz download** for Pass 1 — one request gets all ~4000 mods, same approach as CKAN's own C# client. URL: `https://github.com/KSP-CKAN/CKAN-meta/archive/refs/heads/master.tar.gz`
- **ETag-based skip** — re-running harvest is a no-op if nothing changed upstream. ETag stored in the `meta` table.
- **`--force` flag** — bypasses ETag, required when the schema changes and a re-harvest is needed.
- **`mod_versions` table** — stores every `.ckan` file as a version row (~29k rows). The `mods` table stores one row per unique identifier (latest wins). Version history is used for KSP compatibility filtering.
- **`max_ksp_version`** — denormalized onto `mods` at harvest time. Normalized to `major.minor`, capped at `1.12` (KSP1 ceiling). Mods with no upper bound or no constraints default to `1.12`.
- **KSP version filter** — prefix-based tuple comparison in Python (`identifiers_supporting_ksp`), not SQL. Runs against the full `mod_versions` table then passes a set of identifiers into the WHERE clause.
- **`CKAN_DB` env var** — controls DB path for both `harvest` and `ckan-mcp-server`. Essential when installed via `uv tool install` since the package directory isn't writable.
- **Error handling in tools** — `@_tool` decorator catches all exceptions and returns them as `{"error": "..."}` JSON so the model gets a readable message rather than a traceback.

## DB schema

```sql
meta          (key PK, value)                          -- etag storage
mods          (identifier PK, ckan_json, name,
               abstract, tags, max_ksp_version,
               download_count, pass1_at)
mod_versions  (identifier, mod_version PK,
               ksp_version_exact, ksp_version_min,
               ksp_version_max, release_date)
```

`tags` is stored as a comma-separated string. Split on `,` to get a list.
`pass1_at` is an ISO timestamp; null means not yet indexed.

## MCP tools

- `search_mods_tool` — name regex, tags + tags_mode (and/or), ksp_versions, sort_by, limit/offset
- `get_mod_tool` — full CKAN JSON + `_download_count` + `_versions` array
- `list_tags_tool` — all tags ranked by mod count
- `index_status` — DB stats, last harvest time, etag

## Passes (see ROADMAP.md)

Only Pass 1 is implemented. Passes 2 (external enrichment) and 3 (LM synthesis + embeddings) are planned. Schema has nullable columns to support graceful degradation — tools work at whatever enrichment level is present.

## Common tasks

**Force re-harvest after schema change:**
```bash
uv run harvest --force
```

**Run MCP server locally for testing:**
```bash
uv run ckan-mcp-server
```

**Test tools via stdin:**
```bash
printf '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}\n{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"index_status","arguments":{}}}\n' \
  | uv run ckan-mcp-server 2>/dev/null
```
