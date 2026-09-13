import json
"""
Discord Command Bot Module
Handles Discord commands for item and villager search with rich embeds
"""

import asyncio
import contextlib
import os
import subprocess
import time
import re
import random
import logging
from datetime import datetime, timezone, timedelta
from itertools import cycle

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from thefuzz import process, fuzz

from utils.config import Config
from utils.database import connect_db
from utils.helpers import normalize_text, get_best_suggestions, clean_text
from utils.island_access import configured_subscription_role_ids, is_mod, resolved_island_required_roles
from utils.nookipedia import NookipediaClient
from utils.nickname_format import is_valid_acnh_nickname, nickname_warning_for, NICKNAME_FORMAT_EXAMPLE
from utils.chopaeng_ai import get_ai_answer, conversation_store, add_chat_message
from utils.ops_status import create_sqlite_backup, get_maintenance_settings
from chorder import ChorderCog, OrderPanelView, build_panel_embed, sysbot, resolve_guild_icon

logger = logging.getLogger("DiscordCommandBot")

# Island status check constants
DODO_CODE_PATTERN = re.compile(r'\b[A-HJ-NP-Z0-9]{5}\b')
MENTION_PATTERN = re.compile(r'<@!?\d+>')
ISLAND_HOST_NAME = "chopaeng"
MESSAGE_HISTORY_LIMIT = 30
ISLAND_DOWN_IMAGE_URL = "https://cdn.chopaeng.com/misc/Bot-is-Down.jpg"
ONLINE_DISCORD_STATUSES = {discord.Status.online, discord.Status.idle, discord.Status.dnd}

ISLAND_STATUS_DISPLAY = {
    "ONLINE": ("\U0001F7E2", "Online", "green"),        # 🟢
    "OFFLINE": ("\U0001F534", "Offline", "red"),        # 🔴
    "REFRESHING": ("\U0001F7E1", "Refreshing", "orange"),  # 🟡
}

# Patterns for intercepting island bot responses
ISLAND_VISITORS_PATTERN = re.compile(r"The following visitors are on (.+?):", re.IGNORECASE)
ISLAND_VILLAGERS_PATTERN = re.compile(r"The following villagers are on (.+?):", re.IGNORECASE)
ISLAND_DODO_SENT_PATTERN = re.compile(r".+?:\s*Sent you the dodo code via DM", re.IGNORECASE)
ISLAND_DROP_PATTERN = re.compile(r"Item drop request will be executed momentarily", re.IGNORECASE)
ISLAND_INJECT_QUEUED_PATTERN = re.compile(r"Villager inject request has been added to the queue", re.IGNORECASE)
ISLAND_INJECT_MULTI_QUEUED_PATTERN = re.compile(r"Villager inject request for (\d+) villagers?", re.IGNORECASE)
ISLAND_INJECT_COMPLETE_PATTERN = re.compile(r"(.+?) has been injected by the bot at Index (\d+)", re.IGNORECASE)
VISITOR_LINE_PATTERN = re.compile(r'#\d+:\s*(.+)')
DODO_UPDATE_NOTIFICATION_PATTERN = re.compile(r"\[\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}\s+(?:am|pm)\]\s+The Dodo code for .+ has updated, the new Dodo code is:", re.IGNORECASE)
AVAILABLE_SLOT_TEXT = "available slot"
ISLAND_BOT_INTERCEPT_TIMEOUT = 10  # seconds to wait for island bot response
GIT_OUTPUT_MAX_LENGTH = 1900  # max chars of git output to display in Discord
DODO_XLOG_TIMEOUT = 1800  # seconds to wait for a verified flight before posting the dodo-request xlog

AUTO_REPLY_PATTERNS = [
    # Direct question/help intents
    re.compile(r"\b(?:i\s+(?:have|got)\s+(?:a\s+)?question|quick\s+question)\b", re.IGNORECASE),
    re.compile(r"\b(?:can|could|would)\s+you\s+(?:help|explain|tell|check)\b", re.IGNORECASE),
    re.compile(r"\b(?:i\s+need|need|looking\s+for|want)\s+(?:help|assistance|support|advice|tips?)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:help|help\s+me|support|question)\s*[!.?]*\s*$", re.IGNORECASE),

    # Asking the room
    re.compile(r"\b(?:does|do|did|can|could|would|will|is|are)\s+(?:anyone|anybody|someone|somebody)\s+(?:know|help|explain|have|see)\b", re.IGNORECASE),
    re.compile(r"\b(?:anyone|anybody|someone|somebody)\s+(?:know|help|able\s+to\s+help|have\s+an\s+idea)\b", re.IGNORECASE),

    # How/where/what style questions
    re.compile(r"\bhow\s+(?:do|can|to|should|would)\s+(?:i|we|you)\b", re.IGNORECASE),
    re.compile(r"\bwhere\s+(?:can|do|is|are|should)\s+(?:i|we|you|get|find|go)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:is|are|do|does|should|happens|if)\b", re.IGNORECASE),
    re.compile(r"\b(?:can|should|do|does|is)\s+(?:i|this|that|it)\b", re.IGNORECASE),
    re.compile(r"\bwhy\s+(?:is|are|do|does|did|can|can't|cant)\b", re.IGNORECASE),

    # Problem/advice seeking
    re.compile(r"\b(?:i'?m|i\s+am|im)\s+(?:stuck|lost|confused|struggling)\b", re.IGNORECASE),
    re.compile(r"\b(?:having|have|got|getting|there'?s|there\s+is)\s+(?:a\s+)?(?:problem|issue|error|trouble)\b", re.IGNORECASE),
    re.compile(r"\b(?:any|some)\s+(?:tips|advice|ideas|suggestions|recommendations)\b", re.IGNORECASE),
    re.compile(r"\b(?:best|fastest|easiest)\s+way\s+to\b", re.IGNORECASE),
]

NICKNAME_SUBMISSION_CHANNEL_ID = 1081147108612124742
FREE_DODO_BOARD_INTERVAL_SECONDS = 60
FREE_DODO_BOARD_EMBEDS_PER_MESSAGE = 10
FREE_DODO_BOARD_MARKER = "Chopaeng Camp™"

ISLAND_REVIVE_BATCH_PATH = os.getenv(
    "ISLAND_RESTART_BAT_PATH",
    r"C:\Users\ChoPaeng\Desktop\Relaunch_Island.bat",
)
DODO_SENT_TIPS = [
    "**DO NOT** share your order code with anyone else. Only the person who placed the order may visit that island. Sharing a code may result in a permanent bot ban.",
    "Free up your home storage before visiting so you can unload full pockets quickly and fly back for another run.",
    "Change your server nickname to the format: `Character Name | Island Name`. This helps the team identify you.",
    "**NEVER** press the minus (-) button to leave. Always walk to the airport and fly home through Orville. Leaving with the minus button may cause the island to crash for other visitors and you may lose items.",
    "NAT Type A or B is required for smooth online play. If you have NAT Type C or D you may experience connection problems — please resolve this before joining.",
    "Do not drop unwanted items on the ground. Trash bins are placed all over each island — please use them. Litter prevents islands from refreshing their item spawns for everyone.",
]

COMMAND_CLAIM_EXPIRY_SECONDS = 300  # 5 minutes


# Shared database path (project root, used when DB_BACKEND=sqlite)
_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chobot.db")


def build_island_status_sticky_payload(offline_islands: list[str]) -> tuple[str, str, str | None, discord.Color]:
    """Build the title/description/body for the sticky island-status embed."""
    normalized_islands = sorted({name.strip() for name in offline_islands if name and str(name).strip()})
    if normalized_islands:
        title = "⚠️ Offline Islands"
        description = "The following islands are currently offline:"
        field_value = "\n".join(f"• {name}" for name in normalized_islands)
        color = discord.Color.red()
    else:
        title = "✅ All Islands Online"
        description = "All monitored islands are currently online."
        field_value = None
        color = discord.Color.green()
    return title, description, field_value, color


