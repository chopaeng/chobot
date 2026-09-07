"""
NHL Island Map Parser Module
Locates and parses Animal Crossing: New Horizons .nhl (New Horizons Layer) files:
Paths checked:
  - VILLAGERS_DIR/<island_name>/nhl/maprefresh.nhl       (VIP / Sub Islands)
  - TWITCH_VILLAGERS_DIR/<island_name>/nhl/maprefresh.nhl (Free / Twitch Islands)

Extracts ground items, coordinates (X, Y), acre sectors (A1..G7), and cross-references
with the local item catalog for names, categories, icons, and search indexing.
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

_ITEM_CATALOG_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_MAP_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 60.0


def _load_item_catalog() -> Dict[str, Dict[str, Any]]:
    """Load items_detail.json / acnh.json once into a fast hex-ID lookup table."""
    global _ITEM_CATALOG_CACHE
    if _ITEM_CATALOG_CACHE is not None:
        return _ITEM_CATALOG_CACHE

    catalog: Dict[str, Dict[str, Any]] = {}
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    items_path = os.path.join(base_dir, "items_detail.json")

    if not os.path.exists(items_path):
        items_path = os.path.join(base_dir, "acnh.json")

    if os.path.exists(items_path):
        try:
            with open(items_path, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
                items_list = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                for item in items_list:
                    raw_id = item.get("Internal ID") or item.get("pokerId") or item.get("id")
                    name = item.get("Name")
                    if not raw_id or not name:
                        continue

                    hex_key = str(raw_id).strip().upper()
                    if len(hex_key) < 4:
                        hex_key = hex_key.zfill(4)

                    image_url = ""
                    variations = item.get("Variations")
                    if isinstance(variations, list) and len(variations) > 0:
                        image_url = variations[0].get("imageUrl") or ""

                    entry = {
                        "name": name,
                        "category": item.get("Category", "Miscellaneous"),
                        "diy": item.get("DIY") == "Yes",
                        "imageUrl": image_url,
                        "internalId": hex_key,
                        "tag": item.get("ItemTag", ""),
                    }
                    catalog[hex_key] = entry

                    if isinstance(variations, list):
                        for var in variations:
                            var_poker = var.get("pokerId")
                            if var_poker:
                                var_hex = str(var_poker).strip().upper().zfill(4)
                                if var_hex not in catalog:
                                    catalog[var_hex] = {
                                        **entry,
                                        "imageUrl": var.get("imageUrl") or image_url,
                                        "internalId": var_hex,
                                    }
            logger.info(f"[NhlMapParser] Loaded {len(catalog)} item definitions from catalog.")
        except Exception as exc:
            logger.error(f"[NhlMapParser] Failed to load catalog: {exc}")

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


def _coord_to_sector(x: int, y: int, acre_size: int = 16) -> str:
    """Map tile (X, Y) to standard ACNH sector (e.g. A1, B3, F6)."""
    col_idx = min(6, max(0, x // acre_size))
    row_idx = min(7, max(0, y // acre_size + 1))
    col_letter = chr(ord("A") + col_idx)
    return f"{col_letter}{row_idx}"


def parse_nhl_bytes(raw_bytes: bytes, island_name: str) -> Dict[str, Any]:
    """Parse raw binary .nhl data into items, sectors, and summary statistics."""
    catalog = _load_item_catalog()
    file_size = len(raw_bytes)

    if file_size % 8 == 0 and (file_size // 8 in (10752, 25600, 43008) or file_size % 4 != 0):
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

    for idx in range(total_slots):
        offset = idx * stride
        if offset + 2 > file_size:
            break

        item_id = struct.unpack_from("<H", raw_bytes, offset)[0]

        # 0x0000 = Empty, 0xFFFE/0xFFFF = Ignored/Void
        if item_id in (0x0000, 0xFFFE, 0xFFFF, 0xFEFF):
            continue

        count_or_flag = 1
        if stride >= 4 and offset + 4 <= file_size:
            count_or_flag = struct.unpack_from("<H", raw_bytes, offset + 2)[0]

        tile_x = idx % grid_w
        tile_y = idx // grid_w
        sector = _coord_to_sector(tile_x, tile_y, acre_size)

        hex_id = f"{item_id:04X}"
        info = catalog.get(hex_id)

        name = info["name"] if info else f"Item #{item_id} (0x{hex_id})"
        category = info["category"] if info else "Miscellaneous"
        diy = info["diy"] if info else False
        image_url = info["imageUrl"] if info else ""

        category_counts[category] = category_counts.get(category, 0) + 1
        total_valid_items += 1

        item_obj = {
            "name": name,
            "category": category,
            "diy": diy,
            "internalId": hex_id,
            "x": tile_x,
            "y": tile_y,
            "sector": sector,
            "count": count_or_flag if count_or_flag > 1 else 1,
            "imageUrl": image_url,
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
            "items": [],
            "sectors": {},
            "sector_summary": {},
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
        sec_match = q == item["sector"].lower()

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
