"""
NHL Island Map Parser Module
Locates and parses Animal Crossing: New Horizons .nhl (New Horizons Layer) files:
Paths checked:
  - VILLAGERS_DIR/<island_name>/nhl/maprefresh.nhl       (VIP / Sub Islands)
  - TWITCH_VILLAGERS_DIR/<island_name>/nhl/maprefresh.nhl (Free / Twitch Islands)

Extracts ground items, coordinates (X, Y), acre sectors (A1..G7), and cross-references
with the local item catalog (data/acnh.min.json) for names, categories, icons, and search indexing.
Strictly parses real files only. No mock data.
"""

import os
import struct
import json
import logging
import re
from typing import Dict, List, Optional, Any, Tuple

from utils.config import Config

logger = logging.getLogger("NhlMapParser")

_ITEM_CATALOG_CACHE: Optional[Dict[Any, Dict[str, Any]]] = None
_MAP_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 60.0

# Confirmed 16-byte record layout for real .nhl files:
#   offset +0 (uint16): A (raw field)
#   offset +2 (uint16): B (raw field)
#   offset +4 (uint32): item_id (little-endian uint32)
#   offset +8 (uint16): marker / sentinel field (0xFFFD, 0xFFFE)
#   offset +10 (uint16): padding / reserved
#   offset +12 (uint16): F (raw field)
#   offset +14 (uint16): G (raw field)
RECORD_SIZE_16 = 16
ITEM_ID_OFFSET = 4
ITEM_ID_STRUCT = "<I"
MARKER_OFFSET = 8
MARKER_STRUCT = "<H"

# Observed bulk background pattern markers
BACKGROUND_A_VALUES = {0xFFFD, 0xFFFE}
KNOWN_MARKER_VALUES = {0xFFFD, 0xFFFE}

# Confirmed lowest real ACNH item ID (0x50 = "clackercart").
# Nonzero values below 0x50 are non-item fields (terrain/count/flags)
# and are excluded from catalog lookup to prevent "everything is a painting" collisions.
MIN_PLAUSIBLE_ITEM_ID = 0x50


def _load_item_catalog() -> Dict[Any, Dict[str, Any]]:
    """Load items from data/acnh.min.json (or fallback items_detail.json / acnh.json)
    into a fast lookup table supporting 4-hex, 8-hex, and integer decimal keys.
    """
    global _ITEM_CATALOG_CACHE
    if _ITEM_CATALOG_CACHE is not None:
        return _ITEM_CATALOG_CACHE

    catalog: Dict[Any, Dict[str, Any]] = {}
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

    def _index_entry(id_val: Any, entry: Dict[str, Any]):
        if id_val is None:
            return
        try:
            if isinstance(id_val, int):
                int_v = id_val
            else:
                s = str(id_val).strip()
                if s.startswith(("0x", "0X")):
                    int_v = int(s, 16)
                elif s.isdigit():
                    int_v = int(s)
                else:
                    int_v = int(s, 16)

            h4 = f"{int_v:04X}"
            h8 = f"{int_v:08X}"
            for k in (h4, h4.lower(), h8, h8.lower(), int_v):
                if k not in catalog:
                    catalog[k] = entry
        except Exception:
            pass

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

                entry = {
                    "name": name,
                    "category": cat,
                    "diy": diy,
                    "imageUrl": image,
                }
                top_id = item.get("internalId") or item.get("Internal ID") or item.get("id")
                _index_entry(top_id, entry)

                # Variations (different colors, remakes, etc.)
                for var in item.get("variations", []) or []:
                    var_name = var.get("variation")
                    display_name = (
                        f"{name} ({var_name})"
                        if var_name and str(var_name).lower() not in ("na", "none", "")
                        else name
                    )
                    v_entry = {
                        "name": display_name,
                        "category": cat,
                        "diy": diy,
                        "imageUrl": var.get("image") or image,
                    }
                    _index_entry(var.get("internalId") or var.get("variantId"), v_entry)

            # 2. Creatures (fish, bugs, sea creatures)
            for cr in data.get("creatures", []):
                c_name = cr.get("name")
                if not c_name:
                    continue
                c_cat = cr.get("sourceSheet") or "Creatures"
                c_img = cr.get("iconImage") or cr.get("critterpediaImage") or ""
                _index_entry(
                    cr.get("internalId"),
                    {"name": c_name, "category": c_cat, "diy": False, "imageUrl": c_img},
                )

            # 3. Recipes
            for rec in data.get("recipes", []):
                r_name = rec.get("name")
                if not r_name:
                    continue
                r_img = rec.get("image") or rec.get("imageSh") or ""
                _index_entry(
                    rec.get("internalId"),
                    {"name": f"{r_name} (DIY Recipe)", "category": "Recipes", "diy": True, "imageUrl": r_img},
                )

        else:
            # Fallback legacy items_detail.json / acnh.json
            items_list = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for item in items_list:
                raw_id = item.get("Internal ID") or item.get("pokerId") or item.get("id") or item.get("internalId")
                name = item.get("Name") or item.get("name")
                if not raw_id or not name:
                    continue

                image_url = ""
                variations = item.get("Variations")
                if isinstance(variations, list) and len(variations) > 0:
                    image_url = variations[0].get("imageUrl") or ""

                entry = {
                    "name": name,
                    "category": item.get("Category", "Miscellaneous"),
                    "diy": item.get("DIY") == "Yes",
                    "imageUrl": image_url,
                }
                _index_entry(raw_id, entry)

                if isinstance(variations, list):
                    for var in variations:
                        var_poker = var.get("pokerId")
                        if var_poker:
                            _index_entry(
                                var_poker,
                                {
                                    **entry,
                                    "imageUrl": var.get("imageUrl") or image_url,
                                },
                            )

        logger.info(f"[NhlMapParser] Loaded {len(catalog)} item lookup keys from {items_path}.")
    except Exception as exc:
        logger.error(f"[NhlMapParser] Failed to load catalog from {items_path}: {exc}")

    _ITEM_CATALOG_CACHE = catalog
    return catalog


