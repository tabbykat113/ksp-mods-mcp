import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _default_db_path() -> Path:
    """Platform-aware default DB location. CKAN_DB env var overrides."""
    if "CKAN_DB" in os.environ:
        return Path(os.environ["CKAN_DB"])
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ksp-mods-mcp" / "ckan.db"


DB_PATH = _default_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mods (
    identifier       TEXT PRIMARY KEY,
    ckan_json        TEXT NOT NULL,
    name             TEXT,
    abstract         TEXT,
    tags             TEXT,
    authors          TEXT,
    max_ksp_version  TEXT,
    latest_version   TEXT,
    last_updated_at  TEXT,
    download_size    INTEGER,
    install_size     INTEGER,
    download_count   INTEGER,
    pass1_at         TEXT
);

CREATE TABLE IF NOT EXISTS mod_versions (
    identifier          TEXT NOT NULL,
    mod_version         TEXT NOT NULL,
    ksp_version_exact   TEXT,
    ksp_version_min     TEXT,
    ksp_version_max     TEXT,
    release_date        TEXT,
    download_size       INTEGER,
    install_size        INTEGER,
    PRIMARY KEY (identifier, mod_version)
);

CREATE INDEX IF NOT EXISTS idx_mod_versions_identifier ON mod_versions (identifier);
"""

MIGRATIONS = [
    "ALTER TABLE mods ADD COLUMN tags TEXT",
    "ALTER TABLE mods ADD COLUMN ksp_version TEXT",
    "ALTER TABLE mods ADD COLUMN max_ksp_version TEXT",
    "ALTER TABLE mods ADD COLUMN authors TEXT",
    "ALTER TABLE mods ADD COLUMN latest_version TEXT",
    "ALTER TABLE mods ADD COLUMN download_size INTEGER",
    "ALTER TABLE mods ADD COLUMN install_size INTEGER",
    "ALTER TABLE mods ADD COLUMN last_updated_at TEXT",
    "ALTER TABLE mod_versions ADD COLUMN download_size INTEGER",
    "ALTER TABLE mod_versions ADD COLUMN install_size INTEGER",
]


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists / already applied


def get_etag(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key='etag'").fetchone()
    return row[0] if row else None


def set_etag(conn: sqlite3.Connection, etag: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('etag', ?)", (etag,))


def apply_mod_data(conn: sqlite3.Connection, mod_data: dict[str, dict]) -> None:
    """Bulk-insert one row per identifier using the latest-version's metadata.

    mod_data maps identifier → dict with keys:
      ckan_json, name, abstract, tags (list|None), authors (list|None),
      download_size (int|None), install_size (int|None),
      latest_version (str|None), last_updated_at (str|None), pass1_at (str ISO).
    """
    rows = [
        (
            ident,
            d["ckan_json"],
            d["name"],
            d["abstract"],
            ",".join(d["tags"]) if d["tags"] else None,
            ",".join(d["authors"]) if d["authors"] else None,
            d["download_size"],
            d["install_size"],
            None,               # download_count applied separately
            d["latest_version"],
            d["last_updated_at"] or None,
            d["pass1_at"],
        )
        for ident, d in mod_data.items()
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO mods
            (identifier, ckan_json, name, abstract, tags, authors,
             download_size, install_size, download_count, latest_version, last_updated_at, pass1_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def upsert_mod_version(
    conn: sqlite3.Connection,
    identifier: str,
    mod_version: str,
    ksp_version_exact: str | None,
    ksp_version_min: str | None,
    ksp_version_max: str | None,
    release_date: str | None,
    download_size: int | None = None,
    install_size: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO mod_versions
            (identifier, mod_version, ksp_version_exact, ksp_version_min, ksp_version_max,
             release_date, download_size, install_size)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (identifier, mod_version, ksp_version_exact, ksp_version_min, ksp_version_max,
         release_date, download_size, install_size),
    )


def apply_download_counts(conn: sqlite3.Connection, counts: dict[str, int]) -> None:
    conn.executemany(
        "UPDATE mods SET download_count = ? WHERE identifier = ?",
        ((count, ident) for ident, count in counts.items()),
    )


def apply_max_ksp_versions(conn: sqlite3.Connection, max_versions: dict[str, str]) -> None:
    conn.executemany(
        "UPDATE mods SET max_ksp_version = ? WHERE identifier = ?",
        ((ver, ident) for ident, ver in max_versions.items()),
    )



def get_mod_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM mods").fetchone()[0]


# ---------------------------------------------------------------------------
# KSP version compatibility
# ---------------------------------------------------------------------------

def _parse_ver(v: str) -> tuple[int, ...]:
    """Parse a KSP version string into a comparable tuple. '1.12.5' → (1, 12, 5)."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _ver_gte(a: str, b: str) -> bool:
    return _parse_ver(a) >= _parse_ver(b)


def _ver_lte(a: str, b: str) -> bool:
    return _parse_ver(a) <= _parse_ver(b)


def _version_row_supports(row: sqlite3.Row, target: str) -> bool:
    """Return True if a mod_versions row covers the given KSP target version."""
    exact = row["ksp_version_exact"]
    mn    = row["ksp_version_min"]
    mx    = row["ksp_version_max"]
    t     = _parse_ver(target)

    if exact:
        # prefix match: "1.12" matches "1.12.5", "1.12.0", etc.
        e = _parse_ver(exact)
        return e[: len(t)] == t[: len(e)]

    min_ok = _ver_lte(mn, target) if mn else True
    max_ok = _ver_gte(mx, target) if mx else True
    return min_ok and max_ok


