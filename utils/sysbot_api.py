"""
SysBot API Client & Bundles Integration Module
Handles REST API interactions with SysBot.ACNHOrders, bundles API, and SQLite order queue sync.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3.exceptions

from utils.config import Config
from utils.database import connect_db

logger = logging.getLogger("SysBotAPI")

BUNDLES_API_URL = "https://console.chopaeng.com/api/bundles"
_INVALID_DODO_CODES = frozenset({"00000", "-----", "null", "None", ""})

_SYSBOT_STATUS_MAP = {
    "queued": "queued",
    "next": "preparing",
    "ready": "ready",
    "completed": "completed",
    "cancelled": "cancelled",
    "not_found": "error",
    "error": "error",
    "active": "preparing",
    "in_progress": "preparing",
    "preparing": "preparing",
}


def _ensure_order_table():
    """Ensure the database schema and order_bot_queue table exist."""
    try:
        connect_db().close()
    except Exception as exc:
        logger.warning(f"[SysBotAPI] Failed to ensure database schema: {exc}")
 

def _parse_eta_minutes(data: dict) -> int:
    """Extract estimated minutes from estimated_seconds or eta string."""
    if data.get("estimated_seconds") is not None:
        try:
            return max(1, int(round(float(data["estimated_seconds"]) / 60.0)))
        except (ValueError, TypeError):
            pass
    if data.get("eta"):
        m = re.search(r"(\d+)m", str(data["eta"]))
        if m:
            return max(1, int(m.group(1)))
    return 2


def normalize_order_data(data: dict) -> dict:
    """Normalize order response from SysBot."""
    if not isinstance(data, dict):
        return data

    dodo = data.get("dodo_code")
    has_dodo = bool(dodo and str(dodo).strip() not in _INVALID_DODO_CODES)
    if not has_dodo:
        data["dodo_code"] = None

    raw_status = str(data.get("status") or "queued").lower()
    if has_dodo and raw_status not in ("completed", "cancelled", "error"):
        data["status"] = "ready"
    else:
        data["status"] = _SYSBOT_STATUS_MAP.get(raw_status, raw_status)

    raw_pos = data.get("queue_position") or data.get("position")
    if raw_pos is not None:
        try:
            pos_int = int(raw_pos)
            if pos_int <= 0 and data["status"] not in ("ready", "preparing"):
                pos_int = 1
            data["queue_position"] = max(0, pos_int)
        except (ValueError, TypeError):
            data["queue_position"] = 1
    else:
        data["queue_position"] = 0 if data["status"] in ("ready", "preparing") else 1

    if "estimated_minutes" not in data or data.get("estimated_minutes") is None:
        data["estimated_minutes"] = 0 if data["status"] == "ready" else _parse_eta_minutes(data)

    return data


def parse_order_input(raw_input: str) -> Tuple[str, Optional[str]]:
    """
    Parse a raw order string into (items_string, villager_name_or_id).
    Supports:
      - Name lists: "Gold nugget 30, Iron nugget 30, villager:Raymond"
      - Pure hex sequences: "14BB 16DB 0000002000003604 villager:Raymond"
      - Villager prefix/suffix: "villager:Raymond, Royal crown 40"
    """
    cleaned = re.sub(r"^!(order|drop)\s*", "", raw_input.strip(), flags=re.IGNORECASE).strip()
    if not cleaned:
        return "", None

    villager = None
    # 1. Check for 'villager:<name_or_id>' format
    v_match = re.search(r"\bvillager:([a-zA-Z0-9_\-]+)\b", cleaned, flags=re.IGNORECASE)
    if v_match:
        villager = v_match.group(1).strip()
        # Remove the villager:xxx portion from items
        cleaned = re.sub(r"\bvillager:[a-zA-Z0-9_\-]+\b", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r",\s*,", ",", cleaned).strip(" ,")

    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    return cleaned, villager


class SysBotClient:
    """HTTP Client for communicating with SysBot.ACNHOrders REST API and external bundles."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 10,
    ):
        raw_url = base_url if base_url is not None else getattr(Config, "SYSBOT_API_URL", "")
        self.base_url = (raw_url or "").rstrip("/")
        self.api_key = api_key if api_key is not None else getattr(Config, "SYSBOT_API_KEY", "") or ""
        self.timeout = timeout
        self._session: Optional[requests.Session] = None
        self._bundles_cache: Optional[List[Dict[str, Any]]] = None
        self._bundles_cache_time: float = 0
        self._bundles_cache_ttl: float = 300  # 5 minutes
        self._offline_until: float = 0.0
        self._offline_cooldown: float = 10.0

        _ensure_order_table()

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            retry = Retry(
                total=1,
                connect=False,  # Never retry when target actively refused connection / offline
                read=1,
                status=1,
                backoff_factor=0.3,
                status_forcelist=[502, 503, 504],
                allowed_methods=["GET", "POST", "DELETE"],
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=10)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
        return self._session

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    # ── Synchronous HTTP Request Helpers ─────────────────────────────────────

    def _get(self, path: str, **params) -> Tuple[dict, int]:
        if not self.base_url:
            return {"success": False, "error": "SysBot API is not configured."}, 503

        now = time.monotonic()
        if path in ("/api/status", "/api/queue") and now < self._offline_until:
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503

        url = f"{self.base_url}{path}"
        timeout_val = (1.5, 3.0) if path in ("/api/status", "/api/queue") else (3.0, float(self.timeout))
        try:
            resp = self._get_session().get(
                url,
                headers=self._headers(),
                params={k: v for k, v in params.items() if v is not None},
                timeout=timeout_val,
            )
            if resp.status_code in (502, 503, 504):
                self._offline_until = time.monotonic() + self._offline_cooldown
            else:
                self._offline_until = 0.0
            try:
                return resp.json(), resp.status_code
            except Exception:
                return {"success": False, "error": f"HTTP {resp.status_code}: non-JSON response"}, resp.status_code
        except (requests.exceptions.ConnectionError, urllib3.exceptions.HTTPError, OSError):
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503
        except requests.exceptions.Timeout:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API request timed out."}, 504
        except Exception as exc:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": str(exc)}, 500

    def _post(self, path: str, payload: dict) -> Tuple[dict, int]:
        if not self.base_url:
            return {"success": False, "error": "SysBot API is not configured."}, 503

        now = time.monotonic()
        if now < self._offline_until and path != "/api/order/cancel":
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503

        url = f"{self.base_url}{path}"
        try:
            resp = self._get_session().post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=(3.0, float(self.timeout)),
            )
            if resp.status_code in (502, 503, 504):
                self._offline_until = time.monotonic() + self._offline_cooldown
            else:
                self._offline_until = 0.0
            try:
                return resp.json(), resp.status_code
            except Exception:
                return {"success": False, "error": f"HTTP {resp.status_code}: non-JSON response"}, resp.status_code
        except (requests.exceptions.ConnectionError, urllib3.exceptions.HTTPError, OSError):
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503
        except requests.exceptions.Timeout:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API request timed out."}, 504
        except Exception as exc:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": str(exc)}, 500

    # ── Async API Methods ───────────────────────────────────────────────────

    async def get_bot_status(self) -> dict:
        """Fetch SysBot island and order bot state."""
        island_default = getattr(Config, "ORDER_BOT_ISLAND", "Sinta")
        if not self.base_url or time.monotonic() < self._offline_until:
            return {
                "success": False,
                "error": "SysBot API is not configured." if not self.base_url else "SysBot API is offline.",
                "island_name": island_default,
                "is_running": False,
                "accepting_commands": False,
                "queue_count": 0,
            }

        data, code = await asyncio.to_thread(self._get, "/api/status")
        if not isinstance(data, dict):
            data = {"success": False, "error": "Invalid response"}

        if code == 200 or data.get("success"):
            data.setdefault("island_name", island_default)
            data.setdefault("is_running", True)
            data.setdefault("accepting_commands", True)
            data.setdefault("queue_count", 0)
        else:
            data.setdefault("is_running", False)
            data.setdefault("accepting_commands", False)
            data.setdefault("island_name", island_default)
            data.setdefault("queue_count", 0)

        return data

    async def submit_order(
        self,
        username: str,
        order_text: str = "",
        items: Optional[List[str]] = None,
        villager: Optional[str] = None,
        user_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> dict:
        """
        Submit an order to SysBot and persist to SQLite DB.
        Returns normalized order submission result.
        """
        parsed_order, parsed_villager = parse_order_input(order_text)
        final_order = parsed_order or ""
        final_villager = villager or parsed_villager

        payload: Dict[str, Any] = {"username": username}
        if final_order:
            payload["order"] = final_order
        elif items:
            payload["items"] = items

        if final_villager:
            payload["villager"] = final_villager
        if user_id:
            payload["user_id"] = str(user_id)
        if order_id:
            payload["order_id"] = str(order_id)

        result, code = await asyncio.to_thread(self._post, "/api/order", payload)
        result = normalize_order_data(result)

        # Persist order to database if successful
        if isinstance(result, dict) and (result.get("success") or code in (200, 201)):
            assigned_id = str(result.get("order_id") or result.get("id") or order_id or f"ord_{int(time.time())}")
            result["order_id"] = assigned_id
            now_ts = int(time.time())
            cmd_str = final_order or (", ".join(items) if items else (f"villager:{final_villager}" if final_villager else "Order"))
            if final_villager and f"villager:{final_villager}" not in cmd_str:
                cmd_str = f"{cmd_str}, villager:{final_villager}"

            try:
                with connect_db() as conn:
                    conn.execute(
                        """
                        REPLACE INTO order_bot_queue
                            (id, user_id, username, command, order_type, status,
                             queue_position, estimated_minutes, dodo_code, island_name,
                             message, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            assigned_id,
                            str(user_id or "anonymous"),
                            username,
                            cmd_str,
                            "order",
                            result.get("status", "queued"),
                            int(result.get("queue_position") or 1),
                            int(result.get("estimated_minutes") or 2),
                            result.get("dodo_code"),
                            result.get("island_name") or getattr(Config, "ORDER_BOT_ISLAND", "Sinta"),
                            result.get("message") or "Order placed via Order Panel",
                            now_ts,
                            now_ts,
                        ),
                    )
                    conn.commit()
            except Exception as exc:
                logger.warning(f"[SysBotAPI] Failed to save order {assigned_id} to DB: {exc}")

        return result

    async def get_order_status(self, order_id: str) -> dict:
        """Poll order status from SysBot and update DB."""
        if not order_id:
            return {"success": False, "error": "Order ID is required."}

        data, code = await asyncio.to_thread(self._get, "/api/order/status", id=order_id)
        data = normalize_order_data(data)

        if isinstance(data, dict) and data.get("status"):
            now_ts = int(time.time())
            try:
                with connect_db() as conn:
                    conn.execute(
                        """
                        UPDATE order_bot_queue
                        SET status = ?,
                            queue_position = COALESCE(?, queue_position),
                            estimated_minutes = COALESCE(?, estimated_minutes),
                            dodo_code = COALESCE(?, dodo_code),
                            island_name = COALESCE(?, island_name),
                            message = COALESCE(?, message),
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            data.get("status"),
                            data.get("queue_position"),
                            data.get("estimated_minutes"),
                            data.get("dodo_code"),
                            data.get("island_name"),
                            data.get("message"),
                            now_ts,
                            order_id,
                        ),
                    )
                    conn.commit()
            except Exception as exc:
                logger.warning(f"[SysBotAPI] Failed to update order status for {order_id}: {exc}")

        return data

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an active order on SysBot and mark cancelled in DB."""
        if not order_id:
            return {"success": False, "error": "Order ID is required."}

        result, code = await asyncio.to_thread(self._post, "/api/order/cancel", {"id": order_id})
        try:
            with connect_db() as conn:
                conn.execute(
                    "UPDATE order_bot_queue SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (int(time.time()), order_id),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"[SysBotAPI] Failed to mark order {order_id} as cancelled: {exc}")

        return result

    async def get_queue(self) -> dict:
        """Fetch the live queue of pending orders."""
        data, code = await asyncio.to_thread(self._get, "/api/queue")
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            for entry in data["orders"]:
                if "position" in entry and "queue_position" not in entry:
                    entry["queue_position"] = entry["position"]
                raw_st = str(entry.get("status") or "queued").lower()
                entry["status"] = _SYSBOT_STATUS_MAP.get(raw_st, raw_st)
                if "estimated_minutes" not in entry:
                    entry["estimated_minutes"] = _parse_eta_minutes(entry)
            data["queue"] = data["orders"]
        elif isinstance(data, dict) and not data.get("orders"):
            data["orders"] = []
            data["queue"] = []
        return data

    async def get_dodo(self, order_id: Optional[str] = None, user_id: Optional[str] = None) -> dict:
        """Fetch Dodo code for an order or drop mode."""
        params = {}
        if order_id:
            params["order_id"] = order_id
        if user_id:
            params["user_id"] = str(user_id)
        data, code = await asyncio.to_thread(self._get, "/api/dodo", **params)
        return data

    async def get_presets(self) -> List[str]:
        """Fetch preset names configured directly in SysBot."""
        data, code = await asyncio.to_thread(self._get, "/api/presets")
        if isinstance(data, dict) and isinstance(data.get("presets"), list):
            return data["presets"]
        if isinstance(data, list):
            return data
        return []

    async def get_bundles(self) -> List[Dict[str, Any]]:
        """Fetch official bundles from https://console.chopaeng.com/api/bundles."""
        now = time.time()
        if self._bundles_cache and (now - self._bundles_cache_time) < self._bundles_cache_ttl:
            return self._bundles_cache

        def _fetch():
            try:
                resp = requests.get(BUNDLES_API_URL, timeout=8)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                logger.warning(f"[SysBotAPI] Failed to fetch bundles from {BUNDLES_API_URL}: {e}")
            return None

        data = await asyncio.to_thread(_fetch)
        if isinstance(data, list) and data:
            self._bundles_cache = data
            self._bundles_cache_time = now
            return data

        # Fallback default presets if external API is unreachable
        return [
            {
                "id": "bundle-materials",
                "name": "Essential Materials",
                "title": "Essential Materials",
                "category": "Popular",
                "description": "Full stack of essential crafting materials (Gold, Iron, Wood, NMT).",
                "orderItems": [
                    {"itemId": "09C9", "name": "Gold nugget", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/2/26/Gold_Nugget_NH_Inv_Icon.png"},
                    {"itemId": "09CF", "name": "Iron nugget", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/5/52/Iron_Nugget_NH_Inv_Icon.png"},
                    {"itemId": "16DB", "name": "Nook Miles Ticket", "quantity": 10, "category": "Currency", "image": "https://dodo.ac/np/images/4/43/Nook_Miles_Ticket_NH_Inv_Icon.png"},
                    {"itemId": "0AD0", "name": "Wood", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/d/df/Wood_NH_Inv_Icon.png"},
                ],
            },
            {
                "id": "bundle-wealth",
                "name": "Crowns & Wealth Pack",
                "title": "Crowns & Wealth Pack",
                "category": "Wealth",
                "description": "Max value Bell bundles, Royal Crowns, and Nook Miles Tickets.",
                "orderItems": [
                    {"itemId": "14BB", "name": "Royal Crown", "quantity": 10, "category": "Clothing", "image": "https://dodo.ac/np/images/c/c7/Royal_Crown_NH_Storage_Icon.png"},
                    {"itemId": "16DB", "name": "Nook Miles Ticket", "quantity": 10, "category": "Currency", "image": "https://dodo.ac/np/images/4/43/Nook_Miles_Ticket_NH_Inv_Icon.png"},
                    {"itemId": "09C9", "name": "Gold nugget", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/2/26/Gold_Nugget_NH_Inv_Icon.png"},
                ],
            },
            {
                "id": "bundle-golden-tools",
                "name": "Golden Tools Set",
                "title": "Golden Tools Set",
                "category": "Tools",
                "description": "Complete set of durable Golden Tools for island maintenance.",
                "orderItems": [
                    {"itemId": "2591", "name": "Golden axe", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/8/87/Golden_Axe_NH_Inv_Icon.png"},
                    {"itemId": "217E", "name": "Golden shovel", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/e/e5/Golden_Shovel_NH_Inv_Icon.png"},
                    {"itemId": "1FF3", "name": "Golden net", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/7/7b/Golden_Net_NH_Inv_Icon.png"},
                    {"itemId": "2155", "name": "Golden watering can", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/2/2c/Golden_Watering_Can_NH_Inv_Icon.png"},
                    {"itemId": "2182", "name": "Golden slingshot", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/6/6f/Golden_Slingshot_NH_Inv_Icon.png"},
                ],
            },
            {
                "id": "bundle-celestial",
                "name": "Star Fragments & Celestial",
                "title": "Star Fragments & Celestial",
                "category": "Seasonal",
                "description": "Celeste crafting essentials including regular and large Star Fragments.",
                "orderItems": [
                    {"itemId": "175F", "name": "Star fragment", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/6/64/Star_Fragment_NH_Inv_Icon.png"},
                    {"itemId": "1760", "name": "Large star fragment", "quantity": 10, "category": "Materials", "image": "https://dodo.ac/np/images/5/5a/Large_Star_Fragment_NH_Inv_Icon.png"},
                ],
            },
        ]

    async def get_user_order_history(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch user order history from the SQLite database."""
        if not user_id:
            return []

        def _query():
            orders = []
            try:
                with connect_db() as conn:
                    cur = conn.execute(
                        """
                        SELECT id, user_id, username, command, order_type, status,
                               queue_position, estimated_minutes, dodo_code, island_name,
                               message, created_at, updated_at
                        FROM order_bot_queue
                        WHERE user_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (str(user_id), limit),
                    )
                    rows = cur.fetchall()
                    for r in rows:
                        orders.append({
                            "id": r["id"],
                            "user_id": r["user_id"],
                            "username": r["username"] or "",
                            "command": r["command"] or "",
                            "order_type": r["order_type"] or "order",
                            "status": r["status"] or "queued",
                            "queue_position": r["queue_position"],
                            "estimated_minutes": r["estimated_minutes"],
                            "dodo_code": r["dodo_code"],
                            "island_name": r["island_name"] or "Sinta",
                            "message": r["message"] or "",
                            "created_at": r["created_at"],
                            "updated_at": r["updated_at"],
                        })
            except Exception as exc:
                logger.warning(f"[SysBotAPI] Failed to fetch user order history: {exc}")
            return orders

        return await asyncio.to_thread(_query)
