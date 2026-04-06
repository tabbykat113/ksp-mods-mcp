# ksp-mods-mcp

MCP server for searching KSP mods from the [CKAN](https://github.com/KSP-CKAN/CKAN) index.

## Installation

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/tabbykat113/ksp-mods-mcp
```

That's it. The MCP server automatically builds the index on first launch.

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
| `search_mods_tool` | Search by name/author regex, tags, KSP version compatibility. Paginated. |
| `get_mod_tool` | Details for a mod by identifier. Selectable categories: metadata, relations, install, versions, raw. |
| `list_tags_tool` | All tags in the index ranked by mod count. |
| `index_status` | DB stats and last harvest timestamp. |
| `refresh_index` | Re-harvest the CKAN-meta archive. No-op if unchanged; use `force=True` to rebuild. |

### Search options

- `name` — regex matched against identifier and display name, e.g. `"engineer"`, `"^MechJeb"`, `"visual|scatter"`
- `author` — regex matched against author(s), e.g. `"sarbian"`, `"squad|nertea"`
- `tags` + `tags_mode` — `"and"` (all tags required) or `"or"` (any tag)
- `ksp_versions` — e.g. `["1.12"]`; matches any mod with a release supporting that version
- `sort_by` — `"downloads"` (default), `"downloads asc"`, `"name"`, `"name desc"`, `"download_size"`, `"install_size"`
- `limit` / `offset` — pagination (max 100 per page)

## Development

```bash
git clone https://github.com/tabbykat113/ksp-mods-mcp
cd ksp-mods-mcp
uv sync
uv run harvest
uv run ksp-mods-mcp
```
