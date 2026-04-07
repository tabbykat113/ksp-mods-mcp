"""
Pass 1 harvester: download the CKAN-meta archive and populate the local SQLite DB.

Usage:
    uv run harvest
"""

import io
import json
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)

from .db import (
    _parse_ver,
    apply_download_counts,
    apply_mod_data,
    apply_max_ksp_versions,
    get_etag,
    get_mod_count,
    open_db,
    set_etag,
    upsert_mod_version,
)

ARCHIVE_URL = "https://github.com/KSP-CKAN/CKAN-meta/archive/refs/heads/master.tar.gz"
CHUNK_SIZE  = 65_536  # 64 KiB


# ---------------------------------------------------------------------------
# Streaming bridge: httpx byte iterator → tarfile-compatible file-like object
# ---------------------------------------------------------------------------

class StreamingBuffer(io.RawIOBase):
    """
    Wraps an httpx streaming response iterator into a RawIOBase so that
    tarfile.open(mode='r|gz') can consume it without loading into memory.
    """

    def __init__(self, iterator, on_bytes=None):
        self._iter     = iterator
        self._buf      = b""
        self._eof      = False
        self._on_bytes = on_bytes  # optional callback(n_bytes)

    def readable(self):
        return True

    def readinto(self, b):
        # Fill b from internal buffer, pulling from iterator as needed.
        target = len(b)
        while len(self._buf) < target and not self._eof:
            try:
                chunk = next(self._iter)
                self._buf += chunk
            except StopIteration:
                self._eof = True

        n = min(target, len(self._buf))
        if n == 0:
            return 0  # EOF

        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]

        if self._on_bytes:
            self._on_bytes(n)

        return n


# ---------------------------------------------------------------------------
# ETag helpers
# ---------------------------------------------------------------------------

def check_etag(client: httpx.Client, stored_etag: str | None) -> tuple[bool, str | None]:
    """
    HEAD the archive URL.
    Returns (skip, server_etag): skip=True means content is unchanged.
    """
    headers = {}
    if stored_etag:
        headers["If-None-Match"] = stored_etag

    try:
        r = client.head(ARCHIVE_URL, headers=headers)
    except httpx.RequestError:
        return False, None  # network error — proceed with full download

    if r.status_code == 304:
        return True, stored_etag

    server_etag = r.headers.get("etag")
    return False, server_etag


# ---------------------------------------------------------------------------
# Core streaming + parsing
# ---------------------------------------------------------------------------