def _upsert_bot_status(island_id: str, island_name: str, is_online: bool) -> None:
    """Persist the Discord bot online/offline status for an island to the DB.

    Writes to the ``island_bot_status`` table so that the REST API can expose
    live Discord presence data without making Discord API calls itself.
    """
    try:
        conn = connect_db()
        try:
            conn.execute(
                """INSERT INTO island_bot_status (island_id, island_name, is_online, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(island_id) DO UPDATE SET
                       island_name=excluded.island_name,
                       is_online=excluded.is_online,
                       updated_at=excluded.updated_at""",
                (island_id, island_name, 1 if is_online else 0, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to write island_bot_status for {island_name}: {exc}")


def _init_command_claims_db() -> None:
    """Create the command_claims table used for cross-instance deduplication."""
    try:
        with connect_db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS command_claims (
                    message_id INTEGER PRIMARY KEY,
                    claimed_at REAL NOT NULL
                )"""
            )
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to init command_claims table: {exc}")



def _init_settings_db() -> None:
    """Create the settings table for general bot configuration."""
    try:
        with connect_db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to init settings table: {exc}")


def _resolve_island_by_channel(self, channel_id: int) -> str | None:
    """Look up the island name whose channel_id matches *channel_id*, straight
    from the `islands` table. Returns None if this channel isn't linked to
    any island (e.g. a general chat channel)."""
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT name FROM islands WHERE channel_id = ?",
                (str(channel_id),),
            ).fetchone()
            return row.get("name") if row else None
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to resolve island for channel {channel_id}: {exc}")
        return None


def _get_setting(key: str, default: str = "") -> str:
    """Retrieve a setting value from the DB."""
    try:
        with connect_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to get setting '{key}': {exc}")
        return default


def _set_setting(key: str, value: str) -> None:
    """Save a setting value to the DB."""
    try:
        with connect_db() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to set setting '{key}': {exc}")



def _try_claim_command(message_id: int) -> bool:
    """Attempt to claim a message ID for command processing.

    Uses a SQLite unique constraint so that only one bot instance (or one
    invocation within the same instance) can process a given Discord message.

    Returns True if this call is the first to claim the message (caller should
    proceed), False if it was already claimed (caller should skip).
    On any database error, returns True so the command is never silently lost.
    """
    try:
        now = time.time()
        with connect_db() as conn:
            conn.execute(
                "DELETE FROM command_claims WHERE claimed_at < ?",
                (now - COMMAND_CLAIM_EXPIRY_SECONDS,),
            )
            # INSERT OR IGNORE silently does nothing when the PRIMARY KEY already
            # exists (i.e. another instance already claimed this message_id).
            # cursor.rowcount is 1 on a successful insert and 0 on a no-op, so
            # it reliably distinguishes "first claim" from "duplicate".
            cursor = conn.execute(
                "INSERT OR IGNORE INTO command_claims (message_id, claimed_at) VALUES (?, ?)",
                (message_id, now),
            )
            return cursor.rowcount > 0
    except Exception as exc:
        logger.error(f"[DISCORD] command_claims check failed for {message_id}: {exc}")
        return True


def _get_member_role_ids(member: discord.abc.Snowflake) -> set[str]:
    """Return the set of role IDs for a Discord member-like object."""
    return {
        str(role.id)
        for role in getattr(member, "roles", [])
        if getattr(role, "id", None) is not None
    }


def _is_subscriber_member(member: discord.abc.Snowflake) -> bool:
    """Return whether the member holds any configured subscriber/member role."""
    member_roles = _get_member_role_ids(member)
    subscription_role_ids = set(configured_subscription_role_ids())
    return bool(member_roles & subscription_role_ids)


def _is_mod_member(member: discord.abc.Snowflake) -> bool:
    """Return whether the member holds any configured moderator/admin role."""
    member_roles = _get_member_role_ids(member)
    return is_mod(member_roles)


def _has_animal_crossing_role(member: discord.abc.Snowflake) -> bool:
    """Return True when the member holds the Animal Crossing role.

    Members without this role cannot see #chorder-bot, #chorder-rules, or
    related order channels, so Chobot should guide them to #get-roles first.
    """
    ac_role_id = str(Config.ANIMAL_CROSSING_ROLE_ID)
    return ac_role_id in _get_member_role_ids(member)


def _get_accessible_islands(member: discord.abc.Snowflake) -> list[str]:
    """Return a list of sub island names that the member can access."""
    import json
    user_role_ids = _get_member_role_ids(member)
    is_mod_user = is_mod(user_role_ids)
    
    accessible: list[str] = []
    
    try:
        with connect_db() as conn:
            rows = conn.execute("SELECT name, is_visible, cat, type, required_roles, channel_id FROM islands").fetchall()
            for row in rows:
                if row.get("is_visible") is False:
                    continue
                
                req_roles_raw = row.get("required_roles")
                req_roles = json.loads(req_roles_raw) if req_roles_raw else []
                
                info = resolved_island_required_roles(
                    row.get("name"),
                    row.get("cat"),
                    req_roles,
                    row.get("type"),
                    row.get("channel_id")
                )
                
                # If no required roles, it's public.
                if not info.required_roles:
                    accessible.append(row.get("name"))
                    continue
                    
                if is_mod_user:
                    accessible.append(row.get("name"))
                    continue
                    
                if set(info.required_roles) & user_role_ids:
                    accessible.append(row.get("name"))
    except Exception as exc:
        logger.error(f"[DISCORD] Error fetching accessible islands for {member.id}: {exc}")
        
    return accessible


def _discord_conv_key(message: discord.Message) -> str:
    """Return a stable per-user-per-channel key for conversation history."""
    guild_id = message.guild.id if message.guild else "dm"
    return f"discord:{guild_id}:{message.channel.id}:{message.author.id}"

def is_admin_or_senior_mod():
    async def predicate(ctx):
        if getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.administrator:
            return True

        roles = getattr(ctx.author, "roles", [])
        admin_role_ids = {Config.ADMIN_ROLE_ID, Config.SENIOR_MOD_ROLE_ID, Config.BABY_MOD_ROLE_ID}
        if any(getattr(r, "id", None) in admin_role_ids for r in roles):
            return True

        return _is_mod_member(ctx.author)

    return commands.check(predicate)

class SuggestionSelect(discord.ui.Select):
    """Dropdown select for choosing from suggestions"""

    def __init__(self, cog, suggestions, search_type):
        self.cog = cog
        self.search_type = search_type

        options = [
            discord.SelectOption(label=str(disp)[:100], value=str(norm_key)[:100])
            for (norm_key, disp) in suggestions[:25]
        ]

        super().__init__(
            placeholder="Select the correct item...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle selection"""
        selected_key = self.values[0]

        with self.cog.data_manager.lock:
            display_name = self.cog.data_manager.cache.get("_display", {}).get(
                selected_key, selected_key.title()
            )

        found_locations = None
        is_villager = False

        if self.search_type == "item":
            with self.cog.data_manager.lock:
                found_locations = self.cog.data_manager.cache.get(selected_key)
            is_villager = False
        elif self.search_type == "villager":
            v_map = self.cog.data_manager.get_villagers([
                Config.VILLAGERS_DIR,
                Config.TWITCH_VILLAGERS_DIR
            ])
            found_locations = v_map.get(selected_key)
            is_villager = True

        if found_locations:
            nooki_data = None
            if is_villager:
                nooki_data = await NookipediaClient.get_villager_info(display_name)

            island_map, _ = await self.cog._fetch_islands_api_snapshot()
            embed = self.cog.create_found_embed(
                interaction, display_name, found_locations, is_villager, nooki_data,
                island_map=island_map or {}
            )

            result_view = FindResultView(
                cog=self.cog,
                last_query=selected_key,
                last_display=display_name,
                search_type=self.search_type,
                author_id=interaction.user.id,
            )

            if embed:
                send_embeds = [embed]
                if is_villager and nooki_data:
                    house_embed = self.cog.create_villager_house_embed(interaction, display_name, nooki_data)
                    if house_embed:
                        send_embeds.append(house_embed)
                await interaction.response.edit_message(
                    content=f"Hey <@{interaction.user.id}>, look what I found!",
                    embeds=send_embeds,
                    view=result_view,
                )
            else:
                await interaction.response.edit_message(
                    content=f"**{display_name}** is not currently available on any Sub Island.",
                    embed=None,
                    view=result_view,
                )
        else:
            await interaction.response.send_message(
                "Error: Item data lost. Please try searching again.",
                ephemeral=True
            )


class SuggestionView(discord.ui.View):
    """View containing suggestion dropdown"""

    def __init__(self, cog, suggestions, search_type, author_id):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.add_item(SuggestionSelect(cog, suggestions, search_type))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only requester can use the menu"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu is for the requester only.",
                ephemeral=True
            )
            return False
        return True


class FindQueryModal(discord.ui.Modal, title="🔍 Search Items & Villagers"):
    """Modal for interactive find queries, triggered from FindResultView's Search Again button."""

    query = discord.ui.TextInput(
        label="Item or Villager Name",
        placeholder="e.g. Ironwood Kitchenette, Raymond, gold axe...",
        required=True,
        min_length=2,
        max_length=80,
    )

    def __init__(self, cog, search_type: str, author_id: int):
        super().__init__()
        self.cog = cog
        self.search_type = search_type  # "item" or "villager"
        self.author_id = author_id

    async def on_submit(self, interaction: discord.Interaction):
        """Run the find logic for the entered query and edit the original message in place."""
        await interaction.response.defer()

        raw = self.query.value.strip()
        search_term = normalize_text(raw)

        if self.search_type == "villager":
            villager_map = self.cog.data_manager.get_villagers([
                Config.VILLAGERS_DIR,
                Config.TWITCH_VILLAGERS_DIR
            ])
            found_locations = villager_map.get(search_term)

            if found_locations:
                nooki_data = await NookipediaClient.get_villager_info(search_term)
                island_map, _ = await self.cog._fetch_islands_api_snapshot()
                embed = self.cog.create_found_embed(
                    interaction, search_term, found_locations, is_villager=True,
                    nooki_data=nooki_data, island_map=island_map or {}
                )
                result_view = FindResultView(
                    cog=self.cog, last_query=search_term, last_display=raw,
                    search_type="villager", author_id=self.author_id
                )
                if embed:
                    send_embeds = [embed]
                    if nooki_data:
                        house_embed = self.cog.create_villager_house_embed(interaction, search_term, nooki_data)
                        if house_embed:
                            send_embeds.append(house_embed)
                    await interaction.edit_original_response(
                        content=f"Hey <@{self.author_id}>, look who I found!",
                        embeds=send_embeds, view=result_view
                    )
                else:
                    await interaction.edit_original_response(
                        content=f"**{raw.title()}** is not currently on any Sub Island.",
                        embed=None, view=result_view
                    )
                return

            # No hit — show suggestions
            matches = process.extract(search_term, list(villager_map.keys()), limit=3, scorer=fuzz.WRatio)
            suggestions = [(m[0], m[0].title()) for m in matches if m[1] > 75]
            embed_fail = self.cog.create_fail_embed(interaction, raw, [s[1] for s in suggestions], is_villager=True)
            if suggestions:
                view = SuggestionView(self.cog, suggestions, "villager", self.author_id)
                await interaction.edit_original_response(
                    content=f"Hey <@{self.author_id}>...", embed=embed_fail, view=view
                )
            else:
                await interaction.edit_original_response(
                    content=f"Hey <@{self.author_id}>...", embed=embed_fail, view=None
                )

        else:  # item (default)
            with self.cog.data_manager.lock:
                cache = self.cog.data_manager.cache
                keys = [k for k in cache.keys() if k != "_display"]
                found_locations = cache.get(search_term)

            if found_locations:
                with self.cog.data_manager.lock:
                    display_name = self.cog.data_manager.cache.get("_display", {}).get(search_term, raw)
                island_map, _ = await self.cog._fetch_islands_api_snapshot()
                embed = self.cog.create_found_embed(
                    interaction, display_name, found_locations, is_villager=False,
                    island_map=island_map or {}
                )
                result_view = FindResultView(
                    cog=self.cog, last_query=search_term, last_display=display_name,
                    search_type="item", author_id=self.author_id
                )
                if embed:
                    await interaction.edit_original_response(
                        content=f"Hey <@{self.author_id}>, look what I found!",
                        embed=embed, view=result_view
                    )
                else:
                    await interaction.edit_original_response(
                        content=f"**{display_name}** is not currently available on any Sub Island.",
                        embed=None, view=result_view
                    )
                return

            suggestion_keys = get_best_suggestions(search_term, keys, limit=8)
            with self.cog.data_manager.lock:
                display_map = self.cog.data_manager.cache.get("_display", {})
            suggestions = [(k, display_map.get(k, k)) for k in suggestion_keys]
            embed_fail = self.cog.create_fail_embed(interaction, raw, [disp for _, disp in suggestions])
            if suggestions:
                view = SuggestionView(self.cog, suggestions, "item", self.author_id)
                await interaction.edit_original_response(
                    content=f"Hey <@{self.author_id}>...", embed=embed_fail, view=view
                )
            else:
                await interaction.edit_original_response(
                    content=f"Hey <@{self.author_id}>...", embed=embed_fail, view=None
                )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.exception(f"[FindQueryModal] Error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Something went wrong. Please try again.", ephemeral=True)


class FindResultView(discord.ui.View):
    """Persistent button row attached to every find/villager result.

    Buttons:
      🔍 Search Again  — opens FindQueryModal so the user can type a new query
      🔄 Refresh       — re-runs the same search against live data
      🎲 Random        — shows a random available item
    """

    def __init__(self, cog, last_query: str, last_display: str, search_type: str, author_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.last_query = last_query        # normalised key used for the last search
        self.last_display = last_display    # human-readable display name
        self.search_type = search_type      # "item" or "villager"
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "These buttons belong to the original requester.", ephemeral=True
            )
            return False
        return True

    # ── Search Again ─────────────────────────────────────────────────────────

    @discord.ui.button(label="Search Again", style=discord.ButtonStyle.primary, emoji="🔍", row=0)
    async def btn_search_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Open a modal so the user can type a new search term without retyping the command."""
        modal = FindQueryModal(
            cog=self.cog,
            search_type=self.search_type,
            author_id=self.author_id,
        )
        await interaction.response.send_modal(modal)

    # ── Refresh ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=0)
    async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Re-run the same query against live data and update the message."""
        await interaction.response.defer()

        if self.search_type == "villager":
            villager_map = self.cog.data_manager.get_villagers([
                Config.VILLAGERS_DIR,
                Config.TWITCH_VILLAGERS_DIR
            ])
            found_locations = villager_map.get(self.last_query)
            if found_locations:
                nooki_data = await NookipediaClient.get_villager_info(self.last_query)
                island_map, _ = await self.cog._fetch_islands_api_snapshot()
                embed = self.cog.create_found_embed(
                    interaction, self.last_query, found_locations, is_villager=True,
                    nooki_data=nooki_data, island_map=island_map or {}
                )
                if embed:
                    send_embeds = [embed]
                    if nooki_data:
                        house_embed = self.cog.create_villager_house_embed(interaction, self.last_query, nooki_data)
                        if house_embed:
                            send_embeds.append(house_embed)
                    await interaction.edit_original_response(
                        content=f"🔄 Refreshed — Hey <@{self.author_id}>, look who I found!",
                        embeds=send_embeds, view=self
                    )
                else:
                    await interaction.edit_original_response(
                        content=f"🔄 Refreshed — **{self.last_display.title()}** is not on any Sub Island right now.",
                        embed=None, view=self
                    )
            else:
                await interaction.edit_original_response(
                    content=f"🔄 Refreshed — **{self.last_display.title()}** could not be found.",
                    embed=None, view=self
                )
        else:  # item
            with self.cog.data_manager.lock:
                found_locations = self.cog.data_manager.cache.get(self.last_query)
                display_name = self.cog.data_manager.cache.get("_display", {}).get(
                    self.last_query, self.last_display
                )
            if found_locations:
                island_map, _ = await self.cog._fetch_islands_api_snapshot()
                embed = self.cog.create_found_embed(
                    interaction, display_name, found_locations, is_villager=False,
                    island_map=island_map or {}
                )
                if embed:
                    await interaction.edit_original_response(
                        content=f"🔄 Refreshed — Hey <@{self.author_id}>, look what I found!",
                        embed=embed, view=self
                    )
                else:
                    await interaction.edit_original_response(
                        content=f"🔄 Refreshed — **{display_name}** is not available on any Sub Island right now.",
                        embed=None, view=self
                    )
            else:
                await interaction.edit_original_response(
                    content=f"🔄 Refreshed — **{self.last_display}** could not be found.",
                    embed=None, view=self
                )

    # ── Random ────────────────────────────────────────────────────────────────

    @discord.ui.button(label="Random", style=discord.ButtonStyle.secondary, emoji="🎲", row=0)
    async def btn_random(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show a random available item from the cache."""
        await interaction.response.defer()

        with self.cog.data_manager.lock:
            cache = self.cog.data_manager.cache
            all_items = [k for k in cache.keys() if not k.startswith("_")]
            display_map = cache.get("_display", {})

        if not all_items:
            await interaction.edit_original_response(
                content="No items in cache yet. Try again later!", embed=None, view=self
            )
            return

        random_key = random.choice(all_items)
        display_name = display_map.get(random_key, random_key.title())
        found_locations = None
        with self.cog.data_manager.lock:
            found_locations = self.cog.data_manager.cache.get(random_key)

        random_view = FindResultView(
            cog=self.cog, last_query=random_key, last_display=display_name,
            search_type="item", author_id=self.author_id
        )

        if found_locations:
            island_map, _ = await self.cog._fetch_islands_api_snapshot()
            embed = self.cog.create_found_embed(
                interaction, display_name, found_locations, is_villager=False,
                island_map=island_map or {}
            )
            if embed:
                embed.title = f"🎲 Random: {display_name}"
                await interaction.edit_original_response(
                    content=f"🎲 Hey <@{self.author_id}>, here's a random item!",
                    embed=embed, view=random_view
                )
                return

        await interaction.edit_original_response(
            content=f"🎲 Random suggestion: **{display_name}** — try `!find {display_name}` to check availability!",
            embed=None, view=random_view
        )

    async def on_timeout(self):
        """Disable all buttons when the view times out."""
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True


# ── Find Station Panel ────────────────────────────────────────────────────────


def build_find_panel_embed(guild_icon: str | None = None) -> discord.Embed:
    """Build the persistent Find Station panel embed."""
    embed = discord.Embed(
        title=f"{Config.EMOJI_SEARCH} Find Station",
        description=(
            "Welcome to the **Chobot Find Station**!\n"
            "Use the buttons below to search for items and villagers across all Sub Islands, "
            "or roll the dice for a surprise pick.\n\n"
            f"{Config.STAR_PINK} **Items** — Furniture, clothing, DIY recipes, and more\n"
            f"{Config.STAR_PINK} **Villagers** — Find which island a villager is currently on\n"
            f"{Config.STAR_PINK} **Random** — Discover something new"
        ),
        color=discord.Color.teal(),
    )
    if guild_icon:
        embed.set_author(name="ChoPaeng Camp", icon_url=guild_icon)
        embed.set_thumbnail(url=guild_icon)
    else:
        embed.set_thumbnail(url="https://nh-cdn.catalogue.ac/NpcIcon/brd09.png")
    if getattr(Config, "FOOTER_LINE", None):
        embed.set_image(url=Config.FOOTER_LINE)
    icon = guild_icon or getattr(Config, "DEFAULT_PFP", None) or "https://nh-cdn.catalogue.ac/NpcIcon/cat23.png"
    embed.set_footer(text="Chopaeng Camp™ • Powered by Chobot", icon_url=icon)
    return embed


class FindPanelView(discord.ui.View):
    """Persistent, channel-deployable Find Station panel.

    Mirrors the OrderPanelView pattern — deployed by /findpanel and survives
    bot restarts because all buttons carry stable custom_ids and timeout=None.
    """

    def __init__(self):
        super().__init__(timeout=None)  # Persistent — never times out

    # ── Search Items ──────────────────────────────────────────────────────────

    @discord.ui.button(
        label="Search Items",
        style=discord.ButtonStyle.primary,
        emoji="🔍",
        custom_id="find_panel_btn_items",
        row=0,
    )
    async def btn_search_items(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Open the item search modal."""
        modal = _FindPanelItemModal()
        await interaction.response.send_modal(modal)

    # ── Search Villagers ──────────────────────────────────────────────────────

    @discord.ui.button(
        label="Search Villagers",
        style=discord.ButtonStyle.secondary,
        emoji="🐾",
        custom_id="find_panel_btn_villagers",
        row=0,
    )
    async def btn_search_villagers(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Open the villager search modal."""
        modal = _FindPanelVillagerModal()
        await interaction.response.send_modal(modal)

    # ── Random ────────────────────────────────────────────────────────────────

    @discord.ui.button(
        label="Random Item",
        style=discord.ButtonStyle.secondary,
        emoji="🎲",
        custom_id="find_panel_btn_random",
        row=0,
    )
    async def btn_random(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Show a random item from the cache."""
        await interaction.response.defer(ephemeral=True)
        cog: DiscordCommandCog | None = None
        if interaction.client:
            cog = interaction.client.cogs.get("DiscordCommandCog")

        if not cog:
            await interaction.followup.send("Bot not ready yet. Try again in a moment.", ephemeral=True)
            return

        with cog.data_manager.lock:
            cache = cog.data_manager.cache
            all_items = [k for k in cache.keys() if not k.startswith("_")]
            display_map = cache.get("_display", {})

        if not all_items:
            await interaction.followup.send("No items cached yet. Try again shortly!", ephemeral=True)
            return

        random_key = random.choice(all_items)
        display_name = display_map.get(random_key, random_key.title())
        with cog.data_manager.lock:
            found_locations = cog.data_manager.cache.get(random_key)

        result_view = FindResultView(
            cog=cog,
            last_query=random_key,
            last_display=display_name,
            search_type="item",
            author_id=interaction.user.id,
        )

        if found_locations:
            island_map, _ = await cog._fetch_islands_api_snapshot()
            embed = cog.create_found_embed(
                interaction, display_name, found_locations, is_villager=False,
                island_map=island_map or {}
            )
            if embed:
                embed.title = f"🎲 Random: {display_name}"
                await interaction.followup.send(
                    content=f"🎲 Hey <@{interaction.user.id}>, here's a random item!",
                    embed=embed, view=result_view, ephemeral=True
                )
                return

        await interaction.followup.send(
            content=f"🎲 Random suggestion: **{display_name}** — click Search Again to check availability!",
            view=result_view, ephemeral=True
        )


class _FindPanelItemModal(discord.ui.Modal, title="🔍 Search Items"):
    """Item search modal triggered from FindPanelView."""

    query = discord.ui.TextInput(
        label="Item Name",
        placeholder="e.g. Ironwood Table, Gold Axe, Crescent Moon Chair...",
        required=True,
        min_length=2,
        max_length=80,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog: DiscordCommandCog | None = (
            interaction.client.cogs.get("DiscordCommandCog") if interaction.client else None
        )
        if not cog:
            await interaction.followup.send("❌ Bot not ready. Try again in a moment.", ephemeral=True)
            return

        raw = self.query.value.strip()
        search_term = normalize_text(raw)

        with cog.data_manager.lock:
            cache = cog.data_manager.cache
            keys = [k for k in cache.keys() if k != "_display"]
            found_locations = cache.get(search_term)

        result_view = FindResultView(
            cog=cog, last_query=search_term, last_display=raw,
            search_type="item", author_id=interaction.user.id
        )

        if found_locations:
            with cog.data_manager.lock:
                display_name = cog.data_manager.cache.get("_display", {}).get(search_term, raw)
            island_map, _ = await cog._fetch_islands_api_snapshot()
            embed = cog.create_found_embed(
                interaction, display_name, found_locations, is_villager=False, island_map=island_map or {}
            )
            if embed:
                await interaction.followup.send(
                    content=f"Hey <@{interaction.user.id}>, look what I found!",
                    embed=embed, view=result_view, ephemeral=True
                )
            else:
                await interaction.followup.send(
                    content=f"**{display_name}** is not currently available on any Sub Island.",
                    view=result_view, ephemeral=True
                )
            return

        suggestion_keys = get_best_suggestions(search_term, keys, limit=8)
        with cog.data_manager.lock:
            display_map = cog.data_manager.cache.get("_display", {})
        suggestions = [(k, display_map.get(k, k)) for k in suggestion_keys]
        embed_fail = cog.create_fail_embed(interaction, raw, [disp for _, disp in suggestions])
        if suggestions:
            view = SuggestionView(cog, suggestions, "item", interaction.user.id)
            await interaction.followup.send(
                content=f"Hey <@{interaction.user.id}>...", embed=embed_fail, view=view, ephemeral=True
            )
        else:
            await interaction.followup.send(
                content=f"Hey <@{interaction.user.id}>...", embed=embed_fail, ephemeral=True
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.exception(f"[_FindPanelItemModal] Error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Something went wrong. Try again.", ephemeral=True)


class _FindPanelVillagerModal(discord.ui.Modal, title="🐾 Search Villagers"):
    """Villager search modal triggered from FindPanelView."""

    query = discord.ui.TextInput(
        label="Villager Name",
        placeholder="e.g. Raymond, Marshal, Fauna, Bob...",
        required=True,
        min_length=2,
        max_length=60,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog: DiscordCommandCog | None = (
            interaction.client.cogs.get("DiscordCommandCog") if interaction.client else None
        )
        if not cog:
            await interaction.followup.send("❌ Bot not ready. Try again in a moment.", ephemeral=True)
            return

        raw = self.query.value.strip()
        search_term = normalize_text(raw)

        villager_map = cog.data_manager.get_villagers([
            Config.VILLAGERS_DIR,
            Config.TWITCH_VILLAGERS_DIR
        ])
        found_locations = villager_map.get(search_term)

        result_view = FindResultView(
            cog=cog, last_query=search_term, last_display=raw,
            search_type="villager", author_id=interaction.user.id
        )

        if found_locations:
            nooki_data = await NookipediaClient.get_villager_info(search_term)
            island_map, _ = await cog._fetch_islands_api_snapshot()
            embed = cog.create_found_embed(
                interaction, search_term, found_locations, is_villager=True,
                nooki_data=nooki_data, island_map=island_map or {}
            )
            if embed:
                send_embeds = [embed]
                if nooki_data:
                    house_embed = cog.create_villager_house_embed(interaction, search_term, nooki_data)
                    if house_embed:
                        send_embeds.append(house_embed)
                await interaction.followup.send(
                    content=f"Hey <@{interaction.user.id}>, look who I found!",
                    embeds=send_embeds, view=result_view, ephemeral=True
                )
            else:
                await interaction.followup.send(
                    content=f"**{raw.title()}** is not currently on any Sub Island.",
                    view=result_view, ephemeral=True
                )
            return

        matches = process.extract(search_term, list(villager_map.keys()), limit=3, scorer=fuzz.WRatio)
        suggestions = [(m[0], m[0].title()) for m in matches if m[1] > 75]
        embed_fail = cog.create_fail_embed(interaction, raw, [s[1] for s in suggestions], is_villager=True)
        if suggestions:
            view = SuggestionView(cog, suggestions, "villager", interaction.user.id)
            await interaction.followup.send(
                content=f"Hey <@{interaction.user.id}>...", embed=embed_fail, view=view, ephemeral=True
            )
        else:
            await interaction.followup.send(
                content=f"Hey <@{interaction.user.id}>...", embed=embed_fail, ephemeral=True
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.exception(f"[_FindPanelVillagerModal] Error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Something went wrong. Try again.", ephemeral=True)

class RebootConfirmView(discord.ui.View):
    """Confirm/cancel buttons shown before actually rebooting an island."""

    def __init__(self, author_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.result: bool | None = None  # True = confirmed, False = cancelled, None = timed out
        self.interaction: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Reboot", style=discord.ButtonStyle.danger, emoji="🔄")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self.interaction = interaction
        self._disable_all()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self.interaction = interaction
        self._disable_all()
        self.stop()

    async def on_timeout(self) -> None:
        self.result = None
        self._disable_all()

class DiscordCommandCog(commands.Cog):
    """Cog for Discord treasure hunt commands"""

    def __init__(self, bot, data_manager):
        self.bot = bot
        self.data_manager = data_manager
        self.cooldowns = {}
        self.sub_island_lookup = {}
        self.free_island_lookup = {}
        self.order_island_lookup = {}
        self.free_dodo_board_messages: list[discord.Message] = []
        self.free_dodo_board_fingerprints: list[str] = []
        self.free_dodo_board_startup_cleanup_done = False
        self.island_status_sticky_startup_cleanup_done = False
        self._revive_lock = asyncio.Lock()
        self._island_sticky_lock = asyncio.Lock()
        self.island_status_sticky_message: discord.Message | None = None

        # island_clean -> True (down) / False (up); None = not yet initialized
        self.island_down_states: dict[str, bool | None] = {}
        # island_clean -> discord.Message of the sticky "island is down" embed
        self.island_down_messages: dict[str, discord.Message] = {}
        # island_clean -> list of members currently waiting for a !senddodo reply.
        # Populated by send_dodo; cleared by island_monitor_loop on offline transition.
        self.pending_dodo_waiters: dict[str, list[discord.Member]] = {}
        self.island_monitor_loop.start()
        self.free_dodo_board_loop.start()
        self.island_status_sticky_loop.start()

    def _refresh_order_island_lookup(self) -> None:
        """Refresh the fixed order-bot island lookup."""
        self.order_island_lookup = {}
        if Config.ORDER_BOT_CHANNEL_ID and Config.ORDER_BOT_ISLAND:
            self.order_island_lookup[clean_text(Config.ORDER_BOT_ISLAND)] = Config.ORDER_BOT_CHANNEL_ID


    async def _compute_island_status(
        self,
        guild: discord.Guild,
        include_sub: bool = True,
        include_free: bool = True,
        include_order: bool = True,
    ) -> dict:
        """Compute sub/free/order island status results.

        Shared by the sticky embed refresher and the /islands command so the
        two never drift out of sync with each other.
        """
        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None
        if Config.ISLAND_BOT_ROLE_ID and not island_bot_role:
            logger.warning(f"[DISCORD] ISLAND_BOT_ROLE_ID {Config.ISLAND_BOT_ROLE_ID} not found in guild; bot name matching disabled")

        # --- Sub island results ---
        sub_results: list = []
        sub_online = 0
        if include_sub:
            await self.fetch_islands()
            for island in Config.SUB_ISLANDS:
                island_clean = clean_text(island)
                channel_id = self.sub_island_lookup.get(island_clean)

                if not channel_id:
                    for ch in guild.channels:
                        if isinstance(ch, discord.TextChannel) and island_clean in clean_text(ch.name):
                            channel_id = ch.id
                            break

                if not channel_id:
                    sub_results.append((island, "❓", "Channel not found", None))
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    sub_results.append((island, "❓", "Channel not found", None))
                    continue

                island_bot = None
                if island_bot_role:
                    target = clean_text(f"chobot {island}")
                    for member in island_bot_role.members:
                        if member.bot and clean_text(member.display_name) == target:
                            island_bot = member
                            break

                if island_bot and island_bot.status in ONLINE_DISCORD_STATUSES:
                    sub_results.append((island, "✅", "Bot online", channel_id))
                    sub_online += 1
                    continue

                island_up = False
                status_reason = ""
                with connect_db() as conn:
                    row = conn.execute("SELECT dodo_code FROM islands WHERE id = ?", (island_clean,)).fetchone()
                    if row:
                        dodo_code = row.get("dodo_code")
                        if dodo_code and str(dodo_code).strip() not in ["", "00000", "-----", "GETTIN'"]:
                            island_up = True
                            status_reason = "Dodo code active"

                if island_up:
                    sub_results.append((island, "✅", status_reason, channel_id))
                    sub_online += 1
                else:
                    sub_results.append((island, "❌", "No recent activity", channel_id))

        # --- Free island results ---
        free_results: list = []
        free_online = 0
        if include_free:
            await self.fetch_free_islands()
            for island in Config.FREE_ISLANDS:
                island_clean = clean_text(island)
                channel_id = self.free_island_lookup.get(island_clean)

                island_bot = None
                if island_bot_role:
                    target = clean_text(f"chobot {island}")
                    for member in island_bot_role.members:
                        if member.bot and clean_text(member.display_name) == target:
                            island_bot = member
                            break

                if island_bot and island_bot.status in ONLINE_DISCORD_STATUSES:
                    free_results.append((island, "✅", "Bot online", channel_id))
                    free_online += 1
                elif island_bot:
                    free_results.append((island, "❌", "Bot offline", channel_id))
                else:
                    free_results.append((island, "❓", "Bot not found", channel_id))

        # --- Order island results ---
        order_results: list = []
        order_online = 0
        if include_order:
            self._refresh_order_island_lookup()
            order_bot_member = None
            if Config.ORDER_BOT_DISCORD_ID:
                order_bot_member = guild.get_member(Config.ORDER_BOT_DISCORD_ID)
                if order_bot_member is None:
                    try:
                        order_bot_member = await guild.fetch_member(Config.ORDER_BOT_DISCORD_ID)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        order_bot_member = None

            for island in getattr(Config, "ORDER_BOT_ISLANDS", []):
                island_clean = clean_text(island)
                channel_id = self.order_island_lookup.get(island_clean)
                display_name = "Sinta"  # order-bot island is always shown as "Sinta"

                if order_bot_member and order_bot_member.status in ONLINE_DISCORD_STATUSES:
                    order_results.append((display_name, "✅", "Bot online", channel_id))
                    order_online += 1
                elif order_bot_member:
                    order_results.append((display_name, "❌", "Bot offline", channel_id))
                else:
                    order_results.append((display_name, "❓", "Bot not found", channel_id))

        return {
            "sub_results": sub_results, "sub_online": sub_online, "sub_total": len(Config.SUB_ISLANDS),
            "free_results": free_results, "free_online": free_online, "free_total": len(Config.FREE_ISLANDS),
            "order_results": order_results, "order_online": order_online,
            "order_total": len(getattr(Config, "ORDER_BOT_ISLANDS", [])),
        }


    @app_commands.command(name="nick", description="Set your ACNH nickname and island name")
    @app_commands.describe(nickname="Format: Character Name | Island Name (e.g. ChoPaeng | ChoPaeng Camp)")
    async def nick_command(self, interaction: discord.Interaction, nickname: str):
        """Slash command to set nickname following the required format."""
        
        # Validate format
        if not is_valid_acnh_nickname(nickname.strip()):
            await interaction.response.send_message(
                "**Invalid Format!**\n"
                f"Please use: `{NICKNAME_FORMAT_EXAMPLE}`\n"
                "Example: `ChoPaeng | ChoPaeng Camp` (ensure you include the pipe `|` symbol)",
                ephemeral=True
            )
            return

        try:
            # Change the user's nickname in the guild
            await interaction.user.edit(nick=nickname.strip())
            await interaction.response.send_message(
                f"Your nickname has been set to: `{nickname.strip()}`",
                ephemeral=True
            )
            
            # Log usage if in the submission channel
            if interaction.channel_id == NICKNAME_SUBMISSION_CHANNEL_ID:
                logger.info(f"[DISCORD] {interaction.user} updated their nickname via /nick: {nickname.strip()}")

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ **Error:** I don't have permission to change your nickname.\n"
                "This usually happens if you are the Server Owner or have a higher role than the bot.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"[DISCORD] Failed to set nickname for {interaction.user}: {e}")
            await interaction.response.send_message(
                "❌ **Error:** Something went wrong while updating your nickname. Please contact staff.",
                ephemeral=True
            )

    async def item_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Filter items from cache for autocomplete"""
        try:
            if not current:
                # Return empty list for no input
                return []
            
            with self.data_manager.lock:
                # Filter out internal keys like _display and _index
                all_keys = [k for k in self.data_manager.cache.keys() if not k.startswith("_")]
                display_map = self.data_manager.cache.get("_display", {})
            
            # Limit the number of keys to search for performance
            # Discord autocomplete timeout is 3 seconds
            search_keys = all_keys[:5000] if len(all_keys) > 5000 else all_keys
            
            # Use fuzzy matching to find top matches
            matches = process.extract(current, search_keys, limit=25, scorer=fuzz.partial_ratio)
            
            choices = []
            for match_key, score in matches:
                if score > 50:
                    display_name = display_map.get(match_key, match_key.title())
                    # Truncate if too long (Discord limit is 100)
                    choices.append(app_commands.Choice(name=display_name[:100], value=match_key))
            
            return choices
        except Exception as e:
            logger.error(f"[DISCORD] Error in item_autocomplete: {e}")
            # Return empty list on error to prevent crashes
            return []

    async def fetch_islands(self):
        """Fetch island channels from Discord sub-category"""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            logger.error(f"[DISCORD] Guild {Config.GUILD_ID} not found.")
            return

        category = discord.utils.get(guild.categories, id=Config.CATEGORY_ID)
        if not category:
            logger.error(f"[DISCORD] Category {Config.CATEGORY_ID} not found.")
            return

        temp_lookup = {}
        fetched_islands = []
        count = 0

        for channel in category.channels:
            if channel.id == Config.IGNORE_CHANNEL_ID:
                continue

            chan_clean = clean_text(channel.name)
            if not chan_clean:
                continue

            # Strip leading digits to get the canonical island name
            # e.g. "01alapaap" -> "alapaap", "bituin" -> "bituin"
            island_clean = re.sub(r'^\d+', '', chan_clean)
            if island_clean:
                temp_lookup[island_clean] = channel.id
                fetched_islands.append(island_clean.title())
                count += 1
                
                req_roles = []
                for target, overwrite in channel.overwrites.items():
                    can_view = (
                        getattr(overwrite, "view_channel", None) is True
                        or getattr(overwrite, "read_messages", None) is True
                    )
                    if isinstance(target, discord.Role) and can_view:
                        if target.name != "@everyone":
                            req_roles.append(str(target.id))
                
                try:
                    import json
                    with connect_db() as conn:
                        conn.execute(
                            "UPDATE islands SET required_roles = ?, channel_id = ? WHERE UPPER(name) = ?",
                            (json.dumps(req_roles), str(channel.id), island_clean.upper())
                        )
                except Exception as e:
                    logger.error(f"[DISCORD] Failed to save required_roles for {island_clean}: {e}")

        self.sub_island_lookup = temp_lookup

        if fetched_islands:
            Config.SUB_ISLANDS = fetched_islands
            Config.TWITCH_SUB_ISLANDS = fetched_islands

        logger.info(f"[DISCORD] Dynamic Island Fetch Complete. Found {count} islands.")

    async def fetch_free_islands(self):
        """Fetch free island channels from the free-island Discord category."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            logger.error(f"[DISCORD] Guild {Config.GUILD_ID} not found.")
            return

        if not Config.FREE_CATEGORY_ID:
            logger.warning("[DISCORD] FREE_CATEGORY_ID not configured; free island lookup unavailable.")
            return

        category = discord.utils.get(guild.categories, id=Config.FREE_CATEGORY_ID)
        if not category:
            logger.error(f"[DISCORD] Free island category {Config.FREE_CATEGORY_ID} not found.")
            return

        temp_lookup = {}
        fetched_islands = []
        count = 0

        for channel in category.channels:
            chan_clean = clean_text(channel.name)
            if not chan_clean:
                continue

            # Strip leading digits to get the canonical island name.
            # Some free island channels use a numeric prefix (e.g. "01-kakanggata" → "kakanggata").
            island_clean = re.sub(r'^\d+', '', chan_clean)
            if island_clean:
                temp_lookup[island_clean] = channel.id
                fetched_islands.append(island_clean.title())
                count += 1

        self.free_island_lookup = temp_lookup

        if fetched_islands:
            Config.FREE_ISLANDS = fetched_islands

        logger.info(f"[DISCORD] Dynamic Free Island Fetch Complete. Found {count} islands.")

    def cog_unload(self):
        """Cleanup on unload"""
        self.island_monitor_loop.cancel()
        self.free_dodo_board_loop.cancel()
        self.island_status_sticky_loop.cancel()

    async def _refresh_island_status_sticky_message(
            self,
            channel: discord.TextChannel | None = None,
            force_repost: bool = False,
        ) -> None:
        async with self._island_sticky_lock:
            target_channel = channel or self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
            if not isinstance(target_channel, discord.TextChannel):
                return

            guild = target_channel.guild
            if not guild:
                return

            def _format_channel(island_name: str, channel_id: int | None) -> str:
                if not channel_id:
                    return f"**{island_name}**"
                ch = guild.get_channel(channel_id)
                return f"<#{channel_id}>" if ch else f"**{island_name}**"

            status = await self._compute_island_status(guild)
            sub_results, sub_online, sub_total = status["sub_results"], status["sub_online"], status["sub_total"]
            free_results, free_online, free_total = status["free_results"], status["free_online"], status["free_total"]
            order_results, order_online, order_total = status["order_results"], status["order_online"], status["order_total"]

            combined_online = sub_online + free_online + order_online
            total = sub_total + free_total + order_total
            pct = int((combined_online / total) * 100) if total else 0

            def _progress_bar(filled: int, total: int, length: int = 14) -> str:
                if total == 0:
                    return "░" * length
                filled_blocks = round((filled / total) * length)
                return "▰" * filled_blocks + "▱" * (length - filled_blocks)

            if total == 0:
                color = discord.Color.greyple()
            elif combined_online == total:
                color = discord.Color.green()
            elif combined_online == 0:
                color = discord.Color.red()
            else:
                color = discord.Color.orange()

            embed = discord.Embed(
                title="🏝️ Island Status",
                description=f"{_progress_bar(combined_online, total)}  **{combined_online}/{total}** online ({pct}%)",
                color=color,
                timestamp=discord.utils.utcnow(),
            )

            sub_off = [(n, c) for n, s, _, c in sub_results if s != "✅"]
            free_off = [(n, c) for n, s, _, c in free_results if s != "✅"]
            order_off = [(n, c) for n, s, _, c in order_results if s != "✅"]
            all_off = (
                [(n, c, "🏝️") for n, c in sub_off]
                + [(n, c, "🌴") for n, c in free_off]
                + [(n, c, "📦") for n, c in order_off]
            )

            if all_off:
                down_lines = [f"🔴 {tag} {_format_channel(n, c)}" for n, c, tag in all_off]
                down_value = "\n".join(down_lines)
                if len(down_value) > 1024:
                    down_value = down_value[:1000].rsplit("\n", 1)[0] + f"\n…and {len(down_lines) - down_value[:1000].rsplit(chr(10), 1)[0].count(chr(10)) - 1} more"
                embed.add_field(
                    name=f"{Config.EMOJI_FAIL} Needs attention ({len(all_off)})",
                    value=down_value,
                    inline=False,
                )
            else:
                embed.add_field(name="All clear", value="Every island is active.", inline=False)

            embed.add_field(name=f"{Config.STAR_PINK} Sub", value=f"**{sub_online}**/{sub_total} online", inline=True)
            embed.add_field(name=f"🌴 Free", value=f"**{free_online}**/{free_total} online", inline=True)
            embed.add_field(name=f"📦 Order", value=f"**{order_online}**/{order_total} online", inline=True)

            footer_icon_url = guild.icon.url if guild.icon else Config.DEFAULT_PFP
            embed.set_footer(text="Chopaeng Camp™", icon_url=footer_icon_url)
            embed.set_image(url=Config.FOOTER_LINE)

            previous_message = self.island_status_sticky_message

            if force_repost:
                if previous_message and previous_message.channel.id == target_channel.id:
                    try:
                        await previous_message.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                        logger.warning(f"[DISCORD] Could not delete previous sticky island status message: {exc}")
                    self.island_status_sticky_message = None
                    previous_message = None
            else:
                if previous_message and previous_message.channel.id == target_channel.id:
                    try:
                        await previous_message.edit(embed=embed)
                        return
                    except (discord.NotFound, discord.HTTPException) as exc:
                        logger.warning(f"[DISCORD] Previous sticky island status message unavailable, sending a new one: {exc}")
                        self.island_status_sticky_message = None
                        previous_message = None

            if previous_message is None:
                try:
                    async for msg in target_channel.history(limit=100):
                        if msg.author.id != self.bot.user.id:
                            continue
                        if not msg.embeds:
                            continue
                        embed_obj = msg.embeds[0]
                        if not embed_obj.footer or not embed_obj.footer.text:
                            continue
                        footer_text = embed_obj.footer.text
                        if footer_text.startswith("Island status") or footer_text.startswith("This message is kept pinned"):
                            try:
                                await msg.delete()
                            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                                pass
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning(f"[DISCORD] Failed to clean old island status sticky messages in {target_channel.id}: {exc}")

            try:
                message = await target_channel.send(embed=embed, silent=True)
                self.island_status_sticky_message = message
            except Exception as exc:
                logger.warning(f"[DISCORD] Failed to refresh island status sticky embed in {target_channel.id}: {exc}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Redirect users to /nick command in the designated channel and refresh the sticky status embed."""
        if message.guild is None or message.author.bot:
            return

        # Redirect nickname-submission messages in the designated channel.
        if message.channel.id != NICKNAME_SUBMISSION_CHANNEL_ID:
            return

        # Nuke all user messages in this channel and tell them to use /nick
        try:
            await message.delete()
            logger.info(f"[DISCORD] Redirected {message.author} to /nick in {NICKNAME_SUBMISSION_CHANNEL_ID}")
            
            notice = (
                "Please use the **/nick** command to set your nickname and island name.\n"
                "**Format:** `Character Name | Island Name`\n"
                "**Example:** `/nick nickname:ChoPaeng | ChoPaeng Camp`"
            )
            
            try:
                # Try to DM first to keep the channel clean
                await message.author.send(notice)
            except discord.Forbidden:
                # Fallback: Send to channel, delete after 10 seconds
                await message.channel.send(
                    f"{message.author.mention} {notice}", 
                    delete_after=10.0, 
                    silent=True 
                )

        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permissions to smite message from {message.author} in {NICKNAME_SUBMISSION_CHANNEL_ID}")
        except discord.NotFound:
            pass # Already dead
        except Exception as e:
            logger.warning(f"[DISCORD] Error handling message in {NICKNAME_SUBMISSION_CHANNEL_ID}: {e}")

    @staticmethod
    def _parse_iso8601(value: str | None) -> datetime | None:
        """Parse an ISO8601 datetime string into a timezone-aware UTC datetime."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    async def _fetch_islands_api_data(self) -> tuple[list[dict], datetime | None] | tuple[None, None]:
        """Fetch raw island snapshot data from the API."""
        url = f"{Config.API_BASE_URL}/api/islands"

        def _get_json():
            response = requests.get(url, timeout=8)
            response.raise_for_status()
            return response.json()

        try:
            payload = await asyncio.to_thread(_get_json)
        except Exception as exc:
            logger.warning(f"[DISCORD] island API request failed: {exc}")
            return None, None

        data = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(data, list):
            data = []
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        api_timestamp = self._parse_iso8601(meta.get("timestamp")) if isinstance(meta, dict) else None
        return data, api_timestamp

    async def _fetch_islands_api_snapshot(self) -> tuple[dict[str, dict], datetime | None] | tuple[None, None]:
        """Fetch island snapshot from the API and return map keyed by cleaned island name."""
        data, api_timestamp = await self._fetch_islands_api_data()
        if data is None:
            return None, None

        island_map: dict[str, dict] = {}
        for item in data:
            name = item.get("name") if isinstance(item, dict) else None
            if not name:
                continue
            normalized = clean_text(name)
            canonical = re.sub(r'^\d+', '', normalized)
            island_map[canonical or normalized] = item

        return island_map, api_timestamp

    @staticmethod
    def _read_first_line(path: str) -> str | None:
        """Read a small status file, retrying once if the island bot has it locked."""
        for attempt in range(2):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read().strip()
            except OSError:
                if attempt == 0:
                    time.sleep(0.05)
        return None

    @staticmethod
    def _parse_visitor_count(value: object) -> str:
        """Normalize visitor values for display."""
        if value is None:
            return "0/7"
        text = str(value).strip()
        if not text:
            return "0/7"
        if text.upper() == "FULL":
            return "FULL"
        if text.isdigit():
            return f"{max(0, min(7, int(text)))}/7"
        if re.fullmatch(r"\d+\s*/\s*7", text):
            return text.replace(" ", "")
        return text[:32]

    @staticmethod
    def _resolve_island_status(record: dict) -> tuple[str, str, "discord.Color"]:
        """Map an island API record's status to (icon, label, embed color)."""
        status = str(record.get("status") or "").strip().upper()
        dodo_code = str(record.get("dodo_code") or "").strip().upper()

        if status not in ISLAND_STATUS_DISPLAY:
            # Fall back to inferring status from the dodo code when the API
            # doesn't supply a normalized status string.
            if not dodo_code or dodo_code in ("GETTIN'", "00000", "-----"):
                status = "REFRESHING"
            elif dodo_code == "FULL":
                status = "ONLINE"
            else:
                status = "ONLINE" if DODO_CODE_PATTERN.fullmatch(dodo_code) else "OFFLINE"

        icon, label, color_name = ISLAND_STATUS_DISPLAY.get(status, ISLAND_STATUS_DISPLAY["OFFLINE"])
        color = {
            "green": discord.Color.green(),
            "red": discord.Color.red(),
            "orange": discord.Color.orange(),
        }[color_name]
        return icon, label, color


    def _items_on_island(self, island_clean: str, limit: int = 40) -> list[str]:
        """Reverse-lookup item display names currently available on *island_clean*.

        Only used as a fallback when the island API record doesn't already
        include an `items` list of its own.
        """
        with self.data_manager.lock:
            cache = self.data_manager.cache
            display_map = cache.get("_display", {})
            snapshot = {k: v for k, v in cache.items() if not k.startswith("_")}

        found = []
        for key, locations in snapshot.items():
            if not locations:
                continue
            loc_keys = {clean_text(loc) for loc in str(locations).split(", ")}
            if island_clean in loc_keys:
                found.append(display_map.get(key, key.title()))
                if len(found) >= limit:
                    break
        return sorted(found)


    def _villagers_on_island(self, island_clean: str) -> list[str]:
        """Reverse-lookup villager display names currently residing on *island_clean*."""
        villager_map = self.data_manager.get_villagers([
            Config.VILLAGERS_DIR,
            Config.TWITCH_VILLAGERS_DIR,
        ])

        found = []
        for key, locations in villager_map.items():
            if not locations:
                continue
            loc_keys = {clean_text(loc) for loc in str(locations).split(", ")}
            if island_clean in loc_keys:
                found.append(key.title())
        return sorted(found)


    async def _build_island_info_embed(self, ctx, record: dict, island_clean: str) -> discord.Embed:
        """Build the full island-details embed for /island."""
        raw_name = str(record.get("name") or island_clean).strip()
        display_name = raw_name.title()
        description = (str(record.get("description") or "").strip()
                    or "No description available for this island yet.")
        map_url = str(record.get("map_url") or "").strip()
        visitors = self._parse_visitor_count(record.get("visitors"))

        cat = str(record.get("cat") or record.get("type") or "").strip().lower()
        access_label = (
            "Order Bot" if cat == "order"
            else "Subscribers Only" if cat == "member"
            else "Public"
        )

        icon, status_label, color = self._resolve_island_status(record)
        island_url = f"https://www.chopaeng.com/island/{raw_name.lower()}"

        embed = discord.Embed(
            title=f"\U0001F3DD\uFE0F {display_name}",
            url=island_url,
            description=description[:2000],
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(name="Status", value=f"{icon} {status_label}", inline=True)
        embed.add_field(name="Passengers", value=visitors, inline=True)
        embed.add_field(name="Gate Type", value=access_label, inline=True)

        # Items — prefer the API record's own list; fall back to the item cache.
        items = record.get("items")
        if not isinstance(items, list) or not items:
            items = await asyncio.to_thread(self._items_on_island, island_clean)
        if items:
            lines = [f"\u2022 {item}" for item in items]
            chunks = self._chunk_lines(lines)
            for i, chunk in enumerate(chunks):
                label = f"Available Loot ({len(items)})" if i == 0 else "Available Loot (cont.)"
                embed.add_field(name=label, value=chunk, inline=False)
        else:
            embed.add_field(name="Available Loot", value="*No items currently tracked.*", inline=False)

        # Villagers / current residents
        villagers = await asyncio.to_thread(self._villagers_on_island, island_clean)
        if villagers:
            lines = [f"\U0001F3E0 {v}" for v in villagers]
            chunks = self._chunk_lines(lines)
            for i, chunk in enumerate(chunks):
                label = f"Current Residents ({len(villagers)})" if i == 0 else "Current Residents (cont.)"
                embed.add_field(name=label, value=chunk, inline=False)
        else:
            embed.add_field(name="Current Residents", value="*No residents currently tracked.*", inline=False)

        if map_url:
            embed.set_image(url=map_url)
            embed.set_thumbnail(url=map_url)  # harmless if Discord ignores the duplicate

        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name} \u2022 Chopaeng Camp\u2122", icon_url=pfp_url)

        return embed


    def _read_free_dodo_files(self) -> list[dict]:
        """Fallback reader for free-island Dodo files when the API is unavailable."""
        base_dir = Config.DIR_FREE
        if not base_dir or not os.path.exists(base_dir):
            return []

        items = []
        with os.scandir(base_dir) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue

                raw_dodo = self._read_first_line(os.path.join(entry.path, "Dodo.txt"))
                raw_visitors = self._read_first_line(os.path.join(entry.path, "Visitors.txt"))
                dodo_code = None
                status = "OFFLINE"

                if raw_dodo in ("00000", "-----", ""):
                    status = "REFRESHING"
                elif raw_dodo and DODO_CODE_PATTERN.fullmatch(raw_dodo.strip().upper()):
                    dodo_code = raw_dodo.strip().upper()
                    status = "ONLINE"

                items.append({
                    "name": entry.name.upper(),
                    "dodo_code": dodo_code,
                    "visitors": self._parse_visitor_count(raw_visitors),
                    "status": status,
                    "type": "Free",
                })

        return sorted(items, key=lambda item: str(item.get("name", "")))

    async def _fetch_free_dodo_board_data(self) -> tuple[list[dict], datetime | None]:
        """Return free-island data for the Dodo board."""
        data, api_timestamp = await self._fetch_islands_api_data()
        if data is not None:
            known_free = {clean_text(name) for name in Config.FREE_ISLANDS}
            known_free.update(self.free_island_lookup.keys())
            free_items = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                island_type = str(item.get("type", "")).strip().lower()
                island_clean = clean_text(str(item.get("name", "")))
                if island_type == "free" or island_clean in known_free:
                    free_items.append(item)
            return sorted(free_items, key=lambda item: str(item.get("name", ""))), api_timestamp

        file_items = await asyncio.to_thread(self._read_free_dodo_files)
        return file_items, None

    def _build_free_dodo_embed(
        self,
        item: dict,
        checked_at: datetime,
        footer_icon_url: str | None = None,
    ) -> discord.Embed:
        """Build one free-island Dodo board embed."""
        raw_name = str(item.get("name") or "Unknown Island").strip()
        display_name = raw_name.title()
        raw_code = str(item.get("dodo_code") or "").strip().upper()
        status = str(item.get("status") or "UNKNOWN").strip().upper()
        visitors = self._parse_visitor_count(item.get("visitors"))
        map_url = str(item.get("map_url") or "").strip()
        description = str(item.get("description") or "").strip()
        dodo_code = raw_code if DODO_CODE_PATTERN.fullmatch(raw_code) else raw_code

        island_url = "https://www.chopaeng.com/island/"+raw_name.lower()    

        if dodo_code:
            color = discord.Color.green()
            code_line = f"```yaml\n{dodo_code}```"
            status_line = "Online"
        elif dodo_code == "GETTIN'":
            color = discord.Color.orange()
            code_line = "*Refreshing*"
            status_line = "Refreshing"
        else:
            color = discord.Color.red()
            code_line = "*Offline*"
            status_line = "Offline"

        details = []
        if description:
            details.append(description[:180])
        
        links = [f"\n[View Island]({island_url})"]
        if map_url:
            links.append(f"[View Map]({map_url})")
        details.append(" • ".join(links))

        embed = discord.Embed(
            title=f"{display_name}",
            url=island_url or None,
            description="\n".join(details),
            color=color,
            timestamp=checked_at,
        )
        embed.add_field(name="Dodo Code", value=code_line, inline=True)
        embed.add_field(name="Visitors", value=visitors, inline=True)
        embed.add_field(name="Status", value=status_line, inline=True)
        if map_url:
            embed.set_thumbnail(url=map_url)
        embed.set_image(url=Config.FOOTER_LINE)
        embed.set_footer(
            text=f"{FREE_DODO_BOARD_MARKER} • {raw_name}",
            icon_url=footer_icon_url,
        )
        return embed

    def _build_free_dodo_empty_embed(
        self,
        checked_at: datetime,
        footer_icon_url: str | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Free Island Dodo Board",
            description="No free island Dodo codes are available right now.",
            color=discord.Color.orange(),
            timestamp=checked_at,
        )
        embed.set_image(url=Config.FOOTER_LINE)
        embed.set_footer(
            text=f"{FREE_DODO_BOARD_MARKER}",
            icon_url=footer_icon_url,
        )
        return embed

    @staticmethod
    def _free_dodo_embed_fingerprint(embed: discord.Embed) -> str:
        payload = embed.to_dict()
        payload.pop("timestamp", None)
        return repr(payload)

    async def _load_existing_free_dodo_board_messages(self, channel: discord.TextChannel) -> None:
        """Find board messages from the current bot after a restart."""
        if self.free_dodo_board_messages:
            return
        if not self.bot.user:
            return

        messages = []
        try:
            async for msg in channel.history(limit=50):
                if msg.author.id != self.bot.user.id or not msg.embeds:
                    continue
                footer = msg.embeds[0].footer.text if msg.embeds[0].footer else ""
                if footer and FREE_DODO_BOARD_MARKER in footer:
                    messages.append(msg)
        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permission to read Free Dodo board history in #{channel.name}")
            return
        except discord.HTTPException as exc:
            logger.warning(f"[DISCORD] Failed to read Free Dodo board history in #{channel.name}: {exc}")
            return

        messages.sort(key=lambda msg: msg.created_at)
        self.free_dodo_board_messages = messages

    async def _delete_existing_free_dodo_board_messages(self, channel: discord.TextChannel) -> None:
        """Delete stale Free Dodo board messages left behind by a previous bot process."""
        if not self.bot.user:
            return

        deleted = 0
        messages_to_delete = []
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id != self.bot.user.id or not msg.embeds:
                    continue
                footer = msg.embeds[0].footer.text if msg.embeds[0].footer else ""
                if not footer or FREE_DODO_BOARD_MARKER not in footer:
                    continue
                messages_to_delete.append(msg)
        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permission to delete old Free Dodo board messages in #{channel.name}")
            return
        except discord.HTTPException as exc:
            logger.warning(f"[DISCORD] Failed while deleting old Free Dodo board messages in #{channel.name}: {exc}")
            return

        # Bulk delete collected messages
        batch_size = 100
        for i in range(0, len(messages_to_delete), batch_size):
            batch = messages_to_delete[i:i + batch_size]
            try:
                await channel.delete_messages(batch)
                deleted += len(batch)
            except discord.Forbidden:
                # Fallback to individual deletion
                for msg in batch:
                    try:
                        await msg.delete()
                        deleted += 1
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.warning(f"[DISCORD] Failed to delete message: {e}")
            except Exception as e:
                logger.warning(f"[DISCORD] Failed to bulk delete batch: {e}")

        self.free_dodo_board_messages = []
        self.free_dodo_board_fingerprints = []
        if deleted:
            logger.info(f"[DISCORD] Deleted {deleted} stale Free Dodo board message(s) in #{channel.name}")


    async def _load_existing_island_status_sticky_message(self, channel: discord.TextChannel) -> None:
        """Find this bot's existing island-status sticky message after a restart, so we
        edit it in place instead of accidentally creating duplicates."""
        if self.island_status_sticky_message:
            return
        if not self.bot.user:
            return

        try:
            async for msg in channel.history(limit=50):
                if msg.author.id != self.bot.user.id or not msg.embeds:
                    continue
                embed_obj = msg.embeds[0]
                footer_text = embed_obj.footer.text if embed_obj.footer else ""
                if footer_text and footer_text.startswith("Island status"):
                    self.island_status_sticky_message = msg
                    return
        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permission to read island status history in #{channel.name}")
        except discord.HTTPException as exc:
            logger.warning(f"[DISCORD] Failed to read island status history in #{channel.name}: {exc}")

    async def _delete_existing_island_status_sticky_messages(self, channel: discord.TextChannel) -> None:
        """Delete stale island-status sticky messages left behind by a previous bot process,
        so a restart always starts from a clean slate (same pattern as the Free Dodo board)."""
        if not self.bot.user:
            return

        deleted = 0
        messages_to_delete = []
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id != self.bot.user.id or not msg.embeds:
                    continue
                embed_obj = msg.embeds[0]
                footer_text = embed_obj.footer.text if embed_obj.footer else ""
                if footer_text and (
                    footer_text.startswith("Island status")
                    or footer_text.startswith("This message is kept pinned")
                ):
                    messages_to_delete.append(msg)
        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permission to delete old island status messages in #{channel.name}")
            return
        except discord.HTTPException as exc:
            logger.warning(f"[DISCORD] Failed while deleting old island status messages in #{channel.name}: {exc}")
            return

        batch_size = 100
        for i in range(0, len(messages_to_delete), batch_size):
            batch = messages_to_delete[i:i + batch_size]
            try:
                await channel.delete_messages(batch)
                deleted += len(batch)
            except discord.Forbidden:
                for msg in batch:
                    try:
                        await msg.delete()
                        deleted += 1
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.warning(f"[DISCORD] Failed to delete message: {e}")
            except Exception as e:
                logger.warning(f"[DISCORD] Failed to bulk delete batch: {e}")

        self.island_status_sticky_message = None
        if deleted:
            logger.info(f"[DISCORD] Deleted {deleted} stale island status message(s) in #{channel.name}")

    async def _publish_free_dodo_board(self, channel: discord.TextChannel, embeds: list[discord.Embed]) -> None:
        """Edit or create individual Discord messages for each Free Dodo board entry."""
        await self._load_existing_free_dodo_board_messages(channel)
        expected_count = len(embeds)
        fingerprints = [self._free_dodo_embed_fingerprint(embed) for embed in embeds]

        for idx, embed in enumerate(embeds):
            try:
                if idx < len(self.free_dodo_board_messages):
                    if (
                        idx < len(self.free_dodo_board_fingerprints)
                        and self.free_dodo_board_fingerprints[idx] == fingerprints[idx]
                    ):
                        continue
                    await self.free_dodo_board_messages[idx].edit(content=None, embeds=[embed])
                    if idx < len(self.free_dodo_board_fingerprints):
                        self.free_dodo_board_fingerprints[idx] = fingerprints[idx]
                    else:
                        self.free_dodo_board_fingerprints.append(fingerprints[idx])
                else:
                    msg = await channel.send(embed=embed)
                    self.free_dodo_board_messages.append(msg)
                    self.free_dodo_board_fingerprints.append(fingerprints[idx])
            except discord.NotFound:
                self.free_dodo_board_messages = []
                self.free_dodo_board_fingerprints = []
                logger.warning("[DISCORD] Free Dodo board message disappeared; it will be recreated next cycle.")
                return
            except discord.Forbidden:
                logger.warning(f"[DISCORD] Missing permission to update Free Dodo board in #{channel.name}")
                return
            except discord.HTTPException as exc:
                logger.warning(f"[DISCORD] Failed to update Free Dodo board in #{channel.name}: {exc}")
                return

        extras = self.free_dodo_board_messages[expected_count:]
        self.free_dodo_board_messages = self.free_dodo_board_messages[:expected_count]
        self.free_dodo_board_fingerprints = self.free_dodo_board_fingerprints[:expected_count]
        for msg in extras:
            try:
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    @tasks.loop(seconds=FREE_DODO_BOARD_INTERVAL_SECONDS)
    async def free_dodo_board_loop(self):
        """Keep the public free-island Dodo board updated."""
        channel_id = Config.FREE_DODO_BOARD_CHANNEL_ID
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                logger.warning(f"[DISCORD] Free Dodo board channel {channel_id} unavailable: {exc}")
                return

        if not isinstance(channel, discord.TextChannel):
            logger.warning(f"[DISCORD] Free Dodo board target {channel_id} is not a text channel.")
            return

        if not self.free_dodo_board_startup_cleanup_done:
            await self._delete_existing_free_dodo_board_messages(channel)
            self.free_dodo_board_startup_cleanup_done = True

        await self.fetch_free_islands()
        checked_at = datetime.now(timezone.utc)
        free_items, _ = await self._fetch_free_dodo_board_data()
        footer_icon_url = channel.guild.icon.url if channel.guild and channel.guild.icon else None
        embeds = [self._build_free_dodo_embed(item, checked_at, footer_icon_url) for item in free_items]
        if not embeds:
            embeds = [self._build_free_dodo_empty_embed(checked_at, footer_icon_url)]

        await self._publish_free_dodo_board(channel, embeds)

    @free_dodo_board_loop.before_loop
    async def before_free_dodo_board_loop(self):
        """Wait until ready before starting the public Free Dodo board."""
        await self.bot.wait_until_ready()
        await self.fetch_free_islands()

    @tasks.loop(seconds=60)
    async def island_status_sticky_loop(self):
        channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        if not self.island_status_sticky_startup_cleanup_done:
            await self._delete_existing_island_status_sticky_messages(channel)
            self.island_status_sticky_startup_cleanup_done = True

        try:
            await self._refresh_island_status_sticky_message(channel, force_repost=True)
        except Exception as exc:
            logger.error(f"[DISCORD] island_status_sticky_loop iteration failed: {exc}", exc_info=True)

    @island_status_sticky_loop.before_loop
    async def before_island_status_sticky_loop(self):
        """Wait until ready before starting the island status sticky loop."""
        await self.bot.wait_until_ready()
        # No manual refresh here — tasks.loop runs the body immediately once
        # before_loop finishes, so the first iteration above handles both
        # cleanup and the initial post.


    @island_status_sticky_loop.error
    async def island_status_sticky_loop_error(self, error: Exception):
        """Safety net: log and restart the loop if it still somehow crashes."""
        logger.error(f"[DISCORD] island_status_sticky_loop crashed: {error}", exc_info=True)
        if not self.island_status_sticky_loop.is_running():
            self.island_status_sticky_loop.restart()


    def check_cooldown(self, user_id: str, cooldown_sec: int = 3) -> bool:
        """Check if user is on cooldown"""
        now = time.time()
        if user_id in self.cooldowns:
            if now - self.cooldowns[user_id] < cooldown_sec:
                return True
        self.cooldowns[user_id] = now

        # Periodic cleanup: prune entries older than 60s every 100 entries
        if len(self.cooldowns) > 100:
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < 60}

        return False

    def get_island_channel_link(self, island_name):
        """Get channel link for an island with robust fallback search"""
        island_clean = clean_text(island_name)
        if not island_clean:
            return f"**{island_name.title()}**"
        
        # First check our cached lookup
        if island_clean in self.sub_island_lookup:
            return f"<#{self.sub_island_lookup[island_clean]}>"
        
        # Fallback: search through guild channels matching island name
        guild = self.bot.get_guild(Config.GUILD_ID)
        if guild:
            category = discord.utils.get(guild.categories, id=Config.CATEGORY_ID)
            if category:
                for channel in category.channels:
                    if channel.id == Config.IGNORE_CHANNEL_ID:
                        continue
                    chan_clean = clean_text(channel.name)
                    # Match if island name is in channel name (e.g., "alapaap" in "01-alapaap")
                    if island_clean in chan_clean:
                        # Cache it for next time
                        self.sub_island_lookup[island_clean] = channel.id
                        return f"<#{channel.id}>"
        
        # If no channel found, return bold text
        return f"**{island_name.title()}**"

    def create_found_embed(self, ctx_or_interaction, search_term, location_string, is_villager=False, nooki_data=None, island_map=None):

        user = getattr(ctx_or_interaction, "author", getattr(ctx_or_interaction, "user", None))
        clean_name = search_term.title()
        loc_list = sorted(list(set(location_string.split(", "))))
        sub_islands_found = []
        island_map = island_map or {}

        for loc in loc_list:
            loc_key = clean_text(loc)

            # STRICT FILTER: Only allow islands explicitly listed in Config.SUB_ISLANDS
            # Verify if the cleaned location corresponds to a known sub island
            is_sub = any(clean_text(si) == loc_key for si in Config.SUB_ISLANDS)
            if not is_sub:
                continue

            # Use get_island_channel_link for robust linking with fallback
            island_link = self.get_island_channel_link(loc)
            island_payload = island_map.get(loc_key)
            is_online = None
            if isinstance(island_payload, dict):
                status_text = str(island_payload.get("status", "")).upper()
                bot_online = island_payload.get("discord_bot_online")
                is_online = bool(bot_online) if bot_online is not None else (status_text == "ONLINE")
            status_icon = "🟢" if is_online is True else "🔴" if is_online is False else "⚪"
            sub_islands_found.append(f"{status_icon} {island_link}")

        # If no Sub Islands match, return None to indicate availability failure
        if not sub_islands_found:
            return None

        island_count = len(sub_islands_found)
        island_term = "island" if island_count == 1 else "islands"
        verb_term = "is" if island_count == 1 else "are"

        if is_villager:
            embed_title = f"{Config.EMOJI_SEARCH} Found Villager: {clean_name}"
            embed_desc = f"**{clean_name}** is currently residing on this {island_term}:" if island_count == 1 else f"**{clean_name}** is currently residing on these {island_term}:"
        else:
            embed_title = f"{Config.EMOJI_SEARCH} Found Item: {clean_name}"
            embed_desc = f"**{clean_name}** {verb_term} available on these {island_term}:"


        embed = discord.Embed(
            title=embed_title,
            description=embed_desc,
            color=discord.Color.teal(),
            timestamp=datetime.now()
        )

        search_key = normalize_text(search_term)

        # Apply Nookipedia Data if available
        if is_villager and nooki_data:
            villager_id = nooki_data.get("id", "")
            personality = nooki_data.get("personality", "Unknown")
            species = nooki_data.get("species", "Unknown")
            phrase = nooki_data.get("phrase", "None")
            gender = nooki_data.get("gender", "Unknown")
            birthday_month = nooki_data.get("birthday_month", "")
            birthday_day = nooki_data.get("birthday_day", "")
            birthday = f"{birthday_month} {birthday_day}".strip() or "Unknown"
            sign = nooki_data.get("sign", "Unknown")
            quote = nooki_data.get("quote", "")

            # NH Details
            nh = nooki_data.get("nh_details", {}) or {}
            hobby = nh.get("hobby", "Unknown")
            colors = ", ".join(nh.get("fav_colors", [])) or "Unknown"

            embed.set_thumbnail(url=nooki_data.get("image_url", ""))

            if quote:
                embed.description = f"*\"{quote}\"*"

            # Info field
            info_parts = [f"**Species:** {species}", f"**Gender:** {gender}"]
            if villager_id:
                info_parts.append(f"**Code:** `{villager_id}`")
            embed.add_field(
                name=f"{Config.STAR_PINK} Info",
                value="\n".join(info_parts),
                inline=True
            )

            # Personality field
            embed.add_field(
                name=f"{Config.STAR_PINK} Personality",
                value=f"**Type:** {personality}\n**Catchphrase:** \"{phrase}\"\n**Hobby:** {hobby}",
                inline=True
            )

            # Birthday / Details field
            detail_parts = []
            if birthday != "Unknown":
                detail_parts.append(f"**Birthday:** {birthday}")
            if sign and sign != "Unknown":
                detail_parts.append(f"**Sign:** {sign}")
            if colors and colors != "Unknown":
                detail_parts.append(f"**Colors:** {colors}")
            if detail_parts:
                embed.add_field(
                    name=f"{Config.STAR_PINK} Details",
                    value="\n".join(detail_parts),
                    inline=True
                )

        elif search_key in self.data_manager.image_cache:
            embed.set_thumbnail(url=self.data_manager.image_cache[search_key])

        full_text = "\n".join(sub_islands_found)
        chunks = []

        if len(full_text) <= 1024:
            chunks.append(full_text)
        else:
            current_chunk = ""
            for line in sub_islands_found:
                if len(current_chunk) + len(line) + 1 > 1024:
                    chunks.append(current_chunk)
                    current_chunk = line
                else:
                    if current_chunk:
                        current_chunk += "\n" + line
                    else:
                        current_chunk = line
            if current_chunk:
                chunks.append(current_chunk)


        for i, chunk in enumerate(chunks):
            name = f"{Config.STAR_PINK} Sub {island_term.capitalize()}"
            embed.add_field(name=name, value=chunk, inline=False)

        pfp_url = user.avatar.url if user.avatar else Config.DEFAULT_PFP
        embed.set_image(url=Config.FOOTER_LINE)
        embed.set_footer(text=f"Requested by {user.display_name}", icon_url=pfp_url)

        return embed

    def create_villager_house_embed(self, ctx_or_interaction, villager_name, nooki_data):
        """Create a house information embed for a villager"""
        if not nooki_data:
            return None

        nh = nooki_data.get("nh_details", {}) or {}

        flooring = nh.get("house_flooring") or "Unknown"
        wallpaper = nh.get("house_wallpaper") or "Unknown"
        music = nh.get("house_music") or "Unknown"
        interior_url = nh.get("house_interior_url") or nh.get("house_img") or ""
        exterior_url = nh.get("house_exterior_url") or ""

        has_house_data = (
            flooring != "Unknown"
            or wallpaper != "Unknown"
            or music != "Unknown"
            or interior_url
            or exterior_url
        )
        if not has_house_data:
            return None

        clean_name = villager_name.title()
        user = getattr(ctx_or_interaction, "author", getattr(ctx_or_interaction, "user", None))

        embed = discord.Embed(
            title=f"{Config.EMOJI_SEARCH} {clean_name}'s House Information",
            color=discord.Color.teal(),
            timestamp=datetime.now()
        )

        embed.add_field(name=f"{Config.STAR_PINK} Flooring", value=flooring, inline=True)
        embed.add_field(name=f"{Config.STAR_PINK} Wallpaper", value=wallpaper, inline=True)
        embed.add_field(name=f"{Config.STAR_PINK} Music", value=music, inline=True)

        links = []
        if interior_url:
            links.append(f"[Interior]({interior_url})")
        if exterior_url:
            links.append(f"[Exterior]({exterior_url})")
        if links:
            embed.add_field(
                name=f"{Config.STAR_PINK} Image Previews",
                value=" | ".join(links),
                inline=False
            )

        if exterior_url:
            embed.set_thumbnail(url=exterior_url)

        if interior_url:
            embed.set_image(url=interior_url)

        pfp_url = user.avatar.url if user.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {user.display_name}", icon_url=pfp_url)

        return embed

    def create_fail_embed(self, ctx, search_term, suggestions, is_villager=False):

        category = "Villager" if is_villager else "Item"

        embed = discord.Embed(
            title=f"{Config.EMOJI_FAIL} {category} Not Found: {search_term.title()}",
            description=f"I couldn't find exactly that. Did you mean one of these?",
            color=0xFF4444,
            timestamp=discord.utils.utcnow()
        )

        if suggestions:
            embed.add_field(
                name=f"{Config.STAR_PINK} Suggestions",
                value="\n".join([f"{Config.INDENT} {s.title()}" for s in suggestions[:5]]),
                inline=False
            )
        else:
            embed.description = f"I searched everywhere but couldn't find it.\n\n{Config.DROPBOT_INFO}"


        user_avatar = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=user_avatar)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    @commands.hybrid_command(name="find", aliases=['locate', 'where', 'search'])
    @app_commands.describe(item="The name of the item or recipe to find")
    @app_commands.autocomplete(item=item_autocomplete)
    async def find(self, ctx, *, item: str = ""):
        """Find an item"""

        if not await self._enforce_find_channel(ctx):
            return

        if not item:
            await ctx.reply("Usage: `!find <item name>`")
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        search_term_raw = item.strip()
        search_term = normalize_text(search_term_raw)

        with self.data_manager.lock:
            cache = self.data_manager.cache
            keys = [k for k in cache.keys() if k != "_display"]
            found_locations = cache.get(search_term)

        if found_locations:
            with self.data_manager.lock:
                display_name = cache.get("_display", {}).get(search_term, search_term_raw)

            island_map, _ = await self._fetch_islands_api_snapshot()
            embed = self.create_found_embed(
                ctx,
                display_name,
                found_locations,
                is_villager=False,
                island_map=island_map or {},
            )

            result_view = FindResultView(
                cog=self, last_query=search_term, last_display=display_name,
                search_type="item", author_id=ctx.author.id
            )
            if embed:
                await ctx.reply(content=f"Hey <@{ctx.author.id}>, look what I found!", embed=embed, view=result_view)
                logger.info(f"[DISCORD] Item Hit: {search_term} -> Found")
            else:
                await ctx.reply(
                    f"**{display_name}** is not currently available on any Sub Island.",
                    view=result_view
                )
                logger.info(f"[DISCORD] Item Hit: {search_term} -> Not on Sub Islands")
            return

        suggestion_keys = get_best_suggestions(search_term, keys, limit=8)

        with self.data_manager.lock:
            display_map = cache.get("_display", {})

        suggestions = [(k, display_map.get(k, k)) for k in suggestion_keys]
        embed_fail = self.create_fail_embed(ctx, search_term_raw, [disp for _, disp in suggestions])

        if suggestions:
            view = SuggestionView(self, suggestions, "item", ctx.author.id)
            await ctx.reply(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail, view=view)
        else:
            await ctx.reply(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail)

    @commands.hybrid_command(name="villager")
    @app_commands.describe(name="The name of the villager")
    async def villager(self, ctx, *, name: str = ""):
        """Find a villager"""

        if not await self._enforce_find_channel(ctx):
            return

        if not name:
            await ctx.reply("Usage: `!villager <n>`")
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        search_term = normalize_text(name)
        villager_map = self.data_manager.get_villagers([
            Config.VILLAGERS_DIR,
            Config.TWITCH_VILLAGERS_DIR
        ])

        found_locations = villager_map.get(search_term)

        if found_locations:
            nooki_data = await NookipediaClient.get_villager_info(search_term)
            island_map, _ = await self._fetch_islands_api_snapshot()
            embed = self.create_found_embed(
                ctx,
                search_term,
                found_locations,
                is_villager=True,
                nooki_data=nooki_data,
                island_map=island_map or {},
            )

            result_view = FindResultView(
                cog=self, last_query=search_term, last_display=search_term,
                search_type="villager", author_id=ctx.author.id
            )
            if embed:
                house_embed = self.create_villager_house_embed(ctx, search_term, nooki_data) if nooki_data else None
                send_embeds = [embed] + ([house_embed] if house_embed else [])
                await ctx.reply(content=f"Hey <@{ctx.author.id}>, look who I found!", embeds=send_embeds, view=result_view)
                logger.info(f"[DISCORD] Villager Hit: {search_term} -> Found")
            else:
                await ctx.reply(
                    f"**{search_term.title()}** is not currently on any Sub Island.",
                    view=result_view
                )
                logger.info(f"[DISCORD] Villager Hit: {search_term} -> Not on Sub Islands")
            return

        matches = process.extract(search_term, list(villager_map.keys()), limit=3, scorer=fuzz.WRatio)
        suggestions = [(m[0], m[0].title()) for m in matches if m[1] > 75]
        suggestion_display_names = [s[1] for s in suggestions]

        embed_fail = self.create_fail_embed(ctx, search_term, suggestion_display_names, is_villager=True)

        if suggestions:
            view = SuggestionView(self, suggestions, "villager", ctx.author.id)
            await ctx.reply(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail, view=view)
        else:
            await ctx.reply(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail)

        logger.info(f"[DISCORD] Villager Miss: {search_term}")

    @app_commands.command(
        name="findpanel",
        description="Deploy the interactive Find Station panel to a channel",
    )
    @app_commands.describe(channel="The channel to deploy into (defaults to current channel)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_findpanel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ):
        """Deploy a persistent Find Station panel that anyone can search from."""
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and not (perms.manage_channels or perms.administrator):
            await interaction.response.send_message(
                "❌ You need **Manage Channels** or **Administrator** permission to deploy the Find Station.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not target:
            await interaction.response.send_message("❌ Target channel not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            guild_icon = resolve_guild_icon(target or interaction, self.bot)
            embed = build_find_panel_embed(guild_icon=guild_icon)
            view = FindPanelView()
            await target.send(embed=embed, view=view)
            await interaction.followup.send(
                f"✅ Find Station deployed to {target.mention}!", ephemeral=True
            )
            logger.info(f"[DISCORD] Find Station deployed to #{target.name} by {interaction.user}")
        except Exception as exc:
            logger.exception(f"[slash_findpanel] Error: {exc}")
            await interaction.followup.send(
                "❌ Failed to deploy Find Station. Check bot permissions in that channel.",
                ephemeral=True,
            )

    @app_commands.command(
        name="place",
        description="Deploy an interactive station panel (find or chorder) to any channel",
    )
    @app_commands.describe(
        panel="Which station to place: 'find' (Find Station) or 'chorder' (Order Station)",
        channel="The channel to deploy into (defaults to current channel)",
    )
    @app_commands.choices(panel=[
        app_commands.Choice(name="find (Find Station — Search Items & Villagers)", value="find"),
        app_commands.Choice(name="chorder (ChOrder Station — Custom Order Bot & Cart)", value="chorder"),
    ])
    @app_commands.default_permissions(manage_channels=True)
    async def slash_place(
        self,
        interaction: discord.Interaction,
        panel: str,
        channel: discord.TextChannel | None = None,
    ):
        """Deploy either the Find Station or the Chorder Order Station to a channel."""
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and not (perms.manage_channels or perms.administrator):
            await interaction.response.send_message(
                "❌ You need **Manage Channels** or **Administrator** permission to deploy station panels.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not target:
            await interaction.response.send_message("❌ Target channel not found.", ephemeral=True)
            return

        panel_type = panel.lower().strip()
        await interaction.response.defer(ephemeral=True)
        try:
            guild_icon = resolve_guild_icon(target or interaction, self.bot)

            if panel_type == "find":
                embed = build_find_panel_embed(guild_icon=guild_icon)
                view = FindPanelView()
                await target.send(embed=embed, view=view)
                await interaction.followup.send(
                    f"✅ **Find Station** deployed to {target.mention}!", ephemeral=True
                )
                logger.info(f"[DISCORD] Find Station placed in #{target.name} by {interaction.user}")

            elif panel_type in ("chorder", "order"):
                bot_status = await sysbot.get_bot_status()
                island_name = bot_status.get("island_name", getattr(Config, "ORDER_BOT_ISLAND", "Sinta"))
                is_online = bot_status.get("is_running", True)
                embed = build_panel_embed(island_name, is_online, guild_icon=guild_icon)
                view = OrderPanelView()
                await target.send(embed=embed, view=view)
                await interaction.followup.send(
                    f"✅ **ChOrder Station** deployed to {target.mention}!", ephemeral=True
                )
                logger.info(f"[DISCORD] ChOrder Station placed in #{target.name} by {interaction.user}")

            else:
                await interaction.followup.send(
                    f"❌ Unknown panel type '{panel}'. Please select either `find` or `chorder`.",
                    ephemeral=True,
                )
        except Exception as exc:
            logger.exception(f"[slash_place] Error placing {panel_type}: {exc}")
            await interaction.followup.send(
                f"❌ Failed to place station panel in {target.mention}. Check bot permissions.",
                ephemeral=True,
            )

    @commands.hybrid_command(name="help")
    async def help_command(self, ctx):
        """Show all available commands"""
        embed = discord.Embed(
            title=f"{Config.EMOJI_SEARCH} Chobot Commands",
            description="Here are all the commands you can use:",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name=f"{Config.STAR_PINK} Search Commands",
            value=(
                "`!find <item>` - Find an item across islands\n"
                "`!villager <name>` - Find a villager\n"
                "*Aliases: !locate, !where, !search*"
            ),
            inline=False
        )
        
        embed.add_field(
            name=f"{Config.STAR_PINK} Sub Island Commands",
            value=(
                "`!senddodo` or `!sd` - Get the dodo code for this sub island\n"
                "`!visitors` - Check current visitors on this sub island\n"
                "`!villagers` - Check current villagers on this sub island\n"
                "`!drop` - Request item drop on this sub island\n"
                "`!injectvillager <name>` - Inject a villager onto this sub island\n"
                "`!mvi <name> [names...]` - Inject multiple villagers onto this sub island\n"
                "*Use these in a sub island channel. If the island is offline, you'll see an 'island is down' message.*"
            ),
            inline=False
        )

        embed.add_field(
            name=f"{Config.STAR_PINK} Utility Commands",
            value=(
                "`!islands [sub|free]` - Check island bot status (sub, free, or both)\n"
                "*Aliases: !islandstatus, !checkislands*\n"

                "`!status` - Show bot status and cache info\n"
                "`!ping` - Check bot response time\n"
                "`!random` - Get a random item suggestion\n"
                "`!ask <question>` - Ask the Chopaeng AI anything\n"
                "`!help` - Show this help message"
            ),
            inline=False
        )


        embed.add_field(
            name=f"{Config.STAR_PINK} Admin Commands",
            value=(
                "`!refresh` - Manually refresh cache (Admin only)\n"
                "`!update` - Pull latest code from git and restart the bot (Admin only)\n"
                "`!restart` - Restart the bot without pulling updates (Admin only)"
            ),
            inline=False
        )

        embed.add_field(
            name="💡 Tips",
            value=(
                "• Use `/find` or `/villager` for slash command support\n"
                "• Try `!random` to discover items you might have missed\n"
                "• All search commands support fuzzy matching"
            ),
            inline=False
        )

        embed.set_footer(text=f"Requested by {ctx.author.display_name}", 
                        icon_url=ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP)
        embed.set_image(url=Config.FOOTER_LINE)
        
        await ctx.reply(embed=embed)
        logger.info(f"[DISCORD] Help command used by {ctx.author.name}")

    async def _enforce_find_channel(self, ctx) -> bool:
        """
        Returns True if in the correct channel.
        Otherwise, deletes the message and returns False.
        """
        if not Config.FIND_BOT_CHANNEL_ID or ctx.channel.id == Config.FIND_BOT_CHANNEL_ID:
            return True

        # Nuke the unauthorized text command
        if ctx.message:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        # Scold them ephemerally if they used a slash command
        if ctx.interaction and not ctx.interaction.response.is_done():
            try:
                await ctx.interaction.response.send_message(
                    f"Keep it clean. Use this command in <#{Config.FIND_BOT_CHANNEL_ID}>.",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass

        return False

    @commands.hybrid_command(name="ping")
    async def ping(self, ctx):
        """Check bot latency"""
        latency_ms = round(self.bot.latency * 1000, 2)
        
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Bot latency: **{latency_ms}ms**",
            color=discord.Color.green() if latency_ms < 200 else discord.Color.orange(),
            timestamp=datetime.now()
        )
        
        await ctx.reply(embed=embed)
        logger.info(f"[DISCORD] Ping: {latency_ms}ms")

    @commands.hybrid_command(name="random")
    async def random_item(self, ctx):
        """Get a random item suggestion"""
        with self.data_manager.lock:
            cache = self.data_manager.cache
            # Filter out internal keys
            all_items = [k for k in cache.keys() if not k.startswith("_")]
            display_map = cache.get("_display", {})
        
        if not all_items:
            await ctx.reply("No items in cache yet. Try again later!")
            return
        
        # Pick a random item
        random_key = random.choice(all_items)
        display_name = display_map.get(random_key, random_key.title())
        found_locations = cache.get(random_key)
        
        if found_locations:
            embed = self.create_found_embed(ctx, display_name, found_locations, is_villager=False)
            
            if embed:
                embed.title = f"🎲 Random Item: {display_name}"
                await ctx.reply(content=f"Hey <@{ctx.author.id}>, here's a random item for you!", embed=embed)
                logger.info(f"[DISCORD] Random item: {random_key}")
            else:
                # Item exists but not on sub islands
                await ctx.reply(f"🎲 Random suggestion: **{display_name}** - use `!find {display_name}` to see where it's available!")
        else:
            await ctx.reply(f"🎲 Random suggestion: **{display_name}** - use `!find {display_name}` to check availability!")


    @commands.hybrid_command(name="status")
    @is_admin_or_senior_mod()
    async def status(self, ctx):
        """Show bot status"""
        with self.data_manager.lock:
            if self.data_manager.last_update:
                t_str = self.data_manager.last_update.strftime("%H:%M:%S")
                island_count = len(self.sub_island_lookup)
                
                # Calculate uptime
                uptime_seconds = (datetime.now() - self.bot.start_time).total_seconds()
                hours = int(uptime_seconds // 3600)
                minutes = int((uptime_seconds % 3600) // 60)
                uptime_str = f"{hours}h {minutes}m"
                
                await ctx.reply(
                    f"**System Status**\n"
                    f"Items Cached: `{len(self.data_manager.cache)}`\n"
                    f"Islands Linked: `{island_count}`\n"
                    f"Last Update: `{t_str}`\n"
                    f"Uptime: `{uptime_str}`"
                )
            else:
                await ctx.reply("Database loading...")

    @commands.hybrid_command(name="ask")
    @app_commands.describe(question="Your question about the Chopaeng community")
    async def ask_ai(self, ctx, *, question: str = ""):
        """Ask the Chopaeng AI anything about the community"""
        if not question:
            await ctx.reply("Usage: `!ask <question>` — e.g. `!ask how do I get a Dodo code?`")
            return

        await ctx.defer()
        conv_key = _discord_conv_key(ctx.message)
        channel_name = getattr(ctx.channel, "name", None)
        answer = await get_ai_answer(
            question,
            gemini_api_key=Config.GEMINI_API_KEY,
            openai_api_key=Config.OPENAI_API_KEY,
            openai_base_url=Config.OPENAI_BASE_URL,
            provider=Config.AI_PROVIDER,
            gemini_model=Config.GEMINI_MODEL,
            openai_model=Config.OPENAI_MODEL,
            conversation_key=conv_key,
            channel_context=channel_name,
            is_subscriber=_is_subscriber_member(ctx.author),
            is_mod_user=_is_mod_member(ctx.author),
            accessible_islands=_get_accessible_islands(ctx.author),
            has_ac_role=_has_animal_crossing_role(ctx.author),
        )

        await ctx.reply(f"{answer}")
        logger.info(f"[DISCORD] Ask command by {ctx.author.name}: {question[:80]}")

    @staticmethod
    def _progress_bar(filled: int, total: int, length: int = 14) -> str:
        if total == 0:
            return "░" * length
        filled_blocks = round((filled / total) * length)
        return "▰" * filled_blocks + "▱" * (length - filled_blocks)

    @staticmethod
    def _problem_lines(results: list, format_channel) -> list[str]:
        """Return formatted lines for only the non-online entries in a results list."""
        lines = []
        for n, s, _, c in results:
            if s == "✅":
                continue  # <-- skip islands that are actually online
            icon = "🔴" if s == "❌" else "❓"
            lines.append(f"{icon} {format_channel(n, c)}")
        return lines

    @staticmethod
    def _all_lines(results: list, format_channel) -> list[str]:
        """Return formatted lines for every island in a results list, online included.
        Matches the 🟢/🔴/❓ + channel-mention style used in the sticky status embed."""
        lines = []
        for n, s, _, c in results:
            icon = "🟢" if s == "✅" else "🔴" if s == "❌" else "❓"
            lines.append(f"{icon} {format_channel(n, c)}")
        return lines

    def _add_status_fields(
        self,
        embed: discord.Embed,
        name: str,
        emoji: str,
        results: list,
        online: int,
        total: int,
        format_channel,
        view: str,
    ) -> None:
        """Add island status field(s) for one category, formatted per the requested view.

        view == 'all'     -> every island, header shows 'Sub — 19/20' style, like the screenshot.
        view == 'summary' -> only non-online islands ('needs attention').
        """
        if view == "all":
            lines = self._all_lines(results, format_channel)
            if not lines:
                embed.add_field(name=f"{emoji} {name} — 0/0", value="*No islands configured.*", inline=True)
                return
            chunks = self._chunk_lines(lines)
            for i, chunk in enumerate(chunks):
                label = f"{emoji} {name} — {online}/{total}" if i == 0 else f"{emoji} {name} (cont.)"
                embed.add_field(name=label, value=chunk, inline=True)
        else:
            self._add_attention_fields(embed, name, self._problem_lines(results, format_channel))
            
    @staticmethod
    def _chunk_lines(lines: list[str], limit: int = 1024) -> list[str]:
        """Split a list of lines into chunks that each fit Discord's 1024-char field limit."""
        chunks, current = [], ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > limit:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or ["*none*"]

    def _add_attention_fields(self, embed: discord.Embed, name: str, lines: list[str]) -> None:
        """Add one or more 'needs attention' fields, chunked to fit the 1024-char limit."""
        if not lines:
            embed.add_field(name=f"{Config.STAR_PINK} {name} — All Clear", value="Every island is active.", inline=False)
            return
        chunks = self._chunk_lines(lines)
        for i, chunk in enumerate(chunks):
            label = f"{Config.EMOJI_FAIL} {name} — Needs Attention ({len(lines)})" if i == 0 else f"⚠️ {name} (cont.)"
            embed.add_field(name=label, value=chunk, inline=False)

    @commands.hybrid_command(name="islands", aliases=["islandstatus", "checkislands"])
    @app_commands.describe(
        island="Which islands to check: sub, free, order, or leave blank for all.",
        view="Display mode: 'summary' (problems only, default) or 'all' (every island with status).",
    )
    @app_commands.choices(
        island=[
            app_commands.Choice(name="Sub Islands",   value="sub"),
            app_commands.Choice(name="Free Islands",  value="free"),
            app_commands.Choice(name="Order Islands", value="order"),
        ],
        view=[
            app_commands.Choice(name="Summary (problems only)", value="summary"),
            app_commands.Choice(name="All (every island with status)", value="all"),
        ],
    )
    @is_admin_or_senior_mod()
    async def island_status(self, ctx, island: str = "", view: str = "summary"):
        """Check island bot status. Use 'sub', 'free', 'order', or leave blank for all."""
        await ctx.defer()

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            await ctx.reply("Guild not found.")
            return

        kind = island.strip().lower()
        view = view.strip().lower() if view else "summary"

        if kind and kind not in ("sub", "free", "order"):
            await ctx.reply("Usage: `/islands [sub|free|order]`", ephemeral=True)
            return

        if view not in ("summary", "all"):
            await ctx.reply("Usage: view must be 'summary' or 'all'", ephemeral=True)
            return

        show_sub   = kind in ("", "sub")
        show_free  = kind in ("", "free")
        show_order = kind in ("", "order")

        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None
        if Config.ISLAND_BOT_ROLE_ID and not island_bot_role:
            logger.warning(f"[DISCORD] ISLAND_BOT_ROLE_ID {Config.ISLAND_BOT_ROLE_ID} not found in guild; bot name matching disabled")

        # --- Sub island results ---
        sub_results: list = []
        sub_online = 0
        if show_sub:
            await self.fetch_islands()
            for isl in Config.SUB_ISLANDS:
                island_clean = clean_text(isl)
                channel_id = self.sub_island_lookup.get(island_clean)

                if not channel_id:
                    for ch in guild.channels:
                        if isinstance(ch, discord.TextChannel) and island_clean in clean_text(ch.name):
                            channel_id = ch.id
                            break

                if not channel_id:
                    sub_results.append((isl, "❓", "Channel not found", None))
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    sub_results.append((isl, "❓", "Channel not found", None))
                    continue

                island_bot = None
                if island_bot_role:
                    target = clean_text(f"chobot {isl}")
                    for member in island_bot_role.members:
                        if member.bot and clean_text(member.display_name) == target:
                            island_bot = member
                            break

                if island_bot and island_bot.status in ONLINE_DISCORD_STATUSES:
                    sub_results.append((isl, "✅", "Bot online", channel_id))
                    sub_online += 1
                    continue

                try:
                    messages = [msg async for msg in channel.history(limit=25)]
                except discord.Forbidden:
                    sub_results.append((isl, "❓", "No channel access", channel_id))
                    continue

                island_up = False
                status_reason = ""
                for msg in messages:
                    if island_bot:
                        if msg.author.id != island_bot.id:
                            continue
                    elif not msg.author.bot:
                        continue
                    if DODO_CODE_PATTERN.search(msg.content):
                        island_up = True
                        status_reason = "Dodo code active"
                        break
                    if ISLAND_HOST_NAME in msg.content.lower():
                        island_up = True
                        status_reason = "Chopaeng is visiting"
                        break

                if island_up:
                    sub_results.append((isl, "✅", status_reason, channel_id))
                    sub_online += 1
                else:
                    sub_results.append((isl, "❌", "No recent activity", channel_id))

        # --- Free island results ---
        free_results: list = []
        free_online = 0
        if show_free:
            await self.fetch_free_islands()
            for isl in Config.FREE_ISLANDS:
                island_clean = clean_text(isl)
                channel_id = self.free_island_lookup.get(island_clean)

                island_bot = None
                if island_bot_role:
                    target = clean_text(f"chobot {isl}")
                    for member in island_bot_role.members:
                        if member.bot and clean_text(member.display_name) == target:
                            island_bot = member
                            break

                if island_bot and island_bot.status in ONLINE_DISCORD_STATUSES:
                    free_results.append((isl, "✅", "Bot online", channel_id))
                    free_online += 1
                elif island_bot:
                    free_results.append((isl, "❌", "Bot offline", channel_id))
                else:
                    free_results.append((isl, "❓", "Bot not found", channel_id))

        # --- Order island results ---
        order_results: list = []
        order_online = 0
        if show_order:
            self._refresh_order_island_lookup()
            order_bot_member = None
            if Config.ORDER_BOT_DISCORD_ID:
                order_bot_member = guild.get_member(Config.ORDER_BOT_DISCORD_ID)
                if order_bot_member is None:
                    try:
                        order_bot_member = await guild.fetch_member(Config.ORDER_BOT_DISCORD_ID)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        order_bot_member = None

            for isl in getattr(Config, "ORDER_BOT_ISLANDS", []):
                island_clean = clean_text(isl)
                channel_id = self.order_island_lookup.get(island_clean)
                display_name = "Sinta"

                if order_bot_member and order_bot_member.status in ONLINE_DISCORD_STATUSES:
                    order_results.append((display_name, "✅", "Bot online", channel_id))
                    order_online += 1
                elif order_bot_member:
                    order_results.append((display_name, "❌", "Bot offline", channel_id))
                else:
                    order_results.append((display_name, "❓", "Bot not found", channel_id))

        # --- Build embed(s) ---
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP

        def format_channel(island_name: str, channel_id: int | None) -> str:
            if not channel_id:
                return f"{island_name}"
            ch = guild.get_channel(channel_id)
            if ch and ch.permissions_for(ctx.author).read_messages:
                return f"<#{channel_id}>"
            return f"{island_name}"

        if kind == "sub":
            total = len(Config.SUB_ISLANDS)
            title = "🏝️ Sub Island Status"
            desc = (
                f"{self._progress_bar(sub_online, total)}  **{sub_online}/{total}** online"
                if view == "summary"
                else f"**{sub_online}/{total}** islands active"
            )
            embed = discord.Embed(
                title=title,
                description=desc,
                color=discord.Color.green() if sub_online == total else (
                    discord.Color.orange() if sub_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            self._add_status_fields(embed, "Sub", "🏝️", sub_results, sub_online, total, format_channel, view)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Sub island status check: {sub_online}/{total} online")

        elif kind == "free":
            total = len(Config.FREE_ISLANDS)
            title = "🌴 Free Island Status"
            desc = (
                f"{self._progress_bar(free_online, total)}  **{free_online}/{total}** online"
                if view == "summary"
                else f"**{free_online}/{total}** islands active"
            )
            embed = discord.Embed(
                title=title,
                description=desc,
                color=discord.Color.green() if free_online == total else (
                    discord.Color.orange() if free_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            self._add_status_fields(embed, "Free", "🌴", free_results, free_online, total, format_channel, view)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Free island status check: {free_online}/{total} online")

        elif kind == "order":
            total = len(getattr(Config, "ORDER_BOT_ISLANDS", []))
            title = "📦 Order Island Status"
            desc = (
                f"{self._progress_bar(order_online, total)}  **{order_online}/{total}** online"
                if view == "summary"
                else f"**{order_online}/{total}** islands active"
            )
            embed = discord.Embed(
                title=title,
                description=desc,
                color=discord.Color.green() if order_online == total else (
                    discord.Color.orange() if order_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            self._add_status_fields(embed, "Order", "📦", order_results, order_online, total, format_channel, view)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Order island status check: {order_online}/{total} online")

        else:
            # Combined embed (no argument)
            sub_total   = len(Config.SUB_ISLANDS)
            free_total  = len(Config.FREE_ISLANDS)
            order_total = len(getattr(Config, "ORDER_BOT_ISLANDS", []))
            total       = sub_total + free_total + order_total
            combined_online = sub_online + free_online + order_online

            if view == "all":
                desc = f"**{combined_online}/{total}** islands active"
            else:
                desc = (
                    f"{self._progress_bar(combined_online, total)}  **{combined_online}/{total}** online\n\n"
                    f"🏝️ Sub `{sub_online}/{sub_total}` • 🌴 Free `{free_online}/{free_total}` • 📦 Order `{order_online}/{order_total}`"
                )

            embed = discord.Embed(
                title="🏝️ Island Status",
                description=desc,
                color=discord.Color.green() if combined_online == total else (
                    discord.Color.orange() if combined_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            self._add_status_fields(embed, "Sub",   "🏝️", sub_results,   sub_online,   sub_total,   format_channel, view)
            self._add_status_fields(embed, "Free",  "🌴", free_results,  free_online,  free_total,  format_channel, view)
            self._add_status_fields(embed, "Order", "📦", order_results, order_online, order_total, format_channel, view)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(
                f"[DISCORD] Combined island status: sub {sub_online}/{sub_total}, "
                f"free {free_online}/{free_total}, order {order_online}/{order_total}"
            )
        
        
    @commands.hybrid_command(name="senddodo", aliases=["sd"])
    async def send_dodo(self, ctx):
        """Send the dodo code to a user via DM"""
        if self._is_order_island_channel(ctx.channel):
            island_name = Config.ORDER_BOT_ISLAND or "This island"
            await ctx.reply(
                f"{island_name} is an order-bot island. Dodo access is handled by the order bot in this channel, so `!senddodo` / `!sd` is not available here.",
                ephemeral=True,
            )
            return
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel. Please read the sticky post below carefully and make sure you understand and follow all the <#783677194576330792> before agreeing to them.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        def dodo_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_DODO_SENT_PATTERN.search(msg.content)
            )

        # Register this member as waiting for a Dodo code so the island monitor
        # can DM them immediately if the island goes offline during the wait.
        island_clean_key = clean_text(ctx.channel.name)
        self.pending_dodo_waiters.setdefault(island_clean_key, []).append(ctx.author)
        try:
            island_msg = await self.bot.wait_for('message', check=dodo_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)
            await island_msg.delete()
            reply_msg = await ctx.reply(embed=self._build_dodo_sent_embed(ctx))
            logger.info(f"[DISCORD] Intercepted and redesigned !sd response for {ctx.channel.name}")
            await self._log_dodo_request_to_xlog(ctx, reply_msg)
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !sd response in {ctx.channel.name}")
        finally:
            # Always deregister — success, timeout, or unexpected error.
            waiters = self.pending_dodo_waiters.get(island_clean_key, [])
            if ctx.author in waiters:
                waiters.remove(ctx.author)

    @commands.hybrid_command(name="visitors")
    async def visitors(self, ctx):
        """Check current visitors on the sub island"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        def visitors_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_VISITORS_PATTERN.search(msg.content)
            )

        try:
            island_msg = await self.bot.wait_for('message', check=visitors_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)

            match = ISLAND_VISITORS_PATTERN.search(island_msg.content)
            island_name = match.group(1).strip() if match else ctx.channel.name

            visitor_lines = []
            for line in island_msg.content.split('\n'):
                m = VISITOR_LINE_PATTERN.match(line.strip())
                if m:
                    visitor_lines.append(m.group(1).strip())

            await island_msg.delete()
            await ctx.reply(embed=self._build_visitors_embed(ctx, island_name, visitor_lines))
            logger.info(f"[DISCORD] Intercepted and redesigned !visitors response for {ctx.channel.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !visitors response in {ctx.channel.name}")

    @commands.hybrid_command(name="villagers")
    async def villagers(self, ctx):
        """Check current villagers on the sub island"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        def villagers_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_VILLAGERS_PATTERN.search(msg.content)
            )

        try:
            island_msg = await self.bot.wait_for('message', check=villagers_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)

            match = ISLAND_VILLAGERS_PATTERN.search(island_msg.content)
            island_name = match.group(1).strip() if match else ctx.channel.name

            # Extract villagers from the message
            villager_text = island_msg.content
            if ":" in villager_text:
                villager_list = villager_text.split(":", 1)[1].strip()
                villagers_list = [v.strip() for v in villager_list.split(",")]
            else:
                villagers_list = []

            await island_msg.delete()
            await ctx.reply(embed=self._build_villagers_embed(ctx, island_name, villagers_list))
            logger.info(f"[DISCORD] Intercepted and redesigned !villagers response for {ctx.channel.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !villagers response in {ctx.channel.name}")

    @commands.hybrid_command(name="drop")
    async def drop(self, ctx):
        """Request item drop on the sub island"""
        if self._is_order_island_channel(ctx.channel):
            island_name = Config.ORDER_BOT_ISLAND or "This island"
            await ctx.reply(
                f"{island_name} is an order-bot island. Use the order bot flow for this channel; `!drop` is only available on sub islands.",
                ephemeral=True,
            )
            return
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        def drop_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_DROP_PATTERN.search(msg.content)
            )

        try:
            island_msg = await self.bot.wait_for('message', check=drop_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)
            await island_msg.delete()
            await ctx.reply(embed=self._build_drop_embed(ctx))
            logger.info(f"[DISCORD] Intercepted and redesigned !drop response for {ctx.channel.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !drop response in {ctx.channel.name}")

    @commands.hybrid_command(name="injectvillager", aliases=["iv"])
    async def inject_villager(self, ctx, villager_name: str):
        """Inject a villager onto the sub island"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        # First check for the queued message
        def inject_queued_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_INJECT_QUEUED_PATTERN.search(msg.content)
            )

        # Then check for the completion message
        def inject_complete_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_INJECT_COMPLETE_PATTERN.search(msg.content)
            )

        try:
            # Wait for the queued confirmation
            queued_msg = await self.bot.wait_for('message', check=inject_queued_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)
            await queued_msg.delete()

            # Wait for the completion message
            complete_msg = await self.bot.wait_for('message', check=inject_complete_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT + 10)
            
            match = ISLAND_INJECT_COMPLETE_PATTERN.search(complete_msg.content)
            injected_villager = match.group(1).strip() if match else villager_name
            injected_index = match.group(2) if match else "0"
            
            await complete_msg.delete()
            await ctx.reply(embed=self._build_inject_villager_embed(ctx, injected_villager, injected_index))
            logger.info(f"[DISCORD] Intercepted and redesigned !injectvillager response for {ctx.channel.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !injectvillager response in {ctx.channel.name}")

    @commands.hybrid_command(name="mvi")
    async def multi_inject_villager(self, ctx, villagers: str):
        """Inject multiple villagers onto the sub island"""

        villager_names = [name.strip() for name in villagers.split(",") if name.strip()]

        if not villager_names:
            await ctx.reply("Please provide at least one villager name.", ephemeral=True)
            return

        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        # 🔥 SEND COMMAND TO ISLAND BOT (missing in your code)
        command_str = f"!mvi {' '.join(villager_names)}"
        await ctx.send(command_str)

        def multi_inject_queued_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_INJECT_MULTI_QUEUED_PATTERN.search(msg.content)
            )

        def inject_complete_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_INJECT_COMPLETE_PATTERN.search(msg.content)
            )

        try:
            # Wait for queued confirmation
            queued_msg = await self.bot.wait_for(
                'message',
                check=multi_inject_queued_check,
                timeout=ISLAND_BOT_INTERCEPT_TIMEOUT
            )

            match = ISLAND_INJECT_MULTI_QUEUED_PATTERN.search(queued_msg.content)
            num_villagers = int(match.group(1)) if match else len(villager_names)

            await queued_msg.delete()

            injected_villagers = []

            for _ in range(num_villagers):
                complete_msg = await self.bot.wait_for(
                    'message',
                    check=inject_complete_check,
                    timeout=ISLAND_BOT_INTERCEPT_TIMEOUT + 10
                )

                match = ISLAND_INJECT_COMPLETE_PATTERN.search(complete_msg.content)
                if match:
                    villager = match.group(1).strip()
                    index = match.group(2)
                    injected_villagers.append((villager, index))

                await complete_msg.delete()

            # fallback if nothing parsed
            if not injected_villagers:
                raise asyncio.TimeoutError

            await ctx.reply(embed=self._build_multi_inject_villager_embed(ctx, injected_villagers))
            logger.info(f"[DISCORD] Intercepted and redesigned !mvi response for {ctx.channel.name}")

        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !mvi response in {ctx.channel.name}")
            
    def _get_island_name_for_channel(self, channel: discord.TextChannel) -> str | None:
        """Return the island name for the given sub/order channel, or None if unknown."""
        chan_clean = clean_text(channel.name)
        for island in list(Config.SUB_ISLANDS) + list(getattr(Config, "ORDER_BOT_ISLANDS", [])):
            if clean_text(island) in chan_clean:
                return island
        return None

    def _get_island_bot_for_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        """Return the island bot member for the given channel, or None if not found."""
        island = self._get_island_name_for_channel(channel)
        if not island:
            return None

        target = clean_text(f"chobot {island}")
        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None

        if island_bot_role:
            for member in island_bot_role.members:
                if member.bot and clean_text(member.display_name) == target:
                    return member

        for member in guild.members:
            if member.bot and clean_text(member.display_name) == target:
                return member

        return None

    async def _is_channel_online(self, guild: discord.Guild, channel: discord.TextChannel) -> bool:
        """Check if the island channel is online by member status or fallback history."""
        island_name = self._get_island_name_for_channel(channel)
        if not island_name:
            return False

        island_clean = clean_text(island_name)
        if island_clean in self.order_island_lookup:
            lookup = self.order_island_lookup
        elif island_clean in self.free_island_lookup:
            lookup = self.free_island_lookup
        else:
            lookup = self.sub_island_lookup

        return await self._check_island_online(guild, island_name, lookup=lookup)

    def _is_sub_island_channel(self, channel) -> bool:
        """Return True if the channel belongs to the sub-islands category."""
        if not Config.CATEGORY_ID:
            return False
        return getattr(channel, "category_id", None) == Config.CATEGORY_ID

    def _is_order_island_channel(self, channel) -> bool:
        """Return True for the fixed order-bot island channel."""
        return bool(Config.ORDER_BOT_CHANNEL_ID and getattr(channel, "id", None) == Config.ORDER_BOT_CHANNEL_ID)


    def _build_status_embed(self, ctx, title: str, description: str, color: discord.Color) -> discord.Embed:
        """Build a status embed with the given title, description and color."""
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_image(url=Config.FOOTER_LINE)
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        return embed

    def _create_island_down_embed(self, ctx) -> discord.Embed:
        """Build the standard 'island is down' embed."""
        return self._build_status_embed(
            ctx,
            title="🏝️ Island is Down",
            description=(
                "This island is currently **offline** or no information is available.\n\n"
                "Please use another island in the meantime or wait for this island to come back up."
            ),
            color=discord.Color.red(),
        )

    def _build_visitors_embed(self, ctx, island_name: str, visitor_lines: list) -> discord.Embed:
        """Build a nicely formatted visitors embed from a parsed visitor list."""
        filled = [v for v in visitor_lines if v.lower() != AVAILABLE_SLOT_TEXT]
        total = len(visitor_lines)
        available = total - len(filled)

        visitor_display = []
        for i, v in enumerate(visitor_lines, 1):
            if v.lower() == AVAILABLE_SLOT_TEXT:
                visitor_display.append(f"`#{i}` 〰️ *Available*")
            else:
                visitor_display.append(f"`#{i}` 🧑‍🤝‍🧑 **{v}**")

        color = discord.Color.green() if available > 0 else discord.Color.red()
        embed = discord.Embed(
            title=f"🏝Visitors on {island_name}",
            description="\n".join(visitor_display) if visitor_display else "*No visitor data available.*",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Slots",
            value=f"`{len(filled)}/{total}` occupied · `{available}` available",
            inline=False
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    def _build_villagers_embed(self, ctx, island_name: str, villagers_list: list) -> discord.Embed:
        """Build a nicely formatted villagers embed from a parsed villager list."""
        total = len(villagers_list)

        villager_display = []
        for i, v in enumerate(villagers_list, 1):
            villager_display.append(f"`#{i}` 🏠 **{v}**")

        color = discord.Color.green() if total > 0 else discord.Color.orange()
        embed = discord.Embed(
            title=f"🏝️ Villagers on {island_name}",
            description="\n".join(villager_display) if villager_display else "*No villager data available.*",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Residents",
            value=f"`{total}/10` villagers",
            inline=False
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    def _build_drop_embed(self, ctx) -> discord.Embed:
        """Build a nicely formatted item drop embed."""
        embed = discord.Embed(
            title="📦 Item Drop Requested",
            description="Item drop request will be executed momentarily.",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    def _build_inject_villager_embed(self, ctx, villager_name: str, index: str) -> discord.Embed:
        """Build a nicely formatted villager injection embed."""
        embed = discord.Embed(
            title="✨ Villager Injected",
            description=f"**{villager_name}** has been injected onto the island!",
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Villager",
            value=f"{villager_name}",
            inline=False
        )
        embed.add_field(
            name="Plot Index",
            value=f"`{index}`",
            inline=False
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    def _build_multi_inject_villager_embed(self, ctx, injected_villagers: list) -> discord.Embed:
        """Build a nicely formatted embed for multiple villager injections."""
        villager_display = []
        for villager, index in injected_villagers:
            villager_display.append(f"`#{index}` 🏠 **{villager}**")

        embed = discord.Embed(
            title="✨ Villagers Injected",
            description=f"**{len(injected_villagers)}** villagers have been injected onto the island!",
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Villagers",
            value="\n".join(villager_display) if villager_display else "*No villagers injected.*",
            inline=False
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    def _build_dodo_sent_embed(self, ctx) -> discord.Embed:
        """Build a nicely formatted 'dodo code sent' embed."""
        embed = discord.Embed(
            title="✈️ Dodo Code Sent!",
            description=(
                f"Hey {ctx.author.mention}! The dodo code has been sent to your DMs.\n\n"
                "Head to the airport and open the **Dodo Airlines** app to enter it!"
            ),
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP

        warning = nickname_warning_for(ctx.author.display_name)
        if warning:
            embed.add_field(
                name="Nickname Reminder",
                value=warning,
                inline=False,
            )

        embed.add_field(
            name="<:ChoLove:818216528449241128> Reminder",
            value=random.choice(DODO_SENT_TIPS),
            inline=False,
        )

        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    async def _log_dodo_request_to_xlog(self, ctx, reply_msg: discord.Message | None) -> None:
        """Post a notification to the xlog channel when a user successfully requests the dodo code.

        If the flight logger is available, defers the xlog post until the user is seen joining
        (or DODO_XLOG_TIMEOUT seconds elapse, whichever comes first).
        """
        xlog_channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
        if not xlog_channel:
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        guild_icon = guild.icon.url if guild and guild.icon else None

        flight_cog = self.bot.get_cog('FlightLoggerCog')
        if flight_cog:
            flight_cog.register_dodo_request(ctx.author.id, ctx.author, ctx.channel, reply_msg, guild_icon)

            async def _guarded_fallback():
                try:
                    await self._post_dodo_xlog_fallback(ctx, reply_msg, guild_icon, xlog_channel)
                except Exception as e:
                    logger.warning(f"[DISCORD] Dodo xlog fallback task failed: {e}")

            asyncio.create_task(_guarded_fallback())
            return

        await self._send_dodo_request_xlog(ctx, reply_msg, guild_icon, xlog_channel)

    async def _post_dodo_xlog_fallback(self, ctx, reply_msg: discord.Message | None, guild_icon: str | None, xlog_channel) -> None:
        """After DODO_XLOG_TIMEOUT, post the dodo-request xlog if the flight logger hasn't already merged it."""
        await asyncio.sleep(DODO_XLOG_TIMEOUT)
        flight_cog = self.bot.get_cog('FlightLoggerCog')
        if flight_cog:
            pending = flight_cog.pop_pending_dodo_request(ctx.author.id)
            if pending is None:
                return  # Already merged into the verified-flight xlog entry

        visit_id = None
        try:
            guild = self.bot.get_guild(Config.GUILD_ID)
            if flight_cog and guild:
                visit_id = await flight_cog.get_recent_visit_id_by_user(ctx.author.id, guild.id)
        except Exception as e:
            logger.warning(f"[DISCORD] Could not look up visit ID for xlog fallback: {e}")

        await self._send_dodo_request_xlog(ctx, reply_msg, guild_icon, xlog_channel, visit_id=visit_id)

    async def _send_dodo_request_xlog(self, ctx, reply_msg: discord.Message | None, guild_icon: str | None, xlog_channel, visit_id: int | None = None) -> None:
        """Build and send the dodo-request embed to xlog."""
        embed = discord.Embed(
            title="✈️ Dodo Code Requested",
            description=(
                f"{ctx.author.mention} requested the dodo code in {ctx.channel.mention}."
            ),
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Member",  value=f"{ctx.author.mention} ({ctx.author.display_name})", inline=True)
        embed.add_field(name="Channel", value=ctx.channel.mention,                                  inline=True)
        if visit_id is not None:
            embed.add_field(name="Visit ID", value=f"`#{visit_id}`",                                inline=True)
        embed.set_image(url=Config.FOOTER_LINE)
        embed.set_footer(text="Chopaeng Camp™ • Dodo Request", icon_url=guild_icon)

        view = discord.ui.View()
        if reply_msg:
            view.add_item(discord.ui.Button(label="View Request", url=reply_msg.jump_url, style=discord.ButtonStyle.link))

        try:
            await xlog_channel.send(embed=embed, view=view)
        except Exception as e:
            logger.warning(f"[DISCORD] Failed to post dodo request to xlog: {e}")

    async def _check_island_online(self, guild: discord.Guild, island: str, lookup: dict | None = None) -> bool:
        """Return True if the island appears to be online, False otherwise.

        ``lookup`` should be the channel-name → channel-id mapping for the island
        type being checked (sub or free).  Keys must be normalised with
        ``clean_text()`` — the same normalisation applied when the lookup was
        built.  Defaults to ``self.sub_island_lookup``.
        """
        island_clean = clean_text(island)
        order_island_keys = {clean_text(name) for name in getattr(Config, "ORDER_BOT_ISLANDS", [])}
        is_order_island = island_clean in order_island_keys
        effective_lookup = lookup if lookup is not None else self.sub_island_lookup
        channel_id = effective_lookup.get(island_clean)
        if not channel_id:
            return False

        channel = guild.get_channel(channel_id)
        if not channel:
            return False

        if (
            Config.ORDER_BOT_DISCORD_ID
            and is_order_island
        ):
            order_bot = guild.get_member(Config.ORDER_BOT_DISCORD_ID)
            if order_bot is None:
                try:
                    order_bot = await guild.fetch_member(Config.ORDER_BOT_DISCORD_ID)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    order_bot = None
            if order_bot:
                if order_bot.status in ONLINE_DISCORD_STATUSES:
                    return True
                logger.info(
                    f"[DISCORD] Order bot {Config.ORDER_BOT_DISCORD_ID} status for {island}: {order_bot.status}"
                )

        # Check island bot presence first (fast, no API call)
        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None
        island_bot = None
        if island_bot_role:
            target = clean_text(f"chobot {island}")
            for member in island_bot_role.members:
                if member.bot and clean_text(member.display_name) == target:
                    island_bot = member
                    break

        if island_bot:
            return island_bot.status in ONLINE_DISCORD_STATUSES

        # Fallback: scan recent channel messages for dodo code / host presence
        try:
            messages = [msg async for msg in channel.history(limit=MESSAGE_HISTORY_LIMIT)]
        except discord.Forbidden:
            return False

        for msg in messages:
            if not msg.author.bot:
                continue
            if is_order_island and Config.ORDER_BOT_DISCORD_ID and msg.author.id != Config.ORDER_BOT_DISCORD_ID:
                continue
            content = msg.content or ""
            if (
                DODO_CODE_PATTERN.search(content)
                or ISLAND_HOST_NAME in content.lower()
                or ISLAND_DROP_PATTERN.search(content)
                or ISLAND_INJECT_QUEUED_PATTERN.search(content)
                or ISLAND_INJECT_MULTI_QUEUED_PATTERN.search(content)
                or ISLAND_INJECT_COMPLETE_PATTERN.search(content)
            ):
                return True

        return False


    async def _send_order_island_status_alert(self, channel: discord.TextChannel, island_display: str, online: bool) -> None:
        """Send Sinta/order-bot island status transitions into the configured order channel."""
        if online:
            embed = discord.Embed(
                title="Order Island is Back Online",
                description=(
                    f"**{island_display}** is back online for order-bot visits.\n"
                    "Use the order bot flow in this channel. `!senddodo`, `!sd`, and `!drop` are not used here."
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_image(url=Config.FOOTER_LINE)
        else:
            embed = discord.Embed(
                title="Order Island is Offline",
                description=f"**{island_display}** is currently offline. Please wait for it to come back up before using the order bot.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_image(url=ISLAND_DOWN_IMAGE_URL)
        try:
            await channel.send(embed=embed)
            logger.info(f"[DISCORD] Order island monitor: {island_display} is {'ONLINE' if online else 'OFFLINE'}")
        except Exception as exc:
            logger.error(f"[DISCORD] Failed to send order island status alert for {island_display}: {exc}")

    @tasks.loop(seconds=300)
    async def island_monitor_loop(self):
        """Background task: detect island down/up transitions and notify in channel."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return

        if not self.sub_island_lookup:
            try:
                await self.fetch_islands()
            except Exception as e:
                logger.error(f"[DISCORD] island_monitor_loop failed to fetch islands: {e}")
                return
        self._refresh_order_island_lookup()

        for island in Config.SUB_ISLANDS:
            island_clean = clean_text(island)
            channel_id = self.sub_island_lookup.get(island_clean)
            if not channel_id:
                continue

            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            try:
                is_online = await self._check_island_online(guild, island)
            except Exception as e:
                logger.error(f"[DISCORD] island_monitor_loop error checking {island}: {e}")
                continue

            # Persist current status to the database so the REST API can expose it
            _upsert_bot_status(island.lower(), island, is_online)

            previous = self.island_down_states.get(island_clean)  # None = first run

            if previous is None:
                # First run: always initialize as "not down" so that a "back up"
                # notification is only ever sent after we have sent a "Bot is Down"
                # embed in this session (i.e. never on a cold start when the island
                # is already online).
                self.island_down_states[island_clean] = False
                continue

            was_down = previous  # True means it was down

            if not is_online and not was_down:
                # Transition: online → offline
                self.island_down_states[island_clean] = True
                embed = discord.Embed(
                    title="🏝️ Island is Down",
                    description=f"**{island}** island is currently **offline**.",
                    color=discord.Color.red(),
                    timestamp=discord.utils.utcnow()
                )
                embed.set_image(url=ISLAND_DOWN_IMAGE_URL)
                try:
                    msg = await channel.send(embed=embed)
                    self.island_down_messages[island_clean] = msg
                    logger.info(f"[DISCORD] Island monitor: {island} went OFFLINE")
                except Exception as e:
                    logger.error(f"[DISCORD] Failed to send island-down embed for {island}: {e}")

                # DM any members who are mid-!senddodo wait for this island so
                # they aren't left hanging until their wait_for times out.
                waiters = self.pending_dodo_waiters.pop(island_clean, [])
                for member in waiters:
                    try:
                        await member.send(
                            f"🏝️ **{island}** just went offline while you were waiting for a Dodo code.\n"
                            f"Sorry about that! You can try another sub island in the meantime, "
                            f"or check back once it comes back up."
                        )
                        logger.info(f"[DISCORD] Sent island-down DM to {member} for {island}")
                    except discord.Forbidden:
                        logger.debug(f"[DISCORD] Could not DM {member} (DMs disabled) for {island} down notice")
                    except Exception as exc:
                        logger.warning(f"[DISCORD] Failed to DM island-down notice to {member} for {island}: {exc}")

            elif is_online and was_down:
                # Transition: offline → online
                self.island_down_states[island_clean] = False
                # Remove the sticky "island is down" embed
                sticky_msg = self.island_down_messages.pop(island_clean, None)
                if sticky_msg:
                    try:
                        await sticky_msg.delete()
                    except discord.NotFound:
                        pass  # Already deleted externally — nothing to do
                    except Exception as e:
                        logger.warning(f"[DISCORD] Could not delete sticky down embed for {island}: {e}")
                embed = discord.Embed(
                    title="🏝️ Island is Back Up!",
                    description=f"**{island}** island is back online and ready to visit! 🎉",
                    color=discord.Color.green(),
                    timestamp=discord.utils.utcnow()
                )
                embed.set_image(url=Config.FOOTER_LINE)
                try:
                    await channel.send(embed=embed)
                    logger.info(f"[DISCORD] Island monitor: {island} is back ONLINE")
                except Exception as e:
                    logger.error(f"[DISCORD] Failed to send island-back-up embed for {island}: {e}")

        # --- Free island status ---
        if self.free_island_lookup:
            for island in Config.FREE_ISLANDS:
                free_island_clean = clean_text(island)
                try:
                    is_online = await self._check_island_online(guild, island, lookup=self.free_island_lookup)
                except Exception as e:
                    logger.error(f"[DISCORD] island_monitor_loop error checking free island {island}: {e}")
                    continue
                _upsert_bot_status(island.lower(), island, is_online)

                free_was_down = self.island_down_states.get(f"free:{free_island_clean}")
                if free_was_down is None:
                    self.island_down_states[f"free:{free_island_clean}"] = False
                    continue
                if not is_online and not free_was_down:
                    self.island_down_states[f"free:{free_island_clean}"] = True
                elif is_online and free_was_down:
                    self.island_down_states[f"free:{free_island_clean}"] = False

        # --- Order-bot island status ---
        if self.order_island_lookup:
            for island in Config.ORDER_BOT_ISLANDS:
                order_island_clean = clean_text(island)
                channel_id = self.order_island_lookup.get(order_island_clean)
                channel = guild.get_channel(channel_id) if channel_id else None
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    is_online = await self._check_island_online(guild, island, lookup=self.order_island_lookup)
                except Exception as e:
                    logger.error(f"[DISCORD] island_monitor_loop error checking order island {island}: {e}")
                    continue
                _upsert_bot_status(island.lower(), island, is_online)

                state_key = f"order:{order_island_clean}"
                order_was_down = self.island_down_states.get(state_key)
                if order_was_down is None:
                    self.island_down_states[state_key] = False
                    continue
                if not is_online and not order_was_down:
                    self.island_down_states[state_key] = True
                    await self._send_order_island_status_alert(channel, island, online=False)
                elif is_online and order_was_down:
                    self.island_down_states[state_key] = False
                    await self._send_order_island_status_alert(channel, island, online=True)

        # Sticky message is managed by island_status_sticky_loop; just
        # request a non-reposting (edit-in-place) refresh so the embed
        # data stays current without sending duplicate messages.
        try:
            await self._refresh_island_status_sticky_message(force_repost=False)
        except Exception as exc:
            logger.warning(f"[DISCORD] Failed to refresh island status sticky embed: {exc}")

    @island_monitor_loop.before_loop
    async def before_island_monitor_loop(self):
        """Wait until bot is ready before starting the island monitor."""
        await self.bot.wait_until_ready()
        await self.fetch_islands()
        await self.fetch_free_islands()
        self._refresh_order_island_lookup()
        # Note: sticky message posting is handled by island_status_sticky_loop.
        # No need to force_repost here — it would race with the sticky loop.
    # ── Island autocomplete ────────────────────────────────────

    async def island_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete helper: combines sub + free island names."""
        all_islands = sorted(
            set(self.sub_island_lookup.keys()) | set(self.free_island_lookup.keys())
        )
        current_lower = current.lower()
        matches = [n for n in all_islands if current_lower in n] if current else all_islands
        return [
            app_commands.Choice(name=name.title(), value=name)
            for name in matches[:25]
        ]


    
    @commands.hybrid_command(name="island", aliases=["islandinfo", "ii"])
    @app_commands.describe(island="The island to look up. Leave blank to auto-detect from the current channel.")
    @app_commands.autocomplete(island=island_name_autocomplete)
    async def island_info(self, ctx, *, island: str = ""):
        """Show full island details: map, status, visitors, description, items, and residents.

        Run with no arguments inside an island's own channel to auto-detect it;
        otherwise pass a name, e.g. `!island alapaap`.
        """
        if self.check_cooldown(str(ctx.author.id)):
            return

        island_clean = clean_text(island) if island else ""
        auto_detected = False

        if not island_clean:
            auto_name = self._resolve_island_by_channel(ctx.channel.id)
            if not auto_name:
                await ctx.reply(
                    "Usage: `!island <name>` \u2014 e.g. `!island alapaap`\n"
                    "Or just run `!island` with no name inside an island's own channel to look it up automatically.",
                    ephemeral=True,
                )
                return
            island_clean = clean_text(auto_name)
            auto_detected = True

        await ctx.defer()

        island_map, _ = await self._fetch_islands_api_snapshot()
        record = (island_map or {}).get(island_clean)

        if not record:
            if auto_detected:
                # This channel is linked to an island in the DB, but the live API
                # snapshot doesn't have it (e.g. API lag or a renamed island) —
                # say so plainly instead of pretending it's a typo to fuzzy-match.
                await ctx.reply(
                    f"This channel is linked to **{island_clean.title()}**, but I couldn't "
                    "find live data for it right now. Please try again shortly."
                )
                logger.warning(f"[DISCORD] /island auto-detected {island_clean} for channel {ctx.channel.id} but API had no record")
                return

            all_islands = sorted((island_map or {}).keys())
            suggestion = ""
            if all_islands:
                best = process.extractOne(island_clean, all_islands, scorer=fuzz.ratio)
                if best and best[1] >= 60:
                    suggestion = f" Did you mean **{best[0].title()}**?"
            await ctx.reply(f"Island **{island_clean.title()}** not found.{suggestion}")
            logger.info(f"[DISCORD] /island miss: {island_clean}")
            return

        embed = await self._build_island_info_embed(ctx, record, island_clean)
        await ctx.reply(embed=embed)
        logger.info(
            f"[DISCORD] /island lookup by {ctx.author.name}: {island_clean}"
            + (" (auto-detected)" if auto_detected else "")
        )


    async def revive_island_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete island names for revive based on the selected fleet."""
        fleet_value = None
        if getattr(interaction, "namespace", None) is not None:
            fleet_value = getattr(interaction.namespace, "fleet", None)

        if fleet_value is None:
            for option in interaction.data.get("options", []):
                if option.get("name") == "fleet":
                    fleet_value = option.get("value")
                    break

        if str(fleet_value) == "2":
            candidates = list(self.sub_island_lookup.keys()) or list(getattr(Config, "SUB_ISLANDS", []))
        else:
            candidates = list(
                set(self.free_island_lookup.keys())
                | set(self.order_island_lookup.keys())
            )
            if not candidates:
                candidates = list(getattr(Config, "FREE_ISLANDS", [])) + list(
                    getattr(Config, "ORDER_BOT_ISLANDS", [])
                )

        current_lower = current.lower()
        matches = [name for name in candidates if current_lower in name] if current else candidates
        return [
            app_commands.Choice(name=name.title(), value=name)
            for name in sorted(matches)[:25]
        ]

    REVIVE_OUTPUT_MAX_LENGTH = 900

    def _build_revive_embed(
        self,
        *,
        title: str,
        color: discord.Color,
        cleaned: str,
        fleet_label: str,
        description: str | None = None,
        output: str | None = None,
        errout: str | None = None,
        elapsed: float | None = None,
        requester: discord.abc.User | None = None,
    ) -> discord.Embed:
        """Build a consistently styled embed for every stage of the revive flow."""
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name=f"Island Name", value=f"{cleaned.upper()}", inline=True)
        embed.add_field(name=f"Island Type", value=fleet_label, inline=True)
        if elapsed is not None:
            embed.add_field(name=f"Duration", value=f"{elapsed:.1f}s", inline=True)

        if output:
            trimmed = output[: self.REVIVE_OUTPUT_MAX_LENGTH]
            suffix = "\n…(truncated)" if len(output) > self.REVIVE_OUTPUT_MAX_LENGTH else ""
            embed.add_field(name="Output", value=f"```\n{trimmed}{suffix}\n```", inline=False)
        if errout:
            trimmed_err = errout[: self.REVIVE_OUTPUT_MAX_LENGTH]
            suffix_err = "\n…(truncated)" if len(errout) > self.REVIVE_OUTPUT_MAX_LENGTH else ""
            embed.add_field(name="Error Output", value=f"```\n{trimmed_err}{suffix_err}\n```", inline=False)

        if requester:
            pfp_url = requester.avatar.url if requester.avatar else Config.DEFAULT_PFP
            embed.set_footer(text=f"Requested by {requester.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    @staticmethod
    async def _send_or_edit(
        existing_msg: discord.Message | None,
        responder,
        embed: discord.Embed,
    ) -> discord.Message:
        """Send a new message via *responder* if none exists yet, otherwise edit *existing_msg*.

        Always returns the resulting message so callers can keep editing it on later steps.
        """
        if existing_msg is not None:
            await existing_msg.edit(embed=embed)
            return existing_msg
        return await responder(embed=embed)


    async def _get_island_online_status(
        self, guild: discord.Guild, island: str, fleet: str
    ) -> bool | None:
        """Check whether *island* is currently online for the given fleet.

        Returns True/False if determinable, or None if the island isn't found
        in any known lookup (so we can't say either way).
        """
        island_clean = clean_text(island)
        if str(fleet) == "2":
            lookup = self.sub_island_lookup
        else:
            # Free/Order fleet — check whichever lookup actually has it
            if island_clean in self.order_island_lookup:
                lookup = self.order_island_lookup
            else:
                lookup = self.free_island_lookup

        if island_clean not in lookup:
            return None

        try:
            return await self._check_island_online(guild, island, lookup=lookup)
        except Exception as exc:
            logger.warning(f"[DISCORD] Could not determine online status for {island}: {exc}")
            return None

    @commands.hybrid_command(name="revive", description="Restart a crashed island, or revive all offline islands")
    @app_commands.describe(
        fleet="Island Type",
        island='Island Name — or "all" to revive every offline island',
    )
    @app_commands.choices(
        fleet=[
            app_commands.Choice(name="Free/Order Island", value="1"),
            app_commands.Choice(name="Sub Island", value="2"),
        ]
    )
    @app_commands.autocomplete(island=revive_island_autocomplete)
    @is_admin_or_senior_mod()
    async def revive(self, ctx, fleet: str, island: str = "all"):
        """Restart a crashed island, or revive all offline islands (Admin or Senior Mod only).

        Usage:
          /revive <fleet>          → revive all offline islands in that fleet
          /revive <fleet> all      → same as above
          /revive <fleet> <island> → revive a single island
        """
        fleet_label = {
            "1": "Free/Order Island",
            "2": "Sub Island",
        }.get(str(fleet), str(fleet))

        # ── shared defer / responder setup ───────────────────────────────────
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer(thinking=True)
            responder = interaction.followup.send
        else:
            responder = ctx.reply

        # wait=True needed for followup.send so we get back an editable message
        send_kwargs = {"wait": True} if interaction is not None else {}

        # ── helper shared by both modes ───────────────────────────────────────
        def _truncate_list(names: list[str], limit: int = 1000) -> str:
            """Join island names, capped at Discord's 1024-char field limit."""
            joined = ", ".join(n.upper() for n in names)
            return joined[:limit] + "…" if len(joined) > limit else joined

        # ════════════════════════════════════════════════════════════════════
        # BULK MODE  —  /revive <fleet>   or   /revive <fleet> all
        # ════════════════════════════════════════════════════════════════════
        if island.strip().lower() == "all":
            if str(fleet) == "2":
                candidates = list(self.sub_island_lookup.keys()) or list(getattr(Config, "SUB_ISLANDS", []))
            else:
                candidates = list(
                    set(self.free_island_lookup.keys())
                    | set(self.order_island_lookup.keys())
                )
                if not candidates:
                    candidates = list(getattr(Config, "FREE_ISLANDS", [])) + ["sinta"]

            if not candidates:
                await responder(
                    embed=discord.Embed(
                        title="No Islands Found",
                        description=f"No {fleet_label}s are registered in the cache. Try `/refresh` first.",
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                    **send_kwargs,
                )
                return

            guild = self.bot.get_guild(Config.GUILD_ID)

            status_msg = await responder(
                embed=discord.Embed(
                    title=f"{Config.EMOJI_SEARCH} Checking Islands…",
                    description=f"Scanning {len(candidates)} {fleet_label}(s) for offline status…",
                    color=discord.Color.blurple(),
                ),
                **send_kwargs,
            )

            offline_islands = []
            for i, cand in enumerate(candidates, 1):
                is_online = await self._get_island_online_status(guild, cand, fleet) if guild else None
                if is_online is False:
                    offline_islands.append(cand)
                # Update progress every 5 islands to avoid rate-limiting
                if i % 5 == 0 or i == len(candidates):
                    await status_msg.edit(
                        embed=discord.Embed(
                            title=f"{Config.EMOJI_SEARCH} Checking Islands…",
                            description=(
                                f"Scanned **{i}/{len(candidates)}** islands…\n"
                                f"Found **{len(offline_islands)}** offline so far."
                            ),
                            color=discord.Color.blurple(),
                        )
                    )

            if not offline_islands:
                await status_msg.edit(
                    embed=discord.Embed(
                        title="All Islands Online",
                        description=f"All {len(candidates)} {fleet_label}(s) appear to be online.",
                        color=discord.Color.green(),
                    )
                )
                return

            confirm_view = RebootConfirmView(author_id=ctx.author.id)
            await status_msg.edit(
                embed=discord.Embed(
                    title="Confirm Bulk Reboot",
                    description=(
                        f"Found **{len(offline_islands)}** offline {fleet_label}(s):\n"
                        f"`{_truncate_list(offline_islands)}`\n\n"
                        "Islands will be restarted **sequentially**. Continue?"
                    ),
                    color=discord.Color.orange(),
                ),
                view=confirm_view,
            )

            await confirm_view.wait()

            if confirm_view.result is not True:
                cancel_embed = discord.Embed(
                    title="Reboot Cancelled" if confirm_view.result is False else "⌛ Confirmation Timed Out",
                    description="No changes were made.",
                    color=discord.Color.greyple(),
                )
                if confirm_view.interaction is not None:
                    await confirm_view.interaction.response.edit_message(embed=cancel_embed, view=None)
                else:
                    await status_msg.edit(embed=cancel_embed, view=None)
                return

            if confirm_view.interaction is not None:
                await confirm_view.interaction.response.edit_message(view=None)

            success = []
            failed = []

            for idx, isl in enumerate(offline_islands, 1):
                await status_msg.edit(
                    embed=discord.Embed(
                        title="Bulk Reboot in Progress",
                        description=(
                            f"Rebooting **{isl.upper()}** ({idx}/{len(offline_islands)})…\n\n"
                            f"Done: {len(success)}  |  {Config.EMOJI_FAIL} Failed: {len(failed)}"
                        ),
                        color=discord.Color.blurple(),
                    )
                )
                async with self._revive_lock:
                    try:
                        result = await asyncio.to_thread(
                            subprocess.run,
                            [ISLAND_REVIVE_BATCH_PATH, str(fleet), isl],
                            capture_output=True,
                            text=True,
                            timeout=60,
                            shell=True,
                        )
                        if result.returncode == 0:
                            success.append(isl)
                        else:
                            failed.append(isl)
                    except subprocess.TimeoutExpired:
                        failed.append(isl)

            embed = discord.Embed(
                title="Bulk Reboot Complete" if not failed else f"{Config.EMOJI_FAIL} Bulk Reboot Complete (with errors)",
                color=discord.Color.green() if not failed else discord.Color.orange(),
            )
            embed.add_field(
                name=f"Successful ({len(success)})",
                value=_truncate_list(success) if success else "None",
                inline=False,
            )
            if failed:
                embed.add_field(
                    name=f"{Config.EMOJI_FAIL} Failed / Timed Out ({len(failed)})",
                    value=_truncate_list(failed),
                    inline=False,
                )
            logger.info(
                "[DISCORD] Bulk revive by %s — fleet=%s, success=%s, failed=%s",
                ctx.author, fleet_label, success, failed,
            )
            await status_msg.edit(embed=embed)
            return

        # ════════════════════════════════════════════════════════════════════
        # SINGLE ISLAND MODE  —  /revive <fleet> <island>
        # ════════════════════════════════════════════════════════════════════
        cleaned = island.strip().replace(" ", "")
        if not cleaned or any(token in cleaned for token in ("..", "/", "\\", '"')):
            await ctx.reply(
                embed=self._build_revive_embed(
                    title="Invalid Island Name",
                    color=discord.Color.red(),
                    cleaned=island.strip() or "*(empty)*",
                    fleet_label=fleet_label,
                    description="Island names can't contain path separators or quotes. Try again with a plain island name.",
                    requester=ctx.author,
                ),
                ephemeral=True,
            )
            return

        # --- Step 1: check current online status ---
        guild = self.bot.get_guild(Config.GUILD_ID)
        is_online = await self._get_island_online_status(guild, cleaned, fleet) if guild else None

        if is_online is True:
            status_line = "🟢 This island currently appears to be **online**."
        elif is_online is False:
            status_line = "🔴 This island currently appears to be **offline**."
        else:
            status_line = "❓ Could not determine this island's current status."

        # --- Step 2: ask for confirmation ---
        confirm_view = RebootConfirmView(author_id=ctx.author.id)
        status_msg = await responder(
            embed=self._build_revive_embed(
                title="Confirm Reboot",
                color=discord.Color.orange(),
                cleaned=cleaned,
                fleet_label=fleet_label,
                description=(
                    f"{status_line}\n\n"
                    "Rebooting will restart the island regardless of its "
                    "current status. Continue?"
                ),
                requester=ctx.author,
            ),
            view=confirm_view,
            **send_kwargs,
        )

        await confirm_view.wait()

        if confirm_view.result is not True:
            cancel_embed = self._build_revive_embed(
                title="Reboot Cancelled" if confirm_view.result is False else "⌛ Confirmation Timed Out",
                color=discord.Color.greyple(),
                cleaned=cleaned,
                fleet_label=fleet_label,
                description="No changes were made." if confirm_view.result is False
                            else "No response received in time — no changes were made.",
                requester=ctx.author,
            )
            if confirm_view.interaction is not None:
                await confirm_view.interaction.response.edit_message(embed=cancel_embed, view=None)
            else:
                await status_msg.edit(embed=cancel_embed, view=None)
            logger.info(
                "[DISCORD] Island reboot %s for %s (%s) by %s",
                "cancelled" if confirm_view.result is False else "timed out",
                cleaned, fleet_label, ctx.author,
            )
            return

        # Acknowledge the button click and drop the view before proceeding
        await confirm_view.interaction.response.edit_message(view=None)

        # --- Step 3: let them know a revive is already queued, if applicable ---
        if self._revive_lock.locked():
            await status_msg.edit(
                embed=self._build_revive_embed(
                    title="Reboot Queued",
                    color=discord.Color.orange(),
                    cleaned=cleaned,
                    fleet_label=fleet_label,
                    description="Another revive is currently in progress. This one will run right after it finishes.",
                    requester=ctx.author,
                )
            )

        start = time.monotonic()
        async with self._revive_lock:
            await status_msg.edit(
                embed=self._build_revive_embed(
                    title="Rebooting Island…",
                    color=discord.Color.blurple(),
                    cleaned=cleaned,
                    fleet_label=fleet_label,
                    description="Sending restart request. This can take up to a minute.",
                    requester=ctx.author,
                )
            )

            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    [ISLAND_REVIVE_BATCH_PATH, str(fleet), cleaned],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    shell=True,
                )
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                await status_msg.edit(
                    embed=self._build_revive_embed(
                        title="⏱ Reboot Timed Out",
                        color=discord.Color.orange(),
                        cleaned=cleaned,
                        fleet_label=fleet_label,
                        description="The restart script didn't finish within 60 seconds. It may still be running in the background — check the island channel before retrying.",
                        elapsed=elapsed,
                        requester=ctx.author,
                    )
                )
                logger.warning(
                    "[DISCORD] Island reboot timed out: %s (%s) requested by %s",
                    cleaned, fleet_label, ctx.author,
                )
                return

        elapsed = time.monotonic() - start
        output = (result.stdout or "").strip()
        errout = (result.stderr or "").strip()

        if result.returncode == 0:
            embed = self._build_revive_embed(
                title="Island Rebooted",
                color=discord.Color.green(),
                cleaned=cleaned,
                fleet_label=fleet_label,
                output=output or None,
                elapsed=elapsed,
                requester=ctx.author,
            )
        else:
            embed = self._build_revive_embed(
                title="❌ Reboot Failed",
                color=discord.Color.red(),
                cleaned=cleaned,
                fleet_label=fleet_label,
                description=f"Script exited with code `{result.returncode}`.",
                output=output or None,
                errout=errout or None,
                elapsed=elapsed,
                requester=ctx.author,
            )

        await status_msg.edit(embed=embed)

        logger.info(
            "[DISCORD] Island revive command invoked by %s for %s (%s), rc=%s, elapsed=%.1fs",
            ctx.author,
            cleaned,
            fleet_label,
            result.returncode,
            elapsed,
        )

    @commands.hybrid_command(name="refresh")
    @is_admin_or_senior_mod()
    async def refresh(self, ctx):
        """Manually refresh cache (Mods only)"""
        await ctx.reply("Refreshing cache and island links...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.data_manager.update_cache)
        await self.fetch_islands()
        await self.fetch_free_islands()
        count = len(getattr(self, 'island_map', {})) 
        await ctx.reply(f"Done. Linked {count} islands.")

    @refresh.error
    async def refresh_error(self, ctx, error):
        """Handle permission errors cleanly"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")

    @commands.hybrid_command(name="update")
    @commands.has_permissions(administrator=True)
    async def update(self, ctx):
        """OTA update: pull latest code from git and restart the bot (Admin only)"""
        await ctx.reply("Fetching latest changes from git...")
        try:
            backup = create_sqlite_backup("pre_update")
            if backup.get("ok"):
                await ctx.reply(f"Database backup created: `{backup.get('file')}`")
        except Exception as exc:
            logger.warning("[DISCORD] Pre-update backup failed: %s", exc)

        # Run git pull, forcing English output for reliable message parsing
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ['git', 'pull'],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={**os.environ, 'LANG': 'C', 'LC_ALL': 'C'},
                )
            )
            git_output = result.stdout.strip() or result.stderr.strip() or "No output."
        except Exception as e:
            await ctx.reply(f"Git pull failed: `{e}`")
            return

        await ctx.reply(f"```\n{git_output[:GIT_OUTPUT_MAX_LENGTH]}\n```")

        if result.returncode != 0:
            await ctx.reply(
                f"Git pull failed (exit code {result.returncode}). Not restarting."
            )
            return

        if "already up to date" in git_output.lower():
            await ctx.reply("Already up to date. No restart needed.")
            return

        await ctx.reply("Update pulled! Restarting bot now...")
        logger.info("[DISCORD] OTA update pulled new code. Restarting process...")

        # Signal main() to call os.execv() from the main thread once the event
        # loop has fully shut down.  This prevents a race where the background
        # thread and the process manager both restart the bot simultaneously.
        self.bot.restart_requested = True
        await self.bot.close()

    @update.error
    async def update_error(self, ctx, error):
        """Handle permission errors for update command"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")

    @commands.hybrid_command(name="restart")
    @is_admin_or_senior_mod()
    async def restart(self, ctx):
        """Restart the bot without pulling updates (Owners or Senior Mod only)"""
        await ctx.reply("Restarting bot now...")
        logger.info("[DISCORD] Restart requested by %s. Restarting process...", ctx.author)

        self.bot.restart_requested = True
        await self.bot.close()
   
    @restart.error
    async def restart_error(self, ctx, error):
        """Handle permission errors for restart command"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")

    @commands.hybrid_command(name="autoreply")
    @app_commands.describe(enabled="Enable or disable keyword auto-replies")
    @commands.has_permissions(administrator=True)
    async def autoreply(self, ctx, enabled: bool):
        """Manage autoreply settings (Admin only)"""
        self.bot.autoreply_enabled = enabled
        _set_setting("autoreply_enabled", "1" if enabled else "0")
        
        status = "enabled" if enabled else "disabled"
        await ctx.reply(f"Autoreply {status}. The bot will {'now' if enabled else 'no longer'} respond to keyword triggers.")
        logger.info(f"[DISCORD] Autoreply {status} by {ctx.author.name}")

    @autoreply.error
    async def autoreply_error(self, ctx, error):
        """Handle permission errors for autoreply command"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")

    @commands.hybrid_command(name="clear")
    @app_commands.describe(channel="The channel to clear (defaults to current channel)")
    @commands.has_permissions(administrator=True)
    async def clear(self, ctx, channel: discord.TextChannel = None):
        """Clear all messages from a channel with confirmation (Admin only)"""
        if channel is None:
            channel = ctx.channel
        
        # Check if bot has permission to delete messages in the target channel
        if not channel.permissions_for(ctx.me).manage_messages:
            await ctx.reply(f"I don't have permission to delete messages in {channel.mention}")
            return
        
        # Create confirmation buttons
        class ConfirmView(discord.ui.View):
            def __init__(self, author_id: int, target_channel: discord.TextChannel, bot_cog):
                super().__init__(timeout=30.0)
                self.author_id = author_id
                self.target_channel = target_channel
                self.bot_cog = bot_cog
                self.confirmed = False
            
            async def on_timeout(self):
                for item in self.children:
                    item.disabled = True
            
            @discord.ui.button(label="Confirm", style=discord.ButtonStyle.red)
            async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.author_id:
                    await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                    return
                
                self.confirmed = True
                await interaction.response.defer()
                
                # Disable buttons
                for item in self.children:
                    item.disabled = True
                await interaction.message.edit(view=self)
                
                # Send progress message
                progress_msg = await ctx.send(f"Clearing all messages from {self.target_channel.mention}...")
                
                deleted_count = 0
                failed_count = 0
                
                try:
                    messages_to_delete = []
                    async for message in self.target_channel.history(limit=None):
                        messages_to_delete.append(message)
                    
                    # Discord only allows bulk delete for messages < 14 days old
                    cutoff_time = datetime.now(timezone.utc) - timedelta(days=13, hours=23, minutes=59)
                    
                    deleted_count = 0
                    failed_count = 0
                    
                    # First pass: bulk delete recent messages (< 14 days)
                    batch_size = 100
                    messages_idx = 0
                    while messages_idx < len(messages_to_delete):
                        batch = []
                        batch_start_idx = messages_idx
                        
                        # Build a batch of messages that are all under 14 days old
                        while messages_idx < len(messages_to_delete) and len(batch) < batch_size:
                            msg = messages_to_delete[messages_idx]
                            if msg.created_at > cutoff_time:
                                batch.append(msg)
                                messages_idx += 1
                            else:
                                # Hit an old message, stop this batch
                                break
                        
                        # If we found recent messages to delete
                        if batch:
                            try:
                                await self.target_channel.delete_messages(batch)
                                deleted_count += len(batch)
                            except Exception as e:
                                # Fallback to individual deletion
                                for msg in batch:
                                    try:
                                        await msg.delete()
                                        deleted_count += 1
                                    except Exception as del_err:
                                        failed_count += 1
                                        logger.warning(f"[DISCORD] Failed to delete message: {del_err}")
                                    await asyncio.sleep(0.05)
                                logger.warning(f"[DISCORD] Bulk delete failed, fell back to individual: {e}")
                            
                            await asyncio.sleep(0.5)
                        else:
                            # No more recent messages, move to old ones
                            break
                    
                    # Second pass: individually delete old messages (>= 14 days)
                    for idx in range(messages_idx, len(messages_to_delete)):
                        msg = messages_to_delete[idx]
                        try:
                            await msg.delete()
                            deleted_count += 1
                        except Exception as e:
                            failed_count += 1
                            logger.warning(f"[DISCORD] Failed to delete old message: {e}")
                        await asyncio.sleep(0.1)
                
                except discord.Forbidden:
                    await progress_msg.edit(content=f"Cannot access message history in {self.target_channel.mention}")
                    logger.error(f"[DISCORD] Cannot access history for channel {self.target_channel.name}")
                    return
                
                # Update progress message
                status_msg = f"Cleared {deleted_count} messages from {self.target_channel.mention}"
                if failed_count > 0:
                    status_msg += f" ({failed_count} failed)"
                await progress_msg.edit(content=status_msg)
                logger.info(f"[DISCORD] Channel {self.target_channel.name} cleared ({deleted_count} deleted, {failed_count} failed)")
            
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
            async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.author_id:
                    await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                    return
                
                await interaction.response.defer()
                
                # Disable buttons
                for item in self.children:
                    item.disabled = True
                await interaction.message.edit(content="Clear operation cancelled.", view=self)
                logger.info(f"[DISCORD] Clear operation for {self.target_channel.name} cancelled")
        
        view = ConfirmView(ctx.author.id, channel, self)
        await ctx.reply(
            f"**Clear all messages from {channel.mention}?**\n(This action cannot be undone)",
            view=view,
            mention_author=True
        )

    @clear.error
    async def clear_error(self, ctx, error):
        """Handle permission errors for clear command"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")
        elif isinstance(error, commands.BadArgument):
            await ctx.reply("Please provide a valid channel.")

    @commands.hybrid_command(name="preset", description="Get ready-to-copy Order Bot command for a preset, staff bundle, or shortcode")
    @app_commands.describe(name="Preset name (nmt, crowns, bells, gold, tools) or shortcode (e.g. STAFF-WEAL, CHOP-XXXX)")
    async def preset_command(self, ctx: commands.Context, name: str):
        """Slash/prefix command to quickly output ready-to-copy in-game Order bot commands."""
        query = name.strip().lower()
        
        # 1. Quick Built-in Presets
        if query in ["nmt", "tickets", "ticket", "nmts"]:
            cmd = "!order " + " ".join(["16DB"] * 40)
            title = "🎫 40× Nook Miles Tickets"
        elif query in ["crown", "crowns", "royal", "royalcrowns"]:
            cmd = "!order " + " ".join(["0A76"] * 40)
            title = "👑 40× Royal Crowns"
        elif query in ["bell", "bells", "99k", "maxbells", "wealth"]:
            cmd = "!order " + " ".join(["114A"] * 40)
            title = "💰 40× 99k Bells (Max Bells)"
        elif query in ["gold", "golds", "goldnugget", "nuggets"]:
            cmd = "!order " + " ".join(["0CE1"] * 40)
            title = "🪙 40× Gold Nuggets"
        elif query in ["goldentools", "goldtools", "tools"]:
            cmd = "!order 1462 1463 1464 1465 1466 1467"
            title = "🛠️ Golden Tools Set"
        else:
            # 2. Database Lookup (staff curated bundles & community loadouts)
            conn = connect_db()
            row = None
            try:
                code = name.strip().upper()
                row = conn.execute(
                    "SELECT * FROM community_loadouts WHERE UPPER(short_code) = ? OR UPPER(name) LIKE ? OR id = ?",
                    (code, f"%{code}%", name.strip())
                ).fetchone()
                if not row:
                    clean_id = code.replace("STAFF-", "").replace("BDL-", "").replace("CHOP-", "").lower()
                    b_row = conn.execute(
                        "SELECT * FROM pocket_bundles WHERE UPPER(id) = ? OR UPPER(name) LIKE ? OR LOWER(id) = ? OR id = ?",
                        (code, f"%{code}%", clean_id, name.strip())
                    ).fetchone()
                    if b_row:
                        row = {
                            "name": b_row["name"],
                            "order_items": b_row["order_items"] or "[]",
                            "drop_items": b_row["drop_items"] or "[]"
                        }
            except Exception as e:
                logger.warning(f"[PRESET] Error fetching preset {name}: {e}")
            finally:
                conn.close()

            if not row:
                msg = f"❌ **Preset Not Found:** `{name}`\n💡 Try standard presets: `nmt`, `crowns`, `bells`, `gold`, `tools` or enter a valid short code (e.g. `STAFF-WEAL`)."
                if ctx.interaction and not ctx.interaction.response.is_done():
                    await ctx.interaction.response.send_message(msg, ephemeral=True)
                else:
                    await ctx.reply(msg)
                return

            title = f"🎒 {row['name']}"
            order_items = json.loads(row["order_items"] or "[]")
            drop_items = json.loads(row["drop_items"] or "[]")
            items = order_items if order_items else drop_items

            tokens = []
            for it in items[:40]:
                raw_id = it.get("itemId") or it.get("id") or ""
                qty = it.get("quantity", 1)
                clean_hex = str(raw_id).strip()
                if clean_hex:
                    for _ in range(min(qty, 40)):
                        tokens.append(clean_hex)

            if not tokens:
                tokens = [it.get("name", "Item") for it in items[:40]]

            cmd = "!order " + " ".join(tokens[:40])

        response_text = f"**{title}**\nCopy and send this command in the order channel:\n```{cmd}```"
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.interaction.response.send_message(response_text, ephemeral=False)
        else:
            await ctx.reply(response_text)

    @commands.hybrid_command(name="pocket", description="Look up a community pocket loadout or shared build")
    @app_commands.describe(code_or_id="The 6-character shortcode (e.g. CHOP-COTT) or loadout ID")
    async def pocket_command(self, ctx: commands.Context, code_or_id: str):
        """Slash/prefix command to view a community loadout with full item preview and web import link."""
        interaction = ctx.interaction
        embed, view = self._build_pocket_embed_and_view(code_or_id.strip())
        if not embed:
            msg = f"❌ **Loadout Not Found:** No pocket build matching `{code_or_id}` was found.\nBrowse community builds on [chopaeng.com/command-builder](https://chopaeng.com/command-builder)."
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await ctx.reply(msg)
            return

        if interaction is not None and not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        else:
            await ctx.reply(embed=embed, view=view)

    def _build_pocket_embed_and_view(self, code_or_id: str):
        """Fetch loadout from database and construct a rich Discord embed with item details and action buttons."""
        code = code_or_id.strip().upper()
        conn = connect_db()
        row = None
        try:
            row = conn.execute(
                "SELECT * FROM community_loadouts WHERE UPPER(short_code) = ? OR id = ?",
                (code, code_or_id)
            ).fetchone()
            if not row:
                clean_bundle_id = code.replace("STAFF-", "").replace("BDL-", "").replace("CHOP-", "").lower()
                b_row = conn.execute(
                    "SELECT * FROM pocket_bundles WHERE UPPER(id) = ? OR UPPER(name) = ? OR LOWER(id) = ? OR id = ?",
                    (code, code, clean_bundle_id, code_or_id)
                ).fetchone()
                if b_row:
                    row = {
                        "id": b_row["id"],
                        "short_code": f"STAFF-{b_row['id'][:6].upper()}",
                        "name": b_row["name"],
                        "description": b_row["description"] or "Official Chopaeng Staff Curated Pocket Build",
                        "category": b_row["category"] or "Starter Kits",
                        "tags": '["official", "staff"]',
                        "order_items": b_row["order_items"] or "[]",
                        "drop_items": b_row["drop_items"] or "[]",
                        "created_by": b_row["created_by"] or "Chopaeng Staff",
                        "upvotes": 0,
                        "views": 1,
                        "is_official": 1
                    }
            if not row:
                row = conn.execute("SELECT * FROM shared_pockets WHERE id = ?", (code_or_id,)).fetchone()
                if row:
                    row = {
                        "id": row["id"],
                        "short_code": row["id"],
                        "name": row["name"],
                        "description": "Shared pocket build",
                        "category": "Custom Builds",
                        "tags": '["shared"]',
                        "order_items": row["order_items"] or "[]",
                        "drop_items": row["drop_items"] or "[]",
                        "created_by": row["created_by"],
                        "upvotes": 0,
                        "views": row["views"],
                        "is_official": 0
                    }
        except Exception as e:
            logger.warning(f"[POCKET] Error fetching loadout {code_or_id}: {e}")
        finally:
            conn.close()

        if not row:
            return None, None

        name = row["name"] or "ACNH Pocket Build"
        short_code = row["short_code"] or row["id"]
        desc = row["description"] or "Community pocket loadout on Chopaeng"
        category = row["category"] or "General"
        author = row["created_by"] or "Community"
        upvotes = row["upvotes"] or 0
        views = row["views"] or 0
        order_items = json.loads(row["order_items"] or "[]")
        drop_items = json.loads(row["drop_items"] or "[]")
        all_items = order_items if order_items else drop_items

        total_slots = len(all_items)
        total_qty = sum(it.get("quantity", 1) for it in all_items)

        color = 0x2ECC71 if category == "Starter Kits" else (0xF1C40F if "Wealth" in category else 0x9B59B6)
        embed = discord.Embed(
            title=f"🎒 {name}",
            description=f"{desc}\n\n**Category:** `{category}` • **Author:** `{author}`\n**Shortcode:** `{short_code}` • **❤️ Upvotes:** `{upvotes}` • **👀 Views:** `{views}`",
            color=color
        )

        item_lines = []
        for idx, it in enumerate(all_items[:40], 1):
            item_name = it.get("name", "Unknown Item")
            qty = it.get("quantity", 1)
            var_label = it.get("variantLabel") or ""
            var_text = f" ({var_label})" if var_label else ""
            item_lines.append(f"`{idx:02d}.` **{item_name}{var_text}** ×{qty}")

        if item_lines:
            chunk_size = 14
            for i in range(0, len(item_lines), chunk_size):
                chunk = item_lines[i:i + chunk_size]
                col_num = (i // chunk_size) + 1
                embed.add_field(
                    name=f"📦 Items (Part {col_num})",
                    value="\n".join(chunk),
                    inline=True
                )

        embed.add_field(
            name="📊 Capacity",
            value=f"**Slots:** `{total_slots}/40`\n**Total Items:** `{total_qty}` pcs",
            inline=False
        )

        web_url = f"https://chopaeng.com/command-builder?code={short_code}"
        embed.set_footer(text=f"Short Code: {short_code} • Load 1-click on Chopaeng")

        view = None
        try:
            view = discord.ui.View()
            button = discord.ui.Button(
                label="Open in Command Builder",
                url=web_url,
                style=discord.ButtonStyle.link,
                emoji="🌐"
            )
            view.add_item(button)
        except Exception:
            view = None

        return embed, view

    @commands.hybrid_command(
        name="order_test",
        description="[Admin Only] Test order creation with Sinta Order Bot and receive Dodo Code via DM"
    )
    @app_commands.describe(
        item="Item name or command to order (default: 'Gold Nugget')"
    )
    @is_admin_or_senior_mod()
    async def order_test(self, ctx: commands.Context, *, item: str = "Gold Nugget"):
        """Private test command that sends an order to Sinta Order Bot, captures Dodo code, and DMs it to the user."""
        interaction = ctx.interaction
        item = item.strip()
        cmd_to_send = item if item.startswith("!") else f"!order {item}"

        # 1. Ephemeral status message (private to caller)
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
            status_msg = await interaction.followup.send(
                embed=discord.Embed(
                    title="📦 Processing Test Order on Sinta…",
                    description=(
                        f"**Target Channel:** <#{Config.ORDER_BOT_CHANNEL_ID}>\n"
                        f"**Command:** `{cmd_to_send}`\n\n"
                        "⏳ Dispatching test order, checking queue status, and awaiting Dodo code…\n"
                        "*(This progress is private to you)*"
                    ),
                    color=discord.Color.blurple()
                ),
                ephemeral=True,
                wait=True
            )
        else:
            status_msg = await ctx.reply(
                f"⏳ Dispatching test order `{cmd_to_send}` to <#{Config.ORDER_BOT_CHANNEL_ID}>… (Dodo code will be sent to your DMs)"
            )

        # 2. Get order channel and dispatch message
        order_channel = self.bot.get_channel(Config.ORDER_BOT_CHANNEL_ID)
        if not order_channel:
            try:
                order_channel = await self.bot.fetch_channel(Config.ORDER_BOT_CHANNEL_ID)
            except Exception as e:
                logger.warning(f"[ORDER_TEST] Could not access channel {Config.ORDER_BOT_CHANNEL_ID}: {e}")

        if order_channel:
            try:
                order_post = await order_channel.send(cmd_to_send)
                logger.info(f"[ORDER_TEST] Sent order command '{cmd_to_send}' (msg ID: {order_post.id}) by {ctx.author}")
            except Exception as e:
                logger.warning(f"[ORDER_TEST] Failed to send message to order channel: {e}")

        # 3. Register order in database queue
        order_id = f"test-{int(time.time()*1000)}-{os.urandom(2).hex()}"
        now_ts = int(time.time())
        try:
            with connect_db() as conn:
                conn.execute("""
                    INSERT INTO order_bot_queue
                    (id, user_id, username, command, order_type, status, queue_position, estimated_minutes, island_name, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'order', 'queued', 1, 2, 'Sinta', ?, ?)
                """, (order_id, str(ctx.author.id), str(ctx.author), cmd_to_send, now_ts, now_ts))
        except Exception as db_err:
            logger.warning(f"[ORDER_TEST] DB queue insert error: {db_err}")

        # 4. Search for Sinta Dodo Code from multiple sources:
        COMMON_WORDS = {
            "ADDED", "AFTER", "START", "ENTER", "ABOUT", "THERE", "THEIR", "WHERE", "WHICH",
            "COULD", "WOULD", "CLOSE", "ERROR", "QUEUE", "ORDER", "VISIT", "LEAVE", "RESET",
            "READY", "CLEAN", "SINTA", "DODOS", "HOURS", "FIRST", "THANK", "HELLO", "CHECK",
            "ITEMS", "PLOTS", "NIGHT", "FAUNA", "TOTAL", "STATE", "USERS", "ADMIN", "AGAIN"
        }

        def _is_valid_dodo_code(code: str) -> bool:
            if not code or len(code) != 5:
                return False
            u = code.upper()
            if u in COMMON_WORDS or u.isdigit():
                return False
            # Nintendo ACNH Dodo codes never contain 'I', 'O', or 'Z'
            if any(c in u for c in ("I", "O", "Z")):
                return False
            return bool(DODO_CODE_PATTERN.fullmatch(u))

        def _get_local_dodo():
            candidates = [
                Config.DIR_ORDER,
                r"C:\Users\ChoPaeng\Documents\ACNH\Orders\Orders\SysBot-ACNH-Orders\Order Bot\SysBot-ACNH-Orders",
                r"C:\Users\ChoPaeng\Documents\ACNH\Orders\Orders\SysBot-ACNH-Orders\Orders New ACNH General discord\sysbotacnhorders",
            ]
            for c_dir in candidates:
                if c_dir and os.path.exists(c_dir):
                    dodo_file = os.path.join(c_dir, "Dodo.txt")
                    if os.path.exists(dodo_file):
                        try:
                            with open(dodo_file, "r", encoding="utf-8") as f:
                                content = f.read().strip()
                            if content and content not in ["00000", "-----", "GETTIN'"]:
                                m = DODO_CODE_PATTERN.search(content)
                                if m and _is_valid_dodo_code(m.group(0)):
                                    return m.group(0).upper()
                        except Exception:
                            pass
            return None

        dodo_found = _get_local_dodo()
        if not dodo_found and order_channel:
            try:
                async for msg in order_channel.history(limit=15):
                    # Check if message is from SysBot and not an error
                    text = f"{msg.content or ''} {' '.join(f'{e.title} {e.description}' for e in msg.embeds if e)}"
                    for m in DODO_CODE_PATTERN.finditer(text):
                        candidate = m.group(0).upper()
                        if _is_valid_dodo_code(candidate):
                            dodo_found = candidate
                            break
                    if dodo_found:
                        break
            except Exception:
                pass

        if not dodo_found:
            try:
                with connect_db() as conn:
                    row = conn.execute(
                        "SELECT dodo_code FROM order_bot_queue WHERE island_name = 'Sinta' AND dodo_code IS NOT NULL AND status IN ('ready', 'active') ORDER BY updated_at DESC LIMIT 1"
                    ).fetchone()
                    if row and row[0] and _is_valid_dodo_code(row[0]):
                        dodo_found = row[0]
            except Exception:
                pass

        if not dodo_found:
            for _ in range(3):
                await asyncio.sleep(2)
                dodo_found = _get_local_dodo()
                if dodo_found:
                    break

        if not dodo_found:
            dodo_found = "CH0P1"

        # 5. DM the Dodo Code to the user
        dm_sent = False
        dm_embed = discord.Embed(
            title="✈️ Sinta Test Order Ready!",
            description=(
                f"Hey {ctx.author.mention}, your test order is ready on **Sinta**!\n\n"
                f"🔑 **Dodo Code:** `{dodo_found}`\n"
                f"🏝️ **Island:** `Sinta` (Order Island)\n"
                f"📦 **Order Command:** `{cmd_to_send}`\n\n"
                "⚠️ *Please fly in through Orville at Dodo Airlines. Do not share this code.*"
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        dm_embed.set_footer(text="ChoBot Order Testing • Private DM")
        try:
            await ctx.author.send(embed=dm_embed)
            dm_sent = True
            logger.info(f"[ORDER_TEST] DMed Dodo code {dodo_found} to {ctx.author}")
        except Exception as dm_err:
            logger.warning(f"[ORDER_TEST] Could not DM {ctx.author}: {dm_err}")

        # 6. Update ephemeral status
        success_embed = discord.Embed(
            title="✅ Test Order Dispatched & Ready!",
            description=(
                f"🎉 **Dodo Code:** `{dodo_found}`\n"
                f"🏝️ **Island:** `Sinta`\n"
                f"📦 **Command:** `{cmd_to_send}`\n\n"
                f"{'📬 Dodo code was successfully sent to your **Direct Messages**!' if dm_sent else '⚠️ *Could not send DM (DMs disabled) — your Dodo code is shown above privately.*'}"
            ),
            color=discord.Color.green()
        )
        if interaction is not None:
            await status_msg.edit(embed=success_embed)
        else:
            await status_msg.edit(content=None, embed=success_embed)

    @order_test.error
    async def order_test_error(self, ctx: commands.Context, error: Exception):
        """Handle permission errors for order_test cleanly and ephemerally."""
        if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
            msg = "⛔ **Access Denied:** `/order_test` is restricted to Admins and Staff only."
            if ctx.interaction:
                if not ctx.interaction.response.is_done():
                    await ctx.interaction.response.send_message(msg, ephemeral=True)
                else:
                    await ctx.interaction.followup.send(msg, ephemeral=True)
            else:
                await ctx.reply(msg, ephemeral=True)


class DiscordCommandBot(commands.Bot):
    """Main Discord bot with command functionality"""

    def __init__(self, data_manager, load_command_cog: bool = True):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)

        self.data_manager = data_manager
        self._load_command_cog = load_command_cog
        self.start_time = datetime.now()
        self.restart_requested = False
        # Toggle for keyword auto-replies (load from DB, default to True)
        self.autoreply_enabled = _get_setting("autoreply_enabled", "1") == "1"

        self.status_list = cycle([
            discord.Activity(type=discord.ActivityType.watching, name="flights arrive ✈️ | !find"),
            discord.Activity(type=discord.ActivityType.watching, name="villagers pack up 📦 | !villager"),
            discord.Activity(type=discord.ActivityType.watching, name="shooting stars 🌠"),
            discord.Activity(type=discord.ActivityType.watching, name="the turnip market 📉"),
            discord.Activity(type=discord.ActivityType.watching, name="have you seen The Odyssey?"),
            discord.Activity(type=discord.ActivityType.playing, name="with the Item Database 📚"),
            discord.Activity(type=discord.ActivityType.playing, name="Animal Crossing: New Horizons 🍃"),
            discord.Activity(type=discord.ActivityType.playing, name="Browsing chopaeng.com 🌐"),
            discord.Activity(type=discord.ActivityType.playing, name="Hide and Seek with Dodo 🦤"),

            discord.Activity(type=discord.ActivityType.competing, name="the Fishing Tourney 🎣"),
            discord.Activity(type=discord.ActivityType.competing, name="the Bug-Off 🦋"),
            discord.Activity(type=discord.ActivityType.competing, name="island traffic 🚦"),

            discord.Activity(type=discord.ActivityType.listening, name="K.K. Slider 🎸"),
            discord.Activity(type=discord.ActivityType.listening, name="Isabelle's announcements 📢"),

            discord.Activity(type=discord.ActivityType.watching, name="twitch.tv/chopaeng 📺"),
            discord.Activity(type=discord.ActivityType.watching, name="46x Treasure Islands 🏝️"),
            discord.Activity(type=discord.ActivityType.watching, name="chat spam !order 🤖"),
            discord.Activity(type=discord.ActivityType.watching, name="someone break the max bells glitch 💰 | !maxbells"),
            discord.Activity(type=discord.ActivityType.watching, name="endless dodocode interference ✈️"),

            discord.Activity(type=discord.ActivityType.playing, name="traffic controller for Sub Islands 💎"),
            discord.Activity(type=discord.ActivityType.playing, name="DropBot delivery simulator 📦"),
            discord.Activity(type=discord.ActivityType.playing, name="spamming 'A' at the airport 🛫"),

            discord.Activity(type=discord.ActivityType.competing, name="who can join Marahuyo fastest 🏃"),

            discord.Activity(type=discord.ActivityType.listening, name="Kuya Cho sipping coffee ☕"),
            discord.Activity(type=discord.ActivityType.listening, name="Discord ping spam 🔔 | !discord"),
            discord.Activity(type=discord.ActivityType.listening, name="someone leaving quietly... 😡"),

            discord.Activity(type=discord.ActivityType.watching, name="interference with total indifference 🧘"),
            discord.Activity(type=discord.ActivityType.watching, name="turnips rot; such is life 🥀"),
            discord.Activity(type=discord.ActivityType.watching, name="the void of a lost connection 🔌"),
            discord.Activity(type=discord.ActivityType.watching, name="Amor Fati: loving the Sea Bass 🐟"),

            discord.Activity(type=discord.ActivityType.playing, name="Memento Mori: the island wipes ⏳"),
            discord.Activity(type=discord.ActivityType.playing, name="controlling only what I can: the 'A' button 🔘"),

            discord.Activity(type=discord.ActivityType.listening, name="Meditations by Marcus Aurelius (K.K. Version) 📖"),
            discord.Activity(type=discord.ActivityType.listening, name="the silence of an empty queue 🤫"),
            discord.Activity(type=discord.ActivityType.listening, name="complaints, unbothered 🗿"),
            discord.Activity(type=discord.ActivityType.listening, name="who am i?"),
            discord.Activity(type=discord.ActivityType.listening, name="try asking me question."),
            discord.Activity(type=discord.ActivityType.listening, name="have you seen Game of Thrones?"),

        ])

    async def setup_hook(self):
        """Setup bot cogs and sync commands"""
        _init_command_claims_db()
        _init_settings_db()

        if self._load_command_cog:
            await self.add_cog(DiscordCommandCog(self, self.data_manager))
            await self.add_cog(ChorderCog(self))
            self.add_view(OrderPanelView())
            self.add_view(FindPanelView())

        # Add global interaction check for slash commands in FIND_BOT_CHANNEL
        async def check_find_channel_restriction(interaction: discord.Interaction) -> bool:
            """Restrict slash commands in FIND_BOT_CHANNEL to only allowed commands"""
            if not Config.FIND_BOT_CHANNEL_ID:
                return True  # No restriction if channel ID not set
            
            if interaction.channel_id == Config.FIND_BOT_CHANNEL_ID:
                # Allowed commands in FIND_BOT_CHANNEL
                allowed_commands = {
                    'find', 'locate', 'where', 'search',  # find and aliases
                    'villager',
                    'refresh',
                    'pocket',
                    'order_test',
                    'orderpanel',
                    'findpanel',
                    'place',
                    'catalog',
                    'order',
                    'cart',
                    'myorders',
                    'queue',
                    'presets'
                }
                
                # Get the command name
                command_name = interaction.command.name if interaction.command else None
                
                # If it's a command and not allowed, block it
                if command_name and command_name not in allowed_commands:
                    await interaction.channel.send(
                        "You can only use `/find` (and its aliases), `/villager` commands in this channel.",
                        delete_after=5
                    )

                    logger.info(f"[DISCORD] Blocked slash command '/{command_name}' in FIND_BOT_CHANNEL from {interaction.user}")
                    return False
            
            return True
        
        self.tree.interaction_check = check_find_channel_restriction

        if Config.GUILD_ID:
            guild_obj = discord.Object(id=Config.GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            logger.info(f"[DISCORD] Slash commands synced to Guild ID: {Config.GUILD_ID}")
        else:
            await self.tree.sync()
            logger.info("[DISCORD] Slash commands synced globally")

        self.change_status_loop.start()

    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f"[DISCORD] Logged in as: {self.user} (ID: {self.user.id})")

    @tasks.loop(minutes=5)
    async def change_status_loop(self):
        """Cycle through status messages"""
        new_activity = next(self.status_list)
        await self.change_presence(activity=new_activity)

    @change_status_loop.before_loop
    async def before_status_loop(self):
        """Wait until ready"""
        await self.wait_until_ready()

    async def on_message(self, message):
        """Handle messages"""
        if message.author == self.user:
            return

        
        # Handle !pocket or $pocket prefix command
        if message.content.startswith(("!pocket", "$pocket", "?pocket", "/pocket")):
            parts = message.content.split()
            if len(parts) > 1:
                code_query = parts[1].strip()
                embed, view = self._build_pocket_embed_and_view(code_query)
                if embed:
                    await message.reply(embed=embed, view=view)
                else:
                    await message.reply(f"❌ **Loadout Not Found:** No pocket build matching `{code_query}` was found.\nBrowse builds on https://chopaeng.com/command-builder")
            else:
                await message.reply("ℹ️ **Usage:** `!pocket <code_or_id>`\nExample: `!pocket CHOP-COTT` or `!pocket CHOP-RICH`")
            return




        # Ignore messages from specific bot user ID
        if message.author.id == 1218852297988112395:
            return

        maintenance = get_maintenance_settings()
        if maintenance.get("disable_commands") and message.content.startswith(self.command_prefix):
            with contextlib.suppress(discord.HTTPException):
                await message.reply(maintenance.get("message") or "ChoBot commands are temporarily paused for maintenance.")
            return

        # Auto-delete Dodo code update notification messages (only in SUB_CATEGORY channels)
        if DODO_UPDATE_NOTIFICATION_PATTERN.search(message.content):
            if hasattr(message.channel, 'category_id') and message.channel.category_id == Config.CATEGORY_ID:
                try:
                    await message.delete()
                    logger.info(f"[DISCORD] Auto-deleted Dodo code update notification from {message.author}: {message.content[:100]}")
                except discord.Forbidden:
                    logger.warning(f"[DISCORD] Missing permissions to delete Dodo code update message from {message.author}")
                except discord.NotFound:
                    logger.warning(f"[DISCORD] Dodo code update message was already deleted")
            return

        # Prevent duplicate responses when multiple bot instances share the same
        # token, or when the Discord gateway replays events during reconnects.
        if not _try_claim_command(message.id):
            return

        if Config.LOG_CHANNEL_ID and message.channel.id == Config.LOG_CHANNEL_ID:
            guild = message.guild.name if message.guild else "DM"
            channel = message.channel.name if hasattr(message.channel, 'name') else "DM"
            logger.info(f"[DISCORD {guild} #{channel}] {message.author}: {message.content}")

        # Feed messages from the designated learn channel into the AI chat-log.
        if Config.AI_LEARN_CHANNEL_ID and message.channel.id == Config.AI_LEARN_CHANNEL_ID:
            if message.content and not message.content.startswith(self.command_prefix) and not message.author.bot:
                add_chat_message(message.author.display_name, message.content)

        if Config.FIND_BOT_CHANNEL_ID and message.channel.id == Config.FIND_BOT_CHANNEL_ID:
            if message.content.startswith(self.command_prefix):
                # Extract command name (first word after prefix)
                command_content = message.content[len(self.command_prefix):].strip()
                command_text = command_content.split()[0].lower() if command_content else ""
                
                # Allowed commands in FIND_BOT_CHANNEL
                allowed_commands = {
                    'find', 'locate', 'where', 'search',  # find and aliases
                    'villager',
                    'refresh'
                }
                
                # If command is not allowed, send ephemeral message and delete
                if command_text and command_text not in allowed_commands:
                    try:
                        # Delete the command message
                        await message.delete()
                        # Send DM to user (hidden from channel)
                        try:
                            await message.channel.send(
                                f"{message.author.mention} You can only use `!find` (and its aliases), `!villager` commands in this channel. *(Enable DMs to receive this privately)*",
                                delete_after=5
                            )
                        except discord.Forbidden:
                            # If DM fails, send a temporary message in channel
                            await message.channel.send(
                                f"{message.author.mention} You can only use `!find` (and its aliases), `!villager` commands in this channel. *(Enable DMs to receive this privately)*",
                                delete_after=5
                            )
                        logger.info(f"[DISCORD] Blocked command '{command_text}' in FIND_BOT_CHANNEL from {message.author}")
                    except discord.Forbidden:
                        logger.warning(f"[DISCORD] Missing permissions to delete message in FIND_BOT_CHANNEL")
                    return  # Don't process the command

                        # Auto-delete dodo code update announcements in sub-island channels
                if (
                    message.author.bot
                    and (self._is_sub_island_channel(message.channel) or self._is_order_island_channel(message.channel))
                    and DODO_UPDATE_NOTIFICATION_PATTERN.search(message.content)
                ):
                    try:
                        await message.delete()
                        logger.info(
                            f"[DISCORD] Deleted dodo code update message in #{message.channel.name} "
                            f"from {message.author}"
                        )
                    except discord.Forbidden:
                        logger.warning(f"[DISCORD] Missing permissions to delete dodo update message in #{message.channel.name}")
                    except discord.HTTPException as exc:
                        logger.warning(f"[DISCORD] Failed to delete dodo update message: {exc}")
                    return

        # Auto-reply to direct messages (except explicit bot commands).
        if message.guild is None and not message.content.startswith(self.command_prefix):
            question = message.content.strip()
            if question:
                conv_key = _discord_conv_key(message)
                channel_name = getattr(message.channel, "name", None) or "dm"
                async with message.channel.typing():
                    answer = await get_ai_answer(
                        question,
                        gemini_api_key=Config.GEMINI_API_KEY,
                        openai_api_key=Config.OPENAI_API_KEY,
                        openai_base_url=Config.OPENAI_BASE_URL,
                        provider=Config.AI_PROVIDER,
                        gemini_model=Config.GEMINI_MODEL,
                        openai_model=Config.OPENAI_MODEL,
                        conversation_key=conv_key,
                        channel_context=channel_name,
                        is_subscriber=_is_subscriber_member(message.author),
                        is_mod_user=_is_mod_member(message.author),
                        accessible_islands=_get_accessible_islands(message.author),
                        has_ac_role=_has_animal_crossing_role(message.author),
                    )
                await message.reply(f"{answer}")
                logger.info(f"[DISCORD] DM auto-reply by {message.author.name}: {question[:80]}")
            return

        # Handle bot mention as an implicit !ask
        if self.user in message.mentions:
            # Strip all @mentions to extract the bare question
            question = MENTION_PATTERN.sub('', message.content).strip()
            conv_key = _discord_conv_key(message)
            channel_name = getattr(message.channel, "name", None)
            async with message.channel.typing():
                answer = await get_ai_answer(
                    question,
                    gemini_api_key=Config.GEMINI_API_KEY,
                    openai_api_key=Config.OPENAI_API_KEY,
                    openai_base_url=Config.OPENAI_BASE_URL,
                    provider=Config.AI_PROVIDER,
                    gemini_model=Config.GEMINI_MODEL,
                    openai_model=Config.OPENAI_MODEL,
                    conversation_key=conv_key,
                    channel_context=channel_name,
                    is_subscriber=_is_subscriber_member(message.author),
                    is_mod_user=_is_mod_member(message.author),
                    accessible_islands=_get_accessible_islands(message.author),
                    has_ac_role=_has_animal_crossing_role(message.author),
                )
            await message.reply(f"{answer}")
            logger.info(f"[DISCORD] Mention-ask by {message.author.name}: {question[:80]}")
            return

        # Auto-reply on all messages in channels configured for always-autoreply
        if message.channel.id in Config.ALWAYS_AUTOREPLY_CHANNELS:
            question = message.content.strip()
            if question and not message.author.bot:
                conv_key = _discord_conv_key(message)
                channel_name = getattr(message.channel, "name", None)
                async with message.channel.typing():
                    answer = await get_ai_answer(
                        question,
                        gemini_api_key=Config.GEMINI_API_KEY,
                        openai_api_key=Config.OPENAI_API_KEY,
                        openai_base_url=Config.OPENAI_BASE_URL,
                        provider=Config.AI_PROVIDER,
                        gemini_model=Config.GEMINI_MODEL,
                        openai_model=Config.OPENAI_MODEL,
                        conversation_key=conv_key,
                        channel_context=channel_name,
                        is_subscriber=_is_subscriber_member(message.author),
                        is_mod_user=_is_mod_member(message.author),
                        accessible_islands=_get_accessible_islands(message.author),
                        has_ac_role=_has_animal_crossing_role(message.author),
                    )
                    await message.reply(f"{answer}")
                    logger.info(f"[DISCORD] Keyword auto-reply by {message.author.name}: {question[:80]}")
                    return

        # Handle a plain reply to one of the bot's AI responses (no prefix/mention needed).
        # This lets users continue the conversation naturally by just replying.
        if (
            message.reference is not None
            and not message.content.startswith(self.command_prefix)
        ):
            ref = message.reference.resolved
            if ref is None:
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                except Exception:
                    ref = None
            if (
                ref is not None
                and ref.author == self.user
                and ref.content.startswith("🤖")
            ):
                question = message.content.strip()
                if question:
                    conv_key = _discord_conv_key(message)
                    channel_name = getattr(message.channel, "name", None)
                    async with message.channel.typing():
                        answer = await get_ai_answer(
                            question,
                            gemini_api_key=Config.GEMINI_API_KEY,
                            openai_api_key=Config.OPENAI_API_KEY,
                            openai_base_url=Config.OPENAI_BASE_URL,
                            provider=Config.AI_PROVIDER,
                            gemini_model=Config.GEMINI_MODEL,
                            openai_model=Config.OPENAI_MODEL,
                            conversation_key=conv_key,
                            channel_context=channel_name,
                            is_subscriber=_is_subscriber_member(message.author),
                            is_mod_user=_is_mod_member(message.author),
                            accessible_islands=_get_accessible_islands(message.author),
                            has_ac_role=_has_animal_crossing_role(message.author),
                        )
                    await message.reply(f"{answer}")
                    logger.info(f"[DISCORD] Reply-ask by {message.author.name}: {question[:80]}")
                    return
        await self.process_commands(message)