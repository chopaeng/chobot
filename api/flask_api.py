"""
Flask API Module
Combines all API endpoints:
- Item/Villager Search
- Dodo Code/Island Status
- Patreon Posts
"""

import asyncio
import os
import re
import time
import json
import hashlib
import secrets as _secrets
import logging
import threading
import urllib.parse
import urllib.error
import urllib.request
import io
from datetime import datetime, timedelta
from types import SimpleNamespace

import requests
from flask import Flask, jsonify, request, session, redirect, url_for, send_file
from flask_cors import CORS
from thefuzz import process, fuzz

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import ThreadedWSGIServer

from utils.config import Config
from utils import island_access
from utils.database import connect_db
from utils.discord_http import request as discord_request
from utils.helpers import format_locations_text, parse_locations_json, normalize_text, clean_text
from utils.nickname_format import nickname_warning_for, is_valid_acnh_nickname
from utils.auth_tokens import get_auth_user, make_auth_token, revoke_auth_token, update_auth_user
from utils.discord_membership import (
    DiscordMembershipUnavailable,
    DiscordNotGuildMember,
    is_beyond_stale_grace,
    refresh_user_payload,
    should_refresh,
)
from utils.nookipedia import NookipediaClient
from utils.ops_status import build_health_payload, get_maintenance_settings, record_service_status, set_active_data_manager
from api.dashboard import (
    dashboard,
    init_dashboard_db,
    get_db,
    row_to_island_dict,
    _check_session as _check_dashboard_session,
    _parse_visitor_value,
    _parse_visitor_list,
)


logger = logging.getLogger("FlaskAPI")

CHOBOT_SQLITE_DB = "chobot.db"


def _client_ip() -> str:
    """Return the most useful client IP for audit logging."""
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return forwarded or request.headers.get("X-Real-IP", "").strip() or request.remote_addr or ""