def stream_and_parse(client: httpx.Client, stored_etag: str | None, *, console: Console) -> dict:
    headers = {}
    if stored_etag:
        headers["If-None-Match"] = stored_etag

    archive_timeout = httpx.Timeout(connect=10, read=300, write=None, pool=10)

    with client.stream("GET", ARCHIVE_URL, headers=headers, timeout=archive_timeout) as resp:
        if resp.status_code == 304:
            console.print("[green]Archive unchanged (ETag match). Nothing to do.[/green]")
            return {"status": "skipped", "reason": "etag_match"}

        resp.raise_for_status()
        server_etag = resp.headers.get("etag")
        content_length = resp.headers.get("content-length")
        total_bytes = int(content_length) if content_length else None

        # Build progress bar — shows download speed and size
        progress_cols = [
            SpinnerColumn(),
            TextColumn("[bold cyan]Downloading CKAN-meta archive[/bold cyan]"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
        ]

        conn = open_db()

        with Progress(*progress_cols, console=console) as progress:
            task = progress.add_task("download", total=total_bytes)

            def on_bytes(n):
                progress.advance(task, n)

            raw_stream   = StreamingBuffer(resp.iter_bytes(CHUNK_SIZE), on_bytes=on_bytes)
            buffered     = io.BufferedReader(raw_stream, buffer_size=256 * 1024)

            counts: dict[str, int] = {}
            max_ksp: dict[str, str] = {}   # identifier → highest KSP version seen
            mod_data: dict[str, dict] = {} # identifier → latest-version metadata
            mods_with_download_size: set[str] = set()
            mods_with_install_size:  set[str] = set()
            mod_count     = 0
            version_count = 0
            skip_count    = 0

            with tarfile.open(fileobj=buffered, mode="r|gz") as tar:
                conn.execute("BEGIN")
                for member in tar:
                    if not member.isfile():
                        continue

                    path = member.name  # e.g. CKAN-meta-master/MechJeb2/MechJeb2-2.9.2.0.ckan
                    f    = tar.extractfile(member)
                    if f is None:
                        continue

                    raw = f.read()

                    if path.endswith("download_counts.json"):
                        try:
                            counts = json.loads(raw.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError) as e:
                            console.print(f"[yellow]Warning: could not parse download_counts.json: {e}[/yellow]")
                        continue

                    if not path.endswith(".ckan"):
                        continue

                    try:
                        text = raw.decode("utf-8")
                        data = json.loads(text)
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        console.print(f"[yellow]Warning: skipping {path}: {e}[/yellow]")
                        skip_count += 1
                        continue

                    identifier = data.get("identifier")
                    if not identifier:
                        console.print(f"[yellow]Warning: no identifier in {path}, skipping[/yellow]")
                        skip_count += 1
                        continue

                    raw_tags = data.get("tags")
                    if isinstance(raw_tags, str):
                        raw_tags = [raw_tags]

                    raw_authors = data.get("author")
                    if isinstance(raw_authors, str):
                        raw_authors = [raw_authors]

                    mod_version = data.get("version")
                    release_date = data.get("release_date")

                    # Track latest-version entry per identifier.
                    # Priority: release_date beats no-date; when both have dates, newer wins;
                    # when neither has a date, fall back to lexicographic mod_version comparison.
                    current_md = mod_data.get(identifier)
                    mv_str = str(mod_version) if mod_version else ""
                    rd = release_date or ""
                    if current_md is None:
                        take = True
                    elif rd and not current_md["release_date"]:
                        take = True   # first real date always wins over no-date
                    elif current_md["release_date"] and not rd:
                        take = False  # existing has date, new doesn't — keep existing
                    elif rd and current_md["release_date"]:
                        take = rd > current_md["release_date"]
                    else:
                        take = mv_str > current_md["latest_version_raw"]
                    if take:
                        mod_data[identifier] = {
                            "ckan_json":    text,
                            "name":         data.get("name"),
                            "abstract":     data.get("abstract"),
                            "tags":         raw_tags,
                            "authors":      raw_authors,
                            "download_size": data.get("download_size"),
                            "install_size": data.get("install_size"),
                            "latest_version": str(mod_version) if mod_version else None,
                            "latest_version_raw": mv_str,
                            "last_updated_at": release_date,  # None if unset
                            "release_date": rd,
                            "pass1_at":     datetime.now(timezone.utc).isoformat(),
                        }

                    if mod_version:
                        kv_exact = data.get("ksp_version")
                        kv_max   = data.get("ksp_version_max")
                        kv_min   = data.get("ksp_version_min")
                        ceiling  = kv_exact or kv_max
                        if not ceiling:
                            # min-only or no constraints at all → no upper bound → treat as latest
                            ceiling = "1.12"
                        if ceiling:
                            t = _parse_ver(ceiling)
                            # Cap at 1.12 — KSP1 never exceeded it; sentinels like 1.99.99 mean "latest"
                            major, minor = t[0], min(t[1], 12) if len(t) > 1 else 0
                            normalized = f"{major}.{minor}"
                            current = max_ksp.get(identifier)
                            if current is None or _parse_ver(normalized) > _parse_ver(current):
                                max_ksp[identifier] = normalized

                        upsert_mod_version(
                            conn,
                            identifier=identifier,
                            mod_version=str(mod_version),
                            ksp_version_exact=kv_exact,
                            ksp_version_min=kv_min,
                            ksp_version_max=kv_max,
                            release_date=release_date,
                            download_size=data.get("download_size"),
                            install_size=data.get("install_size"),
                        )

                    if data.get("download_size"):
                        mods_with_download_size.add(identifier)
                    if data.get("install_size"):
                        mods_with_install_size.add(identifier)

                    mod_count += 1
                    if mod_version:
                        version_count += 1

        # Apply mod rows, download counts, and max KSP versions, then commit atomically
        if mod_data:
            apply_mod_data(conn, mod_data)
        if counts:
            apply_download_counts(conn, counts)
        if max_ksp:
            apply_max_ksp_versions(conn, max_ksp)

        if server_etag:
            set_etag(conn, server_etag)

        conn.execute("COMMIT")
        conn.close()

        total = get_mod_count(open_db())
        console.print(
            f"[green]Done.[/green] Indexed [bold]{total}[/bold] mods, "
            f"[bold]{version_count}[/bold] version entries "
            f"([bold]{skip_count}[/bold] skipped). "
            f"Download counts applied for [bold]{len(counts)}[/bold] mods."
        )
        console.print(
            f"Size metadata: [bold]{len(mods_with_download_size)}[/bold] mods with download_size, "
            f"[bold]{len(mods_with_install_size)}[/bold] mods with install_size."
        )

        return {
            "status": "updated",
            "total_mods": total,
            "version_entries": version_count,
            "skipped_files": skip_count,
            "download_counts_applied": len(counts),
            "mods_with_download_size": len(mods_with_download_size),
            "mods_with_install_size": len(mods_with_install_size),
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

HELP = """\
Usage: harvest [--force]

Download the CKAN-meta archive and index all KSP mod metadata into a local
SQLite database. Safe to re-run - skips the download if nothing has changed
upstream (ETag check).

Options:
  --force   Bypass the ETag check and re-download/re-index unconditionally.
  --help    Show this message and exit.

Environment:
  CKAN_DB   Path to the SQLite database file.
            Default: platform data dir (e.g. ~/.local/share/ksp-mods-mcp/ckan.db).
"""


def run_harvest(*, force: bool = False, console: Console | None = None) -> dict:
    """Run the CKAN-meta harvest. Returns a result dict with status and stats.

    Args:
        force: Bypass ETag check and re-download unconditionally.
        console: Rich Console for output. Defaults to a new Console() for CLI use.
                 Pass Console(quiet=True) to suppress all output.
    """
    if console is None:
        console = Console()

    conn = open_db()
    stored_etag = get_etag(conn) if not force else None
    conn.close()

    with httpx.Client(follow_redirects=True) as client:
        if not force:
            skip, _ = check_etag(client, stored_etag)
            if skip:
                console.print("[green]Archive unchanged (ETag match). Nothing to do.[/green]")
                return {"status": "skipped", "reason": "etag_match"}

        return stream_and_parse(client, stored_etag, console=console)


def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(HELP, end="")
        return

    force = "--force" in sys.argv
    run_harvest(force=force)


if __name__ == "__main__":
    main()
