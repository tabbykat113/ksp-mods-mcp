# ckan-indexer

MCP server for searching KSP mods from the [CKAN](https://github.com/KSP-CKAN/CKAN) index.

## Installation

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/tabbykat113/ckan-indexer
```

Then build the index (downloads ~5 MB, takes a few seconds):

```bash
CKAN_DB=~/.local/share/ckan-indexer/ckan.db harvest
```

Re-run `harvest` periodically to pick up new/updated mods. It's a no-op if nothing changed upstream.

## Adding to your MCP client

### Claude Desktop

Edit `claude_desktop_config.json` (find it via **Settings → Developer**):

```json
{
  "mcpServers": {
    "ckan": {
      "command": "ckan-mcp-server",
      "env": {
        "CKAN_DB": "/home/youruser/.local/share/ckan-indexer/ckan.db"
      }
    }
  }
}
```

On Windows, use a full path with forward slashes or escaped backslashes:

```json
"CKAN_DB": "C:/Users/youruser/AppData/Local/ckan-indexer/ckan.db"
```

### Other MCP clients

Use `ckan-mcp-server` as the command with `CKAN_DB` set to wherever you ran `harvest`. The server communicates over stdio.

## Tools

| Tool | Description |
|---|---|
| `search_mods_tool` | Search by name regex, tags, KSP version compatibility. Paginated. |
| `get_mod_tool` | Full details for a mod by identifier, including release history. |
| `list_tags_tool` | All tags in the index ranked by mod count. |
| `index_status` | DB stats and last harvest timestamp. |

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
