# ksp-mods-mcp

MCP server for searching KSP mods from the [CKAN](https://github.com/KSP-CKAN/CKAN) index.

## Installation

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/tabbykat113/ksp-mods-mcp
```

That's it. The index is built automatically on the first tool call.

The database is stored in your platform's data directory (`~/.local/share/ksp-mods-mcp/ckan.db` on Linux, `AppData/Local/ksp-mods-mcp/ckan.db` on Windows). Override with the `CKAN_DB` environment variable if needed.

You can also run `harvest` manually at any time to update the index — it's a no-op if nothing changed upstream.

## Adding to your MCP client

### Claude Desktop

Edit `claude_desktop_config.json` (find it via **Settings → Developer**):

```json
{
  "mcpServers": {
    "ckan": {
      "command": "ksp-mods-mcp"
    }
  }
}
```

### Other MCP clients

Use `ksp-mods-mcp` as the command. The server communicates over stdio.

## Tools

| Tool | Description |
|---|---|
| `search_mods_tool` | Search by name/author regex, tags, KSP version compatibility, cached status. Paginated. |
| `get_mod_tool` | Details for a mod by identifier. Selectable categories: metadata, relations, install, versions, github, spacedock, raw. |
| `get_recommendations_tool` | Related mods via dependency/recommendation relationships. Forward and reverse. Paginated. |
| `list_tags_tool` | All tags in the index ranked by mod count. |
| `list_parts_tool` | Parts inside a mod's cached ZIP. Three detail levels: summary, basic, long. |
| `index_status` | DB stats, last harvest timestamp, and whether the CKAN download cache is detected. |
| `refresh_index` | Re-harvest the CKAN-meta archive. No-op if unchanged; use `force=True` to rebuild. |

### Search options

- `name` — regex matched against identifier and display name, e.g. `"engineer"`, `"^MechJeb"`, `"visual|scatter"`
- `author` — regex matched against author(s), e.g. `"sarbian"`, `"squad|nertea"`
- `tags` + `tags_mode` — `"and"` (all tags required) or `"or"` (any tag)
- `ksp_versions` — e.g. `["1.12"]`; matches any mod with a release supporting that version
- `sort_by` — `"downloads"` (default), `"downloads asc"`, `"name"`, `"name desc"`, `"download_size"`, `"install_size"`, `"updated"`, `"updated asc"`
- `cached_only` — only return mods whose latest-version ZIP is present in the CKAN download cache
- `limit` / `offset` — pagination (max 100 per page)

### Recommendations options

- `identifiers` — list of CKAN mod identifiers to find relations for
- `categories` — which relationship types to include (default: `depends`, `recommends`, `suggests`):
  - Forward: `depends`, `supports`, `recommends`, `suggests`
  - Reverse: `depends_by`, `supported_by`, `recommended_by`, `suggested_by`
  - Pass `["all"]` to include all categories
- Results are deduplicated: if the same mod appears via multiple sources or categories, the highest-priority category wins and all source mods are listed in `related_mods`
- `limit` / `offset` — pagination (max 100 per page)

### CKAN download cache integration

If CKAN is installed and has downloaded mods, `is_cached: true` appears on any result whose latest-version ZIP is present in the local download cache. The cache directory is detected automatically (`%LOCALAPPDATA%/CKAN/downloads` on Windows, `$XDG_DATA_HOME/CKAN/downloads` on Linux). Override with the `CKAN_DOWNLOAD_CACHE` environment variable.

**Note:** `is_cached` reflects the *latest* version of a mod. If you have an older version cached but not the latest, the flag will not appear — the cached copy may not match what CKAN would install. If you're pinned to an older KSP version (e.g. 1.8.1) and intentionally keep older mod versions, use the `ksp_versions` filter to find compatible mods and disregard the cached status.

The `list_parts_tool` also requires a cached ZIP to work.

## Development

```bash
git clone https://github.com/tabbykat113/ksp-mods-mcp
cd ksp-mods-mcp
uv sync
uv run harvest
uv run ksp-mods-mcp
```
