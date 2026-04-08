"""
CKAN download cache detection.

CKAN stores downloaded mod ZIPs as:
    {8-hex-SHA1-of-URL}-{filename}.zip

We scan the cache directory once per server session, build a set of known
8-char URL hashes, then answer is_cached(url) in O(1).

Cache directory resolution order:
  1. CKAN_DOWNLOAD_CACHE env var
  2. OS default: %LOCALAPPDATA%/CKAN/downloads  (Windows)
                 $XDG_DATA_HOME/CKAN/downloads  (Linux/macOS)
"""

import hashlib
import os
import sys
from pathlib import Path


def _default_cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "CKAN" / "downloads"


def _url_hash(url: str) -> str:
    """Return the 8-char hex prefix CKAN uses for a given download URL."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return digest[:8].upper()


def _scan_cache(cache_dir: Path) -> set[str]:
    """Return set of 8-char uppercase hashes found in the cache directory."""
    hashes: set[str] = set()
    if not cache_dir.is_dir():
        return hashes
    for entry in cache_dir.iterdir():
        name = entry.name
        # CKAN filenames: 8 hex chars + dash + rest, no sha1/sha256 sidecar files
        if (
            len(name) >= 9
            and name[8] == "-"
            and not name.endswith(".sha1")
            and not name.endswith(".sha256")
        ):
            prefix = name[:8].upper()
            if all(c in "0123456789ABCDEF" for c in prefix):
                hashes.add(prefix)
    return hashes


# ---------------------------------------------------------------------------
# Module-level singleton: resolved once, reused across all tool calls
# ---------------------------------------------------------------------------

_cache_hashes: set[str] | None = None
_cache_dir: Path | None = None


def _get_cache_hashes() -> set[str]:
    global _cache_hashes, _cache_dir
    if _cache_hashes is None:
        env = os.environ.get("CKAN_DOWNLOAD_CACHE")
        _cache_dir = Path(env) if env else _default_cache_dir()
        _cache_hashes = _scan_cache(_cache_dir)
    return _cache_hashes


def cache_dir_exists() -> bool:
    """Return True if the CKAN download cache directory is present."""
    env = os.environ.get("CKAN_DOWNLOAD_CACHE")
    d = Path(env) if env else _default_cache_dir()
    return d.is_dir()


def is_cached(download_url: str | None) -> bool:
    """Return True if the given download URL's file is present in the CKAN cache."""
    if not download_url:
        return False
    return _url_hash(download_url) in _get_cache_hashes()


def cached_identifiers(download_urls: dict[str, str | None]) -> set[str]:
    """
    Given a mapping of identifier → download_url, return the set of identifiers
    whose download URL is present in the CKAN cache.
    """
    hashes = _get_cache_hashes()
    return {
        ident
        for ident, url in download_urls.items()
        if url and _url_hash(url) in hashes
    }