def locate_nhl_file(island_name: str) -> Tuple[Optional[str], bool, List[str]]:
    """
    Locates the maprefresh.nhl file for an island across both:
      - Config.VILLAGERS_DIR (Sub / VIP Islands)
      - Config.TWITCH_VILLAGERS_DIR (Free / Twitch Islands)
    Returns: (found_path, file_exists, checked_expected_paths)
    """
    if not island_name:
        return None, False, []

    clean_name = island_name.strip()
    norm_name = re.sub(r"[^a-zA-Z0-9]", "", clean_name).lower()

    search_roots = [
        Config.VILLAGERS_DIR,
        Config.TWITCH_VILLAGERS_DIR,
    ]

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

        # Case-insensitive folder search
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


def _coord_to_sector(x: int, y: int, acre_size: int = 16) -> str:
    """Map tile (X, Y) to standard ACNH sector (e.g. A1, B3, F6)."""
    col_idx = min(6, max(0, x // acre_size))
    row_idx = min(7, max(0, y // acre_size + 1))
    col_letter = chr(ord("A") + col_idx)
    return f"{col_letter}{row_idx}"


def parse_nhl_bytes(raw_bytes: bytes, island_name: str) -> Dict[str, Any]:
    """Parse raw binary .nhl data into items, sectors, and summary statistics.

    Supports confirmed 16-byte records (uint32 item_id at offset +4) as well as
    fallback strides (8/4) if file size does not align with 16.
    """
    catalog = _load_item_catalog()
    file_size = len(raw_bytes)

    # Determine record format and stride
    use_16_byte = (file_size % 16 == 0 and file_size > 0)
    if use_16_byte:
        stride = 16
    elif file_size % 8 == 0 and (file_size // 8 in (10752, 25600, 43008) or file_size % 4 != 0):
        stride = 8
    else:
        stride = 4

    total_slots = file_size // stride

    if total_slots >= 43008:
        grid_w, grid_h = 224, 192
        acre_size = 32
    elif total_slots >= 25600:
        grid_w, grid_h = 160, 160
        acre_size = 20
    else:
        grid_w, grid_h = 112, 96
        acre_size = 16

    items: List[Dict[str, Any]] = []
    sectors: Dict[str, List[Dict[str, Any]]] = {}
    category_counts: Dict[str, int] = {}
    total_valid_items = 0
    empty_count = 0
    background_fill_count = 0
    low_value_count = 0

    for idx in range(total_slots):
        offset = idx * stride
        if offset + stride > file_size:
            break

        if use_16_byte:
            A = struct.unpack_from("<H", raw_bytes, offset + 0)[0]
            B = struct.unpack_from("<H", raw_bytes, offset + 2)[0]
            item_id = struct.unpack_from(ITEM_ID_STRUCT, raw_bytes, offset + ITEM_ID_OFFSET)[0]
            marker = struct.unpack_from(MARKER_STRUCT, raw_bytes, offset + MARKER_OFFSET)[0]
            F = struct.unpack_from("<H", raw_bytes, offset + 12)[0]
            G = struct.unpack_from("<H", raw_bytes, offset + 14)[0]
            count_or_flag = 1

            # Background/bulk-fill records: not genuine placed items
            if A in BACKGROUND_A_VALUES and item_id != 0:
                background_fill_count += 1
                continue
        else:
            item_id = struct.unpack_from("<H", raw_bytes, offset)[0]
            count_or_flag = struct.unpack_from("<H", raw_bytes, offset + 2)[0] if stride >= 4 else 1
            A, B, F, G = 0, 0, 0, 0
            marker = 0

        # Empty / sentinel slots
        if item_id == 0 or item_id in (0xFFFE, 0xFFFF, 0xFEFF, 0xFFFD):
            empty_count += 1
            continue

        # Exclude noise / low-value IDs below 0x50 that collide with paintings
        if item_id < MIN_PLAUSIBLE_ITEM_ID:
            low_value_count += 1
            continue

        tile_x = idx % grid_w
        tile_y = idx // grid_w
        sector = _coord_to_sector(tile_x, tile_y, acre_size)

        hex_id_4 = f"{item_id:04X}"
        hex_id_8 = f"{item_id:08X}"

        # Look up in catalog (checked across 4-hex, 8-hex, and integer keys)
        info = catalog.get(hex_id_4) or catalog.get(hex_id_8) or catalog.get(item_id)

        name = info["name"] if info else f"Item #{item_id} (0x{hex_id_4})"
        category = info["category"] if info else "Miscellaneous"
        diy = info["diy"] if info else False
        image_url = info.get("imageUrl") or ""

        category_counts[category] = category_counts.get(category, 0) + 1
        total_valid_items += 1

        item_obj = {
            "name": name,
            "category": category,
            "diy": diy,
            "internalId": hex_id_4,
            "itemIdHex": hex_id_8,
            "itemIdDecimal": item_id,
            "x": tile_x,
            "y": tile_y,
            "sector": sector,
            "count": count_or_flag if count_or_flag > 1 else 1,
            "imageUrl": image_url,
            "markerField": f"0x{marker:04X}",
            "record_index": idx,
            "raw_A": A,
            "raw_B": B,
            "raw_F": F,
            "raw_G": G,
            "position_confirmed": False,
        }

        items.append(item_obj)
        if sector not in sectors:
            sectors[sector] = []
        sectors[sector].append(item_obj)

    sector_summary = {}
    for sec, sec_items in sectors.items():
        sec_cat_counts: Dict[str, int] = {}
        for item in sec_items:
            cat = item["category"]
            sec_cat_counts[cat] = sec_cat_counts.get(cat, 0) + 1
        top_cats = sorted(sec_cat_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        sector_summary[sec] = {
            "total_items": len(sec_items),
            "top_categories": [cat for cat, _ in top_cats],
            "sample_items": [i["name"] for i in sec_items[:5]],
        }

    return {
        "ok": True,
        "island": island_name,
        "status": "live_nhl",
        "file_found": True,
        "record_size_bytes": stride,
        "total_records": total_slots,
        "grid": {
            "width": grid_w,
            "height": grid_h,
            "acre_size": acre_size,
            "cols": ["A", "B", "C", "D", "E", "F", "G"],
            "rows": [1, 2, 3, 4, 5, 6],
        },
        "stats": {
            "total_items": total_valid_items,
            "unique_categories": len(category_counts),
            "category_counts": category_counts,
            "empty_slots": empty_count,
            "background_fill_slots": background_fill_count,
            "low_value_slots": low_value_count,
        },
        "sector_summary": sector_summary,
        "sectors": sectors,
        "items": items,
    }


def get_island_map_data(island_name: str, force_refresh: bool = False) -> Dict[str, Any]:
    """
    Main entrypoint: retrieves parsed map data for an island (cached).
    Checks both VILLAGERS_DIR and TWITCH_VILLAGERS_DIR.
    Strictly parses real files. If maprefresh.nhl does not exist, returns
    file_found: False and empty data. No mock data.
    """
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
                "grid": {
                    "width": 112,
                    "height": 96,
                    "acre_size": 16,
                    "cols": ["A", "B", "C", "D", "E", "F", "G"],
                    "rows": [1, 2, 3, 4, 5, 6],
                },
                "items": [],
                "sectors": {},
                "sector_summary": {},
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
            "grid": {
                "width": 112,
                "height": 96,
                "acre_size": 16,
                "cols": ["A", "B", "C", "D", "E", "F", "G"],
                "rows": [1, 2, 3, 4, 5, 6],
            },
            "items": [],
            "sectors": {},
            "sector_summary": {},
            "stats": {"total_items": 0, "unique_categories": 0, "category_counts": {}},
        }

    _MAP_CACHE[norm_key] = (time.time(), parsed)
    return parsed


def search_island_items(island_name: str, query: str) -> Dict[str, Any]:
    """Search for items by name, category, or sector on a specific island map."""
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
        name_match = q in item.get("name", "").lower()
        cat_match = q in item.get("category", "").lower()
        sec_match = q == item.get("sector", "").lower()

        if name_match or cat_match or sec_match:
            matches.append(item)

    return {
        "ok": True,
        "island": map_data["island"],
        "query": query,
        "file_found": True,
        "status": map_data.get("status", "live_nhl"),
        "total_matches": len(matches),
        "matches": matches,
    }