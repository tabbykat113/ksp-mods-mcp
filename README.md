# ckan-indexer

MCP server for searching KSP mods from the [CKAN](https://github.com/KSP-CKAN/CKAN) index.

## Installation

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/tabbykat113/ckan-indexer
```

That's it. The MCP server automatically builds the index on first launch.

The database is stored in your platform's data directory (`~/.local/share/ckan-indexer/ckan.db` on Linux, `AppData/Local/ckan-indexer/ckan.db` on Windows). Override with the `CKAN_DB` environment variable if needed.

You can also run `harvest` manually at any time to update the index — it's a no-op if nothing changed upstream.

## Adding to your MCP client

### Claude Desktop

Edit `claude_desktop_config.json` (find it via **Settings → Developer**):

```json
{
  "mcpServers": {
    "ckan": {
      "command": "ckan-mcp-server"
    }
  }
}
```

### Other MCP clients

Use `ckan-mcp-server` as the command. The server communicates over stdio.

## Tools

| Tool | Description |
|---|---|
| `search_mods_tool` | Search by name regex, tags, KSP version compatibility. Paginated. |
| `get_mod_tool` | Full details for a mod by identifier, including release history. |
| `list_tags_tool` | All tags in the index ranked by mod count. |
| `index_status` | DB stats and last harvest timestamp. |
| `refresh_index` | Re-harvest the CKAN-meta archive. No-op if unchanged; use `force=True` to rebuild. |

### Search options

- `name` — regex, e.g. `"engineer"`, `"^MechJeb"`, `"visual\|scatter"`
- `tags` + `tags_mode` — `"and"` (all tags required) or `"or"` (any tag)
- `ksp_versions` — e.g. `["1.12"]`; matches any mod with a release supporting that version
- `sort_by` — `"downloads"` (default), `"downloads asc"`, `"name"`, `"name desc"`
- `limit` / `offset` — pagination (max 100 per page)

## Development

```bash
git clone https://github.com/tabbykat113/ckan-indexer
cd ckan-indexer
uv sync
uv run harvest
uv run ckan-mcp-server
```
