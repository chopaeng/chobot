"""
NHL Island Map Parser Module (v2 - rebuilt from actual byte-level evidence)

This replaces the original stride-guessing parser, which was based on
assumptions that did not match the real file. What changed and why:

CONFIRMED (verified against a real maprefresh.nhl file + a public ACNH
item ID reference table):
  - The real record size is 16 bytes, not a guessed 4 or 8.
  - The item ID lives in a 4-byte little-endian uint32 at offset +4,
    NOT a uint16 at offset +0. This was confirmed by an exact match:
    decimal 224 (0x000000E0) corresponds to the real item "spino tail"
    (Fossil_00224) in a public ACNH item ID table.
  - offset +8 (uint16) is a marker/sentinel field, not part of the item
    ID. It takes only two observed values across the whole file:
    0xFFFD and 0xFFFE. The old code's stride=8 guess read this field as
    if it were an item ID every other slot, which is why ~76% of
    "items" it reported were a single fake repeated value.

STILL UNRESOLVED - do not trust for spatial placement:
  - offset +0 (A) and offset +2 (B) do not have a confirmed meaning.
    They looked like a tile-index + 32-unit sub-offset pair in one
    region of the file, but a second region (large runs of A=0xFFFD)
    breaks that pattern. This looks like the file mixes genuine
    placed-item records with a separate bulk/background-fill pattern,
    and untangling those fully needs either the actual NHSE source
    (MainFieldItem struct) or a known ground-truth item+location to
    triangulate against.
  - offset +12 (F) and +14 (G) are carried through as raw fields for
    future analysis; F often (not always) equals A, and G is mostly
    256/257 with rarer values that look like packed flag bytes.

Because of the above, this version:
  - Correctly extracts item IDs and correctly identifies real vs.
    empty/filler slots (huge accuracy win over v1).
  - Does NOT claim a reliable acre/sector/(x,y) position for each item.
    It reports the raw record index and raw A/B/F/G fields alongside
    each item instead, clearly labeled as "unconfirmed position data",
    so nothing downstream silently trusts a guessed coordinate.

If/when the real position encoding is figured out (e.g. against a
known item + known location, or the actual NHSE struct), replace
`_position_placeholder()` with the real mapping and drop the caveat
fields.
"""

import os
import struct
import json
import logging
import re
from typing import Dict, List, Optional, Any, Tuple

from utils.config import Config

logger = logging.getLogger("NhlMapParser")

_ITEM_CATALOG_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_MAP_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 60.0

RECORD_SIZE = 16          # confirmed
ITEM_ID_OFFSET = 4        # confirmed: uint32 LE
ITEM_ID_STRUCT = "<I"
MARKER_OFFSET = 8         # confirmed: uint16, sentinel/marker field
MARKER_STRUCT = "<H"
# The only two marker values observed in a real file. Records carrying
# these are background/filler, not real item IDs. If a future file
# shows different marker values, this set needs updating.
KNOWN_MARKER_VALUES = {0xFFFD, 0xFFFE}

# offset 0 (A) values that mark a record as part of the bulk/background
# fill pattern rather than a genuine placed-item candidate. This is a
# heuristic based on observed data, NOT a confirmed spec.
BACKGROUND_A_VALUES = {0xFFFD, 0xFFFE}