def _record_website_login(event: dict) -> None:
    """Persist a successful website Discord OAuth login for dashboard audit history."""
    db = get_db()
    try:
        db.execute(
            """INSERT INTO website_login_events
                   (user_id, username, discord_name, global_name, account_name, nickname,
                    avatar, roles, role_count, is_admin, is_mod, ip_address, user_agent,
                    return_to, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.get("user_id") or "",
                event.get("username") or "",
                event.get("discord_name") or "",
                event.get("global_name") or "",
                event.get("account_name") or "",
                event.get("nickname") or "",
                event.get("avatar") or "",
                json.dumps(event.get("roles") or []),
                int(event.get("role_count") or 0),
                int(bool(event.get("is_admin"))),
                int(bool(event.get("is_mod"))),
                event.get("ip_address") or "",
                event.get("user_agent") or "",
                event.get("return_to") or "",
                event.get("created_at") or datetime.utcnow().isoformat(),
            ),
        )
        db.commit()
    except Exception as exc:
        logger.warning("Website login DB log failed: %s", exc)
        return
    finally:
        db.close()


def _persist_dodo_reveal_message(
    user_id: str,
    island_name: str,
    channel_id: str | None,
    message_url: str,
    username: str,
    nickname: str,
) -> None:
    """Store webhook message URL so Flight Logger can link unverified flights to dodo reveals."""
    island_clean = clean_text(island_name)
    if not island_clean:
        island_clean = clean_text(island_name.lower())
    try:
        conn = connect_db()
        try:
            conn.execute(
                """
                INSERT INTO dodo_reveal_messages
                (user_id, island_clean, channel_id, message_url, username, nickname, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(user_id),
                    island_clean,
                    str(channel_id) if channel_id else None,
                    message_url,
                    username or "",
                    nickname or "",
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("dodo_reveal_messages insert failed: %s", exc)


def _log_dodo_reveal_attempt(user: dict | None, island: str, outcome: str, reason: str, **extra) -> None:
    """Log dodo reveal attempts with enough context for dashboard analytics/debugging."""
    logger.info(
        "dodo_reveal user_id=%s username=%s island=%s outcome=%s reason=%s extra=%s",
        user.get("user_id") if user else None,
        user.get("username") if user else None,
        island,
        outcome,
        reason,
        extra,
    )


def _record_api_audit_event(action: str, target: str | None = None, details: dict | None = None) -> None:
    """Best-effort audit log writer for public/API actions."""
    user = _current_auth_user()
    try:
        db = get_db()
        try:
            db.execute(
                """INSERT INTO dashboard_audit_events
                   (actor_user_id, actor_name, action, target, details, ip_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    user.get("user_id") if user else None,
                    user.get("username") if user else None,
                    action,
                    target,
                    json.dumps(details or {}, sort_keys=True),
                    _client_ip(),
                    int(time.time()),
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("API audit insert failed: %s", exc)


def _resolve_search_alias(kind: str, query: str) -> tuple[str, str | None]:
    alias = clean_text(query)
    if not alias:
        return query, None
    try:
        db = get_db()
        try:
            row = db.execute(
                "SELECT target FROM search_aliases WHERE alias = ? AND kind = ?",
                (alias, kind),
            ).fetchone()
        finally:
            db.close()
    except Exception:
        row = None
    return (row["target"], alias) if row else (query, None)


def _log_command_search(
    command: str,
    query: str,
    *,
    found: bool,
    result_count: int = 0,
    source: str = "api",
) -> None:
    try:
        db = get_db()
        try:
            user = _current_auth_user()
            db.execute(
                """INSERT INTO command_search_events
                   (command, query, normalized_query, source, user_id, channel_id, found, result_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    command,
                    query,
                    clean_text(query),
                    source,
                    user.get("user_id") if user else request.args.get("user_id", ""),
                    request.args.get("channel_id", ""),
                    1 if found else 0,
                    int(result_count or 0),
                    int(time.time()),
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Command search log failed: %s", exc)


# Initialize Flask app
app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY
app.permanent_session_lifetime = timedelta(days=max(int(Config.FLASK_SESSION_DAYS or 30), 1))
# Trust one level of X-Forwarded-For / X-Forwarded-Proto headers from the
# reverse proxy (nginx, Cloudflare Tunnel, etc.) so that url_for(_external=True)
# produces the correct https:// URL instead of http://.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
CORS(app, resources={r"/*": {"origins": Config.FRONTEND_ORIGINS}}, supports_credentials=True)

# Register the mod-only web dashboard
app.register_blueprint(dashboard, url_prefix="/dashboard")
init_dashboard_db()
record_service_status("flask", mode="api", status="running")

# Suppress Flask/Werkzeug standard logs
logging.getLogger('werkzeug').setLevel(logging.ERROR)


# Patreon cache
patreon_cache = {
    "list": {"data": None, "timestamp": None},
    "posts": {}
}

# Data manager will be set from main.py
data_manager = None
_fallback_item_cache = None
_fallback_item_cache_mtime = None
_fallback_item_cache_lock = threading.Lock()
_fallback_villager_cache = {}
_fallback_villager_cache_time = None
_fallback_villager_cache_lock = threading.Lock()
_FALLBACK_CACHE_FILE = "cache_dump.json"
_FALLBACK_VILLAGER_CACHE_TTL = 300

# Guard: prevents multiple concurrent cache-refresh operations
_refresh_lock = threading.Lock()


def _request_search_query(*names: str) -> str:
    for name in names:
        value = request.args.get(name, "")
        if value and value.strip():
            return normalize_text(value)
    return ""


def _load_fallback_item_cache() -> tuple[dict, datetime | None]:
    global _fallback_item_cache, _fallback_item_cache_mtime
    cache_path = os.path.join(os.getcwd(), _FALLBACK_CACHE_FILE)
    if not os.path.exists(cache_path):
        return {}, None

    try:
        mtime = os.path.getmtime(cache_path)
        with _fallback_item_cache_lock:
            if _fallback_item_cache is not None and _fallback_item_cache_mtime == mtime:
                return dict(_fallback_item_cache), datetime.fromtimestamp(mtime)

            with open(cache_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                return {}, None
            _fallback_item_cache = loaded
            _fallback_item_cache_mtime = mtime
            return dict(loaded), datetime.fromtimestamp(mtime)
    except Exception as exc:
        logger.warning("Failed to load fallback item cache: %s", exc)
        return {}, None


def _get_item_cache() -> tuple[dict, datetime | None, float | None, str]:
    if data_manager is not None:
        with data_manager.lock:
            return (
                dict(data_manager.cache),
                data_manager.last_update,
                float(data_manager.cache_refresh_hours or 0) * 3600,
                "data_manager",
            )

    cache, last_update = _load_fallback_item_cache()
    return cache, last_update, None, "disk_cache"


def _scan_villager_dirs(villager_dirs) -> dict:
    data = {}
    paths_to_scan = tuple(sorted(p for p in villager_dirs if p and os.path.exists(p)))
    if not paths_to_scan:
        return data

    for base_dir in paths_to_scan:
        for root, _dirs, files in os.walk(base_dir):
            if "Villagers.txt" not in files:
                continue

            location_name = os.path.basename(root)
            file_path = os.path.join(root, "Villagers.txt")
            try:
                with open(file_path, "rb") as fh:
                    raw_content = fh.read().decode("utf-8", errors="ignore")
            except Exception as exc:
                logger.warning("Could not read Villagers.txt at %s: %s", location_name, exc)
                continue

            raw_content = re.sub(r"Villagers\s+on\s+[^:]+:", "", raw_content, flags=re.IGNORECASE)
            for name in re.split(r"[,\n\r]+", raw_content):
                clean_name = name.strip()
                if not clean_name or len(clean_name) > 30:
                    continue
                if clean_name in ["Ren?E", "Ren?e"]:
                    clean_name = "Renee"
                key = normalize_text(clean_name)
                if key in data:
                    current_locs = data[key].split(", ")
                    if location_name not in current_locs:
                        data[key] += f", {location_name}"
                else:
                    data[key] = location_name
    return data


def _get_villager_map(villager_dirs) -> tuple[dict, str]:
    global _fallback_villager_cache, _fallback_villager_cache_time
    if data_manager is not None:
        return data_manager.get_villagers(villager_dirs), "data_manager"

    now = time.time()
    cache_key = tuple(sorted(p for p in villager_dirs if p))
    with _fallback_villager_cache_lock:
        cached = _fallback_villager_cache.get(cache_key)
        if cached is not None and _fallback_villager_cache_time and now - _fallback_villager_cache_time < _FALLBACK_VILLAGER_CACHE_TTL:
            return cached, "disk_scan"
        scanned = _scan_villager_dirs(villager_dirs)
        _fallback_villager_cache[cache_key] = scanned
        _fallback_villager_cache_time = now
        return scanned, "disk_scan"

# ---------------------------------------------------------------------------
# Auth â€” short-lived opaque tokens for Discord OAuth (website subscribers)
# Works cross-domain: frontend stores the token in localStorage and sends it
# as "Authorization: Bearer <token>" on every authenticated request.
# ---------------------------------------------------------------------------
_DISCORD_UA = "DiscordBot (https://chopaeng.com, 1.0)"
_ADMINISTRATOR_PERM = 0x8   # Discord Administrator permission bit
_VIEW_CHANNEL_PERM = 1 << 10
_ROLE_NAME_CACHE: dict[str, tuple[dict[str, str], float]] = {}
_ROLE_NAME_CACHE_TTL = 3600
_CHANNEL_OVERWRITE_CACHE: dict[str, tuple[list[str] | None, str | None, float]] = {}
_GUILD_CHANNELS_CACHE: tuple[list[dict], float] | None = None
_CHANNEL_OVERWRITE_CACHE_TTL = 300
_GUILD_CHANNELS_CACHE_TTL = 300

def _current_auth_user(*, force_refresh: bool = False) -> dict | None:
    """Extract Bearer token or API key from request and return user dict, or None."""
    auth = request.headers.get("Authorization", "")
    api_key_hdr = request.headers.get("X-API-Key", "")
    token = ""
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
    elif api_key_hdr:
        token = api_key_hdr.strip()

    if token:
        dash_secret = getattr(Config, "DASHBOARD_SECRET", "") or ""
        sys_key = getattr(Config, "SYSBOT_API_KEY", "") or ""
        if (dash_secret and _secrets.compare_digest(token, dash_secret)) or (sys_key and _secrets.compare_digest(token, sys_key)):
            uid = request.headers.get("X-User-ID") or "bot_system"
            uname = request.headers.get("X-Username") or "ChorderBot"
            return {
                "user_id": uid,
                "id": uid,
                "username": uname,
                "nickname": uname,
                "roles": ["admin"],
                "is_admin": True,
            }

    if not auth.startswith("Bearer "):
        return None

    token = auth[len("Bearer "):]
    user = get_auth_user(token)
    if not user:
        return None
    if not force_refresh and not should_refresh(user):
        return user
    try:
        refreshed = refresh_user_payload(user)
        update_auth_user(token, refreshed)
        return refreshed
    except DiscordNotGuildMember:
        logger.info("Revoking auth token for user_id=%s: no longer in guild", user.get("user_id"))
        revoke_auth_token(token)
        return None
    except DiscordMembershipUnavailable as exc:
        logger.warning("Could not refresh Discord auth user %s: %s", user.get("user_id"), exc)
        if is_beyond_stale_grace(user):
            revoke_auth_token(token)
            return None
        return user


# Keep legacy helper names stable while sharing the access engine with dashboard APIs.
_is_mod = island_access.is_mod
_has_island_access = island_access.has_island_access
_configured_subscription_role_ids = island_access.configured_subscription_role_ids
_discord_bot_auth_value = island_access.discord_bot_auth_value
_discord_api_json = island_access.discord_api_json
_discord_channel_overwrite_roles = island_access.discord_channel_overwrite_roles
_discord_guild_channels = island_access.discord_guild_channels
_find_discord_island_channel_id = island_access.find_discord_island_channel_id
_is_member_island = island_access.is_member_island
_effective_island_required_roles = island_access.effective_island_required_roles
_excluded_profile_role_ids = island_access.excluded_profile_role_ids
_get_guild_role_names = island_access.get_guild_role_names
_role_payload = island_access.role_payload


def _resolved_island_required_roles(
    island_name: str | None,
    cat: str | None,
    required_roles: list[str] | None,
    island_type: str | None = None,
    channel_id: str | None = None,
) -> tuple[list[str], str | None, str]:
    info = island_access.resolved_island_required_roles(
        island_name,
        cat,
        required_roles,
        island_type,
        channel_id,
    )
    return info.required_roles, info.channel_id, info.access_source


def _iso_to_unix(value: str | None) -> int | None:
    """Convert a Discord ISO timestamp to Unix seconds when possible."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return None


def _user_id_param(user_id: str) -> int | str:
    """Use integer IDs for SQLite INTEGER comparisons, falling back to text."""
    return int(user_id) if str(user_id).isdigit() else str(user_id)


def _load_profile_subscriptions(user: dict) -> dict:
    """Return subscription/access info inferred from Discord roles and local DB."""
    user_role_ids = {str(r) for r in user.get("roles", [])}
    accessible_islands: list[dict] = []
    matched_role_ids: set[str] = set()
    configured_role_ids = set(_configured_subscription_role_ids())
    excluded_role_ids = _excluded_profile_role_ids()
    role_names = _get_guild_role_names()
    alert_subscriptions: list[dict] = []

    db = get_db()
    try:
        all_required_role_ids: set[str] = set()
        rows = db.execute(
            "SELECT id, name, display_name, is_visible, cat, type, required_roles, channel_id FROM islands ORDER BY name"
        ).fetchall()
        for row in rows:
            island = row_to_island_dict(dict(row))
            if island.get("is_visible") is False:
                continue
            raw_required_roles, resolved_channel_id, access_source = _resolved_island_required_roles(
                island.get("name"),
                island.get("cat"),
                island.get("required_roles", []),
                island.get("type"),
                island.get("channel_id"),
            )
            profile_required_roles = [
                str(r)
                for r in raw_required_roles
                if str(r) and str(r) not in excluded_role_ids
            ]
            is_member_island = _is_member_island(island.get("cat"), island.get("type"))

            # The raw channel overwrite roles decide access. The filtered profile roles
            # are only for display so general access/mod roles do not show as subscriptions.
            raw_matching_roles = sorted(user_role_ids & set(raw_required_roles))
            display_matching_roles = sorted(set(raw_matching_roles) - excluded_role_ids)
            all_required_role_ids.update(profile_required_roles)

            has_channel_access = bool(raw_matching_roles) or bool(user.get("is_mod")) or bool(user.get("is_admin"))
            looks_like_sub_island = is_member_island or bool(raw_required_roles)
            if has_channel_access and looks_like_sub_island:
                matched_role_ids.update(display_matching_roles)
                accessible_islands.append({
                    "id": island.get("id"),
                    "name": island.get("display_name") or island.get("name"),
                    "canonical_name": island.get("name"),
                    "type": island.get("type"),
                    "channel_id": resolved_channel_id,
                    "access_source": access_source,
                    "required_roles": [_role_payload(rid, role_names) for rid in profile_required_roles],
                    "matched_roles": [_role_payload(rid, role_names) for rid in display_matching_roles],
                })

        try:
            sub_rows = db.execute(
                "SELECT island_clean, kind, has_island_access "
                "FROM island_subscriptions WHERE user_id = ? ORDER BY island_clean, kind",
                (_user_id_param(user.get("user_id", "")),),
            ).fetchall()
            alert_subscriptions = [
                {
                    "island": row["island_clean"],
                    "kind": row["kind"],
                    "has_island_access": bool(row["has_island_access"]),
                }
                for row in sub_rows
            ]
        except Exception:
            # Older DBs may not have alert subscriptions yet.
            alert_subscriptions = []
    finally:
        db.close()

    subscription_role_ids = sorted((user_role_ids & all_required_role_ids) - excluded_role_ids)
    matched_subscription_role_ids = sorted(matched_role_ids - excluded_role_ids)
    subscription_roles = [_role_payload(rid, role_names) for rid in subscription_role_ids]
    configured_subscription_roles = [
        _role_payload(rid, role_names)
        for rid in sorted(configured_role_ids - excluded_role_ids)
    ]
    matched_subscription_roles = [
        _role_payload(rid, role_names)
        for rid in matched_subscription_role_ids
    ]

    return {
        "role_ids": subscription_role_ids,
        "role_names": [role["name"] for role in subscription_roles],
        "roles": subscription_roles,
        "configured_subscription_role_ids": sorted(configured_role_ids - excluded_role_ids),
        "configured_subscription_role_names": [role["name"] for role in configured_subscription_roles],
        "configured_subscription_roles": configured_subscription_roles,
        "matched_subscription_role_ids": matched_subscription_role_ids,
        "matched_subscription_role_names": [role["name"] for role in matched_subscription_roles],
        "matched_subscription_roles": matched_subscription_roles,
        "accessible_islands": accessible_islands,
        "accessible_member_islands": accessible_islands,
        "alert_subscriptions": alert_subscriptions,
        "island_alert_subscriptions": alert_subscriptions,
    }


def _load_profile_visit_stats(user_id: str) -> dict:
    """Return visit totals, top destinations, recent visits, and warning summary."""
    uid = _user_id_param(user_id)
    guild_id = Config.GUILD_ID
    empty = {
        "total": 0,
        "authorized": 0,
        "unauthorized": 0,
        "first_visit_at": None,
        "last_visit_at": None,
        "by_type": {},
        "most_visited_islands": [],
        "recent_visits": [],
        "warnings": {"total": 0, "last_warning_at": None},
    }

    db = get_db()
    try:
        row = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN authorized = 1 THEN 1 ELSE 0 END) AS authorized, "
            "SUM(CASE WHEN authorized = 0 THEN 1 ELSE 0 END) AS unauthorized, "
            "MIN(timestamp) AS first_visit_at, MAX(timestamp) AS last_visit_at "
            "FROM island_visits WHERE user_id = ? AND guild_id = ?",
            (uid, guild_id),
        ).fetchone()
        if row:
            empty.update({
                "total": int(row["total"] or 0),
                "authorized": int(row["authorized"] or 0),
                "unauthorized": int(row["unauthorized"] or 0),
                "first_visit_at": row["first_visit_at"],
                "last_visit_at": row["last_visit_at"],
            })

        type_rows = db.execute(
            "SELECT island_type, COUNT(*) AS visit_count "
            "FROM island_visits WHERE user_id = ? AND guild_id = ? "
            "GROUP BY island_type ORDER BY visit_count DESC",
            (uid, guild_id),
        ).fetchall()
        empty["by_type"] = {
            (row["island_type"] or "unknown"): int(row["visit_count"] or 0)
            for row in type_rows
        }

        top_rows = db.execute(
            "SELECT destination, island_type, COUNT(*) AS visit_count, MAX(timestamp) AS last_visit_at "
            "FROM island_visits WHERE user_id = ? AND guild_id = ? "
            "GROUP BY destination, island_type "
            "ORDER BY visit_count DESC, last_visit_at DESC LIMIT 10",
            (uid, guild_id),
        ).fetchall()
        empty["most_visited_islands"] = [
            {
                "name": row["destination"],
                "type": row["island_type"],
                "visits": int(row["visit_count"] or 0),
                "last_visit_at": row["last_visit_at"],
            }
            for row in top_rows
        ]

        recent_rows = db.execute(
            "SELECT id, ign, origin_island, destination, authorized, timestamp, island_type "
            "FROM island_visits WHERE user_id = ? AND guild_id = ? "
            "ORDER BY timestamp DESC LIMIT 10",
            (uid, guild_id),
        ).fetchall()
        empty["recent_visits"] = [
            {
                "id": row["id"],
                "ign": row["ign"],
                "origin_island": row["origin_island"],
                "destination": row["destination"],
                "authorized": bool(row["authorized"]),
                "timestamp": row["timestamp"],
                "island_type": row["island_type"],
            }
            for row in recent_rows
        ]

        try:
            warn_row = db.execute(
                "SELECT COUNT(*) AS total, MAX(timestamp) AS last_warning_at "
                "FROM warnings WHERE user_id = ? AND guild_id = ?",
                (uid, guild_id),
            ).fetchone()
            if warn_row:
                empty["warnings"] = {
                    "total": int(warn_row["total"] or 0),
                    "last_warning_at": warn_row["last_warning_at"],
                }
        except Exception:
            pass
    except Exception:
        logger.exception("Failed to load profile visit stats for user_id=%s", user_id)
    finally:
        db.close()

    return empty


_DODO_WEBHOOK_DEBOUNCE = {}
_DODO_WEBHOOK_DEBOUNCE_TTL = 300  # 5 minutes

def _fire_dodo_webhook(
    username: str,
    nickname: str,
    user_id: str,
    avatar_url: str,
    island_name: str,
    dodo_code: str,
    channel_id: str = None,
) -> None:
    """POST a Discord webhook message in the background."""
    url = Config.DODO_LOG_WEBHOOK_URL
    if not url:
        return

    now = time.monotonic()
    cache_key = f"{user_id}:{island_name}"
    last_fired = _DODO_WEBHOOK_DEBOUNCE.get(cache_key)
    if last_fired and now - last_fired < _DODO_WEBHOOK_DEBOUNCE_TTL:
        return
    _DODO_WEBHOOK_DEBOUNCE[cache_key] = now

    display_name = (nickname or "").strip() or (username or "").strip() or "Unknown User"

    island_url_name = urllib.parse.quote(island_name)
    island_link = f"https://www.chopaeng.com/island/{island_url_name.lower()}"

    embed = {
        "title": f"Dodo Code Revealed",
        "color": 0x2ecc71,  # Emerald Green
        "description": f"<@{user_id}> has revealed the Dodo code for island <#{channel_id}>",
        "fields": [
            {
                "name": "Member",
                "value": f"{display_name} (<@{user_id}>)",
            },
            {
                "name": "Island",
                "value": (
                    (f"<#{channel_id}>" if channel_id else "") +
                    f"\n[View Island]({island_link})"
                ),
            }
        ],
        "image": {
            "url": "https://i.ibb.co/wybN7Xn/lg4jVMT.gif"
        },
        "footer": {
            "text": "Chopaeng Campâ„¢ â€¢ Dodo Log",
            "icon_url": "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    warning = nickname_warning_for(nickname)
    if warning:
        embed["fields"].append(
            {
                "name": "Nickname Status",
                "value": "User nickname is not currently in the required ACNH format.",
            }
        )

    payload = json.dumps({"embeds": [embed]}).encode()
    webhook_execute = url
    sep = "&" if "?" in webhook_execute else "?"
    webhook_execute = f"{webhook_execute}{sep}wait=true"
    try:
        resp = discord_request(
            webhook_execute,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": _DISCORD_UA},
            method="POST",
            timeout=10,
        )
        body = resp.body
        if resp.status not in (200, 204):
            logger.warning("Dodo webhook unexpected HTTP status: %s", resp.status)
        else:
            logger.debug("Dodo webhook delivered for island=%s user=%s", island_name, username)
        message_url = None
        if resp.status == 200 and body and Config.GUILD_ID:
            try:
                data = json.loads(body)
                mid = data.get("id")
                cid = data.get("channel_id")
                if mid and cid:
                    message_url = f"https://discord.com/channels/{Config.GUILD_ID}/{cid}/{mid}"
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug("Dodo webhook response not JSON: %s", exc)
        if message_url:
            _persist_dodo_reveal_message(
                user_id=str(user_id),
                island_name=island_name,
                channel_id=channel_id,
                message_url=message_url,
                username=username or "",
                nickname=nickname or "",
            )
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            pass
        logger.warning("Dodo webhook failed HTTP %s: %s", exc.code, body)
    except Exception as exc:
        logger.warning("Dodo webhook failed: %s", exc)


def set_data_manager(dm):
    """Set the data manager instance"""
    global data_manager
    data_manager = dm
    set_active_data_manager(dm)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def extract_image_from_html(html_content):
    """Extract image URL from HTML content"""
    if not html_content:
        return None
    match = re.search(r'<img [^>]*src="([^"]+)"', html_content)
    return match.group(1) if match else None


def process_post_attributes(post_id, attrs):
    """Process Patreon post attributes"""
    image_url = None

    if attrs.get("embed_data"):
        embed = attrs["embed_data"]
        if "image" in embed and "url" in embed["image"]:
            image_url = embed["image"]["url"]
        elif "thumbnail_url" in embed:
            image_url = embed["thumbnail_url"]

    if not image_url:
        image_url = extract_image_from_html(attrs.get("content"))

    return {
        "id": post_id,
        "attributes": {
            "embed_data": attrs.get("embed_data"),
            "title": attrs["title"],
            "content": attrs["content"],
            "published_at": attrs["published_at"],
            "url": attrs["url"],
            "is_public": attrs["is_public"],
            "image": {"large_url": image_url}
        },
        "type": "post"
    }


_file_cache: dict = {}
_file_cache_lock = threading.Lock()
_FILE_CACHE_TTL = 3  # seconds


def get_file_content(folder_path, filename):
    """Read file content safely with caching and retry to reduce file-lock contention.

    The C# SysBot writes to these files with exclusive access (FileShare.None).
    Caching minimises how often the file is opened, and the retry handles the
    brief window where C# holds an exclusive write lock.
    """
    path = os.path.join(folder_path, filename)

    now = time.monotonic()
    with _file_cache_lock:
        cached = _file_cache.get(path)
        if cached is not None:
            content, ts = cached
            if now - ts < _FILE_CACHE_TTL:
                return content

    if not os.path.exists(path):
        return None

    for attempt in range(3):
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                content = f.read().strip()
            with _file_cache_lock:
                _file_cache[path] = (content, time.monotonic())
            return content
        except OSError:
            if attempt < 2:
                time.sleep(0.05)
        except Exception:
            break

    # Return stale cache rather than None if the file is still locked
    with _file_cache_lock:
        cached = _file_cache.get(path)
    if cached is not None:
        return cached[0]
    return None


def process_island(entry, island_type):
    """Process island data for Dodo API"""
    name = entry.name.upper()

    raw_dodo = get_file_content(entry.path, "Dodo.txt")
    raw_visitors = _parse_visitor_value(get_file_content(entry.path, "Visitors.txt"))

    status = "ONLINE"
    display_dodo = raw_dodo
    display_visitors = "0/7"

    # Visitor Logic
    if raw_visitors:
        if raw_visitors.upper() == "FULL":
            display_visitors = "FULL"
        elif raw_visitors.isdigit():
            display_visitors = f"{raw_visitors}/7"
        else:
            display_visitors = raw_visitors

    # Dodo/Status Logic
    if island_type == "VIP":
        status = "SUB ONLY"
        display_dodo = "SUB ONLY"
    else:
        if raw_dodo is None:
            status = "OFFLINE"
            display_dodo = "....."
            display_visitors = "0/7"
        elif raw_dodo in ["00000", "-----", ""]:
            status = "REFRESHING"
            display_dodo = "WAIT..."
            display_visitors = "0/7"
        else:
            display_dodo = raw_dodo

    return {
        "name": name,
        "dodo": display_dodo,
        "status": status,
        "type": island_type,
        "visitors": display_visitors
    }


def _build_island_response(
    entry,
    island_type,
    db_island,
    discord_bot_online=None,
    viewer_roles=None,
    viewer_is_mod=False,
):
    """Build the enriched island response merging live filesystem data with DB metadata."""
    name = entry.name.upper()
    viewer_roles = [str(role_id) for role_id in (viewer_roles or []) if str(role_id)]
    default_cat = "member" if island_type == "VIP" else "order" if island_type == "Order" else "public"
    island_cat = db_island.get("cat") or default_cat
    required_roles, resolved_channel_id, access_source = _resolved_island_required_roles(
        name,
        island_cat,
        db_island.get("required_roles", []),
        island_type,
        db_island.get("channel_id"),
    )
    is_member_locked = _is_member_island(island_cat, island_type) and not required_roles and not viewer_is_mod
    viewer_has_access = False if is_member_locked else _has_island_access(
        viewer_roles,
        required_roles,
        viewer_is_mod,
    )

    raw_dodo = get_file_content(entry.path, "Dodo.txt")
    visitors, visitor_list = _parse_visitor_list(get_file_content(entry.path, "Visitors.txt"))

    # Determine live status and dodo_code from filesystem
    if _is_member_island(island_cat, island_type) and not viewer_has_access:
        status = "SUB ONLY"
        dodo_code = None  # Do not expose dodo code for subscriber-only islands
    elif raw_dodo is None:
        status = "OFFLINE"
        dodo_code = None
    elif (raw_dodo or "").strip().upper() in ["00000", "-----", "", "GETTIN'"]:
        status = "REFRESHING"
        dodo_code = "GETTIN'" if (raw_dodo or "").strip().upper() == "GETTIN'" else None
    else:
        status = "ONLINE"
        dodo_code = raw_dodo

    # Keep member/order codes behind their controlled channels/endpoints.
    if _is_member_island(island_cat, island_type) or island_type == "Order" or island_cat == "order":
        dodo_code = None

    # When the Discord bot is not confirmed online, hide live data to avoid stale values (unless refreshing)
    if not discord_bot_online and status != "REFRESHING":
        visitors = 0
        visitor_list = []
        dodo_code = None

    return {
        "id":                db_island.get("id", name.lower()),
        "name":              (db_island.get("display_name") or name),
        "canonical_name":    name,
        "cat":               island_cat,
        "description":       db_island.get("description", ""),
        "dodo_code":         dodo_code,
        "visitors":          visitors,
        "visitor_list":      visitor_list,
        "items":             db_island.get("items", []),
        "map_url":           db_island.get("map_url"),
        "seasonal":          db_island.get("seasonal", ""),
        "status":            status,
        "theme":             db_island.get("theme", "teal"),
        "type":              db_island.get("type") or island_type,
        "updated_at":        db_island.get("updated_at"),
        "discord_bot_online": discord_bot_online,
        "channel_id":        resolved_channel_id,
        "required_roles":    required_roles,
        "access_source":     access_source,
        "accessible":        viewer_has_access,
        "viewer_has_access": viewer_has_access,
    }

# ============================================================================
# ISLAND METADATA CRUD (separate from /api/islands Dodo-status endpoint)
# ============================================================================

ALLOWED_CATEGORIES = {"public", "member", "order"}
ALLOWED_THEMES = {"pink", "teal", "purple", "gold"}
ALLOWED_STATUSES = {"ONLINE", "SUB ONLY", "REFRESHING", "OFFLINE"}

# ============================================================================
# AUTH ROUTES  (Discord OAuth for public website subscribers)
# ============================================================================

@app.route("/api/auth/discord")
def auth_discord():
    """Initiate Discord OAuth flow for public website subscribers."""
    if not Config.DISCORD_CLIENT_ID:
        return jsonify({"error": "Discord OAuth not configured"}), 503
    if not Config.GUILD_ID:
        return jsonify({"error": "GUILD_ID not set"}), 503

    return_to = request.args.get("return_to", "")
    # Whitelist: only allow redirect back to chopaeng.com or localhost
    allowed_hosts = {"www.chopaeng.com", "chopaeng.com", "localhost"}
    try:
        parsed = urllib.parse.urlparse(return_to)
        if parsed.hostname not in allowed_hosts:
            return_to = "https://www.chopaeng.com/auth/callback"
    except Exception:
        return_to = "https://www.chopaeng.com/auth/callback"

    state = _secrets.token_hex(16)
    session["sub_oauth_state"] = state
    session["sub_return_to"] = return_to
    callback_url = url_for("auth_callback", _external=True)
    params = urllib.parse.urlencode({
        "client_id":     Config.DISCORD_CLIENT_ID,
        "redirect_uri":  callback_url,
        "response_type": "code",
        "scope":         "identify guilds.members.read",
        "state":         state,
    })
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")


@app.route("/api/auth/callback")
def auth_callback():
    """Handle Discord OAuth callback for public website subscribers."""
    error = request.args.get("error")
    if error:
        return_to = session.pop("sub_return_to", "https://www.chopaeng.com/auth/callback")
        return redirect(f"{return_to}?error={urllib.parse.quote(error)}")

    state = request.args.get("state", "")
    if state != session.pop("sub_oauth_state", ""):
        return_to = session.pop("sub_return_to", "https://www.chopaeng.com/auth/callback")
        return redirect(f"{return_to}?error=invalid_state")

    code = request.args.get("code", "")
    return_to = session.pop("sub_return_to", "https://www.chopaeng.com/auth/callback")

    callback_url = url_for("auth_callback", _external=True)
    try:
        token_body = urllib.parse.urlencode({
            "client_id":     Config.DISCORD_CLIENT_ID,
            "client_secret": Config.DISCORD_CLIENT_SECRET,
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  callback_url,
        }).encode()
        resp = discord_request(
            "https://discord.com/api/oauth2/token",
            data=token_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _DISCORD_UA},
            method="POST",
            timeout=10,
        )
        token_resp = json.loads(resp.body)
    except Exception:
        return redirect(f"{return_to}?error=token_exchange_failed")

    access_token = token_resp.get("access_token")
    if not access_token:
        return redirect(f"{return_to}?error=no_access_token")

    # Fetch guild member record (roles + permissions)
    member_roles: list[str] = []
    member_nickname = ""
    member_joined_at = ""
    member_perms = 0
    try:
        resp = discord_request(
            f"https://discord.com/api/users/@me/guilds/{Config.GUILD_ID}/member",
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": _DISCORD_UA},
            timeout=10,
        )
        member_data = json.loads(resp.body)
        member_roles = [str(r) for r in member_data.get("roles", [])]
        member_nickname = (member_data.get("nick") or "").strip()
        member_joined_at = str(member_data.get("joined_at") or "")
        try:
            member_perms = int(member_data.get("permissions", "0") or 0)
        except (ValueError, TypeError):
            member_perms = 0
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return redirect(f"{return_to}?error=not_a_member")
        return redirect(f"{return_to}?error=roles_fetch_failed")
    except Exception:
        return redirect(f"{return_to}?error=roles_fetch_failed")

    # Fetch basic user info
    discord_user_id = discord_username = discord_avatar_url = ""
    discord_global_name = discord_account_name = ""
    try:
        resp = discord_request(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": _DISCORD_UA},
            timeout=10,
        )
        user_data = json.loads(resp.body)
        discord_user_id  = str(user_data.get("id", ""))
        discord_global_name = str(user_data.get("global_name") or "")
        discord_account_name = str(user_data.get("username") or "")
        discord_username = (
            member_nickname
            or discord_global_name
            or discord_account_name
        )
        avatar_hash = user_data.get("avatar") or ""
        if discord_user_id and avatar_hash and re.fullmatch(r"(?:a_)?[0-9a-f]{32}", avatar_hash):
            discord_avatar_url = (
                f"https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.png?size=64"
            )
    except Exception:
        pass

    is_admin = bool(member_perms & _ADMINISTRATOR_PERM)
    token = make_auth_token({
        "user_id":   discord_user_id,
        "username":  discord_username,
        "discord_name": discord_global_name or discord_account_name,
        "global_name": discord_global_name,
        "account_name": discord_account_name,
        "nickname":  member_nickname,
        "joined_at": member_joined_at,
        "joined_timestamp": _iso_to_unix(member_joined_at),
        "avatar":    discord_avatar_url,
        "roles":     member_roles,
        "is_admin":  is_admin,
        "is_mod":    _is_mod(member_roles) or is_admin,
        "discord_checked_at": int(time.time()),
    })

    is_mod_user = _is_mod(member_roles) or is_admin
    login_event = {
        "user_id": discord_user_id,
        "username": discord_username,
        "discord_name": discord_global_name or discord_account_name,
        "global_name": discord_global_name,
        "account_name": discord_account_name,
        "nickname": member_nickname,
        "avatar": discord_avatar_url,
        "roles": member_roles,
        "role_count": len(member_roles),
        "is_admin": is_admin,
        "is_mod": is_mod_user,
        "ip_address": _client_ip(),
        "user_agent": request.headers.get("User-Agent", ""),
        "return_to": return_to,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    _record_website_login(login_event)

    logger.info("Website OAuth login: user=%s is_mod=%s", discord_username, is_mod_user)
    return redirect(f"{return_to}?token={urllib.parse.quote(token)}")


@app.route("/api/auth/me")
def auth_me():
    """Return the current authenticated user's info."""
    user = _current_auth_user(force_refresh=True)
    if not user:
        return jsonify({"logged_in": False}), 200
    return jsonify({
        "logged_in":  True,
        "user_id":    user["user_id"],
        "username":   user["username"],
        "discord_name": user.get("discord_name", user["username"]),
        "nickname":   user.get("nickname", ""),
        "joined_at":  user.get("joined_at", ""),
        "avatar":     user["avatar"],
        "roles":      user["roles"],
        "is_admin":   user.get("is_admin", False),
        "is_mod":     user["is_mod"],
    })


@app.route("/api/profile")
def api_profile():
    """Return the authenticated user's Discord profile and ChoPaeng activity."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    subscriptions = _load_profile_subscriptions(user)
    visits = _load_profile_visit_stats(user.get("user_id", ""))

    conn = get_db()
    favorite_islands = []
    try:
        fav_rows = conn.execute(
            "SELECT island_id FROM user_favorite_islands WHERE user_id = ? ORDER BY created_at DESC",
            (user.get("user_id", ""),)
        ).fetchall()
        favorite_islands = [r["island_id"] for r in fav_rows if r["island_id"]]
    except Exception:
        favorite_islands = []
    finally:
        conn.close()

    return jsonify({
        "user": {
            "id": user.get("user_id", ""),
            "discord_name": user.get("discord_name") or user.get("username", ""),
            "global_name": user.get("global_name", ""),
            "account_name": user.get("account_name", ""),
            "display_name": user.get("nickname") or user.get("discord_name") or user.get("username", ""),
            "nickname": user.get("nickname", ""),
            "avatar": user.get("avatar", ""),
            "joined_at": user.get("joined_at", ""),
            "joined_timestamp": user.get("joined_timestamp"),
            "is_admin": bool(user.get("is_admin")),
            "is_mod": bool(user.get("is_mod")),
        },
        "subscriptions": subscriptions,
        "visits": visits,
        "favorite_islands": favorite_islands,
    })


@app.route("/api/subscriptions", methods=["GET", "POST", "DELETE"])
def api_subscriptions():
    """Manage authenticated user island/item/villager/slot notifications."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    user_id = str(user.get("user_id") or "")
    db = get_db()
    try:
        if request.method == "GET":
            rows = db.execute(
                "SELECT island_clean, kind, has_island_access FROM island_subscriptions WHERE user_id = ? ORDER BY kind, island_clean",
                (user_id,),
            ).fetchall()
            return jsonify({"ok": True, "items": [dict(row) for row in rows]})

        data = request.get_json(silent=True) or {}
        target = clean_text(data.get("target") or data.get("island") or data.get("item") or data.get("villager") or "")
        kind = (data.get("kind") or "island_online").strip().lower()
        allowed = {"island_online", "island_slot", "item", "villager", "sub"}
        if kind not in allowed or not target:
            return jsonify({"ok": False, "error": f"kind must be one of {sorted(allowed)} and target is required"}), 400
        if request.method == "POST":
            db.execute(
                "INSERT OR IGNORE INTO island_subscriptions (user_id, island_clean, kind, has_island_access) VALUES (?, ?, ?, ?)",
                (user_id, target, kind, 1 if bool(user.get("is_mod") or user.get("is_admin")) else 0),
            )
        else:
            db.execute(
                "DELETE FROM island_subscriptions WHERE user_id = ? AND island_clean = ? AND kind = ?",
                (user_id, target, kind),
            )
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    _record_api_audit_event(
        "subscription_update",
        target,
        {"kind": kind, "method": request.method},
    )
    return jsonify({"ok": True, "target": target, "kind": kind, "method": request.method})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Invalidate the current auth token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
        revoke_auth_token(token)
    return jsonify({"logged_out": True})


# ============================================================================
# DODO REVEAL â€” authenticated, fires webhook
# ============================================================================

@app.route("/api/islands/<name>/dodo", methods=["POST"])
def reveal_dodo(name):
    """Return the dodo code for an island if the user has the required role.

    The client must send:   Authorization: Bearer <token>
    On success, fires a Discord webhook and returns the dodo code.
    """
    user = _current_auth_user(force_refresh=True)
    if not user:
        _log_dodo_reveal_attempt(None, name.upper(), "denied", "not_logged_in")
        return jsonify({"error": "Authentication required"}), 401

    maintenance = get_maintenance_settings()
    island_maintenance = (maintenance.get("islands") or {}).get(clean_text(name), {})
    if maintenance["maintenance_mode"] or maintenance["disable_dodo_reveals"] or island_maintenance.get("disable_dodo_reveals"):
        _log_dodo_reveal_attempt(user, name.upper(), "denied", "maintenance_mode")
        _record_api_audit_event("dodo_reveal_denied", name.upper(), {"reason": "maintenance_mode"})
        return jsonify({
            "error": island_maintenance.get("message") or maintenance["message"] or "Dodo reveals are temporarily unavailable.",
            "code": "maintenance_mode",
        }), 503

    target = name.upper()

    # Load island metadata (cat + required_roles)
    db = get_db()
    try:
        row = db.execute(
            "SELECT cat, type, required_roles, channel_id, is_visible FROM islands WHERE UPPER(name) = ?", (target,)
        ).fetchone()
    finally:
        db.close()

    island_cat = ""
    island_type = ""
    required_roles: list[str] = []
    channel_id = None
    if row:
        if row["is_visible"] is not None and not bool(row["is_visible"]):
            _log_dodo_reveal_attempt(user, target, "denied", "island_hidden")
            _record_api_audit_event("dodo_reveal_denied", target, {"reason": "island_hidden"})
            return jsonify({"error": "Island is not available"}), 404
        island_cat = (row["cat"] or "").strip().lower()
        island_type = row["type"] or ""
        channel_id = row["channel_id"]
        try:
            required_roles = json.loads(row["required_roles"] or "[]")
        except (ValueError, TypeError):
            required_roles = []
    elif Config.DIR_VIP:
        for candidate in [target, name]:
            if os.path.isdir(os.path.join(Config.DIR_VIP, candidate)):
                island_cat = "member"
                island_type = "VIP"
                break

    # Safety: member islands must never become public because required_roles is empty.
    effective_required_roles, resolved_channel_id, _access_source = _resolved_island_required_roles(
        target,
        island_cat,
        required_roles,
        island_type,
        channel_id,
    )
    channel_id = resolved_channel_id or channel_id

    is_viewer_admin = bool(user.get("is_admin"))
    is_viewer_mod = bool(user.get("is_mod")) or is_viewer_admin

    if _is_member_island(island_cat, island_type) and not effective_required_roles and not is_viewer_mod:
        _log_dodo_reveal_attempt(user, target, "denied", "no_member_roles_configured", channel_id=channel_id)
        _record_api_audit_event("dodo_reveal_denied", target, {"reason": "no_member_roles_configured", "channel_id": channel_id})
        return jsonify({"error": "Subscriber roles are not configured for this island"}), 403

    # Check for general island access role first
    island_access_role = str(Config.ISLAND_ACCESS_ROLE) if Config.ISLAND_ACCESS_ROLE else ""
    if island_access_role and not is_viewer_admin:
        if island_access_role not in set(user.get("roles", [])):
            _log_dodo_reveal_attempt(
                user,
                target,
                "denied",
                "missing_global_island_access_role",
                required_role=island_access_role,
                channel_id=channel_id,
            )
            _record_api_audit_event(
                "dodo_reveal_denied",
                target,
                {"reason": "missing_global_island_access_role", "channel_id": channel_id},
            )
            return jsonify({
                "error": "You need the Discord island access role to reveal this Dodo code. Please accept the rules in the sub-rules channel first: <a href=\"https://discord.com/channels/729590421478703135/783677194576330792\" target=\"_blank\">#sub-rules</a>.",
                "code": "missing_global_island_access_role",
            }), 403

    if not _has_island_access(user.get("roles", []), effective_required_roles, is_viewer_mod):
        _log_dodo_reveal_attempt(
            user,
            target,
            "denied",
            "missing_island_channel_role",
            required_roles=effective_required_roles,
            channel_id=channel_id,
        )
        _record_api_audit_event(
            "dodo_reveal_denied",
            target,
            {"reason": "missing_island_channel_role", "channel_id": channel_id},
        )
        return jsonify({
            "error": "You do not have the Discord role required for this island channel.",
            "code": "missing_island_channel_role",
        }), 403

    # Find the dodo code from the filesystem
    dodo_code = None
    for base_dir in [Config.DIR_FREE, Config.DIR_VIP]:
        if not base_dir or not os.path.exists(base_dir):
            continue
        for candidate in [target, name]:
            path = os.path.join(base_dir, candidate)
            if os.path.isdir(path):
                raw = get_file_content(path, "Dodo.txt")
                if raw and raw not in ["00000", "-----", "", "GETTIN'"]:
                    dodo_code = raw
                break
        if dodo_code:
            break

    if not dodo_code:
        _log_dodo_reveal_attempt(user, target, "failed", "dodo_unavailable", channel_id=channel_id)
        _record_api_audit_event("dodo_reveal_failed", target, {"reason": "dodo_unavailable", "channel_id": channel_id})
        return jsonify({"error": "Dodo code not available right now"}), 404

    # Fire webhook in background thread so the response isn't delayed
    threading.Thread(
        target=_fire_dodo_webhook,
        args=(
            user["username"],
            user.get("nickname", ""),
            user.get("user_id", ""),
            user["avatar"],
            target,
            dodo_code,
            channel_id,
        ),
        daemon=True,
    ).start()

    warning = nickname_warning_for(user.get("nickname"))
    response_payload = {"island": target, "dodo_code": dodo_code}
    if warning:
        response_payload["warning"] = warning

    _log_dodo_reveal_attempt(user, target, "allowed", "revealed", channel_id=channel_id)
    _record_api_audit_event("dodo_reveal_allowed", target, {"channel_id": channel_id})
    return jsonify(response_payload)


@app.route("/api/islands/<name>/queue", methods=["POST"])
def join_dodo_queue(name):
    """Join the Dodo reservation queue for an island."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    target = name.upper().strip()
    island_clean = clean_text(target)
    maintenance = get_maintenance_settings()
    island_maintenance = (maintenance.get("islands") or {}).get(island_clean, {})
    if maintenance["maintenance_mode"] or island_maintenance.get("queue_paused"):
        return jsonify({
            "ok": False,
            "error": island_maintenance.get("message") or maintenance["message"] or "This island queue is temporarily paused.",
            "code": "maintenance_mode",
        }), 503
    note = (request.get_json(silent=True) or {}).get("note", "")
    now = int(time.time())
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id, status FROM dodo_queue WHERE island_clean = ? AND user_id = ? AND status IN ('waiting', 'called', 'investigating')",
            (island_clean, str(user.get("user_id") or "")),
        ).fetchone()
        if existing:
            return jsonify({"ok": True, "id": existing["id"], "status": existing["status"], "already_queued": True})
        cur = db.execute(
            """INSERT INTO dodo_queue
               (island_clean, island_name, user_id, username, status, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'waiting', ?, ?, ?)""",
            (
                island_clean,
                target,
                str(user.get("user_id") or ""),
                user.get("username") or user.get("discord_name") or "",
                str(note or "")[:500],
                now,
                now,
            ),
        )
        db.commit()
        entry_id = cur.lastrowid
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    _record_api_audit_event("dodo_queue_join", target, {"entry_id": entry_id})
    return jsonify({"ok": True, "id": entry_id, "status": "waiting"})


@app.route("/api/dodo-queue/me", methods=["GET"])
def my_dodo_queue():
    """Return the authenticated user's active Dodo queue entries."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, island_name, status, note, created_at, updated_at FROM dodo_queue "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT 25",
            (str(user.get("user_id") or ""),),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        db.close()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/')
def home():
    """API home with endpoint info and system status"""
    cache, last_update, _refresh_interval, source = _get_item_cache()
    cache_count = len([key for key in cache if key != "_display"])

    return jsonify({
        "system": {
            "name": "ChoBot API",
            "version": "1.1.0",
            "status": "online" if cache_count > 0 else "initializing",
            "server_time": datetime.now().isoformat(),
        },
        "data_stats": {
            "items_in_cache": cache_count,
            "last_gsheets_sync": last_update.isoformat() if last_update else None,
            "source": source,
            "data_manager_initialised": data_manager is not None,
            "island_file_cache_ttl": f"{_FILE_CACHE_TTL}s"
        },
        "endpoints": {
            "islands": {
                "path": "/api/islands",
                "description": "Get real-time status, visitors, and dodo codes for all islands"
            },
            "search_items": {
                "path": "/api/find?q=<item>",
                "description": "Search for item availability across all islands"
            },
            "search_villagers": {
                "path": "/api/villager?q=<name>",
                "description": "Locate specific villagers on islands"
            },
            "villager_list": {
                "path": "/api/villagers/list",
                "description": "Get all current villagers grouped by island"
            },
            "patreon_posts": {
                "path": "/api/patreon/posts",
                "description": "List cached community posts"
            },
            "island_map": {
                "path": "/api/islands/<island_name>/map",
                "description": "Get parsed item and sector layout for an island"
            },
            "health": {
                "path": "/api/health",
                "description": "Detailed system health and synchronization metrics"
            }
        }
    })

@app.route('/health')
@app.route('/api/health')
def health():
    """Health check endpoint for monitoring"""
    payload = build_health_payload(
        data_manager=data_manager,
        fallback_loader=_get_item_cache,
        include_private=False,
    )
    payload["islands"] = {
        "file_cache_ttl_seconds": _FILE_CACHE_TTL,
    }
    status_code = 200 if payload["status"] in {"ok", "degraded"} else 503
    return jsonify(payload), status_code

# --- ITEM SEARCH ROUTES ---

@app.route('/find')
def find_item():
    """Text response for item search"""
    user = request.args.get('user', 'User')
    query = _request_search_query("q", "item", "name")

    if not query:
        return f"Hey {user}, type !find <item name> to search."

    cache, _last_update, _refresh_interval, _source = _get_item_cache()
    if not cache:
        return f"Hey {user}, the search service is not available right now. Please try again later."

    found_locs = cache.get(query)

    if found_locs:
        final_msg = format_locations_text(found_locs)
        return f"Hey {user}, I found {query.upper()} {final_msg}"

    matches = process.extract(query, list(cache.keys()), limit=5, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        suggestions_str = ", ".join(valid_suggestions)
        return f"Hey {user}, I couldn't find \"{query}\" - Did you mean: {suggestions_str}? If not, try !orderbot."

    return f"Hey {user}, I couldn't find \"{query}\" or anything similar. Please check spelling."


@app.route('/api/find')
def api_find_item():
    """JSON response for item search"""
    user = request.args.get('user', 'User')
    query = _request_search_query("q", "item", "name")

    if not query:
        return jsonify({"found": False, "message": f"Hey {user}, type !find <item name> to search."})

    original_query = query
    query, alias_used = _resolve_search_alias("item", query)
    cache, _last_update, _refresh_interval, source = _get_item_cache()
    if not cache:
        _log_command_search("find", original_query, found=False, source=source)
        return jsonify({"error": "Service unavailable - item cache is not loaded"}), 503

    found_locs = cache.get(query)

    if found_locs:
        free, sub, order = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        _log_command_search("find", original_query, found=True, result_count=len(free) + len(sub) + len(order), source=source)
        return jsonify({
            "found": True,
            "query": original_query,
            "resolved_query": query,
            "alias_used": alias_used,
            "source": source,
            "results": {"free": free, "sub": sub, "order": order},
            "suggestions": [],
            "message": f"Hey {user}, I found {query.upper()} {final_msg}"
        })

    matches = process.extract(query, list(cache.keys()), limit=5, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        _log_command_search("find", original_query, found=False, source=source)
        return jsonify({
            "found": False,
            "query": original_query,
            "resolved_query": query,
            "alias_used": alias_used,
            "source": source,
            "suggestions": valid_suggestions,
            "message": f"Hey {user}, I couldn't find \"{query}\" - Did you mean: {', '.join(valid_suggestions)}?"
        })

    _log_command_search("find", original_query, found=False, source=source)
    return jsonify({
        "found": False,
        "query": original_query,
        "resolved_query": query,
        "alias_used": alias_used,
        "source": source,
        "suggestions": [],
        "message": f"Hey {user}, I couldn't find \"{query}\" or anything similar."
    })

# --- VILLAGER SEARCH ROUTES ---

@app.route('/villager')
def find_villager():
    """Text response for villager search"""
    user = request.args.get('user', 'User')
    query = _request_search_query("q", "villager", "name")

    if not query:
        return f"Hey {user}, type !villager <n> to search."

    villager_map, _source = _get_villager_map([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR])
    if not villager_map:
        return f"Hey {user}, the search service is not available right now. Please try again later."

    found_locs = villager_map.get(query)

    if found_locs:
        final_msg = format_locations_text(found_locs)
        return f"Hey {user}, I found villager {query.upper()} {final_msg}"

    matches = process.extract(query, list(villager_map.keys()), limit=3, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        suggestions_str = ", ".join(valid_suggestions)
        return f"Hey {user}, I couldn't find villager \"{query}\" - Did you mean: {suggestions_str}?"

    return f"Hey {user}, I couldn't find a villager named \"{query}\"."


@app.route('/api/villager')
def api_find_villager():
    """JSON response for villager search"""
    user = request.args.get('user', 'User')
    query = _request_search_query("q", "villager", "name")

    if not query:
        return jsonify({"found": False, "message": f"Hey {user}, type !villager <n> to search."})

    original_query = query
    query, alias_used = _resolve_search_alias("villager", query)
    villager_map, source = _get_villager_map([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR])
    if not villager_map:
        _log_command_search("villager", original_query, found=False, source=source)
        return jsonify({"error": "Service unavailable - villager cache is not loaded"}), 503

    found_locs = villager_map.get(query)

    if found_locs:
        free, sub, order = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        _log_command_search("villager", original_query, found=True, result_count=len(free) + len(sub) + len(order), source=source)
        return jsonify({
            "found": True,
            "query": original_query,
            "resolved_query": query,
            "alias_used": alias_used,
            "source": source,
            "results": {"free": free, "sub": sub, "order": order},
            "suggestions": [],
            "message": f"Hey {user}, I found villager {query.upper()} {final_msg}"
        })

    matches = process.extract(query, list(villager_map.keys()), limit=3, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        _log_command_search("villager", original_query, found=False, source=source)
        return jsonify({
            "found": False,
            "query": original_query,
            "resolved_query": query,
            "alias_used": alias_used,
            "source": source,
            "suggestions": valid_suggestions,
            "message": f"Hey {user}, I couldn't find villager \"{query}\" - Did you mean: {', '.join(valid_suggestions)}?"
        })

    _log_command_search("villager", original_query, found=False, source=source)
    return jsonify({
        "found": False,
        "query": original_query,
        "resolved_query": query,
        "alias_used": alias_used,
        "source": source,
        "suggestions": [],
        "message": f"Hey {user}, I couldn't find a villager named \"{query}\"."
    })

@app.route('/api/villagers/list')
def api_list_villagers_by_island():
    """List all villagers grouped by island"""
    villager_map, source = _get_villager_map([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR, Config.ORDER_BOT_DIR])
    if not villager_map:
        return jsonify({"error": "Service unavailable - villager cache is not loaded"}), 503

    island_manifest = {}

    for villager_name, locations in villager_map.items():
        loc_list = locations.split(", ")
        for loc in loc_list:
            if loc not in island_manifest:
                island_manifest[loc] = []
            island_manifest[loc].append(villager_name.title())

    for loc in island_manifest:
        island_manifest[loc].sort()

    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "total_islands": len(island_manifest),
        "islands": island_manifest
    })


@app.route('/api/search/similar')
def api_search_similar():
    """Return similar item or villager names for typo-learning/search UI."""
    kind = (request.args.get("kind") or "item").strip().lower()
    query = _request_search_query("q", "query", "name")
    limit = min(max(request.args.get("limit", 8, type=int), 1), 25)
    if kind not in {"item", "villager"}:
        return jsonify({"error": "kind must be item or villager"}), 400
    if not query:
        return jsonify({"error": "query is required"}), 400

    if kind == "villager":
        data, source = _get_villager_map([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR, Config.ORDER_BOT_DIR])
        choices = list(data.keys())
    else:
        cache, _last_update, _refresh_interval, source = _get_item_cache()
        display_map = cache.get("_display", {})
        choices = [key for key in cache if key != "_display"]

    matches = process.extract(query, choices, limit=limit, scorer=fuzz.WRatio)
    suggestions = []
    for key, score in matches:
        label = key.title() if kind == "villager" else display_map.get(key, key.title())
        suggestions.append({"key": key, "label": label, "score": score})
    return jsonify({"kind": kind, "query": query, "source": source, "suggestions": suggestions})

# --- DODO CODE / ISLAND STATUS ROUTES ---

@app.route('/api/islands', methods=['GET'])
def get_islands():
    """Get all island statuses and Dodo codes with full metadata."""
    viewer = _current_auth_user()
    viewer_roles = viewer.get("roles", []) if viewer else []
    viewer_is_admin = bool(viewer and viewer.get("is_admin"))
    viewer_is_mod = bool(viewer and (viewer.get("is_mod") or viewer_is_admin or _is_mod(viewer_roles)))

    # Load island metadata from DB, keyed by uppercase name
    db_map = {}
    discord_status = {}
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, name, display_name, is_visible, cat, description, items, map_url, seasonal, theme, type, updated_at, required_roles, channel_id "
            "FROM islands ORDER BY name"
        ).fetchall()
        for row in rows:
            isl = row_to_island_dict(dict(row))
            # Keep frontend gating aligned with reveal endpoint safety logic.
            isl["required_roles"], resolved_channel_id, access_source = _resolved_island_required_roles(
                isl.get("name"),
                isl.get("cat"),
                isl.get("required_roles") or [],
                isl.get("type"),
                isl.get("channel_id"),
            )
            isl["channel_id"] = resolved_channel_id
            isl["access_source"] = access_source
            if isl.get("name"):
                db_map[isl["name"].upper()] = isl
        # Load Discord bot presence data
        bot_rows = db.execute("SELECT island_id, is_online FROM island_bot_status").fetchall()
        for r in bot_rows:
            discord_status[r["island_id"]] = bool(r["is_online"])
    except Exception:
        logger.exception("Failed to load island metadata from DB for /api/islands")
    finally:
        db.close()

    results = []

    if Config.DIR_FREE and os.path.exists(Config.DIR_FREE):
        with os.scandir(Config.DIR_FREE) as entries:
            for entry in entries:
                if entry.is_dir():
                    name = entry.name.upper()
                    if db_map.get(name, {}).get("is_visible") is False:
                        continue
                    results.append(_build_island_response(
                        entry, "Free", db_map.get(name, {}),
                        discord_status.get(name.lower()),
                        viewer_roles,
                        viewer_is_mod,
                    ))

    if Config.DIR_VIP and os.path.exists(Config.DIR_VIP):
        with os.scandir(Config.DIR_VIP) as entries:
            for entry in entries:
                if entry.is_dir():
                    name = entry.name.upper()
                    if db_map.get(name, {}).get("is_visible") is False:
                        continue
                    results.append(_build_island_response(
                        entry, "VIP", db_map.get(name, {}),
                        discord_status.get(name.lower()),
                        viewer_roles,
                        viewer_is_mod,
                    ))

    if Config.DIR_ORDER and os.path.exists(Config.DIR_ORDER):
        order_entries = []
        direct_order_files = [
            os.path.join(Config.DIR_ORDER, "Dodo.txt"),
            os.path.join(Config.DIR_ORDER, "Visitors.txt"),
            os.path.join(Config.DIR_ORDER, "Villagers.txt"),
        ]
        order_name = Config.ORDER_BOT_ISLAND or os.path.basename(Config.DIR_ORDER)
        basename_matches = clean_text(os.path.basename(Config.DIR_ORDER)) == clean_text(order_name)
        if basename_matches or any(os.path.exists(path) for path in direct_order_files):
            order_entries.append(SimpleNamespace(
                name=order_name,
                path=Config.DIR_ORDER,
            ))
        with os.scandir(Config.DIR_ORDER) as entries:
            order_entries.extend(entry for entry in entries if entry.is_dir())
        for entry in order_entries:
            name = entry.name.upper()
            default_order_meta = {
                "id": name.lower(),
                "name": name,
                "cat": "order",
                "type": "Order Bot",
                "description": "Order bot island. Dodo access is handled in the configured Discord and Twitch channels.",
                "theme": "teal",
                "seasonal": "Year-Round",
                "channel_id": str(Config.ORDER_BOT_CHANNEL_ID or ""),
                "is_visible": True,
            }
            db_meta = {**default_order_meta, **db_map.get(name, {})}
            if db_meta.get("is_visible") is False:
                continue
            results.append(_build_island_response(
                entry, "Order", db_meta,
                discord_status.get(name.lower()),
                viewer_roles,
                viewer_is_mod,
            ))

    results.sort(key=lambda x: x['name'])
    return jsonify({
        "meta": {
            "timestamp": datetime.now().isoformat(),
            "cache_ttl_seconds": _FILE_CACHE_TTL,
            "note": (
                f"Dodo codes and visitor counts are read directly from files written by "
                f"the C# island bot. Each file read is cached for up to "
                f"{_FILE_CACHE_TTL} seconds, so data is near-real-time."
            ),
        },
        "data": results,
    })


@app.route('/api/browser/islands', methods=['GET'])
def api_browser_islands():
    """Frontend-friendly public island cards without Dodo codes."""
    response = get_islands()
    payload = response.get_json() if hasattr(response, "get_json") else {}
    cards = []
    category = (request.args.get("cat") or "").strip().lower()
    seasonal = (request.args.get("seasonal") or "").strip().lower()
    for island in payload.get("data", []):
        if category and str(island.get("cat") or "").lower() != category:
            continue
        if seasonal and seasonal not in str(island.get("seasonal") or "").lower():
            continue
        cards.append({
            "id": island.get("id"),
            "name": island.get("display_name") or island.get("name"),
            "canonical_name": island.get("name"),
            "cat": island.get("cat"),
            "type": island.get("type"),
            "status": island.get("status"),
            "visitors": island.get("visitors"),
            "visitor_list": island.get("visitor_list", []),
            "map_url": island.get("map_url"),
            "items": island.get("items") or [],
            "seasonal": island.get("seasonal"),
            "theme": island.get("theme"),
            "required_role_count": len(island.get("required_roles") or []),
            "accessible": bool(island.get("accessible")),
            "discord_bot_online": island.get("discord_bot_online"),
            "updated_at": island.get("updated_at"),
        })
    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "count": len(cards),
        "items": cards,
    })


@app.route('/api/islands/access', methods=['GET'])
def get_island_access():
    """Return the current user's per-island access state without Dodo/status payloads."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    subscriptions = _load_profile_subscriptions(user)
    accessible = subscriptions.get("accessible_member_islands", [])
    accessible_ids = {str(item.get("id") or "").lower() for item in accessible}
    accessible_names = {str(item.get("name") or "").upper() for item in accessible}
    role_names = _get_guild_role_names()

    rows = []
    db = get_db()
    try:
        db_rows = db.execute(
            "SELECT id, name, display_name, is_visible, cat, type, required_roles, channel_id FROM islands ORDER BY name"
        ).fetchall()
        for row in db_rows:
            island = row_to_island_dict(dict(row))
            if island.get("is_visible") is False:
                continue
            required_roles, resolved_channel_id, access_source = _resolved_island_required_roles(
                island.get("name"),
                island.get("cat"),
                island.get("required_roles", []),
                island.get("type"),
                island.get("channel_id"),
            )
            user_roles = {str(role_id) for role_id in user.get("roles", [])}
            matched = sorted(user_roles & set(required_roles))
            accessible_flag = (
                str(island.get("id") or "").lower() in accessible_ids
                or str(island.get("name") or "").upper() in accessible_names
                or _has_island_access(user.get("roles", []), required_roles, bool(user.get("is_mod") or user.get("is_admin")))
            )
            rows.append({
                "id": island.get("id"),
                "name": island.get("display_name") or island.get("name"),
                "canonical_name": island.get("name"),
                "cat": island.get("cat"),
                "type": island.get("type"),
                "channel_id": resolved_channel_id,
                "access_source": access_source,
                "accessible": accessible_flag,
                "required_roles": [_role_payload(role_id, role_names) for role_id in required_roles],
                "matched_roles": [_role_payload(role_id, role_names) for role_id in matched],
            })
    finally:
        db.close()

    _record_api_audit_event(
        "access_check",
        "islands",
        {"accessible_count": sum(1 for item in rows if item["accessible"]), "island_count": len(rows)},
    )
    return jsonify({
        "user_id": user.get("user_id"),
        "is_mod": bool(user.get("is_mod")),
        "is_admin": bool(user.get("is_admin")),
        "accessible_count": sum(1 for item in rows if item["accessible"]),
        "items": rows,
    })


# --- PATREON ROUTES ---


@app.route('/api/islands/<name>/visitors', methods=['GET'])
def get_island_visitors(name):
    """Get the current visitor list for a single island by name.

    Reads the live Visitors.txt file written by the C# island bot and returns
    the parsed list of in-game names currently on the island.

    Returns 404 if no island directory with that name is found.
    """
    target = name.upper()

    # Load bot online status for all islands (same pattern as get_islands)
    discord_status = {}
    db = get_db()
    try:
        bot_rows = db.execute("SELECT island_id, is_online FROM island_bot_status").fetchall()
        for r in bot_rows:
            discord_status[r["island_id"]] = bool(r["is_online"])
    except Exception:
        pass
    finally:
        db.close()

    # Search Free and VIP directories for a matching island folder
    for base_dir, island_type in [(Config.DIR_FREE, "Free"), (Config.DIR_VIP, "VIP")]:
        if not base_dir or not os.path.exists(base_dir):
            continue
        with os.scandir(base_dir) as entries:
            for entry in entries:
                if entry.is_dir() and entry.name.upper() == target:
                    discord_bot_online = discord_status.get(target.lower())

                    raw_content = get_file_content(entry.path, "Visitors.txt")
                    visitor_count, visitor_list = _parse_visitor_list(raw_content)

                    # Hide live data when the Discord bot is offline
                    if not discord_bot_online:
                        visitor_count = 0
                        visitor_list = []

                    return jsonify({
                        "island":        target,
                        "type":          island_type,
                        "visitor_count": visitor_count,
                        "visitor_list":  visitor_list,
                        "bot_online":    discord_bot_online,
                        "timestamp":     datetime.now().isoformat(),
                    })

    return jsonify({"error": f"Island '{name}' not found"}), 404


@app.route("/api/patreon/posts", methods=["GET"])
def get_patreon_posts():
    """Get recent Patreon posts (cached 15 min)"""
    now = datetime.now()
    if patreon_cache["list"]["data"] and patreon_cache["list"]["timestamp"]:
        if (now - patreon_cache["list"]["timestamp"]) < timedelta(minutes=15):
            return jsonify(patreon_cache["list"]["data"])

    url = f"https://www.patreon.com/api/oauth2/v2/campaigns/{Config.PATREON_CAMPAIGN_ID}/posts"
    headers = {"Authorization": f"Bearer {Config.PATREON_TOKEN}"}
    params = {
        "fields[post]": "title,content,published_at,url,is_public,embed_data,embed_url",
        "sort": "-published_at",
        "page[count]": 10
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if not response.ok:
            return jsonify({"error": "Patreon API Error", "details": response.text}), response.status_code

        raw_data = response.json()
        processed_data = [process_post_attributes(p["id"], p["attributes"]) for p in raw_data["data"]]

        result = {"data": processed_data}
        patreon_cache["list"] = {"data": result, "timestamp": now}
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": "Server error", "details": str(e)}), 500


@app.route("/api/patreon/posts/<post_id>", methods=["GET"])
def get_single_post(post_id):
    """Get a specific Patreon post (cached 15 min)"""
    now = datetime.now()

    if post_id in patreon_cache["posts"]:
        cached_post = patreon_cache["posts"][post_id]
        if (now - cached_post["timestamp"]) < timedelta(minutes=15):
            return jsonify(cached_post["data"])

    url = f"https://www.patreon.com/api/oauth2/v2/posts/{post_id}"
    headers = {"Authorization": f"Bearer {Config.PATREON_TOKEN}"}
    params = {"fields[post]": "title,content,published_at,url,is_public,embed_data,embed_url"}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if not response.ok:
            return jsonify({"error": "Post not found or API error", "details": response.text}), response.status_code

        raw_data = response.json()
        processed_post = process_post_attributes(raw_data["data"]["id"], raw_data["data"]["attributes"])

        result = {"data": processed_post}
        patreon_cache["posts"][post_id] = {"data": result, "timestamp": now}
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": "Server error", "details": str(e)}), 500


# --- STATUS ROUTE ---
@app.route('/status')
def status():
    """Get bot status"""
    if data_manager is None:
        return "Service unavailable â€” data manager not initialised.", 503
    with data_manager.lock:
        count = len(data_manager.cache)
        last_up = data_manager.last_update.strftime("%H:%M:%S") if data_manager.last_update else "Loading..."
    return f"Items: {count} | Last Update: {last_up}"


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """Manually trigger a cache refresh from Google Sheets"""
    auth = request.headers.get("Authorization", "")
    secret_bearer_ok = (
        auth.startswith("Bearer ")
        and Config.DASHBOARD_SECRET
        and _secrets.compare_digest(auth[len("Bearer "):], Config.DASHBOARD_SECRET)
    )
    token_user = _current_auth_user() if auth.startswith("Bearer ") else None
    mod_bearer_ok = bool(
        token_user
        and (
            token_user.get("is_admin")
            or token_user.get("is_mod")
            or _is_mod(token_user.get("roles", []))
        )
    )
    session_ok = _check_dashboard_session()
    if not secret_bearer_ok and not mod_bearer_ok and not session_ok:
        return jsonify({"error": "Unauthorized"}), 401

    maintenance = get_maintenance_settings()
    if maintenance["maintenance_mode"] or maintenance["disable_refresh"]:
        return jsonify({
            "error": maintenance["message"] or "Manual refresh is temporarily disabled.",
            "code": "maintenance_mode",
        }), 503

    if data_manager is None:
        return jsonify({"error": "Service unavailable â€” data manager not initialised"}), 503

    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"status": "refresh already in progress"}), 429

    def _run():
        try:
            data_manager.update_cache()
        finally:
            _refresh_lock.release()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "refresh started"}), 202


@app.route("/api/v1/villager/<name>")
def villager_route(name):
    data = NookipediaClient.get_villager_info_sync(name)
    return jsonify({
        "success": True,
        "villager": data
    })


@app.route("/api/v1/guild/human-members")
def guild_human_members():
    """Return the count of human (non-bot) members in the configured Discord guild.

    Uses the Discord REST API to paginate through all guild members and
    counts only those where the ``bot`` field is falsy.

    Returns:
        200  - success with human_members count
        403  - bot is not in the guild (Missing Access)
        404  - guild does not exist
        503  - Discord token / GUILD_ID not configured, or member intent unavailable
    """
    guild_id = str(Config.GUILD_ID or "").strip()
    if not guild_id or guild_id in ("0", "None"):
        return jsonify({"success": False, "error": "GUILD_ID is not configured."}), 503

    auth_value = _discord_bot_auth_value()
    if not auth_value:
        return jsonify({"success": False, "error": "Discord bot token is not configured."}), 503

    # Step 1: Verify the guild exists and the bot can see it.
    guild_payload = _discord_api_json(f"/guilds/{guild_id}")

    if guild_payload is None:
        # Network / token error â€“ treat as service unavailable.
        return jsonify({"success": False, "error": "Could not reach Discord API. Please try again later."}), 503

    if isinstance(guild_payload, dict):
        error_code = guild_payload.get("code")
        if error_code == 10004 or guild_payload.get("message") == "Unknown Guild":
            return jsonify({"success": False, "error": "Guild not found."}), 404
        if error_code in (50013, 50001):
            return jsonify({"success": False, "error": "Bot is not a member of the specified guild."}), 403
        if "message" in guild_payload and "id" not in guild_payload:
            return jsonify({"success": False, "error": guild_payload.get("message", "Discord API error.")}), 502

    guild_name = guild_payload.get("name", "") if isinstance(guild_payload, dict) else ""

    # Step 2: Paginate through guild members and count humans.
    human_count = 0
    after = 0  # snowflake cursor; 0 means start from the beginning
    page_limit = 1000  # maximum allowed by Discord
    first_page = True

    while True:
        path = f"/guilds/{guild_id}/members?limit={page_limit}&after={after}"
        members_payload = _discord_api_json(path)

        if members_payload is None:
            return jsonify({"success": False, "error": "Could not reach Discord API while fetching members."}), 503

        # Discord returns an error dict when the bot lacks GUILD_MEMBERS intent
        # or when access is denied.
        if isinstance(members_payload, dict):
            error_code = members_payload.get("code")
            if error_code == 10004:
                return jsonify({"success": False, "error": "Guild not found."}), 404
            if error_code in (50013, 50001):
                return jsonify({"success": False, "error": "Bot is not a member of the specified guild."}), 403
            return jsonify({"success": False, "error": members_payload.get("message", "Discord API error.")}), 502

        if not isinstance(members_payload, list):
            return jsonify({"success": False, "error": "Unexpected response from Discord API."}), 502

        # If the first page returns an empty list the bot is in the guild but
        # member intent (GUILD_MEMBERS privileged intent) is not enabled.
        if first_page and len(members_payload) == 0:
            return jsonify({
                "success": False,
                "error": (
                    "Member data is unavailable. "
                    "Ensure the bot has the GUILD_MEMBERS privileged intent enabled."
                ),
            }), 503
        first_page = False

        for member in members_payload:
            user = member.get("user") or {}
            if not user.get("bot"):
                human_count += 1

        # If we received fewer entries than the page limit, we have reached the end.
        if len(members_payload) < page_limit:
            break

        # Advance the cursor to the last member's user ID for the next page.
        last_member = members_payload[-1]
        last_user_id = (last_member.get("user") or {}).get("id")
        if not last_user_id:
            break
        after = int(last_user_id)

    return jsonify({
        "success": True,
        "guild_id": int(guild_id),
        "guild_name": guild_name,
        "human_members": human_count,
    })


# ============================================================================
# POCKET BUNDLES API (Persistent database presets & custom loadouts)
# ============================================================================

@app.route("/api/bundles", methods=["GET"])
def get_pocket_bundles():
    """Return list of official and accessible custom pocket bundles."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id", "")) if auth_user else None
    username = str(auth_user.get("username", "")) if auth_user else None
    is_admin = bool(auth_user.get("is_admin")) if auth_user else False

    conn = get_db()
    try:
        if is_admin:
            rows = conn.execute(
                "SELECT * FROM pocket_bundles ORDER BY is_official DESC, created_at DESC"
            ).fetchall()
        elif user_id or username:
            rows = conn.execute(
                "SELECT * FROM pocket_bundles WHERE is_official = 1 OR created_by = ? OR created_by = ? ORDER BY is_official DESC, created_at DESC",
                (user_id or "", username or "")
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pocket_bundles WHERE is_official = 1 ORDER BY created_at DESC"
            ).fetchall()

        bundles = []
        for r in rows:
            bundles.append({
                "id": r["id"],
                "name": r["name"],
                "description": r["description"] or "",
                "category": r["category"] or "Popular",
                "icon": r["icon"] or "fa-box-open",
                "isOfficial": bool(r["is_official"]),
                "createdBy": r["created_by"] or "Community",
                "orderItems": json.loads(r["order_items"] or "[]"),
                "dropItems": json.loads(r["drop_items"] or "[]"),
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"]
            })
        return jsonify(bundles)
    except Exception as exc:
        logger.warning("Error fetching pocket bundles: %s", exc)
        return jsonify([]), 200
    finally:
        conn.close()


@app.route("/api/bundles", methods=["POST"])
def create_pocket_bundle():
    """Create a new pocket bundle (custom or official)."""
    auth_user = _current_auth_user()
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Bundle name is required"}), 400

    bundle_id = data.get("id") or f"bundle-{int(time.time()*1000)}-{_secrets.token_hex(3)}"
    category = data.get("category") or "Popular"
    icon = data.get("icon") or "fa-box-open"
    description = data.get("description") or ""
    order_items = json.dumps(data.get("orderItems") or [])
    drop_items = json.dumps(data.get("dropItems") or [])
    is_official_req = bool(data.get("isOfficial"))

    is_admin = bool(auth_user.get("is_admin")) if auth_user else False
    is_official = 1 if (is_official_req and is_admin) else 0
    created_by = auth_user.get("username") or auth_user.get("user_id") or "Community" if auth_user else "Community"

    now_iso = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO pocket_bundles
            (id, name, description, category, icon, is_official, created_by, order_items, drop_items, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bundle_id, name, description, category, icon, is_official, created_by, order_items, drop_items, now_iso, now_iso
        ))
        conn.commit()
        return jsonify({
            "success": True,
            "bundle": {
                "id": bundle_id,
                "name": name,
                "description": description,
                "category": category,
                "icon": icon,
                "isOfficial": bool(is_official),
                "createdBy": created_by,
                "orderItems": json.loads(order_items),
                "dropItems": json.loads(drop_items),
                "createdAt": now_iso,
                "updatedAt": now_iso
            }
        }), 201
    except Exception as exc:
        logger.warning("Error creating pocket bundle: %s", exc)
        return jsonify({"error": "Failed to create bundle in database"}), 500
    finally:
        conn.close()


@app.route("/api/bundles/<bundle_id>", methods=["PUT"])
def update_pocket_bundle(bundle_id):
    """Update a pocket bundle."""
    auth_user = _current_auth_user()
    data = request.get_json() or {}
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM pocket_bundles WHERE id = ?", (bundle_id,)).fetchone()
        if not existing:
            return jsonify({"error": "Bundle not found"}), 404

        is_admin = bool(auth_user.get("is_admin")) if auth_user else False
        if existing["is_official"] and not is_admin:
            return jsonify({"error": "Only admins can edit official bundles"}), 403

        name = data.get("name") or existing["name"]
        description = data.get("description") if "description" in data else existing["description"]
        category = data.get("category") or existing["category"]
        icon = data.get("icon") or existing["icon"]
        is_official = 1 if (data.get("isOfficial") and is_admin) else (existing["is_official"] if not is_admin else 0)
        order_items = json.dumps(data["orderItems"]) if "orderItems" in data else existing["order_items"]
        drop_items = json.dumps(data["dropItems"]) if "dropItems" in data else existing["drop_items"]
        now_iso = datetime.utcnow().isoformat()

        conn.execute("""
            UPDATE pocket_bundles
            SET name = ?, description = ?, category = ?, icon = ?, is_official = ?, order_items = ?, drop_items = ?, updated_at = ?
            WHERE id = ?
        """, (name, description, category, icon, is_official, order_items, drop_items, now_iso, bundle_id))
        conn.commit()

        return jsonify({
            "success": True,
            "bundle": {
                "id": bundle_id,
                "name": name,
                "description": description,
                "category": category,
                "icon": icon,
                "isOfficial": bool(is_official),
                "createdBy": existing["created_by"],
                "orderItems": json.loads(order_items),
                "dropItems": json.loads(drop_items),
                "createdAt": existing["created_at"],
                "updatedAt": now_iso
            }
        })
    except Exception as exc:
        logger.warning("Error updating pocket bundle: %s", exc)
        return jsonify({"error": "Failed to update bundle"}), 500
    finally:
        conn.close()


@app.route("/api/bundles/<bundle_id>", methods=["DELETE"])
def delete_pocket_bundle(bundle_id):
    """Delete a pocket bundle."""
    auth_user = _current_auth_user()
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM pocket_bundles WHERE id = ?", (bundle_id,)).fetchone()
        if not existing:
            return jsonify({"success": True}), 200

        is_admin = bool(auth_user.get("is_admin")) if auth_user else False
        if existing["is_official"] and not is_admin:
            return jsonify({"error": "Only admins can delete official bundles"}), 403

        conn.execute("DELETE FROM pocket_bundles WHERE id = ?", (bundle_id,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        logger.warning("Error deleting pocket bundle: %s", exc)
        return jsonify({"error": "Failed to delete bundle"}), 500
# ============================================================================
# SHARED POCKETS API (Short unique-id pocket URLs)
# ============================================================================

# Crockford's Base32 Alphabet (excludes I, L, O, U to avoid confusion)
_ULID_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_ulid() -> str:
    """Generate a standard 26-character Crockford Base32 ULID.
    - 48 bits of UNIX timestamp in milliseconds (10 characters)
    - 80 bits of cryptographic randomness (16 characters)
    - Lexicographically sortable by creation time.
    """
    ts_ms = int(time.time() * 1000)
    time_chars = [_ULID_ENCODING[(ts_ms >> (45 - 5 * i)) & 0x1F] for i in range(10)]
    rand_val = _secrets.randbits(80)
    rand_chars = [_ULID_ENCODING[(rand_val >> (75 - 5 * i)) & 0x1F] for i in range(16)]
    return "".join(time_chars + rand_chars)


@app.route("/api/pockets/share", methods=["POST"])
def create_shared_pocket():
    """Save a pocket configuration and return a unique ULID."""
    auth_user = _current_auth_user()
    data = request.get_json() or {}
    name = (data.get("name") or "ACNH Pocket").strip()[:60]
    order_items = data.get("orderItems") or []
    drop_items = data.get("dropItems") or []

    if not order_items and not drop_items:
        return jsonify({"error": "Cannot share an empty pocket"}), 400

    conn = get_db()
    try:
        pocket_id = _generate_ulid()

        created_by = auth_user.get("username") or auth_user.get("user_id") if auth_user else "Guest"
        now_iso = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT INTO shared_pockets (id, name, order_items, drop_items, created_by, views, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
        """, (
            pocket_id,
            name,
            json.dumps(order_items),
            json.dumps(drop_items),
            created_by,
            now_iso
        ))
        conn.commit()

        return jsonify({
            "success": True,
            "id": pocket_id,
            "name": name,
            "orderItems": order_items,
            "dropItems": drop_items,
            "createdAt": now_iso
        }), 201
    except Exception as exc:
        logger.warning("Error saving shared pocket: %s", exc)
        return jsonify({"error": "Failed to save shared pocket"}), 500
    finally:
        conn.close()


@app.route("/api/pockets/share/<pocket_id>", methods=["GET"])
def get_shared_pocket(pocket_id: str):
    """Retrieve shared pocket data by short unique ID."""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM shared_pockets WHERE id = ?", (pocket_id,)).fetchone()
        if not row:
            return jsonify({"error": "Shared pocket not found"}), 404

        # Increment view count
        try:
            conn.execute("UPDATE shared_pockets SET views = views + 1 WHERE id = ?", (pocket_id,))
            conn.commit()
        except Exception:
            pass

        return jsonify({
            "id": row["id"],
            "name": row["name"],
            "orderItems": json.loads(row["order_items"] or "[]"),
            "dropItems": json.loads(row["drop_items"] or "[]"),
            "createdBy": row["created_by"],
            "views": row["views"],
            "createdAt": row["created_at"]
        })
    except Exception as exc:
        logger.warning("Error fetching shared pocket %s: %s", pocket_id, exc)
        return jsonify({"error": "Failed to retrieve shared pocket"}), 500
    finally:
        conn.close()



# ============================================================================
# ORDER BOT — SysBot.ACNHOrders REST API PROXY
# All /api/order/* routes proxy directly to the SysBot HTTP API.
# Configure via SYSBOT_API_URL (+ optional SYSBOT_API_KEY) in .env
# ============================================================================

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Module-level connection-pooled session with automatic retry on transient
# Cloudflare / network errors (502, 503, 504).  Created lazily on first use.
_sysbot_session: requests.Session | None = None


def _get_sysbot_session() -> requests.Session:
    """Return (or lazily create) a pooled requests.Session for the SysBot API."""
    global _sysbot_session
    if _sysbot_session is None:
        _sysbot_session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,           # 0s → 0.5s → 1s between retries
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST", "DELETE"],
            raise_on_status=False,        # let us read the response body even on errors
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=2,
            pool_maxsize=5,
        )
        _sysbot_session.mount("https://", adapter)
        _sysbot_session.mount("http://", adapter)
    return _sysbot_session


def _sysbot_headers() -> dict:
    """Build request headers for the SysBot API, including optional API key."""
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    key = getattr(Config, "SYSBOT_API_KEY", "") or ""
    if key:
        h["X-API-Key"] = key
    return h


def _sysbot_parse_response(resp: requests.Response, method: str, path: str) -> tuple:
    """
    Safely parse a SysBot response.  Handles Cloudflare HTML error pages
    and non-JSON bodies gracefully.  Returns (dict, http_status).
    """
    try:
        return resp.json(), resp.status_code
    except (ValueError, requests.exceptions.JSONDecodeError):
        # Cloudflare sometimes returns HTML error pages (502/503/504)
        body_preview = resp.text[:200] if resp.text else "(empty)"
        logger.warning(
            "[SysBot] %s %s returned non-JSON (HTTP %d): %s",
            method, path, resp.status_code, body_preview,
        )
        return {
            "success": False,
            "error": f"SysBot returned HTTP {resp.status_code} (non-JSON response).",
        }, resp.status_code


def _sysbot_get(path: str, **params) -> tuple:
    """Forward a GET to the SysBot API.  Returns (dict, http_status)."""
    base = getattr(Config, "SYSBOT_API_URL", "") or ""
    if not base:
        return {"success": False, "error": "SysBot API is not configured (set SYSBOT_API_URL in .env)."}, 503
    url = f"{base}{path}"
    t0 = time.monotonic()
    try:
        resp = _get_sysbot_session().get(
            url,
            headers=_sysbot_headers(),
            params={k: v for k, v in params.items() if v is not None},
            timeout=(5, 15),
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        logger.debug("[SysBot] GET %s → %d (%dms)", path, resp.status_code, elapsed)
        return _sysbot_parse_response(resp, "GET", path)
    except requests.exceptions.ConnectionError:
        logger.warning("[SysBot] GET %s — connection refused / unreachable", path)
        return {"success": False, "error": "SysBot API is unreachable."}, 503
    except requests.exceptions.Timeout:
        logger.warning("[SysBot] GET %s — timed out after %.1fs", path, time.monotonic() - t0)
        return {"success": False, "error": "SysBot API request timed out."}, 504
    except Exception as exc:
        logger.warning("[SysBot] GET %s failed: %s", path, exc)
        return {"success": False, "error": str(exc)}, 500


def _sysbot_post(path: str, body: dict | None = None) -> tuple:
    """Forward a POST to the SysBot API.  Returns (dict, http_status)."""
    base = getattr(Config, "SYSBOT_API_URL", "") or ""
    if not base:
        return {"success": False, "error": "SysBot API is not configured (set SYSBOT_API_URL in .env)."}, 503
    url = f"{base}{path}"
    t0 = time.monotonic()
    try:
        resp = _get_sysbot_session().post(
            url,
            headers=_sysbot_headers(),
            json=body or {},
            timeout=(5, 15),
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        logger.debug("[SysBot] POST %s → %d (%dms)", path, resp.status_code, elapsed)
        return _sysbot_parse_response(resp, "POST", path)
    except requests.exceptions.ConnectionError:
        logger.warning("[SysBot] POST %s — connection refused / unreachable", path)
        return {"success": False, "error": "SysBot API is unreachable."}, 503
    except requests.exceptions.Timeout:
        logger.warning("[SysBot] POST %s — timed out after %.1fs", path, time.monotonic() - t0)
        return {"success": False, "error": "SysBot API request timed out."}, 504
    except Exception as exc:
        logger.warning("[SysBot] POST %s failed: %s", path, exc)
        return {"success": False, "error": str(exc)}, 500


def _sysbot_delete(path: str, **params) -> tuple:
    """Forward a DELETE to the SysBot API.  Returns (dict, http_status)."""
    base = getattr(Config, "SYSBOT_API_URL", "") or ""
    if not base:
        return {"success": False, "error": "SysBot API is not configured (set SYSBOT_API_URL in .env)."}, 503
    url = f"{base}{path}"
    t0 = time.monotonic()
    try:
        resp = _get_sysbot_session().delete(
            url,
            headers=_sysbot_headers(),
            params={k: v for k, v in params.items() if v is not None},
            timeout=(5, 15),
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        logger.debug("[SysBot] DELETE %s → %d (%dms)", path, resp.status_code, elapsed)
        return _sysbot_parse_response(resp, "DELETE", path)
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "SysBot API is unreachable."}, 503
    except requests.exceptions.Timeout:
        return {"success": False, "error": "SysBot API request timed out."}, 504
    except Exception as exc:
        logger.warning("[SysBot] DELETE %s failed: %s", path, exc)
        return {"success": False, "error": str(exc)}, 500


# ── Normalization helpers ────────────────────────────────────────────────────

# Map SysBot status values → frontend-friendly status values
_SYSBOT_STATUS_MAP = {
    "queued": "queued",
    "next": "preparing",         # first in queue, being prepared
    "ready": "ready",            # dodo code available
    "completed": "completed",
    "cancelled": "cancelled",
    "not_found": "error",        # order expired or unknown
    "error": "error",
    # Pass-through values used by the existing backend
    "active": "preparing",
    "in_progress": "preparing",
    "preparing": "preparing",
}

_INVALID_DODO_CODES = frozenset({"00000", "-----", "null", "None", ""})


def _parse_eta_minutes(data: dict) -> int | None:
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
    return None


def _normalize_order_response(data: dict) -> dict:
    """
    Normalize a SysBot order/status response for the frontend:
    - Map status values (next → preparing)
    - Parse eta → estimated_minutes
    - Validate dodo_code
    - Clamp queue_position
    """
    if not isinstance(data, dict):
        return data

    # Dodo code validation
    dodo = data.get("dodo_code")
    has_dodo = bool(dodo and str(dodo).strip() not in _INVALID_DODO_CODES)
    if not has_dodo:
        data["dodo_code"] = None

    # Status normalization
    raw_status = str(data.get("status") or "queued").lower()
    if has_dodo and raw_status not in ("completed", "cancelled", "error"):
        data["status"] = "ready"
    else:
        data["status"] = _SYSBOT_STATUS_MAP.get(raw_status, raw_status)

    # Queue position — allow 0 for ready/preparing (it's valid from Sinta)
    raw_pos = data.get("queue_position")
    if raw_pos is not None:
        try:
            pos_int = int(raw_pos)
            if pos_int <= 0 and data["status"] not in ("ready", "preparing"):
                pos_int = 1
            data["queue_position"] = max(0, pos_int)
        except (ValueError, TypeError):
            data["queue_position"] = 1

    # Estimated minutes
    if "estimated_minutes" not in data or data.get("estimated_minutes") is None:
        parsed = _parse_eta_minutes(data)
        data["estimated_minutes"] = parsed if parsed is not None else (0 if data["status"] == "ready" else 2)

    return data


# ── Bot Status ───────────────────────────────────────────────────────────────

@app.route("/api/order/bot-status", methods=["GET"])
def get_order_bot_status():
    """
    Proxy GET /api/status from SysBot.ACNHOrders.

    Returns the full bot state including:
    - mode (DropMode / OrderMode), is_drop_mode, is_order_mode
    - is_running, accepting_commands
    - island_name, dodo_code (in Drop Mode), layer
    - visitors_count, visitor_list
    - queue_count, battery_charge
    - last_dodo_fetch, server_time, is_dirty
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    data, code = _sysbot_get("/api/status")

    # Forward all fields as-is from Sinta — the frontend will pick what it needs.
    # Only enrich with defaults for critical fields the frontend relies on.
    if isinstance(data, dict) and data.get("success"):
        data.setdefault("island_name", getattr(Config, "ORDER_BOT_ISLAND", "Sinta"))
        data.setdefault("accepting_commands", True)
        data.setdefault("queue_count", 0)
        # Ensure visitor_list is always an array
        if "visitors" in data and "visitor_list" not in data:
            visitors_str = data.get("visitors") or ""
            data["visitor_list"] = [v.strip() for v in visitors_str.split("\n") if v.strip()] if visitors_str else []
        data.setdefault("visitor_list", [])
        data.setdefault("visitors_count", len(data["visitor_list"]))

    return jsonify(data), code


# ── Submit Order ─────────────────────────────────────────────────────────────

@app.route("/api/order/submit", methods=["POST"])
def submit_order_to_bot():
    """
    Proxy POST /api/order to SysBot.ACNHOrders and persist to SQLite DB.

    Accepted body formats:
      { "order": "Gold nugget 30, Iron nugget 10", "villager": "Raymond", "username": "...", "order_id": "..." }
      { "preset": "materials", "username": "..." }
      { "items": ["Gold nugget 30", "Iron nugget 10"], "username": "..." }

    Returns: success, order_id, queue_position, eta, estimated_seconds, item_count, message.
    Save the returned order_id and poll /api/order/status to get the Dodo code when ready.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    data = request.get_json() or {}

    order_text = (data.get("order") or data.get("command") or "").strip()
    # Strip leading bot-command prefix (e.g. "!order ") so SysBot only sees raw item codes.
    order_text = re.sub(r"^!order\s*", "", order_text, flags=re.IGNORECASE).strip()
    items_list = data.get("items")
    preset = (data.get("preset") or "").strip()

    if not order_text and not items_list and not preset:
        return jsonify({"success": False, "error": "Provide 'order', 'items', or 'preset' in the request body."}), 400

    username = (
        data.get("username")
        or auth_user.get("nickname")
        or auth_user.get("username")
        or "WebUser"
    )
    user_id = str(auth_user.get("user_id") or auth_user.get("id") or "")

    # Require user to setup their server nickname in Character Name | Island Name format before ordering
    if not auth_user.get("is_admin") and user_id not in ("bot_system", ""):
        current_nick = str(auth_user.get("nickname") or "").strip()
        candidate_nick = str(data.get("username") or data.get("display_name") or current_nick).strip()
        if not is_valid_acnh_nickname(candidate_nick) and not is_valid_acnh_nickname(current_nick):
            return jsonify({
                "success": False,
                "error": "You must set your Discord server nickname to 'Character Name | Island Name' before ordering.",
                "code": "NICKNAME_REQUIRED",
            }), 400

    payload: dict = {"username": username}
    if order_text:
        payload["order"] = order_text
    elif items_list:
        payload["items"] = items_list
    else:
        payload["preset"] = preset

    if data.get("villager"):
        payload["villager"] = data["villager"]
    if data.get("order_id"):
        payload["order_id"] = data["order_id"]
    if user_id:
        payload["user_id"] = user_id

    result, code = _sysbot_post("/api/order", payload)

    # Normalize result fields for frontend and DB
    result = _normalize_order_response(result)

    # Persist order to database
    if isinstance(result, dict) and (result.get("success") or code in (200, 201)):
        order_id = str(result.get("order_id") or result.get("id") or data.get("order_id") or "")
        if order_id:
            now_ts = int(time.time())
            command_str = order_text or (json.dumps(items_list) if items_list else preset)
            db = get_db()
            try:
                db.execute(
                    """REPLACE INTO order_bot_queue
                           (id, user_id, username, command, order_type, status,
                            queue_position, estimated_minutes, dodo_code, island_name,
                            message, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        order_id,
                        user_id or "anonymous",
                        username,
                        command_str,
                        "order",
                        result.get("status") or "queued",
                        int(result.get("queue_position") or 1),
                        int(result.get("estimated_minutes") or 2),
                        result.get("dodo_code"),
                        result.get("island_name") or "Sinta",
                        result.get("message") or "Order placed",
                        now_ts,
                        now_ts,
                    ),
                )
                db.commit()
            except Exception as exc:
                logger.warning("[OrderBot] Failed to persist order %s: %s", order_id, exc)
            finally:
                db.close()

    return jsonify(result), code


# ── Poll Order Status ────────────────────────────────────────────────────────

@app.route("/api/order/status", methods=["GET"])
def get_order_status():
    """
    Proxy GET /api/order/status?id={order_id} from SysBot.
    Poll this after submitting an order to track queue position, ETA, and Dodo code.

    status values: queued | preparing | ready | completed | cancelled | error
    dodo_code is non-null once status == "ready".
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    order_id = request.args.get("id") or request.args.get("order_id") or ""
    if not order_id:
        return jsonify({"success": False, "error": "order_id is required."}), 400
    data, code = _sysbot_get("/api/order/status", id=order_id)

    # Normalize response fields from SysBot
    data = _normalize_order_response(data)

    # Sync status to SQLite database
    if isinstance(data, dict) and data.get("status"):
        now_ts = int(time.time())
        db = get_db()
        try:
            db.execute(
                """UPDATE order_bot_queue
                   SET status = ?,
                       queue_position = COALESCE(?, queue_position),
                       estimated_minutes = COALESCE(?, estimated_minutes),
                       dodo_code = COALESCE(?, dodo_code),
                       island_name = COALESCE(?, island_name),
                       message = COALESCE(?, message),
                       updated_at = ?
                   WHERE id = ?""",
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
            db.commit()
        except Exception as exc:
            logger.warning("[OrderBot] Failed to update DB status for %s: %s", order_id, exc)
        finally:
            db.close()

    return jsonify(data), code


# ── Cancel Order ─────────────────────────────────────────────────────────────

@app.route("/api/order/cancel", methods=["POST", "DELETE"])
def cancel_order():
    """
    Proxy POST /api/order/cancel to SysBot.
    Body: { "id": "order_id" }
    Also accepts DELETE /api/order/cancel?id=...
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    if request.method == "DELETE":
        order_id = (request.args.get("id") or request.args.get("order_id") or "").strip()
    else:
        body = request.get_json() or {}
        order_id = (body.get("id") or body.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"success": False, "error": "order_id is required."}), 400

    result, code = _sysbot_post("/api/order/cancel", {"id": order_id})

    # Mark as cancelled in DB
    db = get_db()
    try:
        db.execute(
            "UPDATE order_bot_queue SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (int(time.time()), order_id),
        )
        db.commit()
    except Exception as exc:
        logger.warning("[OrderBot] Failed to mark order %s as cancelled: %s", order_id, exc)
    finally:
        db.close()

    return jsonify(result), code


# ── User Order History ───────────────────────────────────────────────────────

@app.route("/api/order/user-history", methods=["GET"])
def get_user_order_history():
    """
    Returns the authenticated user's order history from SQLite order_bot_queue.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required", "orders": []}), 401

    user_id = str(request.args.get("user_id") or auth_user.get("user_id") or auth_user.get("id") or "")
    if not user_id:
        return jsonify({"success": True, "orders": []})

    limit = min(int(request.args.get("limit", 50)), 100)
    db = get_db()
    try:
        rows = db.execute(
            """SELECT id, user_id, username, command, order_type, status,
                      queue_position, estimated_minutes, dodo_code, island_name,
                      message, created_at, updated_at
               FROM order_bot_queue
               WHERE user_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()

        orders = []
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

        return jsonify({"success": True, "orders": orders})
    except Exception as exc:
        logger.warning("[OrderBot] Failed to query user order history: %s", exc)
        return jsonify({"success": False, "error": str(exc), "orders": []}), 500
    finally:
        db.close()


# ── Queue ────────────────────────────────────────────────────────────────────

@app.route("/api/order/queue", methods=["GET"])
def get_order_queue():
    """
    Proxy GET /api/queue from SysBot.
    Returns the full list of pending orders with queue position, ETA, and username.
    Normalizes the response so the frontend gets consistent field names.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    data, code = _sysbot_get("/api/queue")

    # Normalize each order entry for the frontend
    if isinstance(data, dict) and isinstance(data.get("orders"), list):
        for entry in data["orders"]:
            # Sinta uses "position", frontend expects "queue_position"
            if "position" in entry and "queue_position" not in entry:
                entry["queue_position"] = entry["position"]
            # Normalize status (next → preparing)
            raw_st = str(entry.get("status") or "queued").lower()
            entry["status"] = _SYSBOT_STATUS_MAP.get(raw_st, raw_st)
            # Parse eta → estimated_minutes
            if "estimated_minutes" not in entry:
                parsed = _parse_eta_minutes(entry)
                if parsed is not None:
                    entry["estimated_minutes"] = parsed

        # Also populate the legacy "queue" key for backward compat with frontend
        data["queue"] = data["orders"]

    return jsonify(data), code


# ── Dodo Code ────────────────────────────────────────────────────────────────

@app.route("/api/order/dodo", methods=["GET"])
def get_order_dodo():
    """
    Proxy GET /api/dodo from SysBot.
    Drop Mode  -> returns dodo_code immediately (no params needed).
    Order Mode -> pass ?order_id=... to get the code once the order is ready.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    order_id = request.args.get("order_id") or request.args.get("id")
    user_id = request.args.get("user_id") or str(auth_user.get("user_id") or auth_user.get("id") or "")
    data, code = _sysbot_get("/api/dodo", order_id=order_id, user_id=user_id)
    return jsonify(data), code


# ── Item Drop ────────────────────────────────────────────────────────────────

@app.route("/api/order/drop", methods=["POST"])
def order_drop():
    """
    Proxy POST /api/drop to SysBot.
    Body: { "items": "Gold nugget 30", "type": "items"|"diy", "username": "..." }
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    body = request.get_json() or {}
    username = (
        data.get("username")
        if (data := body) and data.get("username")
        else (auth_user.get("nickname") or auth_user.get("username") or "WebUser")
    )
    items = body.get("items") or body.get("item") or ""
    if not items:
        return jsonify({"success": False, "error": "items is required."}), 400
    payload = {"items": items, "type": body.get("type") or "items", "username": username}
    if body.get("count"):
        payload["count"] = body["count"]
    result, code = _sysbot_post("/api/drop", payload)
    return jsonify(result), code


# ── Presets ──────────────────────────────────────────────────────────────────

@app.route("/api/order/presets", methods=["GET"])
def get_order_presets():
    """Proxy GET /api/presets — list all configured SysBot preset names."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    data, code = _sysbot_get("/api/presets")
    return jsonify(data), code


# ── Speak (In-Game Chat) ─────────────────────────────────────────────────────

@app.route("/api/order/speak", methods=["POST"])
def order_speak():
    """
    Proxy POST /api/speak to SysBot.
    Sends a message to the in-game Switch chat.
    Body: { "message": "Welcome to Sinta!" }
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    body = request.get_json() or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "message is required."}), 400
    result, code = _sysbot_post("/api/speak", {"message": message})
    return jsonify(result), code


# ── Turnips (Stalk Market) ───────────────────────────────────────────────────

@app.route("/api/order/turnips", methods=["POST"])
def order_set_turnips():
    """
    Proxy POST /api/turnips to SysBot.
    Sets the island's turnip (stalk market) price.
    Body: { "value": 999999999 }
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    body = request.get_json() or {}
    value = body.get("value")
    if value is None:
        return jsonify({"success": False, "error": "value is required."}), 400
    try:
        value = int(value)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "value must be an integer."}), 400
    result, code = _sysbot_post("/api/turnips", {"value": value})
    return jsonify(result), code