def identifiers_supporting_ksp(
    conn: sqlite3.Connection,
    ksp_versions: list[str],
) -> set[str]:
    """
    Return the set of mod identifiers that have at least one version entry
    compatible with any of the given KSP versions.
    """
    rows = conn.execute(
        "SELECT identifier, ksp_version_exact, ksp_version_min, ksp_version_max FROM mod_versions"
    ).fetchall()

    result: set[str] = set()
    for row in rows:
        for kv in ksp_versions:
            if _version_row_supports(row, kv):
                result.add(row["identifier"])
                break
    return result


# ---------------------------------------------------------------------------
# Search / lookup (used by MCP server)
# ---------------------------------------------------------------------------

def _build_where(
    name_pattern: str | None,
    tags: list[str] | None,
    tags_mode: str,
    ksp_filter_ids: set[str] | None,
    author_pattern: str | None = None,
) -> tuple[str, list]:
    """Build WHERE clause and params for mod searches."""
    wheres: list[str] = []
    params: list = []

    if name_pattern:
        wheres.append("(identifier REGEXP ? OR name REGEXP ?)")
        params += [name_pattern, name_pattern]

    if author_pattern:
        wheres.append("authors REGEXP ?")
        params.append(author_pattern)

    if tags:
        tag_clauses = ["(',' || tags || ',' LIKE ?)" for _ in tags]
        tag_params  = [f"%,{tag},%" for tag in tags]
        joiner = " AND " if tags_mode == "and" else " OR "
        wheres.append("(" + joiner.join(tag_clauses) + ")")
        params += tag_params

    if ksp_filter_ids is not None:
        if not ksp_filter_ids:
            wheres.append("0")  # empty set → no results
        else:
            placeholders = ",".join("?" * len(ksp_filter_ids))
            wheres.append(f"identifier IN ({placeholders})")
            params += list(ksp_filter_ids)

    where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    return where_clause, params


def search_mods(
    conn: sqlite3.Connection,
    name_pattern: str | None = None,
    tags: list[str] | None = None,
    tags_mode: str = "and",
    ksp_versions: list[str] | None = None,
    author_pattern: str | None = None,
    sort_by: str = "downloads",
    limit: int = 20,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """
    Search mods by name regex, tags, author regex, and/or KSP version compatibility.
    - sort_by: "downloads" (default) or "name"
    """
    ksp_ids = identifiers_supporting_ksp(conn, ksp_versions) if ksp_versions else None
    where_clause, params = _build_where(name_pattern, tags, tags_mode, ksp_ids, author_pattern)
    parts = sort_by.lower().split()
    key, direction = parts[0], parts[1] if len(parts) > 1 else None

    if key == "name":
        dir_sql = "DESC" if direction == "desc" else "ASC"
        order = f"name COLLATE NOCASE {dir_sql}"
    elif key == "download_size":
        dir_sql = "ASC" if direction == "asc" else "DESC"
        order = f"download_size {dir_sql} NULLS LAST"
    elif key == "install_size":
        dir_sql = "ASC" if direction == "asc" else "DESC"
        order = f"install_size {dir_sql} NULLS LAST"
    elif key == "updated":
        dir_sql = "ASC" if direction == "asc" else "DESC"
        order = f"last_updated_at {dir_sql} NULLS LAST"
    else:  # downloads
        dir_sql = "ASC" if direction == "asc" else "DESC"
        order = f"download_count {dir_sql} NULLS LAST"
    return conn.execute(
        f"""
        SELECT identifier, name, abstract, tags, authors, max_ksp_version, latest_version,
               last_updated_at, download_count, download_size, install_size
        FROM mods
        {where_clause}
        ORDER BY {order}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()


def count_search(
    conn: sqlite3.Connection,
    name_pattern: str | None = None,
    tags: list[str] | None = None,
    tags_mode: str = "and",
    ksp_versions: list[str] | None = None,
    author_pattern: str | None = None,
) -> int:
    ksp_ids = identifiers_supporting_ksp(conn, ksp_versions) if ksp_versions else None
    where_clause, params = _build_where(name_pattern, tags, tags_mode, ksp_ids, author_pattern)
    return conn.execute(
        f"SELECT count(*) FROM mods {where_clause}", params
    ).fetchone()[0]


def get_mod(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM mods WHERE identifier = ?", (identifier,)
    ).fetchone()


def get_mod_versions(conn: sqlite3.Connection, identifier: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT mod_version, ksp_version_exact, ksp_version_min, ksp_version_max,
               release_date, download_size, install_size
        FROM mod_versions WHERE identifier = ?
        ORDER BY release_date DESC NULLS LAST
        """,
        (identifier,),
    ).fetchall()


def list_tags(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Return all distinct tags with their mod counts, sorted by count desc."""
    rows = conn.execute(
        "SELECT tags FROM mods WHERE tags IS NOT NULL"
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        for tag in row[0].split(","):
            tag = tag.strip()
            if tag:
                counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)