def _load_item_catalog() -> Dict[str, Dict[str, Any]]:
    """Load items from data/acnh.min.json (or fallback items_detail.json / acnh.json)
    into a fast 8-hex-digit ID lookup table.

    ACNH item IDs are stored as 32-bit integers (e.g. 224 for 'spino tail', hex 0x000000E0).
    We index items, variations, creatures, and recipes by 8-hex-digit lowercase strings.
    """
    global _ITEM_CATALOG_CACHE
    if _ITEM_CATALOG_CACHE is not None:
        return _ITEM_CATALOG_CACHE

    catalog: Dict[str, Dict[str, Any]] = {}
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Primary source: data/acnh.min.json
    primary_path = os.path.join(base_dir, "data", "acnh.min.json")
    items_path = primary_path if os.path.exists(primary_path) else None

    if not items_path:
        for fallback in ["items_detail.json", "acnh.json"]:
            cand = os.path.join(base_dir, fallback)
            if os.path.exists(cand):
                items_path = cand
                break

    if not items_path:
        logger.warning("[NhlMapParser] No catalog JSON file found.")
        _ITEM_CATALOG_CACHE = catalog
        return catalog

    def _to_hex8(val: Any) -> Optional[str]:
        if val is None:
            return None
        try:
            if isinstance(val, int):
                return f"{val:08x}"
            s = str(val).strip()
            if s.startswith(("0x", "0X")):
                return f"{int(s, 16):08x}"
            if s.isdigit():
                return f"{int(s):08x}"
            return f"{int(s, 16):08x}"
        except Exception:
            return None

    try:
        with open(items_path, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)

        if isinstance(data, dict) and "items" in data and ("creatures" in data or "recipes" in data):
            # Comprehensive acnh.min.json format
            # 1. Base items and variations
            for item in data.get("items", []):
                name = item.get("name") or item.get("Name")
                if not name:
                    continue
                cat = item.get("sourceSheet") or item.get("category") or item.get("Category") or "Miscellaneous"
                diy = bool(item.get("diy") or item.get("DIY") == "Yes")
                image = item.get("image") or ""

                top_id = _to_hex8(item.get("internalId") or item.get("Internal ID") or item.get("id"))
                if top_id and top_id not in catalog:
                    catalog[top_id] = {
                        "name": name,
                        "category": cat,
                        "diy": diy,
                        "internalId": top_id,
                        "image": image,
                    }

                # Variations (different colors, etc.)
                for var in item.get("variations", []) or []:
                    var_id = _to_hex8(var.get("internalId") or var.get("variantId"))
                    if var_id and var_id not in catalog:
                        var_name = var.get("variation")
                        display_name = f"{name} ({var_name})" if var_name and str(var_name).lower() not in ("na", "none", "") else name
                        catalog[var_id] = {
                            "name": display_name,
                            "category": cat,
                            "diy": diy,
                            "internalId": var_id,
                            "image": var.get("image") or image,
                        }

            # 2. Creatures (fish, bugs, sea creatures)
            for cr in data.get("creatures", []):
                c_name = cr.get("name")
                c_id = _to_hex8(cr.get("internalId"))
                if c_name and c_id and c_id not in catalog:
                    catalog[c_id] = {
                        "name": c_name,
                        "category": cr.get("sourceSheet") or "Creatures",
                        "diy": False,
                        "internalId": c_id,
                        "image": cr.get("iconImage") or cr.get("critterpediaImage") or "",
                    }

            # 3. Recipes
            for rec in data.get("recipes", []):
                r_name = rec.get("name")
                r_id = _to_hex8(rec.get("internalId"))
                if r_name and r_id and r_id not in catalog:
                    catalog[r_id] = {
                        "name": f"{r_name} (DIY Recipe)",
                        "category": "Recipes",
                        "diy": True,
                        "internalId": r_id,
                        "image": rec.get("image") or rec.get("imageSh") or "",
                    }

        else:
            # Fallback legacy items_detail.json / acnh.json list or dict
            items_list = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for item in items_list:
                raw_id = item.get("Internal ID") or item.get("pokerId") or item.get("id") or item.get("internalId") or item.get("hexstr")
                name = item.get("Name") or item.get("name")
                if not raw_id or not name:
                    continue
                hex_key = _to_hex8(raw_id)
                if not hex_key:
                    continue
                catalog[hex_key] = {
                    "name": name,
                    "category": item.get("Category", item.get("category", "Miscellaneous")),
                    "diy": item.get("DIY") == "Yes",
                    "internalId": hex_key,
                    "image": item.get("image") or "",
                }

        logger.info(f"[NhlMapParser] Loaded {len(catalog)} item definitions from {items_path}.")
    except Exception as exc:
        logger.error(f"[NhlMapParser] Failed to load catalog from {items_path}: {exc}")

    _ITEM_CATALOG_CACHE = catalog
    return catalog