@app.route("/api/order/turnips/max", methods=["GET"])
def order_set_turnips_max():
    """Proxy GET /api/turnips/max — set the maximum turnip price directly."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    data, code = _sysbot_get("/api/turnips/max")
    return jsonify(data), code


# ── Clean (Pick Up Items) ────────────────────────────────────────────────────

@app.route("/api/order/clean", methods=["POST"])
def order_clean():
    """
    Proxy POST /api/clean to SysBot.
    Triggers the bot to pick up (clean) all dropped items on the ground.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    result, code = _sysbot_post("/api/clean")
    return jsonify(result), code


# ── Villagers ────────────────────────────────────────────────────────────────

@app.route("/api/order/villagers", methods=["GET"])
def order_villagers():
    """
    Proxy GET /api/villagers from SysBot.
    Returns the list of villagers currently on the island.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    data, code = _sysbot_get("/api/villagers")
    return jsonify(data), code


# ── Sub Island Drop ──────────────────────────────────────────────────────────

@app.route("/api/order/drop-sub", methods=["POST"])
def drop_sub_island():
    """Proxy a drop or villager inject command to a specific Sub Island."""
    auth_user = _current_auth_user()
    data = request.get_json() or {}
    island_id = data.get("island_id")
    island_name = data.get("island_name") or island_id or "Sub Island"
    command = data.get("command") or ""
    plot_num = data.get("plot_number")

    if not island_id or not command:
        return jsonify({"error": "Island and command are required"}), 400
    if not auth_user:
        return jsonify({"error": "Unauthorized. Please log in with Discord to access Sub Islands."}), 401

    return jsonify({
        "success": True,
        "island_id": island_id,
        "island_name": island_name,
        "plot_number": plot_num,
        "message": f"Drop command dispatched silently to {island_name}! Items ready on island.",
    })

# ============================================================================
# COMMUNITY LOADOUTS & CLOUD SYNC API
# ============================================================================

@app.route("/api/loadouts", methods=["GET"])
def get_community_loadouts():
    """List public community and official loadouts."""
    auth_user = _current_auth_user()
    category = request.args.get("category")
    tag = request.args.get("tag")
    author = request.args.get("author")
    search = request.args.get("q", "").strip().lower()

    conn = get_db()
    try:
        sql = "SELECT * FROM community_loadouts WHERE 1=1"
        params = []
        if category and category != "All":
            sql += " AND category = ?"
            params.append(category)
        if author:
            sql += " AND created_by = ?"
            params.append(author)
        
        sql += " ORDER BY is_official DESC, upvotes DESC, created_at DESC LIMIT 100"
        rows = conn.execute(sql, tuple(params)).fetchall()
        
        auth_user = _current_auth_user()
        user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
        if not user_id:
            client_header = request.headers.get("x-client-id") or ""
            ip = request.remote_addr or "127.0.0.1"
            user_id = f"anon_{client_header}_{ip}"[:64]

        upvoted_rows = conn.execute(
            "SELECT loadout_id FROM community_loadout_upvotes WHERE user_id = ?",
            (user_id,)
        ).fetchall()
        user_upvoted_ids = {u["loadout_id"] for u in upvoted_rows}

        loadouts = []
        for r in rows:
            name = r["name"] or ""
            desc = r["description"] or ""
            tags_list = json.loads(r["tags"] or "[]")
            
            if search:
                if search not in name.lower() and search not in desc.lower() and not any(search in t.lower() for t in tags_list):
                    continue
                    
            loadouts.append({
                "id": r["id"],
                "shortCode": r["short_code"],
                "name": name,
                "description": desc,
                "category": r["category"] or "General",
                "tags": tags_list,
                "orderItems": json.loads(r["order_items"] or "[]"),
                "dropItems": json.loads(r["drop_items"] or "[]"),
                "author": r["created_by"] or "Community",
                "userId": r["user_id"],
                "upvotes": r["upvotes"] or 0,
                "views": r["views"] or 0,
                "isOfficial": bool(r["is_official"]),
                "hasUpvoted": r["id"] in user_upvoted_ids,
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"]
            })
        return jsonify(loadouts)
    except Exception as exc:
        logger.warning("Error fetching community loadouts: %s", exc)
        return jsonify([]), 200
    finally:
        conn.close()


@app.route("/api/loadouts", methods=["POST"])
def create_community_loadout():
    """Create a new community loadout."""
    auth_user = _current_auth_user()
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()[:60]
    if not name:
        return jsonify({"error": "Loadout name is required"}), 400
    
    order_items = data.get("orderItems") or []
    drop_items = data.get("dropItems") or []
    if not order_items and not drop_items:
        return jsonify({"error": "Cannot publish an empty loadout"}), 400
    
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    random_code = "CHOP-" + "".join(_secrets.choice(chars) for _ in range(4))
    short_code = (data.get("shortCode") or random_code).strip().upper()[:16]
    
    loadout_id = data.get("id") or f"loadout-{int(time.time()*1000)}-{_secrets.token_hex(3)}"
    category = data.get("category") or "General"
    description = (data.get("description") or "").strip()[:300]
    tags = json.dumps(data.get("tags") or [])
    user_id = str(auth_user.get("user_id", "")) if auth_user else None
    created_by = (auth_user.get("username") or data.get("author") or "Community") if auth_user else (data.get("author") or "Community")
    is_admin = bool(auth_user.get("is_admin")) if auth_user else False
    is_official = 1 if (data.get("isOfficial") and is_admin) else 0
    now_iso = datetime.utcnow().isoformat()

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO community_loadouts
            (id, short_code, name, description, tags, category, order_items, drop_items, user_id, created_by, upvotes, views, is_official, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)
        """, (
            loadout_id, short_code, name, description, tags, category,
            json.dumps(order_items), json.dumps(drop_items),
            user_id, created_by, is_official, now_iso, now_iso
        ))
        conn.commit()

        return jsonify({
            "success": True,
            "loadout": {
                "id": loadout_id,
                "shortCode": short_code,
                "name": name,
                "description": description,
                "tags": json.loads(tags),
                "category": category,
                "orderItems": order_items,
                "dropItems": drop_items,
                "author": created_by,
                "upvotes": 0,
                "views": 1,
                "isOfficial": bool(is_official),
                "createdAt": now_iso,
                "updatedAt": now_iso
            }
        }), 201
    except Exception as exc:
        logger.warning("Error saving community loadout: %s", exc)
        return jsonify({"error": "Failed to save loadout to database"}), 500
    finally:
        conn.close()


