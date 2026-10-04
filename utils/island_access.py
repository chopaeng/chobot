"""Shared island access helpers backed by Discord channel overwrites.

The member island channel permissions are the source of truth.  The local
``islands.required_roles`` column is treated as a cache/fallback so the API can
continue serving access decisions when Discord is temporarily unavailable.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from utils.config import Config
from utils.discord_http import request as discord_request
from utils.helpers import clean_text

logger = logging.getLogger("IslandAccess")

DISCORD_USER_AGENT = "DiscordBot (https://chopaeng.com, 1.0)"
ADMINISTRATOR_PERM = 0x8
VIEW_CHANNEL_PERM = 1 << 10

ROLE_NAME_CACHE_TTL = 3600
CHANNEL_OVERWRITE_CACHE_TTL = 300
GUILD_CHANNELS_CACHE_TTL = 300

_role_name_cache: dict[str, tuple[dict[str, str], float]] = {}
_channel_overwrite_cache: dict[str, tuple[list[str] | None, str | None, float]] = {}
_guild_channels_cache: tuple[list[dict[str, Any]], float] | None = None


@dataclass(frozen=True)
class IslandAccessInfo:
    required_roles: list[str]
    channel_id: str | None
    access_source: str

    @property
    def role_count(self) -> int:
        return len(self.required_roles)


def clear_access_caches() -> None:
    """Clear Discord access caches, usually before an admin-triggered sync."""
    global _guild_channels_cache
    _role_name_cache.clear()
    _channel_overwrite_cache.clear()
    _guild_channels_cache = None


def is_mod(roles: list[str] | set[str] | tuple[str, ...]) -> bool:
    """True if the user holds one of the configured moderator roles."""
    mod_ids = {
        str(Config.ADMIN_ROLE_ID or ""),
        str(Config.SENIOR_MOD_ROLE_ID or ""),
        str(Config.BABY_MOD_ROLE_ID or ""),
    } - {"", "0", "None"}
    return bool(mod_ids & {str(role_id) for role_id in roles})


def has_island_access(roles: list[str], required_roles: list[str], is_mod_user: bool = False) -> bool:
    """Return whether a viewer can access an island.

    An empty ``required_roles`` list means no roles are configured, so access
    is denied for regular users (mods/admins are always granted access).
    """
    if is_mod_user or is_mod(roles):
        return True
    if not required_roles:
        return False  # no roles configured → deny non-mods
    return bool(set(required_roles) & {str(role_id) for role_id in roles})


def configured_subscription_role_ids() -> list[str]:
    """Return configured fallback subscription role IDs for member-only islands."""
    role_ids: list[str] = []
    for attr_name in (
        "ISLAND_ACCESS_ROLE",
        "SUBSCRIPTION_ROLE_ID",
        "SUBSCRIPTION_ROLE_IDS",
        "MEMBER_ROLE_ID",
        "MEMBER_ROLE_IDS",
    ):
        value = getattr(Config, attr_name, None)
        if value in (None, "", "0", "None"):
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if item not in (None, "", "0", "None"):
                    role_ids.append(str(item))
        else:
            role_ids.append(str(value))
    return list(dict.fromkeys(role_ids))


def excluded_profile_role_ids() -> set[str]:
    """Role IDs that should not appear as user subscription roles."""
    excluded = {
        str(Config.GUILD_ID or ""),
        str(Config.ISLAND_BOT_ROLE_ID or ""),
    }
    return {role_id for role_id in excluded if role_id and role_id not in {"0", "None"}}


def is_member_island(cat: str | None, island_type: str | None = None) -> bool:
    """Return whether an island should be gated behind member roles."""
    return (cat or "").strip().lower() == "member" or (island_type or "").strip().upper() == "VIP"


def effective_island_required_roles(
    cat: str | None,
    required_roles: list[str] | None,
    island_type: str | None = None,
) -> list[str]:
    """Return explicit island roles, or configured member roles for member/VIP islands."""
    roles = [str(role_id) for role_id in (required_roles or []) if str(role_id)]
    if is_member_island(cat, island_type) and not roles:
        roles = configured_subscription_role_ids()
    return roles


def discord_bot_auth_value() -> str | None:
    token = str(Config.DISCORD_TOKEN or "").strip()
    if not token:
        return None
    return token if token.lower().startswith("bot ") else f"Bot {token}"


def discord_api_json(path: str, timeout: int = 10) -> dict | list | None:
    auth_value = discord_bot_auth_value()
    if not auth_value:
        return None
    try:
        resp = discord_request(
            f"https://discord.com/api/v10{path}",
            headers={"Authorization": auth_value, "User-Agent": DISCORD_USER_AGENT},
            timeout=timeout,
        )
        return json.loads(resp.body)
    except Exception as exc:
        logger.warning("Discord API request failed for %s: %s", path, exc)
        return None


def get_guild_role_names() -> dict[str, str]:
    """Fetch guild role ID -> name mapping from Discord, cached briefly."""
    guild_id = str(Config.GUILD_ID or "")
    if not guild_id or not Config.DISCORD_TOKEN:
        return {}

    now = time.monotonic()
    cached = _role_name_cache.get(guild_id)
    if cached and now - cached[1] < ROLE_NAME_CACHE_TTL:
        return cached[0]

    payload = discord_api_json(f"/guilds/{guild_id}/roles")
    if not isinstance(payload, list):
        return cached[0] if cached else {}

    role_names = {
        str(role.get("id")): str(role.get("name") or role.get("id"))
        for role in payload
        if isinstance(role, dict) and role.get("id") and role.get("name") != "@everyone"
    }
    _role_name_cache[guild_id] = (role_names, now)
    return role_names


def role_payload(role_id: str, role_names: dict[str, str] | None = None) -> dict:
    """Small role object for API responses."""
    role_names = role_names or {}
    return {
        "id": str(role_id),
        "name": role_names.get(str(role_id), str(role_id)),
    }


def discord_channel_overwrite_roles(channel_id: str | None, *, force_refresh: bool = False) -> tuple[list[str] | None, str | None]:
    """Return role IDs allowed to view a channel from Discord, or None when unavailable."""
    if not channel_id:
        return None, None

    channel_key = str(channel_id)
    now = time.monotonic()
    cached = _channel_overwrite_cache.get(channel_key)
    if not force_refresh and cached and now - cached[2] < CHANNEL_OVERWRITE_CACHE_TTL:
        return cached[0], cached[1]

    payload = discord_api_json(f"/channels/{channel_key}")
    if not isinstance(payload, dict):
        _channel_overwrite_cache[channel_key] = (None, None, now)
        return None, None

    role_ids: list[str] = []
    for overwrite in payload.get("permission_overwrites") or []:
        if str(overwrite.get("type")) not in {"0", "role"}:
            continue
        role_id = str(overwrite.get("id") or "")
        if not role_id or role_id == str(Config.GUILD_ID or ""):
            continue
        try:
            allow_bits = int(overwrite.get("allow") or 0)
        except (TypeError, ValueError):
            allow_bits = 0
        if allow_bits & VIEW_CHANNEL_PERM:
            role_ids.append(role_id)

    resolved = list(dict.fromkeys(role_ids))
    resolved_channel_id = str(payload.get("id") or channel_key)
    _channel_overwrite_cache[channel_key] = (resolved, resolved_channel_id, now)
    return resolved, resolved_channel_id


def discord_guild_channels(*, force_refresh: bool = False) -> list[dict[str, Any]] | None:
    global _guild_channels_cache
    guild_id = str(Config.GUILD_ID or "")
    if not guild_id:
        return None

    now = time.monotonic()
    if not force_refresh and _guild_channels_cache and now - _guild_channels_cache[1] < GUILD_CHANNELS_CACHE_TTL:
        return _guild_channels_cache[0]

    payload = discord_api_json(f"/guilds/{guild_id}/channels")
    if not isinstance(payload, list):
        return None

    channels = [channel for channel in payload if isinstance(channel, dict)]
    _guild_channels_cache = (channels, now)
    return channels


def canonical_island_key(value: str | None) -> str:
    """Normalize an island/channel name for Discord channel matching."""
    return re.sub(r"^\d+", "", clean_text(value or ""))


def find_discord_island_channel_id(island_name: str | None, *, force_refresh: bool = False) -> str | None:
    """Find a member island channel by normalized island name in the configured sub category."""
    island_clean = canonical_island_key(island_name)
    if not island_clean:
        return None

    category_id = str(Config.CATEGORY_ID or "")
    channels = discord_guild_channels(force_refresh=force_refresh)
    if not channels:
        return None

    for channel in channels:
        if category_id and str(channel.get("parent_id") or "") != category_id:
            continue
        channel_clean = canonical_island_key(str(channel.get("name") or ""))
        if channel_clean == island_clean:
            return str(channel.get("id") or "")
    return None


def resolved_island_required_roles(
    island_name: str | None,
    cat: str | None,
    required_roles: list[str] | None,
    island_type: str | None = None,
    channel_id: str | None = None,
    *,
    force_refresh: bool = False,
) -> IslandAccessInfo:
    """Resolve island access roles using live Discord channel overwrites first."""
    if is_member_island(cat, island_type):
        resolved_channel_id = str(channel_id) if channel_id else None
        dynamic_roles: list[str] | None = None
        if resolved_channel_id:
            dynamic_roles, fetched_channel_id = discord_channel_overwrite_roles(
                resolved_channel_id,
                force_refresh=force_refresh,
            )
            resolved_channel_id = fetched_channel_id or resolved_channel_id
        if dynamic_roles is None:
            found_channel_id = find_discord_island_channel_id(island_name, force_refresh=force_refresh)
            if found_channel_id:
                dynamic_roles, fetched_channel_id = discord_channel_overwrite_roles(
                    found_channel_id,
                    force_refresh=force_refresh,
                )
                resolved_channel_id = fetched_channel_id or found_channel_id
        # Only trust Discord overwrites when they contain at least one role.
        # An empty list means the channel has no explicit role grants, so fall
        # through to the DB/configured-subscription-role fallback below.
        if dynamic_roles:
            return IslandAccessInfo(dynamic_roles, resolved_channel_id, "discord_channel")

    return IslandAccessInfo(
        effective_island_required_roles(cat, required_roles, island_type),
        channel_id,
        "database",
    )


def sync_island_role_cache(conn, islands: list[dict], *, force_refresh: bool = True) -> dict:
    """Refresh ``islands.required_roles`` and ``channel_id`` from Discord overwrites."""
    if force_refresh:
        clear_access_caches()

    synced = 0
    skipped = 0
    errors: list[dict] = []
    items: list[dict] = []

    for island in islands:
        island_id = str(island.get("id") or island.get("name") or "").lower()
        island_name = island.get("name") or island_id
        if not island_id:
            skipped += 1
            continue
        if not is_member_island(island.get("cat"), island.get("type")):
            skipped += 1
            continue
        info = resolved_island_required_roles(
            island_name,
            island.get("cat"),
            island.get("required_roles", []),
            island.get("type"),
            island.get("channel_id"),
            force_refresh=force_refresh,
        )
        if info.access_source != "discord_channel":
            skipped += 1
            errors.append({
                "id": island_id,
                "name": island_name,
                "error": "Discord channel permissions unavailable; kept database roles",
            })
            continue
        conn.execute(
            "UPDATE islands SET required_roles = ?, channel_id = ?, updated_at = ? WHERE id = ?",
            (json.dumps(info.required_roles), info.channel_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), island_id),
        )
        synced += 1
        items.append({
            "id": island_id,
            "name": island_name,
            "channel_id": info.channel_id,
            "required_roles": info.required_roles,
            "role_count": info.role_count,
            "access_source": info.access_source,
        })

    return {
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "items": items,
    }