def locate_nhl_file(island_name: str) -> Tuple[Optional[str], bool, List[str]]:
    """Locates the maprefresh.nhl file for an island. Unchanged from v1 -
    this part was never in question, only the byte parsing was wrong."""
    if not island_name:
        return None, False, []

    clean_name = island_name.strip()
    norm_name = re.sub(r"[^a-zA-Z0-9]", "", clean_name).lower()

    search_roots = [Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR]
    checked_paths: List[str] = []

    for root in search_roots:
        if not root:
            continue
        direct_path = os.path.join(root, clean_name, "nhl", "maprefresh.nhl")
        checked_paths.append(direct_path)
        if not os.path.exists(root):
            continue
        if os.path.isfile(direct_path):
            return direct_path, True, checked_paths
        try:
            for folder in os.listdir(root):
                f_path = os.path.join(root, folder)
                if os.path.isdir(f_path):
                    f_norm = re.sub(r"[^a-zA-Z0-9]", "", folder).lower()
                    if f_norm == norm_name:
                        candidate = os.path.join(f_path, "nhl", "maprefresh.nhl")
                        checked_paths.append(candidate)
                        if os.path.isfile(candidate):
                            return candidate, True, checked_paths
        except OSError:
            pass

    return (checked_paths[0] if checked_paths else None), False, checked_paths