@app.route("/api/loadouts/code/<short_code>", methods=["GET"])
def get_loadout_by_code(short_code: str):
    """Lookup loadout by shortcode or ID (supports community loadouts, staff curated bundles, and shared pockets)."""
    code = short_code.strip().upper()
    conn = get_db()
    try:
        # 1. Community Loadouts
        row = conn.execute(
            "SELECT * FROM community_loadouts WHERE UPPER(short_code) = ? OR id = ?",
            (code, short_code)
        ).fetchone()

        # 2. Staff Curated Pocket Bundles
        if not row:
            clean_bundle_id = code.replace("STAFF-", "").replace("BDL-", "").replace("CHOP-", "").lower()
            bundle_row = conn.execute(
                "SELECT * FROM pocket_bundles WHERE UPPER(id) = ? OR UPPER(name) = ? OR LOWER(id) = ? OR id = ?",
                (code, code, clean_bundle_id, short_code)
            ).fetchone()
            if bundle_row:
                order_items = json.loads(bundle_row["order_items"] or "[]")
                drop_items = json.loads(bundle_row["drop_items"] or "[]")
                return jsonify({
                    "id": f"bundle-{bundle_row['id']}",
                    "shortCode": f"STAFF-{bundle_row['id'][:6].upper()}",
                    "name": bundle_row["name"],
                    "description": bundle_row["description"] or "Official Staff Curated Pocket Build",
                    "category": bundle_row["category"] or "Starter Kits",
                    "tags": ["official", "staff", bundle_row["category"] or "bundle"],
                    "orderItems": order_items,
                    "dropItems": drop_items,
                    "author": bundle_row["created_by"] or "Chopaeng Staff",
                    "userId": None,
                    "upvotes": 0,
                    "views": 1,
                    "isOfficial": True,
                    "createdAt": bundle_row["created_at"],
                    "updatedAt": bundle_row["updated_at"]
                })

        # 3. Shared Pockets
        if not row:
            shared_row = conn.execute(
                "SELECT * FROM shared_pockets WHERE id = ?",
                (short_code,)
            ).fetchone()
            if shared_row:
                return jsonify({
                    "id": shared_row["id"],
                    "shortCode": shared_row["id"],
                    "name": shared_row["name"],
                    "description": "Shared pocket build",
                    "category": "Custom Builds",
                    "tags": ["shared"],
                    "orderItems": json.loads(shared_row["order_items"] or "[]"),
                    "dropItems": json.loads(shared_row["drop_items"] or "[]"),
                    "author": shared_row["created_by"],
                    "upvotes": 0,
                    "views": shared_row["views"],
                    "isOfficial": False,
                    "createdAt": shared_row["created_at"],
                    "updatedAt": shared_row["created_at"]
                })
            return jsonify({"error": "Loadout not found"}), 404

        try:
            conn.execute("UPDATE community_loadouts SET views = views + 1 WHERE id = ?", (row["id"],))
            conn.commit()
        except Exception:
            pass

        return jsonify({
            "id": row["id"],
            "shortCode": row["short_code"],
            "name": row["name"],
            "description": row["description"] or "",
            "category": row["category"] or "General",
            "tags": json.loads(row["tags"] or "[]"),
            "orderItems": json.loads(row["order_items"] or "[]"),
            "dropItems": json.loads(row["drop_items"] or "[]"),
            "author": row["created_by"] or "Community",
            "userId": row["user_id"],
            "upvotes": row["upvotes"] or 0,
            "views": (row["views"] or 0) + 1,
            "isOfficial": bool(row["is_official"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"]
        })
    except Exception as exc:
        logger.warning("Error fetching loadout %s: %s", short_code, exc)
        return jsonify({"error": "Failed to retrieve loadout"}), 500
    finally:
        conn.close()


@app.route("/api/loadouts/<loadout_id>/upvote", methods=["POST"])
def upvote_loadout(loadout_id: str):
    """Toggle upvote for a loadout or staff bundle by user/session stored in database."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        # Fallback to client IP / anonymous token from header
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"anon_{client_header}_{ip}"[:64]

    conn = get_db()
    try:
        # Clean loadout_id
        target_id = loadout_id.strip()

        # Check if already upvoted in database
        existing = conn.execute(
            "SELECT 1 FROM community_loadout_upvotes WHERE loadout_id = ? AND user_id = ?",
            (target_id, user_id)
        ).fetchone()

        now_iso = datetime.utcnow().isoformat()
        if existing:
            # Remove upvote (toggle off)
            conn.execute(
                "DELETE FROM community_loadout_upvotes WHERE loadout_id = ? AND user_id = ?",
                (target_id, user_id)
            )
            is_upvoted = False
        else:
            # Insert upvote in database
            conn.execute(
                "REPLACE INTO community_loadout_upvotes (loadout_id, user_id, created_at) VALUES (?, ?, ?)",
                (target_id, user_id, now_iso)
            )
            is_upvoted = True

        # Sync count in community_loadouts table if it exists
        count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM community_loadout_upvotes WHERE loadout_id = ?",
            (target_id,)
        ).fetchone()
        count = count_row["cnt"] if count_row else (1 if is_upvoted else 0)

        # Also update raw ID without bundle- prefix if applicable
        raw_id = target_id.replace("bundle-", "")
        conn.execute(
            "UPDATE community_loadouts SET upvotes = ? WHERE id = ? OR id = ?",
            (count, target_id, raw_id)
        )
        conn.commit()

        return jsonify({
            "success": True,
            "upvoted": is_upvoted,
            "upvotes": count
        })
    except Exception as exc:
        logger.warning("Error toggling upvote for loadout %s: %s", loadout_id, exc)
        return jsonify({"error": "Failed to upvote"}), 500
    finally:
        conn.close()

@app.route("/api/loadouts/<loadout_id>", methods=["DELETE"])
def delete_loadout(loadout_id: str):
    """Delete a loadout (owner or admin)."""
    auth_user = _current_auth_user()
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM community_loadouts WHERE id = ?", (loadout_id,)).fetchone()
        if not row:
            return jsonify({"error": "Loadout not found"}), 404

        is_admin = bool(auth_user.get("is_admin")) if auth_user else False
        user_id = str(auth_user.get("user_id", "")) if auth_user else ""
        if not is_admin and (not user_id or str(row["user_id"]) != user_id):
            return jsonify({"error": "Permission denied"}), 403

        conn.execute("DELETE FROM community_loadouts WHERE id = ?", (loadout_id,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        logger.warning("Error deleting loadout %s: %s", loadout_id, exc)
        return jsonify({"error": "Failed to delete"}), 500
    finally:
        conn.close()


# ─── User Custom Presets (Vault Sync) ─────────────────────────────────────────

@app.route("/api/user/presets", methods=["GET"])
def get_user_presets():
    """Fetch user's private custom presets synced across devices."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM user_custom_presets WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,)
        ).fetchall()

        presets = []
        for r in rows:
            presets.append({
                "id": r["id"],
                "title": r["title"],
                "description": r["description"] or "",
                "category": r["category"] or "Custom Builds",
                "tags": json.loads(r["tags"] or "[]"),
                "orderItems": json.loads(r["order_items"] or "[]"),
                "dropItems": json.loads(r["drop_items"] or "[]"),
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"],
            })
        return jsonify(presets)
    except Exception as exc:
        logger.warning("Error fetching user presets: %s", exc)
        return jsonify([])
    finally:
        conn.close()


@app.route("/api/user/presets", methods=["POST"])
def save_user_preset():
    """Create or update a user's private custom preset."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]

    data = request.get_json() or {}
    preset_id = (data.get("id") or f"local-preset-{int(time.time()*1000)}").strip()
    title = (data.get("title") or data.get("name") or "Untitled Preset").strip()[:80]
    description = (data.get("description") or "").strip()[:300]
    category = data.get("category") or "Custom Builds"
    tags = json.dumps(data.get("tags") or [])
    order_items = json.dumps(data.get("orderItems") or [])
    drop_items = json.dumps(data.get("dropItems") or [])
    now_iso = datetime.utcnow().isoformat()

    conn = get_db()
    try:
        conn.execute("""
            REPLACE INTO user_custom_presets
            (id, user_id, title, description, category, tags, order_items, drop_items, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            preset_id, user_id, title, description, category, tags,
            order_items, drop_items, data.get("createdAt") or now_iso, now_iso
        ))
        conn.commit()

        return jsonify({
            "success": True,
            "preset": {
                "id": preset_id,
                "title": title,
                "description": description,
                "category": category,
                "tags": json.loads(tags),
                "orderItems": json.loads(order_items),
                "dropItems": json.loads(drop_items),
                "createdAt": data.get("createdAt") or now_iso,
                "updatedAt": now_iso
            }
        }), 201
    except Exception as exc:
        logger.warning("Error saving user preset: %s", exc)
        return jsonify({"error": "Failed to save preset to database"}), 500
    finally:
        conn.close()


