"""
KSP part extraction from a mod's cached ZIP.

Parses .cfg files under GameData/{identifier}/Parts/ and resolves
#LOC_... strings from GameData/{identifier}/Localization/en-us.cfg.

Pipeline:
  parse_cfg(text)                     — raw CfgNode tree (cfg_parser.py)
  _extract_part(node, loc) -> dict    — full part data, always
  extract_parts(identifier, url)      — all parts at full detail
  get_part(identifier, url, name)     — single part by name
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .cfg_parser import CfgNode, parse_cfg


# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------

def _parse_localization(text: str) -> dict[str, str]:
    """Parse a KSP en-us.cfg and return {#LOC_key: value}."""
    nodes = parse_cfg(text)
    loc: dict[str, str] = {}
    for node in nodes:
        loc_node = node if node.name == "Localization" else node.child_named("Localization")
        if loc_node is None:
            continue
        en = loc_node.child_named("en-us")
        if en is None:
            continue
        for key, val in en.fields:
            if key.startswith("#LOC_"):
                loc[key] = val
    # Fallback: some mods put loc keys directly at top level
    if not loc:
        for node in nodes:
            for key, val in node.fields:
                if key.startswith("#LOC_"):
                    loc[key] = val
    return loc


def _resolve(val: str, loc: dict[str, str]) -> str:
    if val and val.startswith("#LOC_"):
        return loc.get(val, val)
    return val


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


# ---------------------------------------------------------------------------
# Module formatting
# ---------------------------------------------------------------------------

def _to_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _to_int(s: str | None) -> int | None:
    f = _to_float(s)
    return int(f) if f is not None else None


def _parse_curve_keys(node: CfgNode) -> list[list[float]]:
    result = []
    for k, v in node.fields:
        if k == "key":
            parts = v.split()
            try:
                result.append([float(p) for p in parts[:2]])
            except ValueError:
                pass
    return result


def _format_engine(block: CfgNode) -> dict:
    propellants = []
    for p in block.children_named("PROPELLANT"):
        name = p.get("name")
        ratio = _to_float(p.get("ratio"))
        if name:
            propellants.append({"name": name, "ratio": ratio})

    isp_vac = isp_sl = None
    isp_curve = block.child_named("atmosphereCurve")
    if isp_curve:
        for x, y in _parse_curve_keys(isp_curve):
            if x == 0:
                isp_vac = y
            elif x == 1:
                isp_sl = y

    return {
        "type": block.get("name"),
        "engine_id": block.get("engineID"),
        "engine_type": block.get("EngineType"),
        "thrust_min": _to_float(block.get("minThrust")),
        "thrust_max": _to_float(block.get("maxThrust")),
        "heat_production": _to_float(block.get("heatProduction")),
        "isp_vac": isp_vac,
        "isp_sl": isp_sl,
        "propellants": propellants,
    }


def _format_srb(block: CfgNode) -> dict:
    d = _format_engine(block)
    d["thrust_curve"] = block.get("thrustCurve")
    return d


def _format_rcs(block: CfgNode) -> dict:
    propellants = []
    for p in block.children_named("PROPELLANT"):
        name = p.get("name")
        ratio = _to_float(p.get("ratio"))
        if name:
            propellants.append({"name": name, "ratio": ratio})

    isp_vac = isp_sl = None
    isp_curve = block.child_named("atmosphereCurve")
    if isp_curve:
        for x, y in _parse_curve_keys(isp_curve):
            if x == 0:
                isp_vac = y
            elif x == 1:
                isp_sl = y

    return {
        "type": block.get("name"),
        "thrust_power": _to_float(block.get("thrusterPower")),
        "isp_vac": isp_vac,
        "isp_sl": isp_sl,
        "propellants": propellants,
    }


def _format_reaction_wheel(block: CfgNode) -> dict:
    return {
        "type": block.get("name"),
        "torque_x": _to_float(block.get("PitchTorque")),
        "torque_y": _to_float(block.get("YawTorque")),
        "torque_z": _to_float(block.get("RollTorque")),
        "ec_per_torque": _to_float(block.get("ElectricChargeUpkeep")),
    }


def _format_solar_panel(block: CfgNode) -> dict:
    return {
        "type": block.get("name"),
        "charge_rate": _to_float(block.get("chargeRate")),
        "resource": block.get("resourceName"),
    }


def _format_command(block: CfgNode) -> dict:
    return {
        "type": block.get("name"),
        "minimum_crew": _to_int(block.get("minimumCrew")),
    }


def _format_parachute(block: CfgNode) -> dict:
    return {
        "type": block.get("name"),
        "deploy_altitude": _to_float(block.get("deployAltitude")),
        "semi_deploy_drag": _to_float(block.get("semiDeployedDrag")),
        "fully_deploy_drag": _to_float(block.get("fullyDeployedDrag")),
    }


def _format_decoupler(block: CfgNode) -> dict:
    return {
        "type": block.get("name"),
        "ejection_force": _to_float(block.get("ejectionForce")),
        "explosive_node_id": block.get("explosiveNodeID"),
    }


def _format_docking_node(block: CfgNode) -> dict:
    return {
        "type": block.get("name"),
        "node_type": block.get("nodeType"),
    }


def _format_generator(block: CfgNode) -> dict:
    outputs = []
    for r in block.children_named("OUTPUT_RESOURCE"):
        name = r.get("name")
        rate = _to_float(r.get("rate"))
        if name:
            outputs.append({"name": name, "rate": rate})
    return {
        "type": block.get("name"),
        "outputs": outputs,
    }


def _format_resource_converter(block: CfgNode) -> dict:
    inputs = []
    for r in block.children_named("INPUT_RESOURCE"):
        name = r.get("name")
        rate = _to_float(r.get("Ratio"))
        if name:
            inputs.append({"name": name, "rate": rate})
    outputs = []
    for r in block.children_named("OUTPUT_RESOURCE"):
        name = r.get("name")
        rate = _to_float(r.get("Ratio"))
        if name:
            outputs.append({"name": name, "rate": rate})
    return {
        "type": block.get("name"),
        "converter_name": block.get("ConverterName"),
        "inputs": inputs,
        "outputs": outputs,
    }


def _format_resource_harvester(block: CfgNode) -> dict:
    return {
        "type": block.get("name"),
        "harvester_type": _to_int(block.get("HarvesterType")),
        "resource": block.get("ResourceName"),
        "efficiency": _to_float(block.get("Efficiency")),
    }


_MODULE_FORMATTERS: dict[str, object] = {
    "ModuleEngines":              _format_engine,
    "ModuleEnginesFX":            _format_engine,
    "ModuleSRB":                  _format_srb,
    "ModuleRCS":                  _format_rcs,
    "ModuleRCSFX":                _format_rcs,
    "ModuleReactionWheel":        _format_reaction_wheel,
    "ModuleDeployableSolarPanel": _format_solar_panel,
    "ModuleCommand":              _format_command,
    "ModuleParachute":            _format_parachute,
    "ModuleDecoupler":            _format_decoupler,
    "ModuleAnchoredDecoupler":    _format_decoupler,
    "ModuleDockingNode":          _format_docking_node,
    "ModuleGenerator":            _format_generator,
    "ModuleResourceConverter":    _format_resource_converter,
    "ModuleResourceHarvester":    _format_resource_harvester,
}


def _format_resource_block(block: CfgNode) -> dict:
    return {
        "name": block.get("name"),
        "amount": _to_float(block.get("amount")),
        "max_amount": _to_float(block.get("maxAmount")),
    }


# ---------------------------------------------------------------------------
# Part extraction — always full detail
# ---------------------------------------------------------------------------

def _extract_part(node: CfgNode, loc: dict[str, str]) -> dict | None:
    part_name = node.get("name")
    if not part_name:
        return None

    raw_title    = node.get("title") or ""
    raw_category = node.get("category") or ""
    title    = _strip_html(_resolve(raw_title, loc)) if raw_title else ""
    category = _resolve(raw_category, loc) if raw_category else ""

    supported_modules: list[dict] = []
    unsupported_modules: list[str] = []
    for child in node.children_named("MODULE"):
        mod_name = child.get("name") or ""
        formatter = _MODULE_FORMATTERS.get(mod_name)
        if formatter:
            supported_modules.append(formatter(child))  # type: ignore[operator]
        elif mod_name:
            unsupported_modules.append(mod_name)

    return {
        "name":                part_name,
        "title":               title,
        "category":            category,
        "cost":                _to_float(node.get("cost")),
        "mass":                _to_float(node.get("mass")),
        "tech_required":       node.get("TechRequired") or None,
        "bulkhead_profiles":   [
            p.strip()
            for p in (node.get("bulkheadProfiles") or "").split(",")
            if p.strip()
        ],
        "modules":             supported_modules,
        "unsupported_modules": unsupported_modules,
        "resources":           [
            _format_resource_block(r) for r in node.children_named("RESOURCE")
        ],
    }


# ---------------------------------------------------------------------------
# ZIP access
# ---------------------------------------------------------------------------

def _get_zip_path(identifier: str, download_url: str | None) -> Path | None:
    import harvester.ckan_cache as _cc
    if not download_url:
        return None
    hashes = _cc._get_cache_hashes()  # ensures _cache_dir is populated
    for url in download_url.splitlines():
        if not url:
            continue
        h = _cc._url_hash(url)
        if h not in hashes:
            continue
        if _cc._cache_dir is None:
            return None
        for entry in _cc._cache_dir.iterdir():
            if entry.name.upper().startswith(h) and entry.suffix == ".zip":
                return entry
    return None


def _resolve_gamedata_folder(identifier: str, install_stanzas: list[dict], zip_names: list[str]) -> str:
    """
    Resolve the GameData subfolder name for this mod's parts using CKAN's install stanza logic.

    CKAN install stanzas use either:
      - "find": folder name to locate anywhere in the ZIP tree (most common)
      - "file": exact path prefix

    We replicate CKAN's default fallback: find=identifier if no stanzas present.
    Returns the folder name (e.g. "Benjee10_MMSEV"), not a full path.
    """
    import re as _re

    candidates: list[str] = []
    stanzas = install_stanzas or [{"find": identifier}]

    for stanza in stanzas:
        install_to = stanza.get("install_to", "GameData")
        if install_to != "GameData":
            continue  # we only care about GameData installs

        find = stanza.get("find")
        file_ = stanza.get("file")

        if find:
            # Match folder name anywhere in the ZIP tree — same as CKAN's (?:^|/) regex
            pat = _re.compile(r"(?:^|/)(" + _re.escape(find) + r")/")
            for name in zip_names:
                m = pat.search(name.replace("\\", "/"))
                if m:
                    candidates.append(find)
                    break
        elif file_:
            # Exact prefix — extract the last path component that lands in GameData
            # e.g. "Wrapper/GameData/MyMod" → "MyMod"
            normalized = file_.replace("\\", "/").rstrip("/")
            # Find "GameData/<folder>" in the path
            gd_match = _re.search(r"GameData/([^/]+)", normalized)
            if gd_match:
                candidates.append(gd_match.group(1))
            else:
                # file points directly at the folder to install; use last component
                candidates.append(normalized.split("/")[-1])

    # Return first resolved candidate; fall back to identifier
    return candidates[0] if candidates else identifier


def _open_zip_parts(
    identifier: str,
    download_url: str | None,
    install_stanzas: list[dict] | None = None,
) -> tuple[list[dict], str | None]:
    """
    Open the cached ZIP and return (parts, error).
    parts is a list of fully-extracted part dicts; error is a string on failure.

    install_stanzas should be the parsed 'install' field from the mod's ckan_json,
    used to locate the correct GameData subfolder inside the ZIP.
    """
    zip_path = _get_zip_path(identifier, download_url)
    if zip_path is None:
        return [], f"Mod '{identifier}' is not in the CKAN download cache."

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        return [], f"ZIP is corrupt: {e}"

    with zf:
        names = zf.namelist()
        normalized = [n.replace("\\", "/") for n in names]

        folder = _resolve_gamedata_folder(identifier, install_stanzas or [], normalized)

        # Find parts and localization using a suffix match so any ZIP wrapper is ignored
        parts_suffix = f"GameData/{folder}/Parts/"
        loc_suffix   = f"GameData/{folder}/Localization/en-us.cfg"

        loc: dict[str, str] = {}
        loc_matches = [n for n in normalized if n.endswith(loc_suffix)]
        if loc_matches:
            try:
                loc_text = zf.read(names[normalized.index(loc_matches[0])]).decode("utf-8", errors="replace")
                loc = _parse_localization(loc_text)
            except Exception:
                pass

        parts: list[dict] = []
        cfg_entries = sorted(
            (orig, norm) for orig, norm in zip(names, normalized)
            if parts_suffix in norm and norm.endswith(".cfg")
        )
        for orig_path, _ in cfg_entries:
            try:
                text = zf.read(orig_path).decode("utf-8", errors="replace")
            except Exception:
                continue
            for node in parse_cfg(text):
                if node.name != "PART":
                    continue
                entry = _extract_part(node, loc)
                if entry is not None:
                    parts.append(entry)

    return parts, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_parts(
    identifier: str,
    download_url: str | None,
    install_stanzas: list[dict] | None = None,
) -> dict:
    """
    Open the cached ZIP for the given mod and extract all parts at full detail.

    Returns:
      {total_parts, categories: {name: count}, parts: [full part dicts]}

    Returns {"error": "..."} on failure.
    """
    parts, error = _open_zip_parts(identifier, download_url, install_stanzas)
    if error:
        return {"error": error}

    category_counts: dict[str, int] = {}
    for p in parts:
        cat = p.get("category") or "Uncategorized"
        category_counts[cat] = category_counts.get(cat, 0) + 1

    return {"total_parts": len(parts), "categories": category_counts, "parts": parts}


def get_part(
    identifier: str,
    download_url: str | None,
    part_name: str,
    install_stanzas: list[dict] | None = None,
) -> dict:
    """
    Extract full detail for a single named part from the mod's cached ZIP.

    Returns the part dict or {"error": "..."} on failure.
    """
    parts, error = _open_zip_parts(identifier, download_url, install_stanzas)
    if error:
        return {"error": error}
    for part in parts:
        if part["name"] == part_name:
            return part
    return {"error": f"Part '{part_name}' not found in mod '{identifier}'. Use list_parts_tool to see available parts."}