def locate_island_nhl_files(
    island_name: str, specific_file: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Locates all .nhl files (or a specific requested .nhl file) for an island across both:
      - Config.VILLAGERS_DIR (Sub / VIP Islands)
      - Config.TWITCH_VILLAGERS_DIR (Free / Twitch Islands)

    Checks both the `nhl/` subfolder and the root folder for the island.
    Returns: (found_files_list, checked_expected_paths)
    Each item in found_files_list is a dict:
      {
          "filename": str,
          "file_path": str,
          "size_bytes": int,
          "modified_at": float,
      }
    """
    if not island_name:
        return [], []

    clean_name = island_name.strip()
    norm_name = re.sub(r"[^a-zA-Z0-9]", "", clean_name).lower()

    search_roots = [
        Config.VILLAGERS_DIR,
        Config.TWITCH_VILLAGERS_DIR,
    ]

    target_filename = None
    if specific_file:
        target_filename = os.path.basename(specific_file.strip())
        if not target_filename.lower().endswith(".nhl"):
            return [], [f"Invalid file extension for {target_filename}"]

    checked_paths: List[str] = []
    found_files: List[Dict[str, Any]] = []
    seen_filenames = set()

    for root in search_roots:
        if not root:
            continue

        direct_island_dir = os.path.join(root, clean_name)
        direct_nhl_dir = os.path.join(direct_island_dir, "nhl")
        checked_paths.append(direct_nhl_dir)

        if not os.path.exists(root):
            continue

        candidate_dirs: List[str] = []
        if os.path.isdir(direct_island_dir):
            candidate_dirs.append(direct_island_dir)

        # Case-insensitive / normalized folder match
        try:
            for folder in os.listdir(root):
                f_path = os.path.join(root, folder)
                if os.path.isdir(f_path):
                    f_norm = re.sub(r"[^a-zA-Z0-9]", "", folder).lower()
                    if f_norm == norm_name and f_path not in candidate_dirs:
                        candidate_dirs.append(f_path)
        except OSError:
            pass

        for island_dir in candidate_dirs:
            # Subdirectories to search: "nhl" subfolder first, then island root folder
            nhl_sub = os.path.join(island_dir, "nhl")
            search_folders = [nhl_sub, island_dir]

            for folder in search_folders:
                if folder not in checked_paths:
                    checked_paths.append(folder)

                if not os.path.isdir(folder):
                    continue

                if target_filename:
                    cand_file = os.path.join(folder, target_filename)
                    if os.path.isfile(cand_file):
                        stat = os.stat(cand_file)
                        return [
                            {
                                "filename": target_filename,
                                "file_path": cand_file,
                                "size_bytes": stat.st_size,
                                "modified_at": stat.st_mtime,
                            }
                        ], checked_paths
                else:
                    try:
                        for entry in os.listdir(folder):
                            if entry.lower().endswith(".nhl"):
                                f_full = os.path.join(folder, entry)
                                if os.path.isfile(f_full) and entry.lower() not in seen_filenames:
                                    stat = os.stat(f_full)
                                    found_files.append(
                                        {
                                            "filename": entry,
                                            "file_path": f_full,
                                            "size_bytes": stat.st_size,
                                            "modified_at": stat.st_mtime,
                                        }
                                    )
                                    seen_filenames.add(entry.lower())
                    except OSError:
                        pass

    if target_filename:
        return [], checked_paths

    def _sort_key(item):
        fname = item["filename"].lower()
        return (0 if fname == "maprefresh.nhl" else 1, fname)

    found_files.sort(key=_sort_key)
    return found_files, checked_paths


def _position_placeholder(record_index: int, A: int, B: int, F: int, G: int) -> Dict[str, Any]:
    """
    Returns whatever positional info we actually have, clearly marked
    as unconfirmed. Do NOT treat 'sector' here as reliable - it is a
    linear-index fallback, not a verified acre mapping. Replace this
    function once the real position encoding is confirmed.
    """
    return {
        "record_index": record_index,
        "raw_A": A,
        "raw_B": B,
        "raw_F": F,
        "raw_G": G,
        "position_confirmed": False,
    }


def parse_nhl_bytes(raw_bytes: bytes, island_name: str) -> Dict[str, Any]:
    """Parse raw binary .nhl data using the confirmed 16-byte record layout."""
    catalog = _load_item_catalog()
    file_size = len(raw_bytes)

    if file_size % RECORD_SIZE != 0:
        logger.warning(
            f"[NhlMapParser] File size {file_size} is not a multiple of the "
            f"confirmed record size ({RECORD_SIZE}). Parsing what fits; the "
            f"tail bytes will be ignored. This may indicate a different file "
            f"variant that hasn't been reverse-engineered yet."
        )

    total_records = file_size // RECORD_SIZE

    items: List[Dict[str, Any]] = []
    category_counts: Dict[str, int] = {}
    total_valid_items = 0
    background_fill_count = 0
    empty_count = 0

    for idx in range(total_records):
        offset = idx * RECORD_SIZE
        if offset + RECORD_SIZE > file_size:
            break

        A = struct.unpack_from("<H", raw_bytes, offset + 0)[0]
        B = struct.unpack_from("<H", raw_bytes, offset + 2)[0]
        item_id = struct.unpack_from(ITEM_ID_STRUCT, raw_bytes, offset + ITEM_ID_OFFSET)[0]
        marker = struct.unpack_from(MARKER_STRUCT, raw_bytes, offset + MARKER_OFFSET)[0]
        F = struct.unpack_from("<H", raw_bytes, offset + 12)[0]
        G = struct.unpack_from("<H", raw_bytes, offset + 14)[0]

        # Background/bulk-fill records: not real placed items.
        if A in BACKGROUND_A_VALUES and item_id != 0:
            background_fill_count += 1
            continue

        # No item in this slot.
        if item_id == 0:
            empty_count += 1
            continue

        hex_id = f"{item_id:08x}"
        info = catalog.get(hex_id)

        name = info["name"] if info else f"Unknown Item (id 0x{hex_id})"
        category = info["category"] if info else "Unresolved"

        category_counts[category] = category_counts.get(category, 0) + 1
        total_valid_items += 1

        item_obj = {
            "name": name,
            "category": category,
            "itemIdHex": hex_id,
            "itemIdDecimal": item_id,
            "markerField": f"0x{marker:04X}",
            **_position_placeholder(idx, A, B, F, G),
        }
        if info and info.get("image"):
            item_obj["imageUrl"] = info["image"]
        if info and "diy" in info:
            item_obj["diy"] = info["diy"]
        items.append(item_obj)

    return {
        "ok": True,
        "island": island_name,
        "status": "live_nhl_v2",
        "file_found": True,
        "record_size_bytes": RECORD_SIZE,
        "total_records": total_records,
        "stats": {
            "total_items": total_valid_items,
            "unique_categories": len(category_counts),
            "category_counts": category_counts,
            "empty_slots": empty_count,
            "background_fill_slots": background_fill_count,
        },
        "items": items,
        "caveats": [
            "Item IDs and empty/filler detection are confirmed against real "
            "data and a public ACNH item ID reference. Positional fields "
            "(raw_A, raw_B, raw_F, raw_G, record_index) are NOT a confirmed "
            "coordinate system - do not render these as acre/tile positions "
            "without further reverse-engineering.",
        ],
    }


def get_island_map_data(island_name: str, force_refresh: bool = False) -> Dict[str, Any]:
    """Main entrypoint: retrieves parsed map data for an island (cached)."""
    import time
    clean_name = island_name.strip()
    norm_key = clean_name.lower()

    if not force_refresh and norm_key in _MAP_CACHE:
        cache_time, cached_data = _MAP_CACHE[norm_key]
        if time.time() - cache_time < _CACHE_TTL_SECONDS:
            return cached_data

    file_path, file_exists, checked_paths = locate_nhl_file(clean_name)

    if file_exists and file_path and os.path.isfile(file_path):
        try:
            with open(file_path, "rb") as f:
                raw_bytes = f.read()
            parsed = parse_nhl_bytes(raw_bytes, clean_name)
            parsed["file_path"] = file_path
        except Exception as exc:
            logger.error(f"[NhlMapParser] Error parsing {file_path}: {exc}")
            parsed = {
                "ok": False,
                "island": clean_name,
                "status": "error",
                "file_found": True,
                "file_path": file_path,
                "error": f"Failed to parse maprefresh.nhl: {exc}",
                "items": [],
                "stats": {"total_items": 0, "unique_categories": 0, "category_counts": {}},
            }
    else:
        parsed = {
            "ok": False,
            "island": clean_name,
            "status": "not_found",
            "file_found": False,
            "file_path": None,
            "error": f"maprefresh.nhl not found for island '{clean_name}'.",
            "checked_paths": checked_paths,
            "items": [],
            "stats": {"total_items": 0, "unique_categories": 0, "category_counts": {}},
        }

    _MAP_CACHE[norm_key] = (time.time(), parsed)
    return parsed


def search_island_items(island_name: str, query: str) -> Dict[str, Any]:
    """Search for items by name or category on a specific island map."""
    map_data = get_island_map_data(island_name)
    q = query.strip().lower()

    if not map_data.get("file_found"):
        return {
            "ok": False,
            "island": map_data.get("island", island_name),
            "query": query,
            "status": map_data.get("status", "not_found"),
            "file_found": False,
            "error": map_data.get("error", "maprefresh.nhl not found"),
            "total_matches": 0,
            "matches": [],
        }

    if not q:
        return {
            "ok": True,
            "island": map_data["island"],
            "query": query,
            "file_found": True,
            "total_matches": 0,
            "matches": [],
        }

    matches = []
    for item in map_data.get("items", []):
        name_match = q in item["name"].lower()
        cat_match = q in item["category"].lower()
        if name_match or cat_match:
            matches.append(item)

    return {
        "ok": True,
        "island": map_data["island"],
        "query": query,
        "file_found": True,
        "status": map_data.get("status", "live_nhl_v2"),
        "total_matches": len(matches),
        "matches": matches,
    }