@app.route("/api/user/presets/<preset_id>", methods=["DELETE"])
def delete_user_preset(preset_id: str):
    """Delete a user's private custom preset."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]

    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM user_custom_presets WHERE id = ? AND (user_id = ? OR ? = '1')",
            (preset_id, user_id, "1" if auth_user and auth_user.get("is_admin") else "0")
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        logger.warning("Error deleting user preset %s: %s", preset_id, exc)
        return jsonify({"error": "Failed to delete"}), 500
    finally:
        conn.close()



# ============================================================================
# USER FAVORITE ISLANDS ENDPOINTS
# ============================================================================

@app.route("/api/user/favorites", methods=["GET", "POST"])
@app.route("/api/profile/favorites", methods=["GET", "POST"])
@app.route("/api/user/favorite-islands", methods=["GET", "POST"])
def api_user_favorite_islands():
    """Get or toggle authenticated user favorite islands."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]

    conn = get_db()
    try:
        if request.method == "GET":
            rows = conn.execute(
                "SELECT island_id FROM user_favorite_islands WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            ).fetchall()
            favorite_islands = [r["island_id"] for r in rows if r["island_id"]]
            return jsonify({
                "ok": True,
                "success": True,
                "favorite_islands": favorite_islands,
                "count": len(favorite_islands)
            })

        # POST: Save or toggle single favorite or save batch
        data = request.get_json(silent=True) or {}
        now_iso = datetime.utcnow().isoformat()

        # Case A: Batch list of favorite islands
        if "favorites" in data or "favorite_islands" in data or isinstance(data, list):
            fav_list = data.get("favorites") or data.get("favorite_islands") or data
            if isinstance(fav_list, list):
                conn.execute("DELETE FROM user_favorite_islands WHERE user_id = ?", (user_id,))
                for i_id in fav_list:
                    clean_id = str(i_id).strip().lower()[:64]
                    if clean_id:
                        conn.execute("""
                            REPLACE INTO user_favorite_islands (user_id, island_id, created_at)
                            VALUES (?, ?, ?)
                        """, (user_id, clean_id, now_iso))
                conn.commit()
                return jsonify({"ok": True, "success": True, "count": len(fav_list)})

        # Case B: Single island toggle / set
        island_id = str(data.get("island_id") or data.get("id") or "").strip().lower()[:64]
        if not island_id:
            return jsonify({"ok": False, "error": "island_id is required"}), 400

        is_fav = data.get("is_favorite")
        if is_fav is None:
            is_fav = data.get("isFavorite", True)
        is_fav = bool(is_fav)

        if is_fav:
            conn.execute("""
                REPLACE INTO user_favorite_islands (user_id, island_id, created_at)
                VALUES (?, ?, ?)
            """, (user_id, island_id, now_iso))
        else:
            conn.execute(
                "DELETE FROM user_favorite_islands WHERE user_id = ? AND island_id = ?",
                (user_id, island_id)
            )

        conn.commit()
        return jsonify({
            "ok": True,
            "success": True,
            "island_id": island_id,
            "is_favorite": is_fav
        })
    except Exception as exc:
        logger.warning("Error in user favorite islands API: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()

# ============================================================================
# USER SAVED CHARACTERS & PUBLIC PASSPORT ENDPOINTS
# ============================================================================

@app.route("/api/user/characters", methods=["GET", "POST"])
@app.route("/api/profile/characters", methods=["GET", "POST"])
def api_user_characters():
    """Get or save authenticated user in-game characters."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]

    conn = get_db()
    try:
        if request.method == "GET":
            rows = conn.execute(
                "SELECT * FROM user_saved_characters WHERE user_id = ? ORDER BY is_default DESC, updated_at DESC",
                (user_id,)
            ).fetchall()
            characters = []
            for r in rows:
                characters.append({
                    "id": r["id"],
                    "ign": r["ign"],
                    "islandName": r["island_name"],
                    "title": r["title"] or "Island Resident",
                    "icon": r["icon"] or "fa-leaf",
                    "isDefault": bool(r["is_default"]),
                    "createdAt": r["created_at"],
                    "updatedAt": r["updated_at"],
                })
            return jsonify({"ok": True, "characters": characters})

        # POST: Save character list
        data = request.get_json(silent=True) or {}
        char_list = data.get("characters") if isinstance(data, dict) else data
        if not isinstance(char_list, list):
            char_list = [data] if isinstance(data, dict) and data.get("ign") else []

        now_iso = datetime.utcnow().isoformat()
        # Clear existing and replace with new state
        conn.execute("DELETE FROM user_saved_characters WHERE user_id = ?", (user_id,))
        for idx, item in enumerate(char_list[:10]):
            c_id = str(item.get("id") or f"char_{int(time.time()*1000)}_{idx}")
            ign = str(item.get("ign") or "").strip()[:32]
            island = str(item.get("islandName") or item.get("island_name") or "").strip()[:32]
            if not ign:
                continue
            title = str(item.get("title") or "Island Resident").strip()[:40]
            icon = str(item.get("icon") or "fa-leaf").strip()[:32]
            is_def = 1 if bool(item.get("isDefault") or item.get("is_default") or (idx == 0 and len(char_list) == 1)) else 0
            created_at = str(item.get("createdAt") or item.get("created_at") or now_iso)

            conn.execute("""
                INSERT OR REPLACE INTO user_saved_characters
                (user_id, id, ign, island_name, title, icon, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, c_id, ign, island, title, icon, is_def, created_at, now_iso))

        conn.commit()
        return jsonify({"ok": True, "success": True, "count": len(char_list)})
    except Exception as exc:
        logger.warning("Error in user_characters API: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/nickname", methods=["POST"])
@app.route("/api/profile/nickname", methods=["POST"])
@app.route("/api/user/update-nickname", methods=["POST"])
@app.route("/api/profile/update-nickname", methods=["POST"])
def api_update_discord_nickname():
    """Update the authenticated user's Discord server nickname via Discord Bot API."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "success": False, "error": "Authentication required"}), 401

    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "success": False, "error": "Discord User ID not found"}), 401

    data = request.get_json(silent=True) or {}
    new_nickname = str(data.get("nickname") or data.get("nick") or "").strip()

    if not new_nickname:
        return jsonify({"ok": False, "success": False, "error": "Nickname cannot be empty."}), 400

    if len(new_nickname) > 32:
        return jsonify({"ok": False, "success": False, "error": "Discord nicknames cannot exceed 32 characters."}), 400

    bot_auth = _discord_bot_auth_value()
    guild_id = str(Config.GUILD_ID or "").strip()
    if not bot_auth or not guild_id:
        return jsonify({
            "ok": False,
            "success": False,
            "error": "Discord bot or server ID is not configured on this backend.",
        }), 503

    discord_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
    req_body = json.dumps({"nick": new_nickname}).encode("utf-8")

    try:
        resp = discord_request(
            discord_url,
            method="PATCH",
            headers={
                "Authorization": bot_auth,
                "Content-Type": "application/json",
            },
            data=req_body,
            timeout=10,
        )
        logger.info("Successfully updated Discord nickname for user_id=%s to '%s'", user_id, new_nickname)
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode(errors="replace")
        except Exception:
            body_text = str(exc)
        logger.warning("Discord API nickname update failed (status=%s) for user_id=%s: %s", exc.code, user_id, body_text)

        if exc.code == 403:
            return jsonify({
                "ok": False,
                "success": False,
                "error": "Discord permission denied: The bot cannot change nicknames for the server owner or roles higher than the bot.",
            }), 403
        elif exc.code == 404:
            return jsonify({
                "ok": False,
                "success": False,
                "error": "You are not currently a member of the ChoPaeng Discord server.",
            }), 404
        elif exc.code == 400:
            return jsonify({
                "ok": False,
                "success": False,
                "error": "Discord rejected this nickname format or characters.",
            }), 400
        else:
            return jsonify({
                "ok": False,
                "success": False,
                "error": f"Discord API returned status {exc.code}.",
            }), exc.code
    except Exception as exc:
        logger.error("Unexpected error updating Discord nickname for user_id=%s: %s", user_id, exc)
        return jsonify({
            "ok": False,
            "success": False,
            "error": f"Failed to update Discord nickname: {str(exc)}",
        }), 500

    # Update in-memory auth token cache so subsequent profile calls return the new nickname immediately
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
        auth_user["nickname"] = new_nickname
        try:
            update_auth_user(token, auth_user)
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "success": True,
        "nickname": new_nickname,
        "nick": new_nickname,
        "message": f"Successfully updated your Discord server nickname to '{new_nickname}'!",
    })


@app.route("/api/user/passport", methods=["GET", "POST"])
@app.route("/api/profile/passport", methods=["GET", "POST"])
def api_user_passport():
    """Get or save authenticated user public passport and profile customizer data."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    username = str(auth_user.get("discord_name") or auth_user.get("username") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]
        if not username:
            username = user_id

    conn = get_db()
    try:
        if request.method == "GET":
            row = conn.execute(
                "SELECT * FROM user_public_passports WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            if not row and username:
                row = conn.execute(
                    "SELECT * FROM user_public_passports WHERE LOWER(username) = LOWER(?)",
                    (username,)
                ).fetchone()

            if not row:
                return jsonify({"ok": True, "passport": None})

            passport = {
                "username": row["username"],
                "isPublic": bool(row["is_public"]),
                "showCharacterAndIsland": bool(row["show_character_and_island"]),
                "pronouns": row["pronouns"] or "",
                "birthDay": row["birth_day"] or "1",
                "birthMonth": row["birth_month"] or "January",
                "nativeFruit": row["native_fruit"] or "Apple",
                "favouriteColour": row["favourite_colour"] or "#37b06d",
                "favouriteSong": row["favourite_song"] or "K.K. Cruisin'",
                "country": row["country"] or "Island Paradise",
                "language": row["language"] or "English",
                "personality": row["personality"] or "Normal",
                "hobbies": row["hobbies"] or "",
                "favouriteShowsFilms": row["favourite_shows_films"] or "",
                "aboutYou": row["about_you"] or "",
                "favouriteVillagers": json.loads(row["favourite_villagers"] or "[]"),
                "primaryIgn": row["primary_ign"] or "",
                "primaryIsland": row["primary_island"] or "",
                "avatarUrl": row.get("avatar_url") or "",
                "updatedAt": row["updated_at"],
            }
            return jsonify({"ok": True, "passport": passport})

        # POST: Save passport
        data = request.get_json(silent=True) or {}
        p = data.get("passport") or data.get("public_passport") or data
        uname = str(p.get("username") or username or "Resident").strip()[:50]
        is_pub = 1 if bool(p.get("isPublic") or p.get("is_public")) else 0
        show_char = 1 if bool(p.get("showCharacterAndIsland", p.get("show_character_and_island", True))) else 0
        pronouns = str(p.get("pronouns") or "")[:40]
        b_day = str(p.get("birthDay") or p.get("birth_day") or "1")[:5]
        b_month = str(p.get("birthMonth") or p.get("birth_month") or "January")[:20]
        fruit = str(p.get("nativeFruit") or p.get("native_fruit") or "Apple")[:20]
        colour = str(p.get("favouriteColour") or p.get("favourite_colour") or "#37b06d")[:20]
        song = str(p.get("favouriteSong") or p.get("favourite_song") or "K.K. Cruisin'")[:60]
        country = str(p.get("country") or "Island Paradise")[:60]
        lang = str(p.get("language") or "English")[:40]
        personality = str(p.get("personality") or "Normal")[:30]
        hobbies = str(p.get("hobbies") or "")[:120]
        shows = str(p.get("favouriteShowsFilms") or p.get("favourite_shows_films") or "")[:120]
        about = str(p.get("aboutYou") or p.get("about_you") or "")[:200]
        villagers = json.dumps(p.get("favouriteVillagers") or p.get("favourite_villagers") or [])
        p_ign = str(p.get("primaryIgn") or p.get("primary_ign") or "")[:32]
        p_island = str(p.get("primaryIsland") or p.get("primary_island") or "")[:32]
        avatar_url = str(p.get("avatarUrl") or p.get("avatar_url") or "")[:500]
        now_iso = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT OR REPLACE INTO user_public_passports (
                user_id, username, is_public, show_character_and_island, pronouns,
                birth_day, birth_month, native_fruit, favourite_colour, favourite_song,
                country, language, personality, hobbies, favourite_shows_films,
                about_you, favourite_villagers, primary_ign, primary_island, avatar_url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, uname, is_pub, show_char, pronouns,
            b_day, b_month, fruit, colour, song,
            country, lang, personality, hobbies, shows,
            about, villagers, p_ign, p_island, avatar_url, now_iso
        ))
        conn.commit()

        return jsonify({
            "ok": True,
            "success": True,
            "passport": {
                "username": uname,
                "isPublic": bool(is_pub),
                "showCharacterAndIsland": bool(show_char),
                "pronouns": pronouns,
                "birthDay": b_day,
                "birthMonth": b_month,
                "nativeFruit": fruit,
                "favouriteColour": colour,
                "favouriteSong": song,
                "country": country,
                "language": lang,
                "personality": personality,
                "hobbies": hobbies,
                "favouriteShowsFilms": shows,
                "aboutYou": about,
                "favouriteVillagers": json.loads(villagers),
                "primaryIgn": p_ign,
                "primaryIsland": p_island,
                "updatedAt": now_iso,
            }
        })
    except Exception as exc:
        logger.warning("Error saving user passport: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/public/passport/<username>", methods=["GET"])
@app.route("/api/user/passport/<username>", methods=["GET"])
def api_public_passport_get(username: str):
    """Retrieve public passport data for any user by Discord username or account ID."""
    clean_uname = urllib.parse.unquote(username).strip()
    if not clean_uname:
        return jsonify({"error": "Username is required"}), 400

    auth_user = _current_auth_user()
    current_uid = str(auth_user.get("user_id") or "") if auth_user else ""
    current_uname = str(auth_user.get("discord_name") or auth_user.get("username") or "").lower() if auth_user else ""

    conn = get_db()
    try:
        row = conn.execute(
            """SELECT * FROM user_public_passports 
               WHERE LOWER(username) = LOWER(?) OR user_id = ?""",
            (clean_uname, clean_uname)
        ).fetchone()

        if not row:
            return jsonify({"error": "Passport not found", "passport": None}), 404

        is_owner = (current_uid and row["user_id"] == current_uid) or (current_uname and row["username"].lower() == current_uname)
        if not bool(row["is_public"]) and not is_owner:
            return jsonify({"error": "This profile is private", "isPrivate": True}), 403

        passport = {
            "username": row["username"],
            "isPublic": bool(row["is_public"]),
            "showCharacterAndIsland": bool(row["show_character_and_island"]),
            "pronouns": row["pronouns"] or "",
            "birthDay": row["birth_day"] or "1",
            "birthMonth": row["birth_month"] or "January",
            "nativeFruit": row["native_fruit"] or "Apple",
            "favouriteColour": row["favourite_colour"] or "#37b06d",
            "favouriteSong": row["favourite_song"] or "K.K. Cruisin'",
            "country": row["country"] or "Island Paradise",
            "language": row["language"] or "English",
            "personality": row["personality"] or "Normal",
            "hobbies": row["hobbies"] or "",
            "favouriteShowsFilms": row["favourite_shows_films"] or "",
            "aboutYou": row["about_you"] or "",
            "favouriteVillagers": json.loads(row["favourite_villagers"] or "[]"),
            "primaryIgn": row["primary_ign"] if bool(row["show_character_and_island"]) or is_owner else "",
            "primaryIsland": row["primary_island"] if bool(row["show_character_and_island"]) or is_owner else "",
            "updatedAt": row["updated_at"],
        }
        return jsonify({"ok": True, "passport": passport})
    except Exception as exc:
        logger.warning("Error fetching public passport for %s: %s", clean_uname, exc)
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# REAL-TIME ONLINE PRESENCE & COMMUNITY RADAR ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_presence_table(conn):
    """Ensure user_online_presence table exists with correct schema."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_online_presence (
            session_id          VARCHAR(64) PRIMARY KEY,
            user_id             VARCHAR(64),
            username            VARCHAR(255) NOT NULL,
            display_name        VARCHAR(255),
            avatar_url          TEXT,
            role                VARCHAR(32) DEFAULT 'resident',
            status              VARCHAR(32) DEFAULT 'online',
            current_activity    VARCHAR(255),
            current_path        VARCHAR(255),
            current_island      VARCHAR(64),
            ign                 VARCHAR(64),
            island_name         VARCHAR(64),
            native_fruit        VARCHAR(32),
            has_public_passport INTEGER DEFAULT 0,
            is_guest            INTEGER DEFAULT 0,
            ip_hash             VARCHAR(64),
            last_heartbeat      INTEGER NOT NULL,
            created_at          INTEGER NOT NULL
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS ix_presence_last_heartbeat ON user_online_presence (last_heartbeat)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_presence_user_id ON user_online_presence (user_id)")
    except Exception:
        pass


def _ensure_waves_table(conn):
    """Ensure user_waves table exists for real-time cross-user wave delivery."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_waves (
            id                VARCHAR(64) PRIMARY KEY,
            from_username     VARCHAR(255) NOT NULL,
            from_display_name VARCHAR(255) NOT NULL,
            from_avatar_url   TEXT,
            to_username       VARCHAR(255) NOT NULL,
            to_display_name   VARCHAR(255),
            to_session_id     VARCHAR(64),
            status            VARCHAR(32) DEFAULT 'pending',
            created_at        INTEGER NOT NULL,
            expires_at        INTEGER NOT NULL
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS ix_waves_to_user ON user_waves (to_username, status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_waves_to_sess ON user_waves (to_session_id, status, created_at)")
    except Exception:
        pass


@app.route("/api/presence/heartbeat", methods=["POST"])
@app.route("/api/community/heartbeat", methods=["POST"])
def api_presence_heartbeat():
    """Register or refresh active user presence session."""
    data = request.get_json(silent=True) or {}
    current_path = str(data.get("path") or data.get("currentPath") or "/").strip()
    client_activity = str(data.get("activity") or data.get("currentActivity") or "").strip()
    session_id = str(data.get("sessionId") or request.headers.get("x-session-id") or "").strip()
    current_island = str(data.get("currentIsland") or "").strip()

    now = int(time.time())
    auth_user = _current_auth_user()
    conn = get_db()
    try:
        _ensure_presence_table(conn)

        # 1. Prune stale sessions older than 90 seconds
        conn.execute("DELETE FROM user_online_presence WHERE last_heartbeat < ?", (now - 90,))

        # 2. Extract identity
        if auth_user:
            user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or auth_user.get("id") or "")
            username = str(auth_user.get("username") or auth_user.get("discord_name") or "")
            display_name = str(auth_user.get("nickname") or auth_user.get("name") or username)
            avatar_url = str(auth_user.get("avatar") or auth_user.get("avatar_url") or "")
            is_admin = bool(auth_user.get("is_admin"))
            is_mod = bool(auth_user.get("is_mod"))
            role = "admin" if is_admin else "mod" if is_mod else "member"
            is_guest = 0
            if not session_id:
                session_id = f"usr_{user_id}"

            # Check passport for enrichments
            ign = str(data.get("ign") or "").strip()
            island_name = str(data.get("islandName") or "").strip()
            native_fruit = str(data.get("nativeFruit") or "Peach").strip()
            has_public_passport = 0

            pass_row = conn.execute(
                "SELECT * FROM user_public_passports WHERE user_id = ? OR LOWER(username) = LOWER(?)",
                (user_id, username)
            ).fetchone()
            if pass_row:
                has_public_passport = int(bool(pass_row["is_public"]))
                if not ign:
                    ign = pass_row["primary_ign"] or ""
                if not island_name:
                    island_name = pass_row["primary_island"] or ""
                if pass_row["native_fruit"]:
                    native_fruit = pass_row["native_fruit"]
                if not avatar_url and pass_row["avatar_url"]:
                    avatar_url = pass_row["avatar_url"]
        else:
            ip = request.remote_addr or "127.0.0.1"
            ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:12]
            if not session_id:
                session_id = f"guest_{ip_hash}_{now}"
            user_id = None
            short_id = session_id[-4:] if len(session_id) >= 4 else "0001"
            username = f"Resident #{short_id}"
            display_name = username
            avatar_url = "https://acnhcdn.com/latest/NpcIcon/der00.png"
            role = "resident"
            is_guest = 1
            ign = str(data.get("ign") or "").strip()
            island_name = str(data.get("islandName") or "").strip()
            native_fruit = str(data.get("nativeFruit") or "Peach").strip()
            has_public_passport = 0

        # Determine status & activity
        if current_island or "/island/" in current_path or "/islands" in current_path:
            status = "on_island"
            activity = client_activity or (f"Exploring {current_island}" if current_island else "Viewing Treasure Islands")
        elif "/order" in current_path:
            status = "ordering"
            activity = client_activity or "Crafting Order Bot Item Bag"
        elif "/profile" in current_path:
            status = "online"
            activity = client_activity or "Styling Resident Passport Studio"
        elif "/catalog" in current_path:
            status = "online"
            activity = client_activity or "Browsing 40,000+ ACNH Catalogue"
        elif "/find" in current_path:
            status = "online"
            activity = client_activity or "Searching Island Stock Finder"
        else:
            status = "online"
            activity = client_activity or "Browsing ChoPaeng"

        # UPSERT active session
        conn.execute("""
            INSERT INTO user_online_presence (
                session_id, user_id, username, display_name, avatar_url, role, status,
                current_activity, current_path, current_island, ign, island_name,
                native_fruit, has_public_passport, is_guest, ip_hash, last_heartbeat, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                user_id = excluded.user_id,
                username = excluded.username,
                display_name = excluded.display_name,
                avatar_url = COALESCE(NULLIF(excluded.avatar_url, ''), user_online_presence.avatar_url),
                role = excluded.role,
                status = excluded.status,
                current_activity = excluded.current_activity,
                current_path = excluded.current_path,
                current_island = excluded.current_island,
                ign = COALESCE(NULLIF(excluded.ign, ''), user_online_presence.ign),
                island_name = COALESCE(NULLIF(excluded.island_name, ''), user_online_presence.island_name),
                native_fruit = COALESCE(NULLIF(excluded.native_fruit, ''), user_online_presence.native_fruit),
                has_public_passport = excluded.has_public_passport,
                is_guest = excluded.is_guest,
                last_heartbeat = excluded.last_heartbeat
        """, (
            session_id, user_id, username, display_name, avatar_url, role, status,
            activity, current_path, current_island, ign, island_name,
            native_fruit, has_public_passport, is_guest,
            hashlib.sha256((request.remote_addr or "127.0.0.1").encode()).hexdigest()[:16],
            now, now
        ))
        conn.commit()

        # Check for pending waves directed to this user or session
        _ensure_waves_table(conn)
        incoming_waves = []
        try:
            wave_rows = conn.execute("""
                SELECT * FROM user_waves
                WHERE status = 'pending'
                  AND expires_at >= ?
                  AND (
                      (to_username IS NOT NULL AND LOWER(to_username) = LOWER(?))
                      OR (to_session_id IS NOT NULL AND to_session_id = ?)
                  )
                ORDER BY created_at ASC
            """, (now, username, session_id)).fetchall()

            if wave_rows:
                wave_ids = []
                for w in wave_rows:
                    wave_ids.append(w["id"])
                    incoming_waves.append({
                        "id": w["id"],
                        "fromUsername": w["from_username"],
                        "fromDisplayName": w["from_display_name"],
                        "fromAvatarUrl": w["from_avatar_url"] or "",
                        "toUsername": w["to_username"],
                        "toDisplayName": w["to_display_name"] or "",
                        "timestamp": w["created_at"] * 1000,
                    })
                q_marks = ",".join(["?"] * len(wave_ids))
                conn.execute(f"UPDATE user_waves SET status = 'delivered' WHERE id IN ({q_marks})", wave_ids)
                conn.commit()
        except Exception as wave_err:
            logger.warning("[Presence] Error checking waves on heartbeat: %s", wave_err)

        # Get active count within last 60s
        row_count = conn.execute(
            "SELECT COUNT(DISTINCT session_id) as c FROM user_online_presence WHERE last_heartbeat >= ?",
            (now - 60,)
        ).fetchone()
        online_count = row_count["c"] if row_count else 1

        return jsonify({
            "ok": True,
            "success": True,
            "sessionId": session_id,
            "online_count": online_count,
            "activeOnlineCount": online_count,
            "pending_waves": incoming_waves,
            "timestamp": now,
        })
    except Exception as exc:
        logger.warning("[Presence] Error recording heartbeat: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/presence/leave", methods=["POST"])
