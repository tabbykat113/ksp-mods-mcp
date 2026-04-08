"""
KSP part extraction from a mod's cached ZIP.

Parses .cfg files under GameData/{identifier}/Parts/ and resolves
#LOC_... strings from GameData/{identifier}/Localization/en-us.cfg.

Detail levels:
  summary  — category counts only
  basic    — name, title, category per part
  long     — basic + cost, mass, tech_required, modules, resources, bulkhead_profiles
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Literal

DetailLevel = Literal["summary", "basic", "long"]


# ---------------------------------------------------------------------------
# Localization parser
# ---------------------------------------------------------------------------

def _parse_localization(text: str) -> dict[str, str]:
    """Parse a KSP Localization en-us.cfg and return {key: value} mapping."""
    loc: dict[str, str] = {}
    # Keys look like:  #LOC_Foo_bar_title = Some text here
    # Values can span multiple lines if the line ends with a continuation,
    # but in practice nearly all KSP loc values are single-line.
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#LOC_"):
            continue
        eq = line.find(" = ")
        if eq == -1:
            eq = line.find("=")
            if eq == -1:
                continue
            key = line[:eq].strip()
            val = line[eq + 1:].strip()
        else:
            key = line[:eq].strip()
            val = line[eq + 3:].strip()
        loc[key] = val
    return loc


# ---------------------------------------------------------------------------
# CFG parser (minimal — only what we need)
# ---------------------------------------------------------------------------

_FLOAT_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _to_number(s: str) -> float | int | str:
    s = s.strip()
    if _FLOAT_RE.match(s):
        f = float(s)
        return int(f) if f == int(f) else f
    return s


def _parse_part_cfg(text: str) -> dict | None:
    """
    Parse a KSP part .cfg file and return a dict of extracted fields,
    or None if no PART block is found.
    """
    lines = text.splitlines()
    # Find the top-level PART { block
    in_part = False
    part_depth = 0
    current_block_name = ""
    block_stack: list[str] = []   # stack of block names
    modules: list[str] = []
    resources: list[str] = []
    fields: dict[str, str] = {}

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()

        if not in_part:
            if line == "PART":
                in_part = True
                part_depth = 0
            i += 1
            continue

        # Inside PART block
        if line == "{":
            part_depth += 1
            block_stack.append(current_block_name)
            current_block_name = ""
            i += 1
            continue

        if line == "}":
            part_depth -= 1
            if part_depth == 0:
                break  # end of PART block
            block_stack.pop()
            current_block_name = ""
            i += 1
            continue

        # Sub-block opener (name only on its own line before {)
        if line and not line.startswith("//") and "=" not in line and "{" not in line:
            current_block_name = line
            i += 1
            continue

        # key = value
        if "=" in line and not line.startswith("//"):
            eq = line.index("=")
            key = line[:eq].strip()
            val = line[eq + 1:].strip()
            # Remove inline comment
            ci = val.find("//")
            if ci != -1:
                val = val[:ci].strip()

            depth = len(block_stack)
            if depth == 1:
                # Top-level fields inside PART {}
                fields[key] = val
            elif depth == 2:
                parent = block_stack[-1]
                if parent == "MODULE" and key == "name":
                    modules.append(val)
                elif parent == "RESOURCE" and key == "name":
                    resources.append(val)

        i += 1

    if not in_part:
        return None

    return {
        "fields": fields,
        "modules": modules,
        "resources": resources,
    }


def _resolve(val: str, loc: dict[str, str]) -> str:
    """Resolve a #LOC_ key to its English string, or return val unchanged."""
    if val and val.startswith("#LOC_"):
        return loc.get(val, val)
    return val


def _strip_html(s: str) -> str:
    """Remove simple HTML tags KSP uses in loc strings (e.g. <b>...</b>)."""
    return re.sub(r"<[^>]+>", "", s).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _get_zip_path(identifier: str, download_url: str | None) -> Path | None:
    """Resolve the cached ZIP path for a mod using ckan_cache internals."""
    from .ckan_cache import _get_cache_hashes, _url_hash, _cache_dir
    if not download_url:
        return None
    h = _url_hash(download_url)
    if h not in _get_cache_hashes():
        return None
    if _cache_dir is None:
        return None
    for entry in _cache_dir.iterdir():
        if entry.name.upper().startswith(h) and entry.suffix == ".zip":
            return entry
    return None


def extract_parts(
    identifier: str,
    download_url: str | None,
    detail: DetailLevel = "basic",
) -> dict:
    """
    Open the cached ZIP for the given mod and extract part data.

    Returns a dict with:
      - detail "summary": {total_parts, categories: {name: count}}
      - detail "basic":   {total_parts, categories, parts: [{name, title, category}]}
      - detail "long":    {total_parts, categories, parts: [{name, title, category,
                            cost, mass, tech_required, modules, resources,
                            bulkhead_profiles}]}

    Returns {"error": "..."} on failure.
    """
    zip_path = _get_zip_path(identifier, download_url)
    if zip_path is None:
        return {"error": f"Mod '{identifier}' is not in the CKAN download cache."}

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        return {"error": f"ZIP is corrupt: {e}"}

    with zf:
        names = zf.namelist()
        parts_prefix = f"GameData/{identifier}/Parts/"
        loc_path = f"GameData/{identifier}/Localization/en-us.cfg"

        # Load localization
        loc: dict[str, str] = {}
        if loc_path in names:
            try:
                loc_text = zf.read(loc_path).decode("utf-8", errors="replace")
                loc = _parse_localization(loc_text)
            except Exception:
                pass  # proceed without loc

        # Find all part CFGs under GameData/{identifier}/Parts/
        cfg_paths = [
            n for n in names
            if n.startswith(parts_prefix) and n.endswith(".cfg")
        ]

        parts: list[dict] = []
        for cfg_path in sorted(cfg_paths):
            try:
                text = zf.read(cfg_path).decode("utf-8", errors="replace")
            except Exception:
                continue

            parsed = _parse_part_cfg(text)
            if parsed is None:
                continue

            f = parsed["fields"]
            part_name = f.get("name", "")
            if not part_name:
                continue

            raw_title    = f.get("title", "")
            raw_category = f.get("category", "")
            title    = _strip_html(_resolve(raw_title, loc)) if raw_title else ""
            category = _resolve(raw_category, loc) if raw_category else ""

            if detail == "summary":
                parts.append({"category": category})
                continue

            entry: dict = {
                "name":     part_name,
                "title":    title,
                "category": category,
            }

            if detail == "long":
                cost_raw = f.get("cost")
                mass_raw = f.get("mass")
                entry["cost"]         = _to_number(cost_raw) if cost_raw else None
                entry["mass"]         = _to_number(mass_raw) if mass_raw else None
                entry["tech_required"] = f.get("TechRequired") or None
                entry["modules"]      = parsed["modules"]
                entry["resources"]    = parsed["resources"]
                raw_bp = f.get("bulkheadProfiles", "")
                entry["bulkhead_profiles"] = (
                    [p.strip() for p in raw_bp.split(",") if p.strip()]
                    if raw_bp else []
                )

            parts.append(entry)

    # Build category counts
    category_counts: dict[str, int] = {}
    for p in parts:
        cat = p.get("category") or "Uncategorized"
        category_counts[cat] = category_counts.get(cat, 0) + 1

    if detail == "summary":
        return {
            "total_parts": len(parts),
            "categories": category_counts,
        }

    return {
        "total_parts": len(parts),
        "categories": category_counts,
        "parts": parts,
    }