@app.route("/api/community/leave", methods=["POST"])
def api_presence_leave():
    """Remove active presence session when user navigates away or closes tab."""
    data = request.get_json(silent=True) or {}
    session_id = str(data.get("sessionId") or request.headers.get("x-session-id") or request.args.get("sessionId") or "").strip()
    if not session_id:
        auth_user = _current_auth_user()
        if auth_user:
            uid = str(auth_user.get("user_id") or auth_user.get("discord_id") or "")
            session_id = f"usr_{uid}"

    if session_id:
        conn = get_db()
        try:
            conn.execute("DELETE FROM user_online_presence WHERE session_id = ? OR user_id = ?", (session_id, session_id.replace("usr_", "")))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    return jsonify({"ok": True, "success": True})


@app.route("/api/presence/online", methods=["GET"])
@app.route("/api/community/online", methods=["GET"])
def api_presence_online():
    """Retrieve currently active residents for Who's Online roster."""
    now = int(time.time())
    conn = get_db()
    try:
        _ensure_presence_table(conn)

        # Prune dead sessions older than 60 seconds
        conn.execute("DELETE FROM user_online_presence WHERE last_heartbeat < ?", (now - 60,))
        conn.commit()

        # Query all active sessions within last 60 seconds
        rows = conn.execute("""
            SELECT * FROM user_online_presence
            WHERE last_heartbeat >= ?
            ORDER BY
                is_guest ASC,
                CASE role
                    WHEN 'admin' THEN 1
                    WHEN 'mod' THEN 2
                    WHEN 'member' THEN 3
                    ELSE 4
                END,
                last_heartbeat DESC
            LIMIT 50
        """, (now - 60,)).fetchall()

        residents = []
        for r in rows:
            minutes_ago = max(0, int((now - (r["created_at"] or now)) / 60))
            residents.append({
                "id": r["session_id"],
                "username": r["username"],
                "displayName": r["display_name"] or r["username"],
                "avatarUrl": r["avatar_url"] or "https://acnhcdn.com/latest/NpcIcon/der00.png",
                "ign": r["ign"] or "Resident",
                "islandName": r["island_name"] or "Island",
                "nativeFruit": r["native_fruit"] or "Peach",
                "role": r["role"] or "resident",
                "status": r["status"] or "online",
                "currentActivity": r["current_activity"] or "Browsing ChoPaeng",
                "currentIsland": r["current_island"] or "",
                "currentPath": r["current_path"] or "/",
                "hasPublicPassport": bool(r["has_public_passport"]),
                "isGuest": bool(r["is_guest"]),
                "joinedMinutesAgo": minutes_ago,
                "lastHeartbeat": r["last_heartbeat"],
            })

        total_online = len(residents)
        auth_count = sum(1 for r in residents if not r["isGuest"])
        guest_count = total_online - auth_count

        return jsonify({
            "ok": True,
            "success": True,
            "total_online": total_online,
            "activeOnlineCount": total_online,
            "authenticated_count": auth_count,
            "guest_count": guest_count,
            "residents": residents,
            "timestamp": now,
        })
    except Exception as exc:
        logger.warning("[Presence] Error fetching online presence: %s", exc)
        return jsonify({"ok": False, "error": str(exc), "residents": []}), 500
    finally:
        conn.close()


@app.route("/api/presence/wave", methods=["POST"])
@app.route("/api/community/wave", methods=["POST"])
def api_presence_send_wave():
    """Receive an incoming wave directed to another user/resident."""
    data = request.get_json(silent=True) or {}
    to_username = str(data.get("toUsername") or data.get("to_username") or "").strip()
    to_display_name = str(data.get("toDisplayName") or data.get("to_display_name") or "").strip()
    to_session_id = str(data.get("toSessionId") or data.get("to_session_id") or "").strip()

    if not to_username and not to_session_id:
        return jsonify({"ok": False, "error": "Target user or session is required"}), 400

    auth_user = _current_auth_user()
    now = int(time.time())

    from_username = str(data.get("fromUsername") or data.get("from_username") or "").strip()
    from_display_name = str(data.get("fromDisplayName") or data.get("from_display_name") or "").strip()
    from_avatar_url = str(data.get("fromAvatarUrl") or data.get("from_avatar_url") or "").strip()

    if auth_user:
        if not from_username:
            from_username = str(auth_user.get("username") or auth_user.get("discord_name") or "")
        if not from_display_name:
            from_display_name = str(auth_user.get("nickname") or auth_user.get("name") or from_username)
        if not from_avatar_url:
            from_avatar_url = str(auth_user.get("avatar") or auth_user.get("avatar_url") or "")

    if not from_username:
        sess = str(request.headers.get("x-session-id") or data.get("sessionId") or "").strip()
        short = sess[-4:] if len(sess) >= 4 else "0001"
        from_username = f"Resident #{short}"
    if not from_display_name:
        from_display_name = from_username
    if not from_avatar_url:
        from_avatar_url = "https://acnhcdn.com/latest/NpcIcon/der00.png"

    wave_id = "wave_" + hashlib.sha256(f"{from_username}:{to_username}:{to_session_id}:{now}:{time.time()}".encode()).hexdigest()[:16]
    expires_at = now + 120  # Wave valid for 2 minutes

    conn = get_db()
    try:
        _ensure_waves_table(conn)
        conn.execute("""
            INSERT INTO user_waves (
                id, from_username, from_display_name, from_avatar_url,
                to_username, to_display_name, to_session_id, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            wave_id, from_username, from_display_name, from_avatar_url,
            to_username, to_display_name, to_session_id, now, expires_at
        ))
        conn.commit()

        wave_payload = {
            "id": wave_id,
            "fromUsername": from_username,
            "fromDisplayName": from_display_name,
            "fromAvatarUrl": from_avatar_url,
            "toUsername": to_username,
            "toDisplayName": to_display_name,
            "toSessionId": to_session_id,
            "timestamp": now * 1000,
        }
        return jsonify({
            "ok": True,
            "success": True,
            "wave": wave_payload,
        })
    except Exception as exc:
        logger.warning("[Presence] Error storing wave: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/presence/waves", methods=["GET"])
@app.route("/api/community/waves", methods=["GET"])
def api_presence_poll_waves():
    """Poll for pending waves directed to this user/session."""
    auth_user = _current_auth_user()
    now = int(time.time())
    session_id = str(request.headers.get("x-session-id") or request.args.get("sessionId") or "").strip()
    username = ""
    if auth_user:
        username = str(auth_user.get("username") or auth_user.get("discord_name") or "").strip()

    if not username and not session_id:
        return jsonify({"ok": True, "waves": []})

    conn = get_db()
    try:
        _ensure_waves_table(conn)
        wave_rows = conn.execute("""
            SELECT * FROM user_waves
            WHERE status = 'pending'
              AND expires_at >= ?
              AND (
                  (to_username IS NOT NULL AND to_username != '' AND LOWER(to_username) = LOWER(?))
                  OR (to_session_id IS NOT NULL AND to_session_id != '' AND to_session_id = ?)
              )
            ORDER BY created_at ASC
        """, (now, username, session_id)).fetchall()

        waves = []
        if wave_rows:
            wave_ids = []
            for w in wave_rows:
                wave_ids.append(w["id"])
                waves.append({
                    "id": w["id"],
                    "fromUsername": w["from_username"],
                    "fromDisplayName": w["from_display_name"],
                    "fromAvatarUrl": w["from_avatar_url"] or "",
                    "toUsername": w["to_username"],
                    "toDisplayName": w["to_display_name"] or "",
                    "toSessionId": w["to_session_id"] or "",
                    "timestamp": w["created_at"] * 1000,
                })
            q_marks = ",".join(["?"] * len(wave_ids))
            conn.execute(f"UPDATE user_waves SET status = 'delivered' WHERE id IN ({q_marks})", wave_ids)
            conn.commit()

        return jsonify({"ok": True, "success": True, "waves": waves})
    except Exception as exc:
        logger.warning("[Presence] Error polling waves: %s", exc)
        return jsonify({"ok": False, "waves": []})
    finally:
        conn.close()


# -------------------------------------------------------------
# ISLAND NHL MAP & SECTOR RADAR ROUTES
# -------------------------------------------------------------
@app.route("/api/islands/<island_name>/map", methods=["GET"])
def api_get_island_map(island_name: str):
    """
    Returns full parsed item and sector layout for an island.
    Reads from VILLAGERS_DIR/<island_name>/nhl/maprefresh.nhl or
    TWITCH_VILLAGERS_DIR/<island_name>/nhl/maprefresh.nhl.
    """
    from utils.nhl_map_parser import get_island_map_data
    force = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    data = get_island_map_data(island_name, force_refresh=force)
    return jsonify({"ok": True, **data})


@app.route("/api/islands/<island_name>/map/search", methods=["GET"])
def api_search_island_map(island_name: str):
    """
    Searches items or categories on an island map to return exact coordinates and sectors.
    """
    from utils.nhl_map_parser import search_island_items
    q = request.args.get("q", "").strip()
    result = search_island_items(island_name, q)
    return jsonify({"ok": True, **result})


@app.route("/api/islands/maps", methods=["GET"])
def api_list_island_maps():
    """
    Lists available islands and the detection status of their maprefresh.nhl.
    """
    from utils.nhl_map_parser import locate_nhl_file
    islands_to_check = [
        "SILAKBO", "TALA", "SINAGTALA", "TADHANA", "TINIG", "DALANGIN", "HIRAYA", "LUMINA", "MUTYA"
    ]
    status_list = []
    for isl in islands_to_check:
        filepath, exists, checked = locate_nhl_file(isl)
        status_list.append({
            "island": isl,
            "has_nhl": exists,
            "file_path": filepath if exists else None,
            "expected_paths": checked,
        })
    return jsonify({"ok": True, "islands": status_list})



def run_flask_app(host='0.0.0.0', port=8100):
    """Run Flask app with retry logic for port binding after OTA restart."""
    logger.info(f"[FLASK] Starting API server on {host}:{port}...")
    max_retries = 5
    retry_delay = 3  # seconds between attempts
    for attempt in range(1, max_retries + 1):
        try:
            # ThreadedWSGIServer already sets SO_REUSEADDR before binding.
            # Using it directly (instead of app.run) gives explicit control
            # and allows retrying when the port is still in TIME_WAIT after
            # an os.execv()-based OTA restart.
            server = ThreadedWSGIServer(host, port, app)
            logger.info(f"[FLASK] API server listening on {host}:{port}")
            server.serve_forever()
            return
        except OSError as e:
            if attempt < max_retries:
                logger.warning(
                    f"[FLASK] Port {port} not available (attempt {attempt}/{max_retries}): {e}. "
                    f"Retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
            else:
                logger.error(
                    f"[FLASK] Failed to bind to port {port} after {max_retries} attempts: {e}"
                )
                raise

