"""
Discord Flight Logger Module
Tracks island visitor arrivals, alerts on unknown travelers, and handles moderation internally.
"""

import re
import random
import logging
import unicodedata
import datetime
import asyncio
import json

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, UserSelect, Select, button
from utils.config import Config
from utils.database import connect_async_db, get_backend
from utils.helpers import clean_text

logger = logging.getLogger("FlightLogger")


def ign_matches_dodo_reveal(ign: str, username: str | None, nickname: str | None) -> bool:
    """Heuristic: flight-list IGN vs Discord username/nickname from dodo reveal webhook."""
    i = clean_text(ign)
    if not i or len(i) < 2:
        return False
    for raw in (username, nickname):
        if not raw:
            continue
        c = clean_text(raw)
        if not c:
            continue
        if i == c:
            return True
        if len(i) >= 3 and (i in c or c in i):
            return True
        if c.startswith(i) or i.startswith(c):
            return True
    return False


# --- CONSTANTS ---
# Colors
COLOR_SUCCESS = 0x2ECC71      # Green (for admits, unwarns)
COLOR_INVESTIGATION = 0xF1C40F  # Amber/Yellow (for investigation)
COLOR_WARN = 0xE67E22          # Orange (for warnings)
COLOR_KICK = 0xF1C40F          # Yellow (for kicks)
COLOR_BAN = 0x992D22           # Red (for bans)
COLOR_DISMISS = 0x95A5A6       # Grey (for dismissed/false positives)
COLOR_ALERT = 0xED4245         # Discord red (for unknown traveler alerts)

# --- DATABASE SETUP ---
DB_NAME = "chobot.db"
WARN_EXPIRY_DAYS = 3
RECENT_IDENTITY_WINDOW_SECONDS = 24 * 60 * 60
IDENTITY_EVENT_NICKNAME_CHANGE = "nickname_change"
IDENTITY_EVENT_MEMBER_JOIN = "member_join"
MAX_HISTORY_ENTRIES = 10  # Max entries shown per section in !flighthistory
MAX_DEBUG_CANDIDATES = 5  # Max closest-candidate entries shown in !fdebug
LEGACY_TWO_IDENTITY_CUTOFF_UTC = datetime.datetime(2022, 9, 1, tzinfo=datetime.timezone.utc)
LEGACY_TWO_IDENTITY_LIMIT = 2


def _trim_discord_value(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _format_display_name_for_audit(display_name: str | None) -> str:
    name = (display_name or "Unknown").replace("`", "'").strip()
    if len(name) > 80:
        name = name[:77].rstrip() + "..."
    return f"`{name}`"


def _format_user_for_embed(user=None, user_id: int | str | None = None, fallback_name: str = "Unknown User") -> str:
    """Return a readable user label with a Discord user mention when an ID is available."""
    raw_name = getattr(user, "display_name", None) or getattr(user, "name", None) or fallback_name
    name = str(raw_name or fallback_name).replace("`", "'").strip() or fallback_name
    if len(name) > 80:
        name = name[:77].rstrip() + "..."

    raw_id = getattr(user, "id", None) or user_id
    return f"{name} (<@{raw_id}>)" if raw_id else name


def summarize_recent_identity_events(events: list[dict], max_events: int = 5) -> tuple[str, list[str]]:
    """Return a Discord-field summary and compact reason labels for recent identity events."""
    if not events:
        return "", []

    lines = []
    reasons = []
    for event in events[:max_events]:
        event_type = event.get("event_type")
        created_at = int(event.get("created_at") or 0)
        when = f"<t:{created_at}:R>" if created_at > 0 else "recently"

        if event_type == IDENTITY_EVENT_NICKNAME_CHANGE:
            if "Recent nickname change" not in reasons:
                reasons.append("Recent nickname change")
            old_name = _format_display_name_for_audit(event.get("old_display_name"))
            new_name = _format_display_name_for_audit(event.get("new_display_name"))
            lines.append(f"**Recent nickname change** {when}\n{old_name} -> {new_name}")
        elif event_type == IDENTITY_EVENT_MEMBER_JOIN:
            if "Recently joined server" not in reasons:
                reasons.append("Recently joined server")
            new_name = _format_display_name_for_audit(event.get("new_display_name"))
            lines.append(f"**Recently joined server** {when}\nDisplay name: {new_name}")
        else:
            if "Recent identity activity" not in reasons:
                reasons.append("Recent identity activity")
            lines.append(f"**Recent identity activity** {when}")

    if len(events) > max_events:
        lines.append(f"...and {len(events) - max_events} more recent event(s)")

    return _trim_discord_value("\n".join(lines)), reasons


def filter_recent_identity_events(
    events: list[dict],
    now_ts: int,
    window_seconds: int = RECENT_IDENTITY_WINDOW_SECONDS,
) -> list[dict]:
    """Filter identity-event dicts to the recent window, newest first."""
    cutoff = now_ts - window_seconds
    return sorted(
        [event for event in events if int(event.get("created_at") or 0) >= cutoff],
        key=lambda event: int(event.get("created_at") or 0),
        reverse=True,
    )


def filter_identity_events_after_authorization(
    events: list[dict],
    authorized_at: int | None,
) -> list[dict]:
    """Keep only identity events newer than the authorization that cleared them."""
    if not authorized_at:
        return list(events)
    return [
        event
        for event in events
        if int(event.get("created_at") or 0) > int(authorized_at)
    ]


def resolve_authorized_ambiguous_member(ambiguous_members: list, authorized_target: dict | None):
    """Pick the prior authorized member when it is one of the ambiguous candidates."""
    if not ambiguous_members or not authorized_target:
        return None
    authorized_user_id = int(authorized_target["user_id"])
    return next(
        (member for member in ambiguous_members if int(member.id) == authorized_user_id),
        None,
    )


_SQLITE_AUTOINCREMENT_TABLES = {
    "island_visits": """
        CREATE TABLE island_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ign TEXT NOT NULL,
            origin_island TEXT NOT NULL,
            destination TEXT NOT NULL,
            user_id INTEGER,
            guild_id INTEGER,
            authorized INTEGER NOT NULL DEFAULT 0,
            timestamp INTEGER NOT NULL,
            island_type TEXT NOT NULL DEFAULT 'sub',
            has_island_access INTEGER NOT NULL DEFAULT 0
        )
    """,
    "warnings": """
        CREATE TABLE warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            reason TEXT,
            mod_id INTEGER,
            timestamp INTEGER,
            visit_id INTEGER REFERENCES island_visits(id),
            action_type TEXT NOT NULL DEFAULT 'WARN',
            r1_reminder_sent INTEGER NOT NULL DEFAULT 0
        )
    """,
    "dodo_reveal_messages": """
        CREATE TABLE dodo_reveal_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            island_clean TEXT NOT NULL,
            channel_id TEXT,
            message_url TEXT NOT NULL,
            username TEXT,
            nickname TEXT,
            created_at INTEGER NOT NULL
        )
    """,
    "member_identity_events": """
        CREATE TABLE member_identity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER,
            event_type TEXT NOT NULL,
            old_display_name TEXT,
            new_display_name TEXT,
            created_at INTEGER NOT NULL
        )
    """,
}


async def _repair_sqlite_autoincrement_table(db, table_name: str, create_sql: str) -> None:
    """Rebuild old SQLAlchemy-created BIGINT PK tables as SQLite INTEGER rowid tables."""
    cur = await db.execute(f"PRAGMA table_info({table_name})")
    rows = await cur.fetchall()
    if not rows:
        return

    id_col = next((row for row in rows if row[1] == "id"), None)
    if id_col and str(id_col[2]).upper() == "INTEGER" and int(id_col[5] or 0) == 1:
        return

    backup_name = f"{table_name}__bad_autoinc"
    backup_cur = await db.execute(f"PRAGMA table_info({backup_name})")
    if await backup_cur.fetchall():
        await db.execute(f"DROP TABLE {backup_name}")

    await db.execute(f"ALTER TABLE {table_name} RENAME TO {backup_name}")
    await db.execute(create_sql)

    old_cols_cur = await db.execute(f"PRAGMA table_info({backup_name})")
    new_cols_cur = await db.execute(f"PRAGMA table_info({table_name})")
    old_cols = {row[1] for row in await old_cols_cur.fetchall()}
    new_cols = [row[1] for row in await new_cols_cur.fetchall()]
    shared_cols = [col for col in new_cols if col in old_cols]
    if shared_cols:
        cols_sql = ", ".join(shared_cols)
        await db.execute(f"INSERT INTO {table_name} ({cols_sql}) SELECT {cols_sql} FROM {backup_name}")

    await db.execute(f"DROP TABLE {backup_name}")
    logger.warning(f"[FLIGHT] Repaired SQLite autoincrement table: {table_name}")


async def _repair_sqlite_autoincrement_tables(db) -> None:
    if get_backend() != "sqlite":
        return
    for table_name, create_sql in _SQLITE_AUTOINCREMENT_TABLES.items():
        await _repair_sqlite_autoincrement_table(db, table_name, create_sql)


async def _ensure_mysql_autoincrement_tables(db) -> None:
    if get_backend() != "mysql":
        return
    for table_name in _SQLITE_AUTOINCREMENT_TABLES:
        try:
            await db.execute(f"ALTER TABLE {table_name} MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT")
        except Exception as exc:
            logger.debug(f"[FLIGHT] MySQL auto-increment check skipped for {table_name}: {exc}")


# --- DATABASE HELPERS ---
async def init_db():
    """Initializes the database schema."""
    async with connect_async_db() as db:
        await _repair_sqlite_autoincrement_tables(db)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS island_visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ign TEXT NOT NULL,
                origin_island TEXT NOT NULL,
                destination TEXT NOT NULL,
                user_id INTEGER,
                guild_id INTEGER,
                authorized INTEGER NOT NULL DEFAULT 0,
                timestamp INTEGER NOT NULL,
                island_type TEXT NOT NULL DEFAULT 'sub',
                has_island_access INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                reason TEXT,
                mod_id INTEGER,
                timestamp INTEGER,
                visit_id INTEGER REFERENCES island_visits(id),
                action_type TEXT NOT NULL DEFAULT 'WARN'
            )
        """)
        # Migrate existing databases: add visit_id column if it doesn't exist
        try:
            await db.execute("ALTER TABLE warnings ADD COLUMN visit_id INTEGER REFERENCES island_visits(id)")
        except Exception:
            pass  # Column already exists
        # Migrate existing databases: add action_type column if it doesn't exist
        try:
            await db.execute("ALTER TABLE warnings ADD COLUMN action_type TEXT NOT NULL DEFAULT 'WARN'")
        except Exception:
            pass  # Column already exists
        # Migrate existing databases: add r1_reminder_sent column if it doesn't exist
        try:
            await db.execute("ALTER TABLE warnings ADD COLUMN r1_reminder_sent INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # Column already exists
        # Migrate existing databases: add island_type column if it doesn't exist
        try:
            await db.execute("ALTER TABLE island_visits ADD COLUMN island_type TEXT NOT NULL DEFAULT 'sub'")
        except Exception:
            pass  # Column already exists
        # Migrate existing databases: add has_island_access column if it doesn't exist
        try:
            await db.execute("ALTER TABLE island_visits ADD COLUMN has_island_access INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # Column already exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dodo_reveal_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                island_clean TEXT NOT NULL,
                channel_id TEXT,
                message_url TEXT NOT NULL,
                username TEXT,
                nickname TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS member_identity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER,
                event_type TEXT NOT NULL,
                old_display_name TEXT,
                new_display_name TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        try:
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_member_identity_events_user_guild_ts "
                "ON member_identity_events (user_id, guild_id, created_at)"
            )
        except Exception as exc:
            logger.debug(f"[FLIGHT] member_identity_events index check skipped: {exc}")
        await _ensure_mysql_autoincrement_tables(db)
        await db.commit()

DEFAULT_REASON_TEXT = (
    "Breaking [Sub Rule #2](https://discord.com/channels/729590421478703135/"
    "783677194576330792/1137904975553499217). We have removed your island access "
    "for now. Please read the <#783677194576330792> again to gain access."
)

REASON_TEMPLATES = {
    "rule_top_1": "Breaking [Sub Top Rule](https://discord.com/channels/729590421478703135/783677194576330792/1249835404098801756) or [Sub Rule #1](https://discord.com/channels/729590421478703135/783677194576330792/1249835467067752461). We have removed your island access for now. Please contact someone on the mod team in regards to your recent warning. If we do not hear anything within 24 hours you will be banned.",
    "rule_2": "Breaking [Sub Rule #2](https://discord.com/channels/729590421478703135/783677194576330792/1137904975553499217). We have removed your island access for now. Please read the <#783677194576330792> again to gain access.",
    "rule_3_4": "Breaking [Sub Rule #3](https://discord.com/channels/729590421478703135/783677194576330792/1137905005433733211)/[Sub Rule #4](https://discord.com/channels/729590421478703135/783677194576330792/1137905033699151893). We have removed your island access for now.",
    "rule_6": "Breaking [Sub Rule #6](https://discord.com/channels/729590421478703135/783677194576330792/1137905106919096442). We have removed your island access for now. Please read the <#783677194576330792> again to gain access.",
    "rule_8": "Breaking [Sub Rule #8](https://discord.com/channels/729590421478703135/783677194576330792/1137905158257397875). We have removed your island access for now. Please read the <#783677194576330792> again to gain access."
}

REASON_OPTIONS = [
    discord.SelectOption(label="Sub Top Rule / Rule #1", value="rule_top_1", description="Breaking [Sub Top Rule]"),
    discord.SelectOption(label="Sub Rule #2", value="rule_2", description="Breaking [Sub Rule #2]"),
    discord.SelectOption(label="Sub Rule #3 / #4", value="rule_3_4", description="Breaking [Sub Rule #3]"),
    discord.SelectOption(label="Sub Rule #6", value="rule_6", description="Breaking [Sub Rule #6]"),
    discord.SelectOption(label="Sub Rule #8", value="rule_8", description="Breaking [Sub Rule #8]"),
    discord.SelectOption(label="Custom Reason", value="custom", description="Provide a custom reason"),
]

DURATION_OPTIONS = [
    discord.SelectOption(label="1 Hour",    value="1h"),
    discord.SelectOption(label="1 Day",     value="1d"),
    discord.SelectOption(label="2 Days",    value="2d"),
    discord.SelectOption(label="3 Days",    value="3d"),
    discord.SelectOption(label="1 Week",    value="1w"),
    discord.SelectOption(label="Permanent", value="perm"),
]

def _build_options_with_default(base_options: list[discord.SelectOption], selected_value: str | None, custom_text: str | None = None):
    new_options = []
    for opt in base_options:
        label = opt.label
        description = opt.description
        is_default = (opt.value == selected_value)

        if opt.value == "custom" and custom_text:
            cleaned_text = custom_text.replace("\n", " ").strip()
            display_text = (cleaned_text[:50] + "...") if len(cleaned_text) > 50 else cleaned_text
            label = f"Custom: {display_text}"
            description = "Click to modify your custom reason"

        new_options.append(
            discord.SelectOption(
                label=label, value=opt.value, description=description,
                default=is_default
            )
        )
    return new_options

def _parse_duration(duration: str) -> datetime.timedelta | None:
    """Parse a duration string into a timedelta. Returns None for permanent."""
    mapping = {
        "1h": datetime.timedelta(hours=1),
        "1d": datetime.timedelta(days=1),
        "2d": datetime.timedelta(days=2),
        "3d": datetime.timedelta(days=3),
        "1w": datetime.timedelta(weeks=1),
    }
    return mapping.get(duration)

def create_sapphire_log(member: discord.Member, mod: discord.Member, reason: str, case_id: str, warn_count: int, duration: str, action_verb: str):
    """Generates the visual embed mimicking Sapphire"""
    now = discord.utils.utcnow()
    
    mod_role_name = mod.top_role.name if hasattr(mod, 'top_role') and mod.top_role else "Moderator"

    if action_verb.upper() in ["KICKED", "BANNED"]:
        desc_lines = [
            f"> **{_format_user_for_embed(member)}** has been {action_verb.lower()}!",
            f"> **Reason:** {reason}",
            f"> **Responsible:** {_format_user_for_embed(mod)} ({mod_role_name})",
        ]
    else:
        delta = _parse_duration(duration)
        desc_lines = [
            f"> **{_format_user_for_embed(member)}** has been {action_verb.lower()}!",
            f"> **Reason:** {reason}",
            f"> **Duration:** {duration}",
            f"> **Count:** {warn_count}",
            f"> **Responsible:** {_format_user_for_embed(mod)} ({mod_role_name})",
        ]
        if delta is not None:
            expiry_ts = int((now + delta).timestamp())
            desc_lines.append(f"> Automatically expires <t:{expiry_ts}:R>")
        desc_lines.extend([
            f"> **Proof:** Verified (Log System)",
            "> ",
            "> **For Sub Members**: Please double check our <#783677194576330792> channel.",
            "> **For Free Members**: Kindly refer to our <#755522711492493342> channel."
        ])

    embed = discord.Embed(
        title=f"**{action_verb.title()} Case ID: {case_id}**",
        description="\n".join(desc_lines),
        color=0xff0000,
        timestamp=now
    )
    embed.set_thumbnail(url="https://i.ibb.co/HXyRH3R/2668-Siren.gif")
    embed.set_footer(text=f"Mod: {mod.display_name}", icon_url=mod.display_avatar.url)
    return embed

# --- UI VIEWS ---

# --- REFACTORED UI COMPONENTS ---

class TargetSelect(discord.ui.UserSelect):
    def __init__(self, parent_view, default_member=None):
        defaults = [default_member] if default_member is not None else []
        super().__init__(
            placeholder="1. Select the Target User...",
            min_values=1,
            max_values=1,
            row=0,
            default_values=defaults
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        # discord.py 2.0+ automatically resolves members in self.values
        if self.values:
            # self.values[0] is typically a Member or User object
            self.parent_view.selected_member = self.values[0]
        
        await self.parent_view.refresh_state(interaction)

class DurationSelect(discord.ui.Select):
    def __init__(self, parent_view, current_duration):
        options = _build_options_with_default(DURATION_OPTIONS, current_duration)
        super().__init__(
            placeholder="2. Select Duration",
            min_values=1,
            max_values=1,
            options=options,
            row=1
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if self.values:
            self.parent_view.selected_duration = self.values[0]
        await self.parent_view.refresh_state(interaction)

class CustomReasonModal(discord.ui.Modal, title="Custom Punishment Reason"):
    reason_input = discord.ui.TextInput(
        label="Reason",
        placeholder="Enter the specific reason for this action...",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=5,
        max_length=500
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.selected_reason = "custom"
        self.parent_view.custom_reason_text = self.reason_input.value
        await self.parent_view.refresh_state(interaction)

class ReasonSelect(discord.ui.Select):
    def __init__(self, parent_view, current_reason, custom_text=None):
        options = _build_options_with_default(REASON_OPTIONS, current_reason, custom_text)
        super().__init__(
            placeholder="3. Select Reason",
            min_values=1,
            max_values=1,
            options=options,
            row=2
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if self.values:
            selected = self.values[0]
            if selected == "custom":
                await interaction.response.send_modal(CustomReasonModal(self.parent_view))
            else:
                self.parent_view.selected_reason = selected
                self.parent_view.custom_reason_text = None
                await self.parent_view.refresh_state(interaction)

class ConfirmButton(discord.ui.Button):
    def __init__(self, parent_view, label, style, disabled):
        super().__init__(label=label, style=style, disabled=disabled, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.execute_punishment(interaction)

class CancelButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Action cancelled.", view=None)
        self.parent_view.stop()

# --- REFACTORED BUILDER VIEW ---

class PunishmentBuilderView(discord.ui.View):
    def __init__(self, action_type: str, original_view: "TravelerActionView", log_message: discord.Message):
        super().__init__(timeout=3600)
        self.action_type = action_type
        self.original_view = original_view
        self.log_message = log_message

        self.selected_member: discord.Member | discord.User | None = None
        self.selected_duration: str | None = "3d"
        self.selected_reason: str | None = None
        self.custom_reason_text: str | None = None
        
        # Initial render
        self._update_components()

    def _update_components(self):
        """Clear and re-add components based on current state."""
        self.clear_items()

        self.add_item(TargetSelect(self, default_member=self.selected_member))

        if self.action_type == "WARN":
            self.add_item(DurationSelect(self, self.selected_duration))
        self.add_item(ReasonSelect(self, self.selected_reason, self.custom_reason_text))

        # 4. Confirm & Cancel Buttons
        # Submission restricted until all required fields are filled
        has_member = self.selected_member is not None
        has_reason = self.selected_reason is not None
        has_duration = self.selected_duration is not None or self.action_type != "WARN"

        can_submit = has_member and has_reason and has_duration
        
        if self.selected_member:
            target_name = getattr(self.selected_member, "display_name", str(self.selected_member))
            label = f"Confirm {self.action_type.title()} on {target_name}"
        else:
            label = "Confirm Action"

        style = discord.ButtonStyle.danger
        self.add_item(ConfirmButton(self, label, style, disabled=not can_submit))
        self.add_item(CancelButton(self))

    async def refresh_state(self, interaction: discord.Interaction):
        """Called by children to update the view state and message."""
        self._update_components()
        
        # Use edit_original_response if the interaction has already been responded to (e.g. Modal)
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    async def execute_punishment(self, interaction: discord.Interaction):
        """Pass execution to the Cog for cleaner logic."""
        cog = interaction.client.get_cog("FlightLoggerCog")
        if not cog:
            return await interaction.response.send_message("Error: FlightLoggerCog not found.", ephemeral=True)

        # Disable EVERYTHING in the builder view to prevent double-click
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        target = self.selected_member
        
        if self.selected_reason == "custom" and self.custom_reason_text:
            reason_text = self.custom_reason_text
        else:
            reason_text = REASON_TEMPLATES.get(self.selected_reason, DEFAULT_REASON_TEXT)
        
        await cog._execute_punishment_internal(
            interaction,
            target,
            self.action_type,
            reason_text,
            self.selected_duration,
            self.original_view,
            self.log_message
        )
        self.stop()


class AdmitUserSelect(discord.ui.UserSelect):
    """Optional user selector for linking a Discord member to an admit action."""

    def __init__(self, parent_view: "AdmitConfirmView"):
        super().__init__(
            placeholder="(Optional) Link to a Discord user...",
            min_values=0,
            max_values=1,
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_member = self.values[0] if self.values else None
        try:
            await interaction.response.edit_message(content=self.parent_view._build_content())
        except discord.NotFound:
            pass


class AdmitConfirmView(discord.ui.View):
    """Confirmation dialog for admitting a traveler."""

    def __init__(self, parent_view: "TravelerActionView", ign: str, original_alert_message: discord.Message):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.ign = ign
        self.original_alert_message = original_alert_message
        self.selected_member: discord.Member | discord.User | None = None
        self.add_item(AdmitUserSelect(self))

    def _build_content(self) -> str:
        base = f"Are you sure you want to admit **{self.ign or 'Visitor'}**?"
        if self.selected_member:
            return f"{base}\n👤 Linked to: {_format_user_for_embed(self.selected_member)}"
        return base

    @discord.ui.button(label="Yes, Admit", style=discord.ButtonStyle.success, row=1)
    async def confirm_admit(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Proceed with admission."""
        msg = f"**{self.ign or 'Visitor'}** is cleared for entry."
        if self.selected_member:
            msg += f" Linked to {_format_user_for_embed(self.selected_member)}."
        # Update the original alert message (the flight log alert)
        await self.parent_view._resolve_alert(
            interaction, "AUTHORIZED", COLOR_SUCCESS, msg,
            target_user=self.selected_member,
            log_message=self.original_alert_message,
        )
        cog = self.parent_view.bot.get_cog("FlightLoggerCog") if self.parent_view.bot else None
        if cog:
            ign = self.ign
            visit_id = getattr(self.parent_view, 'visit_id', None)
            # Prefer embed-embedded ID (survives bot restarts), then IGN lookup
            if not visit_id and self.original_alert_message and self.original_alert_message.embeds:
                visit_id = self.parent_view._get_visit_id_from_embed(self.original_alert_message.embeds[0])
            if not visit_id and ign:
                visit_id = await cog._get_recent_visit_id_by_ign(ign)
            if visit_id is not None:
                async with connect_async_db() as db:
                    # Mark the visit as authorized now that a mod has manually admitted the traveler
                    await db.execute(
                        "UPDATE island_visits SET authorized = 1 WHERE id = ?",
                        (visit_id,),
                    )
                    # Link the selected Discord member to the visit record if provided
                    if self.selected_member:
                        await db.execute(
                            "UPDATE island_visits SET user_id = ? WHERE id = ? AND user_id IS NULL",
                            (self.selected_member.id, visit_id),
                        )
            target_id = self.selected_member.id if self.selected_member else None
            await cog.add_warning(target_id, interaction.guild.id, None, interaction.user.id, visit_id, action_type='ADMIT')
        # Update the confirmation message to show success
        await interaction.response.edit_message(content=msg, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_admit(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel admission."""
        await interaction.response.edit_message(content="Admission cancelled.", view=None)
        self.stop()


class NoteModal(discord.ui.Modal, title="Add Note"):
    """Modal for adding a note to a flight alert."""
    note_input = discord.ui.TextInput(
        label="Note",
        style=discord.TextStyle.paragraph,
        placeholder="Enter your note about this traveler...",
        required=True,
        max_length=500
    )

    def __init__(self, parent_view: "TravelerActionView", alert_message: discord.Message, linked_user: discord.Member | discord.User | None = None):
        super().__init__()
        self.parent_view = parent_view
        self.alert_message = alert_message
        self.linked_user = linked_user

    async def on_submit(self, interaction: discord.Interaction):
        try:
            message_to_edit = self.alert_message
            embed = message_to_edit.embeds[0]
            timestamp = int(discord.utils.utcnow().timestamp())
            note_value = self.note_input.value
            if self.linked_user:
                note_value += f"\n-# Linked: {_format_user_for_embed(self.linked_user)}"
            note_value += f"\n-# Added <t:{timestamp}:R>"
            embed.add_field(
                name=f"<:Cho_Notes:1474311464688029817> Note by {interaction.user.display_name}",
                value=note_value,
                inline=False
            )
            await message_to_edit.edit(embed=embed)
            cog = self.parent_view.bot.get_cog("FlightLoggerCog") if self.parent_view.bot else None
            if cog:
                ign = self.parent_view.ign
                visit_id = self.parent_view.visit_id
                # Prefer embed-embedded ID (survives bot restarts), then IGN lookup
                if not visit_id and message_to_edit.embeds:
                    visit_id = self.parent_view._get_visit_id_from_embed(message_to_edit.embeds[0])
                if not visit_id and ign:
                    visit_id = await cog._get_recent_visit_id_by_ign(ign)
                target_id = self.linked_user.id if self.linked_user else None
                await cog.add_warning(target_id, interaction.guild.id, self.note_input.value, interaction.user.id, visit_id, action_type='NOTE')
            await interaction.response.send_message("<:Cho_Notes:1474311464688029817> Note added to the alert.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error adding note: {e}", exc_info=True)
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)


class NoteUserSelect(discord.ui.UserSelect):
    """Optional user selector for linking a Discord member to a note."""

    def __init__(self, parent_view: "NoteBuilderView"):
        super().__init__(
            placeholder="(Optional) Link to a Discord user...",
            min_values=0,
            max_values=1,
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_member = self.values[0] if self.values else None
        content = "<:Cho_Notes:1474311464688029817> **Add Note:**\nOptionally link a Discord user, then click **Write Note...**"
        if self.parent_view.selected_member:
            content += f"\n👤 Linked to: {_format_user_for_embed(self.parent_view.selected_member)}"
        try:
            await interaction.response.edit_message(content=content)
        except discord.NotFound:
            pass


class NoteBuilderView(discord.ui.View):
    """Ephemeral view shown before the note modal to optionally link a Discord user."""

    def __init__(self, parent_view: "TravelerActionView", alert_message: discord.Message):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.alert_message = alert_message
        self.selected_member: discord.Member | discord.User | None = None
        self.add_item(NoteUserSelect(self))

    @discord.ui.button(label="Write Note...", style=discord.ButtonStyle.primary, row=1)
    async def write_note(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            NoteModal(self.parent_view, self.alert_message, linked_user=self.selected_member)
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_note(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class TravelerActionView(discord.ui.View):
    def __init__(self, bot=None, ign=None, visit_id=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.ign = ign
        self.visit_id = visit_id

    def _get_ign_from_embed(self, embed: discord.Embed):
        """Extracts IGN from the '👤 Traveler (IGN)' field in the alert embed."""
        if not embed or not embed.fields:
            return None
        for field in embed.fields:
            if "Traveler (IGN)" in field.name:
                # Value is usually "```yaml\nIGN```"
                match = re.search(r"```(?:yaml)?\n(.*?)\n?```", field.value)
                if match:
                    return match.group(1).strip()
        return None

    def _get_visit_id_from_embed(self, embed: discord.Embed) -> int | None:
        """Extracts the visit ID from the 'Visit ID' field in the alert embed."""
        if not embed or not embed.fields:
            return None
        for field in embed.fields:
            if field.name == "Visit ID":
                match = re.search(r"#(\d+)", field.value)
                if match:
                    return int(match.group(1))
        return None

    async def _resolve_alert(self, interaction, status_label, color, log_msg, target_user=None, log_message=None, reason=None, mod_log_url=None):
        """Internal helper to update the alert embed state. Does NOT send interaction responses."""
        target_str      = _format_user_for_embed(target_user) if target_user else "Visitor (unlinked)"
        message_to_edit = log_message or (interaction.message if interaction.response.is_done() else None)

        if not message_to_edit:
            return

        try:
            # Refresh message state if possible to avoid 404
            embed = message_to_edit.embeds[0]
            
            # Remove investigation fields and update Status field
            fields_to_keep = []
            for f in embed.fields:
                if "Investigating" in f.name:
                    continue
                if f.name == "Status":
                    # Replace Status field with resolved status
                    fields_to_keep.append(("Status", f"<:Cho_Check:1456715827213504593> **{status_label}**", True))
                else:
                    fields_to_keep.append((f.name, f.value, f.inline))

            embed.clear_fields()
            for name, value, inline in fields_to_keep:
                embed.add_field(name=name, value=value, inline=inline)
            
            # Update color and header
            embed.color = color
            embed.set_author(name=f"CASE CLOSED: {status_label}", icon_url=target_user.display_avatar.url if target_user else interaction.user.display_avatar.url)
            resolved_ts = int(discord.utils.utcnow().timestamp())
            action_value = f"**{status_label}** by {_format_user_for_embed(interaction.user)}\nTarget: {target_str}\nResolved <t:{resolved_ts}:R>"
            
            # Add island access status and subscription roles if target is a member
            if target_user and hasattr(target_user, 'roles'):
                has_access = any(r.id == Config.ISLAND_ACCESS_ROLE for r in target_user.roles)
                access_status = "Yes" if has_access else "No"
                action_value += f"\n**Has Island Access?** {access_status}"

                # Obtain cog and parse member nickname using the cog helpers
                cog = interaction.client.get_cog("FlightLoggerCog")
                all_sub_roles = cog.all_sub_roles if cog else set()
                ign_opts, island_opts = (cog.parse_member_nick(target_user.display_name) if cog else ([], []))
                max_identities = max(len(ign_opts), len(island_opts))

                # Get destination from embed to check subscription roles using the cog's island_map
                dest_channel = None
                for field in embed.fields:
                    if field.name == "Destination":
                        dest_clean = clean_text(field.value)
                        channel_id = cog.island_map.get(dest_clean) if cog and getattr(cog, 'island_map', None) else None
                        dest_channel = interaction.guild.get_channel(channel_id) if channel_id and interaction.guild else None
                        break

                if dest_channel:
                    sub_roles = {}
                    for target_obj, overwrite in dest_channel.overwrites.items():
                        can_view = (getattr(overwrite, "view_channel", None) is True) or (getattr(overwrite, "read_messages", None) is True)
                        if isinstance(target_obj, discord.Role) and can_view:
                            if target_obj.name != "@everyone":
                                sub_roles[target_obj.id] = target_obj.name

                    current_island_subs = [sub_roles[r.id] for r in target_user.roles if r.id in sub_roles]
                    other_subs = [r.name for r in target_user.roles if r.id in all_sub_roles and r.id not in sub_roles]
                    total_subs = len(current_island_subs) + len(other_subs)

                    if current_island_subs:
                        subs_str = ", ".join(current_island_subs)
                        action_value += f"\n**Subscription(s):** {subs_str}"
                    else:
                        action_value += f"\n**Subscription(s):** None for this island"
                    
                    if other_subs:
                        other_subs_str = ", ".join(other_subs)
                        action_value += f"\n**Other Subscription(s):** {other_subs_str}"
                    
                    if max_identities > 1 and total_subs < max_identities:
                        action_value += f"\n**MISMATCH: {max_identities} identities in nick, but only {total_subs} sub(s)**"
                    elif total_subs > 1:
                        action_value += f"\n**Multiple Subscriptions Detected**"
            
            if reason:
                action_value += f"\n**Reason:** {reason}"
            if mod_log_url:
                action_value += f"\n[View in Mod Log]({mod_log_url})"
            embed.add_field(
                name="<:ChoLove:818216528449241128> Action Taken",
                value=action_value,
                inline=False
            )
            # Remove all existing items first
            self.clear_items()
            await message_to_edit.edit(embed=embed, view=self)

            # Remove from pending alerts so future joins create a fresh alert
            cog = self.bot.get_cog("FlightLoggerCog") if self.bot else None
            if cog and self.ign:
                ign_clean = clean_text(self.ign)
                keys_to_remove = [k for k in cog._pending_alerts if k[0] == ign_clean]
                for k in keys_to_remove:
                    cog._pending_alerts.pop(k, None)
        except Exception as e:
            logger.error(f"Error editing original message: {e}", exc_info=True)

    def disable_all_items(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Investigate", style=discord.ButtonStyle.secondary, emoji="<:Cho_Investigate:1474310726381338666>", custom_id="fl_investigate", row=0)
    async def investigate_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return  # Stale interaction, silently ignore

        ign = self.ign or self._get_ign_from_embed(interaction.message.embeds[0])
        mod = interaction.user
        timestamp = int(discord.utils.utcnow().timestamp())

        try:
            message_to_edit = interaction.message
            embed = message_to_edit.embeds[0]

            # Update color to amber/yellow
            embed.color = COLOR_INVESTIGATION

            # Update author to show investigation status
            embed.set_author(name="UNDER INVESTIGATION", icon_url=mod.display_avatar.url)

            # Update Status field if it exists
            updated_fields = []
            for f in embed.fields:
                if f.name == "Status":
                    updated_fields.append((f.name, "<:Cho_Investigate:1474310726381338666> **INVESTIGATING**", f.inline))
                else:
                    updated_fields.append((f.name, f.value, f.inline))
            embed.clear_fields()
            for name, value, inline in updated_fields:
                embed.add_field(name=name, value=value, inline=inline)

            # Add investigation field
            embed.add_field(
                name="<:Cho_Investigate:1474310726381338666> Investigating",
                value=f"**{_format_user_for_embed(mod)}** is looking into this. Started <t:{timestamp}:R>",
                inline=False
            )

            # Disable only the Investigate button
            button.disabled = True

            await message_to_edit.edit(embed=embed, view=self)
            await interaction.followup.send("<:Cho_Investigate:1474310726381338666> Marked as under investigation.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error marking as under investigation: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @discord.ui.button(label="Admit", style=discord.ButtonStyle.success, emoji="<:Cho_Check:1456715827213504593>", custom_id="fl_admit", row=0)
    async def confirm_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return  # Stale interaction, silently ignore
        ign = self.ign or self._get_ign_from_embed(interaction.message.embeds[0])
        # Show confirmation dialog
        confirm_view = AdmitConfirmView(self, ign, interaction.message)
        await interaction.followup.send(
            f"Are you sure you want to admit **{ign or 'Visitor'}**?",
            view=confirm_view,
            ephemeral=True
        )

    @discord.ui.button(label="Warn", style=discord.ButtonStyle.primary, emoji="<:Cho_Warn:1456712416271405188>", custom_id="fl_warn", row=1)
    async def warn_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        view = PunishmentBuilderView("WARN", self, log_message=interaction.message)
        await interaction.followup.send("<:Cho_Warn:1456712416271405188> **Build Warning:**", view=view, ephemeral=True)

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.secondary, emoji="<:Cho_Kick:1456714701630214349>", custom_id="fl_kick", row=1)
    async def kick_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        view = PunishmentBuilderView("KICK", self, log_message=interaction.message)
        await interaction.followup.send("<:Cho_Kick:1456714701630214349> **Build Kick:**", view=view, ephemeral=True)

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, emoji="<:Cho_Ban:1473530840725061793>", custom_id="fl_ban", row=1)
    async def ban_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        view = PunishmentBuilderView("BAN", self, log_message=interaction.message)
        await interaction.followup.send("<:Cho_Ban:1473530840725061793> **Build Ban:**", view=view, ephemeral=True)

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, emoji="<:Cho_Dismiss:1474955282026332180>", custom_id="fl_dismiss", row=2)
    async def dismiss_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Dismiss the alert as a false positive or non-threat."""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        ign = self.ign or self._get_ign_from_embed(interaction.message.embeds[0])
        msg = f"**{ign or 'Visitor'}** dismissed."
        await self._resolve_alert(
            interaction, "DISMISSED", COLOR_DISMISS, msg, log_message=interaction.message
        )
        cog = self.bot.get_cog("FlightLoggerCog") if self.bot else None
        if cog:
            visit_id = self.visit_id
            if not visit_id and interaction.message and interaction.message.embeds:
                visit_id = self._get_visit_id_from_embed(interaction.message.embeds[0])
            if not visit_id and ign:
                visit_id = await cog._get_recent_visit_id_by_ign(ign)
            await cog.add_warning(None, interaction.guild.id, None, interaction.user.id, visit_id, action_type='DISMISS')
        await interaction.followup.send(f"**{ign or 'Visitor'}** case has been dismissed.", ephemeral=True)

    @discord.ui.button(label="Note", style=discord.ButtonStyle.secondary, emoji="<:Cho_Notes:1474311464688029817>", custom_id="fl_note", row=2)
    async def note_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Add a note to the alert without taking action."""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        view = NoteBuilderView(self, interaction.message)
        await interaction.followup.send(
            "<:Cho_Notes:1474311464688029817> **Add Note:**\nOptionally link a Discord user, then click **Write Note...**",
            view=view,
            ephemeral=True,
        )

class FlagConfirmView(discord.ui.View):
    """Confirmation dialog for flagging a verified flight."""

    def __init__(self, parent_view: "VerifiedFlightFlagView", ign: str, original_message: discord.Message):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.ign = ign
        self.original_message = original_message

    @discord.ui.button(label="Yes, Flag", style=discord.ButtonStyle.danger, emoji="🚩", row=0)
    async def confirm_flag(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Proceed with flagging."""
        try:
            await self.parent_view._execute_flag(interaction, self.original_message)
            try:
                await interaction.response.edit_message(content="🚩 Flight flagged for review.", view=None)
            except Exception:
                try:
                    await interaction.followup.send("🚩 Flight flagged for review (alert created in flight log channel).", ephemeral=True)
                except Exception:
                    pass  # Silently ignore if even followup fails
        except Exception as e:
            logger.error(f"[FLAG] Error in confirm_flag: {e}", exc_info=True)
            try:
                await interaction.response.edit_message(
                    content="🚩 Flight flagged for review (alert created in flight log channel).",
                    view=None
                )
            except Exception:
                try:
                    await interaction.followup.send("🚩 Flight flagged for review (alert created in flight log channel).", ephemeral=True)
                except Exception:
                    pass
        finally:
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
    async def cancel_flag(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel flag action."""
        await interaction.response.edit_message(content="Flag action cancelled.", view=None)
        self.stop()


class VerifiedFlightFlagView(discord.ui.View):
    """View attached to verified-flight xlog messages.

    Provides a 🚩 Flag button so mods can escalate a bot-matched flight to a
    manual alert when they believe the match was missed or incorrect.
    Once flagged the button is replaced by a static "View Alert (manual)" link.
    """

    def __init__(self, bot=None, ign=None, visit_id=None, message_url=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.ign = ign
        self.visit_id = visit_id
        self.message_url = message_url

    def _extract_field(self, embed: discord.Embed, field_name: str) -> str | None:
        """Return the plain-text value of a named embed field."""
        if not embed or not embed.fields:
            return None
        for field in embed.fields:
            if field.name == field_name:
                match = re.search(r"```(?:yaml)?\n(.*?)\n?```", field.value, re.DOTALL)
                if match:
                    return match.group(1).strip()
                return field.value
        return None

    async def _resolve_member(self, guild: discord.Guild | None, user_id: int) -> discord.Member | None:
        """Resolve a member by ID using cache first, then fetch if missing."""
        if not guild:
            return None
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            logger.debug(f"[FLAG] Could not fetch member {user_id} from guild {getattr(guild, 'id', None)}")
            return None

    async def _execute_flag(self, interaction: discord.Interaction, xlog_message: discord.Message):
        """Execute the flag action: create alert and update xlog message. Also flag for xlog if multiple IGNs/islands and multiple subscriptions."""
        embed = xlog_message.embeds[0] if xlog_message.embeds else None
        ign = self.ign or self._extract_field(embed, "IGN")
        island = None
        if embed:
            island = self._extract_field(embed, "Island Name") or self._extract_field(embed, "Origin Island")
        destination_display = self._extract_field(embed, "Destination") if embed else "Unknown"
        visit_id = self.visit_id
        if not visit_id and embed:
            for field in embed.fields:
                if field.name == "Visit ID":
                    m = re.search(r"#(\d+)", field.value)
                    if m:
                        visit_id = int(m.group(1))
                        break
        msg_url = self.message_url
        if not msg_url and embed and embed.description:
            m = re.search(r'\[.*?\]\((https?://[^)]+)\)', embed.description)
            if m:
                msg_url = m.group(1)

        # --- Check for multiple IGNs/islands and multiple subscriptions ---
        flagged_for_multi = False
        debug_info = None
        guild = self.bot.get_guild(Config.GUILD_ID) if self.bot else None
        traveler_member = None
        if guild and embed and embed.description:
            # Prefer resolving the linked traveler from the verbose-xlog embed.
            # (The interaction user is the mod clicking the button, not the traveler.)
            m = re.search(r"<@!?(?P<uid>\d+)>", embed.description)
            if not m:
                m = re.search(r"`(?P<uid>\d{15,25})`", embed.description)
            if m:
                traveler_member = await self._resolve_member(guild, int(m.group("uid")))

        if traveler_member is None and guild and visit_id is not None:
            # Fallback: resolve via island_visits record, if present.
            try:
                async with connect_async_db() as db:
                    cur = await db.execute("SELECT user_id FROM island_visits WHERE id = ? LIMIT 1", (visit_id,))
                    row = await cur.fetchone()
                if row and row[0]:
                    traveler_member = await self._resolve_member(guild, int(row[0]))
            except Exception:
                traveler_member = None

        # If we cannot reliably resolve the traveler, leave `traveler_member` as None.
        # Do NOT fall back to the moderator who clicked the button; that causes false positives.
        igns, islands = [], []
        if traveler_member:
            # Use the robust parser from FlightLoggerCog
            cog = self.bot.get_cog("FlightLoggerCog") if self.bot else None
            if cog:
                igns, islands = cog.parse_member_nick(traveler_member.display_name)
                # Use the up-to-date sub role set from cog (should be set on fetch_islands)
                await cog._ensure_sub_roles_loaded()
                all_sub_roles = getattr(cog, 'all_sub_roles', set())
                member_sub_roles = [r for r in traveler_member.roles if r.id in all_sub_roles]
                sub_count = len(member_sub_roles)
                ign_count = len(igns)
                island_count = len(islands)
                max_count = max(ign_count, island_count)
                # Add debug info to alert for troubleshooting
                sub_role_mentions = [r.mention for r in member_sub_roles]
                debug_info = (
                    f"Identities: {max_count} | Subs: {sub_count}\n"
                    f"IGNs: {igns}\n"
                    f"Islands: {islands}\n"
                    f"Sub Roles: {sub_role_mentions if sub_role_mentions else '[]'}"
                )
                if max_count > 1 and max_count != sub_count:
                    flagged_for_multi = True
        # If flagged, add a field to the alert and log
        alert_msg = None
        output_channel = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID) if self.bot else None
        dodo_req = None
        if output_channel:
            guild_icon = guild.icon.url if guild and guild.icon else None
            alert_ts = int(discord.utils.utcnow().timestamp())
            alert_embed = discord.Embed(
                description=(
                    f"### {Config.EMOJI_FAIL} Flight Flagged for Review\n"
                    f"A verified flight was manually flagged by {_format_user_for_embed(interaction.user)}.\n"
                    f"Use the buttons below to take action."
                ),
                color=COLOR_INVESTIGATION,
                timestamp=discord.utils.utcnow(),
            )
            alert_embed.add_field(name="Traveler (IGN)", value=f"```yaml\n{ign or 'Unknown'}```", inline=True)
            if island:
                alert_embed.add_field(name="Origin Island", value=f"```yaml\n{island.title()}```", inline=True)
            alert_embed.add_field(name="Destination", value=destination_display or "Unknown", inline=True)
            alert_embed.add_field(name="Flagged", value=f"<t:{alert_ts}:R>", inline=True)
            alert_embed.add_field(name="Status", value="<:Cho_Investigate:1474310726381338666> **PENDING REVIEW**", inline=True)
            if debug_info:
                alert_embed.add_field(name="Debug Info", value=debug_info, inline=False)
            if visit_id is not None:
                alert_embed.add_field(name="Visit ID", value=f"`#{visit_id}`", inline=True)
            if flagged_for_multi:
                alert_embed.add_field(
                    name="Flag Reason",
                    value=(
                        "**Mismatch Detected (Auto-Flag)**\n"
                        f"Traveler {_format_user_for_embed(traveler_member)} has multiple identities in nickname, but fewer subscription roles detected.\n"
                                            ),
                    inline=False,
                )
            alert_embed.set_image(url=Config.FOOTER_LINE)
            alert_embed.set_footer(text="Chopaeng Camp™ • Flight Logger", icon_url=guild_icon)
            action_view = TravelerActionView(self.bot, ign, visit_id=visit_id)
            alert_msg = await output_channel.send(embed=alert_embed, view=action_view)

        # Log the flag action in the warnings table (None user_id = no linked Discord member)
        cog = self.bot.get_cog("FlightLoggerCog") if self.bot else None
        if cog:
            await cog.add_warning(
                None, interaction.guild_id,
                "Flight manually flagged for review" + (" [MULTI-IGN/SUB]" if flagged_for_multi else ""),
                interaction.user.id, visit_id, action_type='FLAG',
            )

        # --- Update the xlog message: mark as flagged, swap button for link ---
        try:
            if embed:
                embed.color = COLOR_INVESTIGATION
                embed.title = "🚩 Flagged Flight"
                flag_ts = int(discord.utils.utcnow().timestamp())
                embed.add_field(
                    name="🚩 Flagged",
                    value=f"By {_format_user_for_embed(interaction.user)} <t:{flag_ts}:R>",
                    inline=False,
                )
            new_view = discord.ui.View()
            if msg_url:
                new_view.add_item(discord.ui.Button(label="View Flight Standing", url=msg_url, style=discord.ButtonStyle.link))
            if alert_msg:
                new_view.add_item(discord.ui.Button(label="View Alert", url=alert_msg.jump_url, style=discord.ButtonStyle.link))
            await xlog_message.edit(embed=embed, view=new_view)
        except (discord.NotFound, discord.HTTPException):
            logger.debug(f"[FLAG] Could not update old xlog message, but alert was created in flight log")
        except Exception as e:
            logger.error(f"[FLAG] Error updating xlog message after flag: {e}", exc_info=True)

    @discord.ui.button(label="Flag", style=discord.ButtonStyle.secondary, emoji="🚩", custom_id="fl_flag_verified", row=0)
    async def flag_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Escalate a verified flight to a manual alert with confirmation."""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return

        try:
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            ign = self.ign or (self._extract_field(embed, "IGN") if embed else None) or "Unknown"

            # Show confirmation dialog
            confirm_view = FlagConfirmView(self, ign, interaction.message)
            await interaction.followup.send(
                f"🚩 Are you sure you want to flag **{ign}** for review?",
                view=confirm_view,
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"[FLAG] Error in flag_action: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

class ProfileTimelineView(discord.ui.View):
    def __init__(self, timeline: list, base_embed: discord.Embed, items_per_page: int = 5):
        super().__init__(timeout=180)
        self.timeline = timeline
        self.base_embed = base_embed
        self.items_per_page = items_per_page
        self.current_page = 0
        self.max_pages = max(1, (len(timeline) - 1) // items_per_page + 1)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.current_page <= 0
        self.next_button.disabled = self.current_page >= self.max_pages - 1

    def _build_embed(self) -> discord.Embed:
        embed = self.base_embed.copy()
        
        if not self.timeline:
            embed.add_field(name="Timeline", value="No recent activity.", inline=False)
            return embed

        start_idx = self.current_page * self.items_per_page
        page_items = self.timeline[start_idx : start_idx + self.items_per_page]
        
        for item in page_items:
            t_type = item["type"]
            label = item["label"]
            title = item["title"]
            timestamp = item["timestamp"]
            
            icon = "⚪"
            if item["severity"] == "critical":
                icon = "🔴"
            elif item["severity"] == "warning":
                icon = "🟠"
            elif item["severity"] == "attention":
                icon = "🟡"
            elif item["severity"] == "info":
                icon = "🔵"
                
            if t_type == "visit":
                icon = "✈️" if item["payload"]["authorized"] else "🚨"
            
            val = f"{icon} **{label}**: {title}"
            embed.add_field(name=timestamp, value=val, inline=False)
            
        embed.set_footer(text=f"Page {self.current_page + 1}/{self.max_pages} • Total Events: {len(self.timeline)}")
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

# Compiled once at module level; shared by all flight-monitoring cogs.
JOIN_PATTERN = re.compile(
    r"\[.*?\]\s*.*?\s+(.*?)\s+from\s+(.*?)\s+is joining\s+(.*?)(?:\.|$)",
    re.IGNORECASE
)

MAX_ALERT_MERGE_SECONDS = 1800  # 30 minutes: re-joins within this window update the existing alert

class FlightLoggerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.island_map = {}
        self.all_sub_roles = set()
        self.join_pattern = JOIN_PATTERN
        self.last_processed = None
        # Maps (ign_clean, dest_clean) -> (message_id, alert_timestamp)
        self._pending_alerts: dict[tuple[str, str], tuple[int, int]] = {}
        self._creating_alerts: set[tuple[str, str]] = set()
        self._pending_dodo_requests: dict[int, dict] = {}
        self._chunk_lock = asyncio.Lock()

    def _prune_stale_caches(self):
        """Prune in-memory alert and dodo request caches to prevent unbounded growth over days."""
        now = int(discord.utils.utcnow().timestamp())
        stale_alert_keys = [
            k for k, v in self._pending_alerts.items()
            if (now - (v[1] if isinstance(v, tuple) else now)) > MAX_ALERT_MERGE_SECONDS * 2
        ]
        for k in stale_alert_keys:
            self._pending_alerts.pop(k, None)

        stale_dodo_users = [
            uid for uid, req in self._pending_dodo_requests.items()
            if hasattr(req.get('timestamp'), 'timestamp') and (now - int(req['timestamp'].timestamp())) > 3600
        ]
        for uid in stale_dodo_users:
            self._pending_dodo_requests.pop(uid, None)

        if stale_alert_keys or stale_dodo_users:
            logger.debug(f"[FLIGHT] Pruned {len(stale_alert_keys)} stale alerts, {len(stale_dodo_users)} stale dodo requests.")

    async def _load_sub_roles_from_db(self) -> set[int]:
        """Fallback: derive subscription role IDs from islands.required_roles in SQLite."""
        roles: set[int] = set()
        try:
            async with connect_async_db() as db:
                cur = await db.execute("SELECT required_roles FROM islands")
                rows = await cur.fetchall()
            for (required_roles_raw,) in rows:
                try:
                    parsed = json.loads(required_roles_raw or "[]")
                except Exception:
                    parsed = []
                for rid in parsed or []:
                    if isinstance(rid, int):
                        roles.add(rid)
                    elif isinstance(rid, str) and rid.isdigit():
                        roles.add(int(rid))
        except Exception as exc:
            logger.warning(f"[FLIGHT] Could not load required_roles from DB for fallback: {exc}")
        return roles

    async def _ensure_sub_roles_loaded(self) -> None:
        """Ensure `self.all_sub_roles` is populated, using DB fallback when needed."""
        if self.all_sub_roles:
            return
        fallback = await self._load_sub_roles_from_db()
        if fallback:
            self.all_sub_roles = fallback
            logger.info(f"[FLIGHT] Loaded {len(fallback)} sub roles from DB fallback.")

    async def lookup_dodo_reveal_jump_url(
        self, ign: str, destination: str, max_age_seconds: int = 7200
    ) -> str | None:
        """Match recent dodo webhook message URL by island + IGN (from SQLite, written by Flask API)."""
        dest_clean = clean_text(destination)
        if not dest_clean:
            return None
        cutoff = int(discord.utils.utcnow().timestamp()) - max_age_seconds
        try:
            async with connect_async_db() as db:
                cur = await db.execute(
                    """
                    SELECT message_url, username, nickname FROM dodo_reveal_messages
                    WHERE island_clean = ? AND created_at >= ?
                    ORDER BY created_at DESC
                    LIMIT 40
                    """,
                    (dest_clean, cutoff),
                )
                rows = await cur.fetchall()
        except Exception as exc:
            logger.warning(f"[FLIGHT] dodo_reveal_messages lookup failed: {exc}")
            return None
        for message_url, username, nickname in rows:
            if ign_matches_dodo_reveal(ign, username, nickname):
                return message_url
        return None

    def _get_db(self):
        """Return an async database connection context manager."""
        return connect_async_db()

    async def _resolve_member(self, guild: discord.Guild | None, user_id: int) -> discord.Member | None:
        """Resolve a member by ID using cache first, then fetch if missing."""
        if not guild:
            return None
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            logger.debug(f"[FLIGHT] Could not fetch member {user_id} from guild {getattr(guild, 'id', None)}")
            return None

    async def add_warning(self, user_id, guild_id, reason, mod_id, visit_id=None, action_type='WARN'):
        async with connect_async_db() as db:
            await db.execute(
                "INSERT INTO warnings (user_id, guild_id, reason, mod_id, timestamp, visit_id, action_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, guild_id, reason, mod_id, int(discord.utils.utcnow().timestamp()), visit_id, action_type)
            )

    async def get_warn_count(self, user_id: int, guild_id: int, days: int = WARN_EXPIRY_DAYS):
        cutoff = int((discord.utils.utcnow() - datetime.timedelta(days=days)).timestamp())
        async with connect_async_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM warnings WHERE user_id = ? AND guild_id = ? AND timestamp > ? AND action_type = 'WARN'",
                (user_id, guild_id, cutoff)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def remove_latest_warning(self, user_id: int, guild_id: int):
        """Remove the most recent warning for a user and return its details."""
        async with connect_async_db() as db:
            cursor = await db.execute(
                "SELECT rowid, reason, mod_id, timestamp FROM warnings WHERE user_id = ? AND guild_id = ? ORDER BY timestamp DESC LIMIT 1",
                (user_id, guild_id)
            )
            row = await cursor.fetchone()
            if row:
                rowid, reason, mod_id, timestamp = row
                await db.execute("DELETE FROM warnings WHERE rowid = ?", (rowid,))
                return {"reason": reason, "mod_id": mod_id, "timestamp": timestamp}
        return None

    async def remove_all_warnings(self, user_id: int, guild_id: int):
        """Remove all warnings for a user and return the count removed."""
        async with connect_async_db() as db:
            cursor = await db.execute(
                "DELETE FROM warnings WHERE user_id = ? AND guild_id = ?",
                (user_id, guild_id)
            )
            return cursor.rowcount

    async def get_warnings(self, user_id: int, guild_id: int, days: int = 30):
        """Get all warnings for a user within the specified number of days, including any linked island visit."""
        cutoff = int((discord.utils.utcnow() - datetime.timedelta(days=days)).timestamp())
        async with connect_async_db() as db:
            cursor = await db.execute(
                """SELECT w.reason, w.mod_id, w.timestamp, w.visit_id,
                          iv.ign, iv.origin_island, iv.destination, iv.timestamp
                   FROM warnings w
                   LEFT JOIN island_visits iv ON w.visit_id = iv.id
                   WHERE w.user_id = ? AND w.guild_id = ? AND w.timestamp > ? AND w.action_type = 'WARN'
                   ORDER BY w.timestamp DESC""",
                (user_id, guild_id, cutoff)
            )
            rows = await cursor.fetchall()
            return [
                {
                    "reason": r[0], "mod_id": r[1], "timestamp": r[2], "visit_id": r[3],
                    "visit_ign": r[4], "visit_origin": r[5], "visit_destination": r[6], "visit_ts": r[7],
                }
                for r in rows
            ]

    async def _get_recent_visit_id_by_ign(self, ign: str, hours: int = 24) -> int | None:
        """Find the most recent island_visits.id for the given IGN within the last N hours."""
        cutoff = int((discord.utils.utcnow() - datetime.timedelta(hours=hours)).timestamp())
        async with connect_async_db() as db:
            cursor = await db.execute(
                "SELECT id FROM island_visits WHERE ign = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT 1",
                (ign, cutoff)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    def register_dodo_request(self, user_id: int, member: discord.Member, channel: discord.abc.GuildChannel, reply_msg: discord.Message | None, guild_icon: str | None) -> None:
        """Register a pending dodo-code request so it can be merged with the verified-flight xlog entry."""
        self._pending_dodo_requests[user_id] = {
            'member': member,
            'channel': channel,
            'reply_msg': reply_msg,
            'guild_icon': guild_icon,
            'created_at': discord.utils.utcnow(),
        }

    def pop_pending_dodo_request(self, user_id: int) -> dict | None:
        """Remove and return the pending dodo-request info for the given user, or None if not found."""
        entry = self._pending_dodo_requests.pop(user_id, None)
        if not entry:
            return None
        created = entry.get('created_at')
        if not created:
            return entry
        try:
            age = (discord.utils.utcnow() - created).total_seconds()
        except Exception:
            return entry
        # Expire stale requests older than 10 minutes
        if age > 600:
            logger.debug(f"[FLIGHT] Dropping stale dodo request for user_id={user_id} (age={age}s)")
            return None
        return entry

    async def get_recent_visit_id_by_user(self, user_id: int, guild_id: int, hours: int = 6) -> int | None:
        """Find the most recent island_visits.id for the given Discord user within the last N hours."""
        cutoff = int((discord.utils.utcnow() - datetime.timedelta(hours=hours)).timestamp())
        async with connect_async_db() as db:
            cursor = await db.execute(
                "SELECT id FROM island_visits WHERE user_id = ? AND guild_id = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT 1",
                (user_id, guild_id, cutoff)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def _get_recent_authorized_target(self, ign: str, hours: int = 24, guild_id: int | None = None) -> dict | None:
        """Return the recent authorized visit for this IGN when it has a linked target user."""
        cutoff = int((discord.utils.utcnow() - datetime.timedelta(hours=hours)).timestamp())
        guild_clause = "AND guild_id = ?" if guild_id is not None else ""
        params = [ign, cutoff]
        if guild_id is not None:
            params.append(guild_id)
        async with connect_async_db() as db:
            cursor = await db.execute(
                f"""SELECT id, user_id, guild_id, destination, timestamp
                   FROM island_visits
                   WHERE ign = ? AND timestamp > ? AND authorized = 1 AND user_id IS NOT NULL {guild_clause}
                   ORDER BY timestamp DESC LIMIT 1""",
                params,
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "user_id": row[1],
                "guild_id": row[2],
                "destination": row[3],
                "timestamp": row[4],
            }

    async def _is_authorized_with_target(self, ign: str, hours: int = 24, guild_id: int | None = None) -> bool:
        """Return True if this IGN has a recent visit that was authorized AND has a linked target user.

        If `guild_id` is provided, restrict the search to that guild to avoid cross-guild matches.
        """
        return await self._get_recent_authorized_target(ign, hours, guild_id=guild_id) is not None

    async def _get_actionable_identity_events(
        self,
        user_id: int,
        guild_id: int | None,
        ign: str,
    ) -> tuple[list[dict], dict | None]:
        """Return recent identity events that happened after the latest linked authorization."""
        recent_events = await self.get_recent_identity_events(user_id, guild_id)
        authorized_target = await self._get_recent_authorized_target(ign, guild_id=guild_id)
        if authorized_target and int(authorized_target["user_id"]) == int(user_id):
            recent_events = filter_identity_events_after_authorization(
                recent_events,
                authorized_target.get("timestamp"),
            )
        return recent_events, authorized_target

    async def record_member_identity_event(
        self,
        member: discord.Member,
        event_type: str,
        old_display_name: str | None,
        new_display_name: str | None,
        created_at: int | None = None,
    ) -> None:
        """Persist recent nickname/join activity used by flight-log triage."""
        if event_type not in {IDENTITY_EVENT_NICKNAME_CHANGE, IDENTITY_EVENT_MEMBER_JOIN}:
            return
        if getattr(member, "bot", False):
            return

        guild_id = member.guild.id if getattr(member, "guild", None) else None
        ts = created_at or int(discord.utils.utcnow().timestamp())
        async with connect_async_db() as db:
            await db.execute(
                """
                INSERT INTO member_identity_events
                    (user_id, guild_id, event_type, old_display_name, new_display_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (member.id, guild_id, event_type, old_display_name, new_display_name, ts),
            )

    async def get_recent_identity_events(
        self,
        user_id: int,
        guild_id: int | None,
        window_seconds: int = RECENT_IDENTITY_WINDOW_SECONDS,
    ) -> list[dict]:
        """Return recent nickname/join events for a member, newest first."""
        cutoff = int(discord.utils.utcnow().timestamp()) - window_seconds
        async with connect_async_db() as db:
            if guild_id is None:
                cursor = await db.execute(
                    """
                    SELECT event_type, old_display_name, new_display_name, created_at
                    FROM member_identity_events
                    WHERE user_id = ? AND created_at >= ?
                    ORDER BY created_at DESC
                    LIMIT 10
                    """,
                    (user_id, cutoff),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT event_type, old_display_name, new_display_name, created_at
                    FROM member_identity_events
                    WHERE user_id = ? AND guild_id = ? AND created_at >= ?
                    ORDER BY created_at DESC
                    LIMIT 10
                    """,
                    (user_id, guild_id, cutoff),
                )
            rows = await cursor.fetchall()
            return [
                {
                    "event_type": row[0],
                    "old_display_name": row[1],
                    "new_display_name": row[2],
                    "created_at": row[3],
                }
                for row in rows
            ]

    async def record_authorized_followup_visit(self, ign: str, origin_island: str, destination: str, user_id: int, guild_id: int | None, timestamp: int, island_type: str = 'sub') -> int | None:
        """Record an authorized follow-up visit linked to a previously identified traveler."""
        async with connect_async_db() as db:
            cursor = await db.execute(
                "INSERT INTO island_visits (ign, origin_island, destination, user_id, guild_id, authorized, timestamp, island_type) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (ign, origin_island, destination, user_id, guild_id, timestamp, island_type)
            )
            return cursor.lastrowid

    async def get_island_visits(self, user_id: int, guild_id: int, days: int = 30):
        """Get all island visits for a user within the specified number of days."""
        cutoff = int((discord.utils.utcnow() - datetime.timedelta(days=days)).timestamp())
        async with connect_async_db() as db:
            cursor = await db.execute(
                """SELECT id, ign, origin_island, destination, authorized, timestamp
                   FROM island_visits
                   WHERE user_id = ? AND guild_id = ? AND timestamp > ?
                   ORDER BY timestamp DESC""",
                (user_id, guild_id, cutoff)
            )
            rows = await cursor.fetchall()
            return [
                {"id": r[0], "ign": r[1], "origin_island": r[2], "destination": r[3],
                 "authorized": bool(r[4]), "timestamp": r[5]}
                for r in rows
            ]

    async def record_island_visit(self, ign: str, origin_island: str, destination: str, found_members: list[discord.Member], guild_id: int | None, timestamp: int, authorized: int | None = None, island_type: str = 'sub') -> int | None:
        """Record an island visit (authorized or unauthorized) in the database. Returns the visit ID."""
        async with connect_async_db() as db:
            visit_id = None
            if found_members:
                for member in found_members:
                    cursor = await db.execute(
                        "INSERT INTO island_visits (ign, origin_island, destination, user_id, guild_id, authorized, timestamp, island_type) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                        (ign, origin_island, destination, member.id, guild_id, timestamp, island_type)
                    )
                    visit_id = cursor.lastrowid
            else:
                auth_val = authorized if authorized is not None else 0
                cursor = await db.execute(
                    "INSERT INTO island_visits (ign, origin_island, destination, user_id, guild_id, authorized, timestamp, island_type) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
                    (ign, origin_island, destination, guild_id, auth_val, timestamp, island_type)
                )
                visit_id = cursor.lastrowid
            return visit_id

    async def cleanup_expired_warnings(self):
        """Delete warnings older than WARN_EXPIRY_DAYS from the database."""
        cutoff = int((discord.utils.utcnow() - datetime.timedelta(days=WARN_EXPIRY_DAYS)).timestamp())
        async with connect_async_db() as db:
            cursor = await db.execute(
                "DELETE FROM warnings WHERE timestamp < ?", (cutoff,)
            )
            count = cursor.rowcount
            if count > 0:
                logger.info(f"[FLIGHT] Expired {count} warning(s) older than {WARN_EXPIRY_DAYS} days.")
            return count

    async def _execute_punishment_internal(self, interaction, target, action_type, reason_text, duration_str, original_view, log_message):
        """Unified internal method for handling moderation actions."""
        mod = interaction.user
        guild = interaction.guild
        
        # 1. Determine action details
        if action_type == "BAN":
            final_duration = "Permanent"
            action_verb = "BANNED"
            color = COLOR_BAN
        elif action_type == "KICK":
            final_duration = "N/A"
            action_verb = "KICKED"
            color = COLOR_KICK
        else: # WARN
            final_duration = duration_str
            action_verb = "WARNED"
            color = COLOR_WARN

        # Generate unique case ID: FL- YYMM-RAND
        now = discord.utils.utcnow()
        case_id = f"FL-{now.strftime('%y%m')}-{hex(int(now.timestamp()))[2:][-4:].upper()}"

        # 1.5 Role Removal (Warn Only)
        if action_type == "WARN":
            visitor_role = guild.get_role(Config.ISLAND_ACCESS_ROLE)
            if visitor_role and visitor_role in target.roles:
                try:
                    await target.remove_roles(visitor_role, reason=f"FlightLog [{case_id}]: Warned - Role Removed")
                    logger.info(f"[FLIGHT] Removed role {visitor_role.name} from {target.display_name}")
                except discord.Forbidden:
                    logger.error(f"[FLIGHT] Permission Denied: Cannot remove role from {target.display_name}")
                except Exception as e:
                    logger.error(f"[FLIGHT] Error removing role: {e}", exc_info=True)

        try:
            # 2. DM Notification
            try:
                emoji = ""
                if action_type == "BAN": emoji = "<:Cho_Ban:1473530840725061793> "
                elif action_type == "KICK": emoji = "<:Cho_Kick:1456714701630214349> "
                elif action_type == "WARN": emoji = "<:Cho_Warn:1456712416271405188> "

                dm_embed = discord.Embed(
                    title=f"{emoji} Chobot Notification",
                    description=f"You have been **{action_verb.lower()}** from **{guild.name}**.",
                    color=color,
                    timestamp=discord.utils.utcnow()
                )
                dm_embed.add_field(name="Reason", value=reason_text, inline=False)
                dm_embed.set_footer(text=f"Case ID: {case_id}")
                if guild.icon:
                    dm_embed.set_thumbnail(url=guild.icon.url)

                await target.send(embed=dm_embed)
            except discord.HTTPException:
                pass # DM Closed

            # 3. Discord Action
            if action_type == "KICK":
                await target.kick(reason=f"FlightLog [{case_id}]: {reason_text}")
            elif action_type == "BAN":
                await target.ban(reason=f"FlightLog [{case_id}]: {reason_text}")

            # 4. Database Log — link to the island visit that triggered this alert if available
            visit_id = getattr(original_view, 'visit_id', None) if original_view else None
            if visit_id is None and original_view is not None:
                # Fall back to IGN-based lookup (handles bot-restart case where visit_id wasn't in view)
                ign = getattr(original_view, 'ign', None)
                if not ign and log_message and log_message.embeds:
                    for field in log_message.embeds[0].fields:
                        if "Traveler (IGN)" in field.name:
                            m = re.search(r"```(?:yaml)?\n(.*?)\n?```", field.value)
                            if m:
                                ign = m.group(1).strip()
                            break
                if ign:
                    visit_id = await self._get_recent_visit_id_by_ign(ign)
            if visit_id is not None:
                # Identify the visitor in the island_visits record now that we know who they are
                async with connect_async_db() as db:
                    await db.execute(
                        "UPDATE island_visits SET user_id = ? WHERE id = ? AND user_id IS NULL",
                        (target.id, visit_id)
                    )
            await self.add_warning(target.id, guild.id, reason_text, mod.id, visit_id, action_type=action_type)
            # Use small delay to ensure DB consistency (though commit is awaited)
            new_count = await self.get_warn_count(target.id, guild.id, days=WARN_EXPIRY_DAYS)

            # 5. Log to Sapphire Channel
            log_embed = create_sapphire_log(target, mod, reason_text, case_id, new_count, final_duration, action_verb)
            sub_mod_channel = guild.get_channel(Config.SUB_MOD_CHANNEL_ID)
            
            if sub_mod_channel:
                sent_log = await sub_mod_channel.send(content=target.mention, embed=log_embed)
                await interaction.followup.send(f"✅ Case `{case_id}` logged in {sub_mod_channel.mention}", ephemeral=True)
            else:
                sent_log = None
                await interaction.followup.send(f"✅ Action executed (Case `{case_id}`), but log channel is missing.", ephemeral=True)

            # 6. Update Original Alert
            msg_to_mod = f"✅ **{target.display_name}** processed ({action_verb}). Case: `{case_id}`"
            if original_view:
                await original_view._resolve_alert(
                    interaction, action_verb, color, msg_to_mod,
                    target_user=target, log_message=log_message, reason=reason_text,
                    mod_log_url=sent_log.jump_url if sent_log else None
                )

            # The R1 Notification Reminder is now handled by the persistent background task check_r1_reminders_task

        except discord.Forbidden:
            await interaction.followup.send("Permission Denied. Check bot role hierarchy.", ephemeral=True)
        except Exception as e:
            logger.error(f"Punishment Error: {e}", exc_info=True)
            await interaction.followup.send(f"System Error: {e}", ephemeral=True)

    async def cog_load(self):
        await init_db()
        self.bot.add_view(TravelerActionView(bot=self.bot))
        self.bot.add_view(VerifiedFlightFlagView(bot=self.bot))
        if not self.fetch_islands_task.is_running():
            self.fetch_islands_task.start()
        if not self.cleanup_warnings_task.is_running():
            self.cleanup_warnings_task.start()
        if not self.check_r1_reminders_task.is_running():
            self.check_r1_reminders_task.start()

    async def _trigger_automatic_flag(
        self,
        ign,
        island,
        destination,
        member,
        identities,
        subs,
        allowed_identities,
        message_url,
        message_content,
        timestamp,
        island_type='sub',
        recent_identity_events=None,
    ):
        """Automatically create a manual alert and an xlog entry for suspicious flights."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if guild is None:
            logger.error(f"[FLIGHT] Guild {Config.GUILD_ID} unavailable, skipping flight for {ign}")
            return
        guild_id = guild.id if guild else None
        guild_icon = guild.icon.url if guild and guild.icon else None
        identity_summary, identity_reasons = summarize_recent_identity_events(recent_identity_events or [])

        # Ensure we have up-to-date roles (member cache can be stale/partial).
        try:
            if guild:
                member = await guild.fetch_member(member.id)
        except Exception:
            pass

        # Derive sub roles using the same source of truth as verbose xlog.
        member_sub_roles = [r for r in member.roles if r.id in self.all_sub_roles]
        sub_role_mentions = [r.mention for r in member_sub_roles]
        
        # 1. Record Visit (linked to member)
        visit_id = await self.record_island_visit(ign, island, destination, [member], guild_id, int(timestamp.timestamp()), island_type=island_type)
        
        # 2. Create Alert in Flight Log Channel
        output_channel = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)
        alert_msg = None
        dodo_req = None
        if output_channel:
            alert_embed = discord.Embed(
                description=(
                    f"### <a:CampWarning:1172346431542140961> Mismatch Detected\n"
                    f"Member {_format_user_for_embed(member)} matched, but their nickname has more identities than their allowed count.\n"
                    f"**Identities:** {identities} | **Subs:** {subs} | **Allowed:** {allowed_identities}\n"
                    f"**Sub Roles:** {' / '.join(sub_role_mentions) if sub_role_mentions else 'None detected'}\n"
                    f"Use the buttons below to take action."
                ),
                color=COLOR_INVESTIGATION,
                timestamp=timestamp,
            )
            alert_embed.add_field(name="Traveler (IGN)", value=f"```yaml\n{ign}```", inline=True)
            alert_embed.add_field(name="Origin Island", value=f"```yaml\n{island.title()}```", inline=True)
            alert_embed.add_field(name="Destination", value=destination or "Unknown", inline=True)
            if identity_summary:
                alert_embed.add_field(name="Recent Identity Activity", value=identity_summary, inline=False)
            if visit_id:
                alert_embed.add_field(name="Visit ID", value=f"`#{visit_id}`", inline=True)
            alert_embed.set_image(url=Config.FOOTER_LINE)
            alert_embed.set_footer(text="Chopaeng Camp™ • Flight Logger", icon_url=guild_icon)
            
            action_view = TravelerActionView(self.bot, ign, visit_id=visit_id)
            # Merge any pending dodo-code request from this member into the alert
            dodo_req = self.pop_pending_dodo_request(member.id)
            if dodo_req is not None:
                alert_embed.add_field(name="Dodo Requested", value=dodo_req['channel'].mention, inline=True)

            alert_msg = await output_channel.send(embed=alert_embed, view=action_view)
            
            # Record in DB
            await self.add_warning(
                member.id,
                guild_id,
                (
                    f"Auto-flagged: Nickname identity mismatch ({identities} vs allowed {allowed_identities}; subs {subs})"
                    + (f"; {', '.join(identity_reasons)}" if identity_reasons else "")
                ),
                self.bot.user.id,
                visit_id,
                action_type='FLAG',
            )

        # 3. Create "Flagged" Log in XLOG Channel
        xlog_channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
        if xlog_channel:
            # Helpful context inline for quick mod triage.
            member_sub_roles = [r for r in member.roles if r.id in self.all_sub_roles]
            sub_role_mentions = [r.mention for r in member_sub_roles]
            xlog_desc = (
                f"**{_format_user_for_embed(member)}**\n"
                f"**Auto-Flag:** Nickname identities exceed allowed count.\n"
                f"**Identities:** {identities}  |  **Subs:** {subs}  |  **Allowed:** {allowed_identities}\n"
                f"**Sub Roles:** {(' / '.join(sub_role_mentions)) if sub_role_mentions else 'None detected'}"
            )
            if identity_reasons:
                xlog_desc += f"\n**Also Flagged:** {', '.join(identity_reasons)}"
            
            xlog_embed = discord.Embed(
                title="🚩 Flagged Flight",
                description=xlog_desc,
                color=COLOR_INVESTIGATION,
                timestamp=timestamp,
            )
            xlog_embed.add_field(name="IGN", value=f"```yaml\n{ign}```", inline=True)
            xlog_embed.add_field(name="Island Name", value=f"```yaml\n{island.title()}```", inline=True)
            xlog_embed.add_field(name="Destination", value=self.get_island_channel_link(destination), inline=True)
            if identity_summary:
                xlog_embed.add_field(name="Recent Identity Activity", value=identity_summary, inline=False)
            
            xlog_embed.set_image(url=Config.FOOTER_LINE)
            xlog_embed.set_footer(text="Chopaeng Camp™ • Match Log", icon_url=guild_icon)

            xlog_view = discord.ui.View()
            if message_url:
                xlog_view.add_item(discord.ui.Button(label="View Flight Log", url=message_url, style=discord.ButtonStyle.link))
            # If we popped a dodo request earlier, add a link to it on the xlog view
            if dodo_req is not None and dodo_req.get('reply_msg'):
                xlog_view.add_item(discord.ui.Button(label="View Dodo Request", url=dodo_req['reply_msg'].jump_url, style=discord.ButtonStyle.link))
            if alert_msg:
                xlog_view.add_item(discord.ui.Button(label="View Alert", url=alert_msg.jump_url, style=discord.ButtonStyle.link))

            await xlog_channel.send(embed=xlog_embed, view=xlog_view)

    async def _trigger_recent_identity_flag(
        self,
        ign,
        island,
        destination,
        member,
        recent_identity_events,
        visit_id,
        message_url,
        timestamp,
    ):
        """Create a manual alert for matched members with recent nickname/join activity."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if guild is None:
            logger.error(f"[FLIGHT] Guild {Config.GUILD_ID} unavailable, aborting recent-identity flag for {ign}")
            return
        guild_id = guild.id
        guild_icon = guild.icon.url if guild.icon else None
        destination_link = self.get_island_channel_link(destination)
        identity_summary, identity_reasons = summarize_recent_identity_events(recent_identity_events)
        reason_text = ", ".join(identity_reasons) if identity_reasons else "Recent identity activity"

        output_channel = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)
        alert_msg = None
        dodo_req = None
        if output_channel:
            alert_embed = discord.Embed(
                description=(
                    f"### <a:CampWarning:1172346431542140961> Recent Identity Activity\n"
                    f"Member {_format_user_for_embed(member)} matched this flight for **{destination_link}**, "
                    f"but they have recent nickname/join activity.\n"
                    f"**Reason:** {reason_text}\n"
                    f"Use the buttons below to take action."
                ),
                color=COLOR_INVESTIGATION,
                timestamp=timestamp,
            )
            alert_embed.add_field(name="Traveler (IGN)", value=f"```yaml\n{ign}```", inline=True)
            alert_embed.add_field(name="Origin Island", value=f"```yaml\n{island.title()}```", inline=True)
            alert_embed.add_field(name="Destination", value=destination_link, inline=True)
            if identity_summary:
                alert_embed.add_field(name="Recent Identity Activity", value=identity_summary, inline=False)
            if visit_id:
                alert_embed.add_field(name="Visit ID", value=f"`#{visit_id}`", inline=True)
            # Merge any pending dodo-code request from this member into the alert
            dodo_req = self.pop_pending_dodo_request(member.id)
            if dodo_req is not None:
                alert_embed.add_field(name="Dodo Requested", value=dodo_req['channel'].mention, inline=True)
            alert_embed.set_image(url=Config.FOOTER_LINE)
            alert_embed.set_footer(text="Chopaeng Camp - Flight Logger", icon_url=guild_icon)

            action_view = TravelerActionView(self.bot, ign, visit_id=visit_id)
            alert_msg = await output_channel.send(embed=alert_embed, view=action_view)

            await self.add_warning(
                member.id,
                guild_id,
                f"Auto-flagged: {reason_text}",
                self.bot.user.id,
                visit_id,
                action_type='FLAG',
            )

        xlog_channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
        if xlog_channel:
            xlog_embed = discord.Embed(
                title="Flagged Flight",
                description=(
                    f"**{_format_user_for_embed(member)}**\n"
                    f"**Auto-Flag:** {reason_text}"
                ),
                color=COLOR_INVESTIGATION,
                timestamp=timestamp,
            )
            xlog_embed.add_field(name="IGN", value=f"```yaml\n{ign}```", inline=True)
            xlog_embed.add_field(name="Island Name", value=f"```yaml\n{island.title()}```", inline=True)
            xlog_embed.add_field(name="Destination", value=destination_link, inline=True)
            if identity_summary:
                xlog_embed.add_field(name="Recent Identity Activity", value=identity_summary, inline=False)
            # If we popped a dodo request earlier, show it on the xlog embed
            if dodo_req is not None:
                xlog_embed.add_field(name="Dodo Requested", value=dodo_req['channel'].mention, inline=True)
            if visit_id:
                xlog_embed.add_field(name="Visit ID", value=f"`#{visit_id}`", inline=True)
            xlog_embed.set_image(url=Config.FOOTER_LINE)
            xlog_embed.set_footer(text="Chopaeng Camp - Match Log", icon_url=guild_icon)

            xlog_view = discord.ui.View()
            if message_url:
                xlog_view.add_item(discord.ui.Button(label="View Flight Log", url=message_url, style=discord.ButtonStyle.link))
            if dodo_req is not None and dodo_req.get('reply_msg'):
                xlog_view.add_item(discord.ui.Button(label="View Dodo Request", url=dodo_req['reply_msg'].jump_url, style=discord.ButtonStyle.link))
            if alert_msg:
                xlog_view.add_item(discord.ui.Button(label="View Alert", url=alert_msg.jump_url, style=discord.ButtonStyle.link))

            await xlog_channel.send(embed=xlog_embed, view=xlog_view)

    async def _trigger_island_access_mismatch_flag(
        self,
        ign,
        island,
        destination,
        member,
        visit_id,
        required_roles,
        member_sub_roles,
        recent_identity_events,
        message_url,
        timestamp,
    ):
        """Create a manual alert when a member joins an island they are not subscribed to."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if guild is None:
            logger.error(f"[FLIGHT] Guild {Config.GUILD_ID} unavailable, aborting island-access mismatch flag for {ign}")
            return
        guild_id = guild.id
        guild_icon = guild.icon.url if guild.icon else None
        destination_link = self.get_island_channel_link(destination)
        identity_summary, identity_reasons = summarize_recent_identity_events(recent_identity_events or [])
        member_sub_text = " / ".join(r.mention for r in member_sub_roles) if member_sub_roles else "None detected"
        reason_text = "No subscription role for destination island"

        output_channel = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)
        alert_msg = None
        dodo_req = None
        if output_channel:
            alert_embed = discord.Embed(
                description=(
                    "### <a:CampWarning:1172346431542140961> Island Access Mismatch\n"
                    f"Member {_format_user_for_embed(member)} matched this flight for **{destination_link}**, "
                    "but they do not have a subscription role for that island.\n"
                    f"**Member Subscription(s):** {member_sub_text}\n"
                    "Use the buttons below to take action."
                ),
                color=COLOR_INVESTIGATION,
                timestamp=timestamp,
            )
            alert_embed.add_field(name="Traveler (IGN)", value=f"```yaml\n{ign}```", inline=True)
            alert_embed.add_field(name="Origin Island", value=f"```yaml\n{island.title()}```", inline=True)
            alert_embed.add_field(name="Destination", value=destination_link, inline=True)
            if identity_summary:
                alert_embed.add_field(name="Recent Identity Activity", value=identity_summary, inline=False)
            if visit_id:
                alert_embed.add_field(name="Visit ID", value=f"`#{visit_id}`", inline=True)
            # Merge any pending dodo-code request from this member into the alert
            dodo_req = self.pop_pending_dodo_request(member.id)
            if dodo_req is not None:
                alert_embed.add_field(name="Dodo Requested", value=dodo_req['channel'].mention, inline=True)
            alert_embed.set_image(url=Config.FOOTER_LINE)
            alert_embed.set_footer(text="Chopaeng Camp - Flight Logger", icon_url=guild_icon)

            action_view = TravelerActionView(self.bot, ign, visit_id=visit_id)
            alert_msg = await output_channel.send(embed=alert_embed, view=action_view)

            await self.add_warning(
                member.id,
                guild_id,
                (
                    f"Auto-flagged: {reason_text}; member subs: "
                    f"{', '.join(r.name for r in member_sub_roles) if member_sub_roles else 'none'}; "
                    f"required: {', '.join(r.name for r in required_roles) if required_roles else 'unknown'}"
                    + (f"; {', '.join(identity_reasons)}" if identity_reasons else "")
                ),
                self.bot.user.id,
                visit_id,
                action_type='FLAG',
            )

        xlog_channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
        if xlog_channel:
            xlog_desc = (
                f"**{_format_user_for_embed(member)}**\n"
                f"**Auto-Flag:** {reason_text}\n"
                f"**Member Subscription(s):** {member_sub_text}\n"
            )
            if identity_reasons:
                xlog_desc += f"\n**Also Flagged:** {', '.join(identity_reasons)}"

            xlog_embed = discord.Embed(
                title="Flagged Flight",
                description=xlog_desc,
                color=COLOR_INVESTIGATION,
                timestamp=timestamp,
            )
            xlog_embed.add_field(name="IGN", value=f"```yaml\n{ign}```", inline=True)
            xlog_embed.add_field(name="Island Name", value=f"```yaml\n{island.title()}```", inline=True)
            xlog_embed.add_field(name="Destination", value=destination_link, inline=True)
            if identity_summary:
                xlog_embed.add_field(name="Recent Identity Activity", value=identity_summary, inline=False)
            if visit_id:
                xlog_embed.add_field(name="Visit ID", value=f"`#{visit_id}`", inline=True)
            xlog_embed.set_image(url=Config.FOOTER_LINE)
            xlog_embed.set_footer(text="Chopaeng Camp - Match Log", icon_url=guild_icon)

            xlog_view = discord.ui.View()
            if message_url:
                xlog_view.add_item(discord.ui.Button(label="View Flight Log", url=message_url, style=discord.ButtonStyle.link))
            if dodo_req is not None and dodo_req.get('reply_msg'):
                xlog_view.add_item(discord.ui.Button(label="View Dodo Request", url=dodo_req['reply_msg'].jump_url, style=discord.ButtonStyle.link))
            if alert_msg:
                xlog_view.add_item(discord.ui.Button(label="View Alert", url=alert_msg.jump_url, style=discord.ButtonStyle.link))

            await xlog_channel.send(embed=xlog_embed, view=xlog_view)

    async def _safe_wait_until_ready(self):
        """Wait until bot cache and connection are fully initialized without raising RuntimeError."""
        while not self.bot.is_ready():
            await asyncio.sleep(0.5)

    def cog_unload(self):
        self.fetch_islands_task.cancel()
        self.cleanup_warnings_task.cancel()
        self.check_r1_reminders_task.cancel()

    @tasks.loop(hours=1)
    async def fetch_islands_task(self):
        try:
            await self.fetch_islands()
        except Exception as exc:
            logger.error(f"[FLIGHT] Error in fetch_islands_task: {exc}", exc_info=True)

    @fetch_islands_task.before_loop
    async def before_fetch(self):
        await self._safe_wait_until_ready()
        try:
            await self.fetch_islands()
        except Exception as exc:
            logger.error(f"[FLIGHT] Error in initial fetch_islands: {exc}", exc_info=True)

    @fetch_islands_task.error
    async def fetch_islands_task_error(self, error):
        logger.error(f"[FLIGHT] Unhandled exception in fetch_islands_task: {error}", exc_info=True)

    @tasks.loop(hours=6)
    async def cleanup_warnings_task(self):
        """Periodically remove warnings older than WARN_EXPIRY_DAYS and prune in-memory caches."""
        try:
            await self.cleanup_expired_warnings()
            self._prune_stale_caches()
        except Exception as exc:
            logger.error(f"[FLIGHT] Error in cleanup_warnings_task: {exc}", exc_info=True)

    @cleanup_warnings_task.before_loop
    async def before_cleanup(self):
        await self._safe_wait_until_ready()

    @cleanup_warnings_task.error
    async def cleanup_warnings_task_error(self, error):
        logger.error(f"[FLIGHT] Unhandled exception in cleanup_warnings_task: {error}", exc_info=True)

    @tasks.loop(minutes=5)
    async def check_r1_reminders_task(self):
        """Periodically check for R1 warnings that have hit the 24 hour mark."""
        try:
            cutoff_max = int((discord.utils.utcnow() - datetime.timedelta(hours=23.95)).timestamp())
            cutoff_min = int((discord.utils.utcnow() - datetime.timedelta(hours=48)).timestamp())

            async with connect_async_db() as db:
                cursor = await db.execute(
                    """SELECT id, user_id, guild_id, timestamp, visit_id, reason 
                       FROM warnings 
                       WHERE action_type = 'WARN' 
                         AND (reason LIKE '%Sub Top Rule%' OR reason LIKE '%Sub Rule #1%')
                         AND r1_reminder_sent = 0
                         AND timestamp >= ? AND timestamp <= ?""",
                    (cutoff_min, cutoff_max)
                )
                rows = await cursor.fetchall()
                for row in rows:
                    warning_id, user_id, guild_id, timestamp, visit_id, reason = row
                    guild = self.bot.get_guild(guild_id)
                    if not guild:
                        continue

                    member = guild.get_member(user_id)
                    if not member:
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.HTTPException:
                            member = None
                    if not member:
                        # Can't find member, mark as sent anyway
                        await db.execute("UPDATE warnings SET r1_reminder_sent = 1 WHERE id = ?", (warning_id,))
                        continue

                    x_channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
                    if x_channel:
                        now = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
                        case_val = f"FL-{now.strftime('%y%m')}-{hex(int(now.timestamp()))[2:][-4:].upper()}"

                        desc = (
                            f"**{_format_user_for_embed(member)}** was warned for {reason} 24 hours ago (Case `{case_val}`).\n\n"
                            f"**Action Required:** If they have not contacted the mod team regarding their warning, they are due for a ban at the 24-hour mark."
                        )

                        embed = discord.Embed(
                            title="⏰ R1 Ban Deadline Approaching",
                            description=desc,
                            color=COLOR_ALERT,
                            timestamp=discord.utils.utcnow()
                        )

                        embed.add_field(name="Traveler (IGN)", value=f"```yaml\n{member.display_name}```", inline=True)
                        embed.add_field(name="Status", value="<:Cho_Investigate:1474310726381338666> **PENDING REVIEW**", inline=True)
                        if visit_id is not None:
                            embed.add_field(name="Visit ID", value=f"`#{visit_id}`", inline=True)

                        view = TravelerActionView(bot=self.bot, ign=member.display_name, visit_id=visit_id)
                        for item in list(view.children):
                            if getattr(item, 'custom_id', None) not in ['fl_ban', 'fl_dismiss']:
                                view.remove_item(item)

                        await x_channel.send(embed=embed, view=view)

                    await db.execute("UPDATE warnings SET r1_reminder_sent = 1 WHERE id = ?", (warning_id,))
        except Exception as exc:
            logger.error(f"[FLIGHT] Error in check_r1_reminders_task: {exc}", exc_info=True)

    @check_r1_reminders_task.before_loop
    async def before_check_r1_reminders(self):
        await self._safe_wait_until_ready()

    @check_r1_reminders_task.error
    async def check_r1_reminders_task_error(self, error):
        logger.error(f"[FLIGHT] Unhandled exception in check_r1_reminders_task: {error}", exc_info=True)

    async def fetch_islands(self):
        """Fetch island channels from Discord sub-category"""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            logger.error(f"[FLIGHT] Guild {Config.GUILD_ID} not found.")
            return

        category = discord.utils.get(guild.categories, id=Config.CATEGORY_ID)
        if not category:
            logger.error(f"[FLIGHT] Category {Config.CATEGORY_ID} not found. Falling back to DB-derived subscription roles only.")
            await self._ensure_sub_roles_loaded()
            return

        temp_map = {}
        sub_roles = set()
        
        # Exclude common non-subscription roles
        excluded_roles = {
            Config.ADMIN_ROLE_ID, Config.SENIOR_MOD_ROLE_ID, Config.BABY_MOD_ROLE_ID, 
            Config.ISLAND_BOT_ROLE_ID
        }

        db_updates = []
        # 1. Collect subscription roles from each Channel overwrite
        for channel in category.channels:
            if channel.id == Config.FLIGHT_LISTEN_CHANNEL_ID:
                continue

            # e.g. "🌴┆bituin" -> "bituin", "01-alapaap" -> "01alapaap"
            chan_clean = clean_text(channel.name)
            if not chan_clean:
                continue

            temp_map[chan_clean] = channel.id

            channel_req_roles = []
            for target, overwrite in channel.overwrites.items():
                # Discord migrated from "read_messages" -> "view_channel". Support both.
                can_view = (getattr(overwrite, "view_channel", None) is True) or (getattr(overwrite, "read_messages", None) is True)
                if isinstance(target, discord.Role) and can_view:
                    if target.name != "@everyone":
                        channel_req_roles.append(str(target.id))
                        if target.id not in excluded_roles:
                            sub_roles.add(target.id)

            # Sync with the 'islands' table used by the Web API
            island_clean = re.sub(r'^\d+', '', chan_clean)
            if island_clean:
                db_updates.append((json.dumps(channel_req_roles), str(channel.id), island_clean.upper()))

            # Also map without leading digits for canonical name lookups
            # e.g. "01alapaap" -> "alapaap"
            island_clean = re.sub(r'^\d+', '', chan_clean)
            if island_clean and island_clean != chan_clean:
                temp_map[island_clean] = channel.id

        if db_updates:
            try:
                async with connect_async_db() as db:
                    for roles_json, chan_id, isl_name in db_updates:
                        await db.execute(
                            "UPDATE islands SET required_roles = ?, channel_id = ? WHERE UPPER(name) = ?",
                            (roles_json, chan_id, isl_name)
                        )
                    await db.commit()
            except Exception as e:
                logger.error(f"[FLIGHT] Failed to batch sync islands to DB: {e}", exc_info=True)

        self.island_map = temp_map
        self.all_sub_roles = sub_roles
        if not self.all_sub_roles:
            # If category scan produced no roles, try DB-derived roles.
            await self._ensure_sub_roles_loaded()
        logger.info(f"[FLIGHT] Dynamic Island Fetch Complete. Mapped {len(temp_map)} keys, {len(sub_roles)} sub roles.")

    def _resolve_island_channel_id(self, island_name: str) -> int | None:
        """Resolve Discord text channel id for an island name (same logic as mention link)."""
        island_clean = clean_text(island_name)
        if not island_clean:
            return None
        if island_clean in self.island_map:
            return self.island_map[island_clean]
        stripped = re.sub(r"^\d+", "", island_clean)
        if stripped and stripped in self.island_map:
            return self.island_map[stripped]
        for key, channel_id in self.island_map.items():
            if island_clean == key or island_clean in key:
                return channel_id
        guild = self.bot.get_guild(Config.GUILD_ID)
        if guild:
            for channel in guild.text_channels:
                chan_clean = clean_text(channel.name)
                if island_clean == chan_clean or island_clean in chan_clean:
                    self.island_map[island_clean] = channel.id
                    return channel.id
        return None

    def get_island_channel_link(self, island_name):
        """Get channel link with robust fallback search"""
        island_clean = clean_text(island_name)
        if not island_clean:
            return island_name.title()
        cid = self._resolve_island_channel_id(island_name)
        if cid:
            return f"<#{cid}>"
        return island_name.title()

    def get_island_channel_browser_url(self, destination: str) -> str | None:
        """Open in browser: https://discord.com/channels/GUILD/CHANNEL"""
        if not Config.GUILD_ID:
            return None
        cid = self._resolve_island_channel_id(destination)
        if not cid:
            return None
        return f"https://discord.com/channels/{Config.GUILD_ID}/{cid}"
    def split_options(self, raw: str):
        if not raw: return []
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        out = []
        for p in parts:
            norm = self.normalize_identity_text(p)
            if norm:
                out.append(norm)
        return out

    def normalize_identity_text(self, text: str) -> str:
        """Normalize IGN/island while preserving symbols for strict identity checks."""
        if not text:
            return ""
        # Keep symbols (e.g. $$$, _, -), normalize Unicode width/compat forms,
        # and collapse whitespace so cosmetic spacing differences do not break matches.
        normalized = unicodedata.normalize("NFKC", text).casefold().strip()

        # Canonicalize common punctuation variants without removing symbols.
        quote_map = {
            "\u2018": "'", "\u2019": "'", "\u02bc": "'", "\u2032": "'", "\uff07": "'",
            "\u201c": '"', "\u201d": '"',
            "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
        }
        normalized = "".join(quote_map.get(ch, ch) for ch in normalized)

        # Normalize spaces around punctuation often introduced by mobile keyboards.
        normalized = re.sub(r"\s*'\s*", "'", normalized)
        normalized = re.sub(r'\s*"\s*', '"', normalized)
        normalized = re.sub(r"\s*-\s*", "-", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized

    def calculate_max_identities(self, display_name: str) -> int:
        """Calculate the number of subscriptions required based on nickname slots and options.
        
        Rules:
        - If a category (IGN or Island) has multiple | blocks, its count is the number of blocks.
        - If it has only one | block, its count is the number of / options in that block.
        - The total count is the maximum of the IGN count and Island count.
        """
        if not display_name or '|' not in display_name:
            return 1
            
        chunks = [c.strip() for c in display_name.split('|') if c.strip()]
        if chunks and chunks[0].upper() == "ACNH":
            chunks = chunks[1:]
        
        if not chunks:
            return 1

        ign_blocks = []
        island_blocks = []

        if len(chunks) % 2 == 0:
            # Even chunks: pairs of (IGN, Island)
            for i in range(0, len(chunks), 2):
                ign_blocks.append(chunks[i])
                island_blocks.append(chunks[i+1])
        else:
            # Odd chunks: first is IGN, rest are Islands
            ign_blocks.append(chunks[0])
            for i in range(1, len(chunks)):
                island_blocks.append(chunks[i])
        
        def get_category_count(blocks):
            if not blocks:
                return 0
            if len(blocks) > 1:
                # Multiple blocks: each block is an identity (ignore internal / splits)
                return len(blocks)
            # Single block: count / options
            return len(self.split_options(blocks[0]))
            
        ign_count = get_category_count(ign_blocks)
        island_count = get_category_count(island_blocks)
        
        return max(ign_count, island_count)

    def get_allowed_identity_count(self, member: discord.Member, sub_count: int) -> int:
        """Return how many nickname identities a member may currently have.

        Legacy members who joined before the one-character/island rule took effect
        are allowed to keep up to two existing character/island identities.
        """
        allowed = max(1, sub_count)
        joined_at = getattr(member, "joined_at", None)
        if joined_at is None:
            return allowed

        if joined_at.tzinfo is None:
            joined_at = joined_at.replace(tzinfo=datetime.timezone.utc)
        else:
            joined_at = joined_at.astimezone(datetime.timezone.utc)

        if joined_at < LEGACY_TWO_IDENTITY_CUTOFF_UTC:
            allowed = max(allowed, LEGACY_TWO_IDENTITY_LIMIT)
        return allowed

    def parse_member_nick(self, display_name: str):
        if not display_name:
            return [], []

        # Support ONLY | as a nick separator
        if '|' not in display_name:
            return [], []
        chunks = [c.strip() for c in display_name.split('|') if c.strip()]

        # Skip "ACNH" prefix if present as the first chunk
        if chunks and chunks[0].upper() == "ACNH":
            chunks = chunks[1:]

        if not chunks:
            return [], []

        ign_opts = []
        island_opts = []

        if len(chunks) % 2 == 0:
            # Even number of chunks: treat as (IGN, Island) pairs
            # e.g., "IGN1 | Island1 | IGN2 | Island2" or "IGN1 | Island1"
            for i in range(0, len(chunks), 2):
                ign_opts.extend(self.split_options(chunks[i]))
                island_opts.extend(self.split_options(chunks[i+1]))
        else:
            # Odd number of chunks (e.g., 1 or 3+): First is IGN, rest are Islands
            # e.g., "IGN1 | Island1 | Island2"
            ign_opts = self.split_options(chunks[0])
            for i in range(1, len(chunks)):
                island_opts.extend(self.split_options(chunks[i]))

        # Deduplicate results
        return list(dict.fromkeys(ign_opts)), list(dict.fromkeys(island_opts))

    def _is_strict_nick_match(self, ign_log_clean: str, island_log_clean: str, ign_opts: list[str], island_opts: list[str]) -> bool:
        """Return True only when both IGN and island from nickname match the flight log.

        Supports paired nick formats like "IGN1/IGN2 | Island1/Island2" by checking
        index-aligned pairs first; otherwise falls back to set membership for both fields.
        """
        if not ign_opts or not island_opts:
            return False

        if len(ign_opts) == len(island_opts):
            for ign_opt, island_opt in zip(ign_opts, island_opts):
                if ign_log_clean == ign_opt and island_log_clean == island_opt:
                    return True

        return ign_log_clean in ign_opts and island_log_clean in island_opts

    def find_matching_members(self, guild, ign_log_clean, island_log_clean):
        if not guild:
            return []
        exact_members = []
        for member in guild.members:
            ign_opts, island_opts = self.parse_member_nick(member.display_name)
            if not ign_opts and not island_opts:
                continue
            if self._is_strict_nick_match(ign_log_clean, island_log_clean, ign_opts, island_opts):
                exact_members.append(member)
        return exact_members

    def find_all_candidates(self, guild, ign_log_clean, island_log_clean):
        """Return all registered members (those with '|' in nickname) with their match details.

        Returns a list of dicts:
            {member, ign_opts, island_opts, ign_match, island_match, full_match}
        Sorted: IGN matches first, then no match.
        """
        if not guild:
            return []
        candidates = []
        for member in guild.members:
            ign_opts, island_opts = self.parse_member_nick(member.display_name)
            if not ign_opts and not island_opts:
                continue
            ign_match    = ign_log_clean in ign_opts
            island_match = island_log_clean in island_opts if island_opts else False
            full_match   = self._is_strict_nick_match(ign_log_clean, island_log_clean, ign_opts, island_opts)
            candidates.append({
                "member":       member,
                "ign_opts":     ign_opts,
                "island_opts":  island_opts,
                "ign_match":    ign_match,
                "island_match": island_match,
                "full_match":   full_match,
            })
        candidates.sort(key=lambda c: (not c["full_match"], not c["ign_match"], not c["island_match"]))
        return candidates

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.display_name == after.display_name:
            return
        try:
            await self.record_member_identity_event(
                after,
                IDENTITY_EVENT_NICKNAME_CHANGE,
                before.display_name,
                after.display_name,
            )
            logger.info(f"[FLIGHT] Recorded nickname change for {after.id}: {before.display_name!r} -> {after.display_name!r}")
        except Exception as exc:
            logger.warning(f"[FLIGHT] Could not record nickname change for {after.id}: {exc}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            await self.record_member_identity_event(
                member,
                IDENTITY_EVENT_MEMBER_JOIN,
                None,
                member.display_name,
            )
            logger.info(f"[FLIGHT] Recorded member join for {member.id}: {member.display_name!r}")
        except Exception as exc:
            logger.warning(f"[FLIGHT] Could not record member join for {member.id}: {exc}")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user or message.channel.id != Config.FLIGHT_LISTEN_CHANNEL_ID:
            return
        match = self.join_pattern.search(message.content)
        if match:
            ign_raw    = match.group(1).strip()
            island_raw = match.group(2).strip()
            dest_raw   = match.group(3).strip()
            ign_norm = self.normalize_identity_text(ign_raw)
            isl_norm = self.normalize_identity_text(island_raw)
            self.last_processed = discord.utils.utcnow()
            asyncio.create_task(
                self._process_flight_log(message.guild, ign_raw, island_raw, dest_raw, ign_norm, isl_norm, message.jump_url, message.content)
            )

    async def _process_flight_log(self, guild, ign_raw, island_raw, dest_raw, ign_norm, isl_norm, message_url=None, message_content=None):
        """Background task: look up members then run the full log pipeline."""
        try:
            target_guild = guild or self.bot.get_guild(Config.GUILD_ID)
            if target_guild and not target_guild.chunked:
                async with self._chunk_lock:
                    if not target_guild.chunked:
                        try:
                            await target_guild.chunk()
                        except Exception as chunk_err:
                            logger.warning(f"[FLIGHT] Guild chunking failed/timed out: {chunk_err}")
            found = await asyncio.to_thread(
                self.find_matching_members, target_guild, ign_norm, isl_norm
            )
            await self.log_result(found, "JOINING", ign_raw, island_raw, dest_raw, island_type='sub', message_url=message_url, message_content=message_content)
        except Exception as e:
            logger.error(f"[FLIGHT] Pipeline error for {ign_raw}: {e}", exc_info=True)

    async def log_result(self, found_members, status, ign, island, destination, timestamp=None, island_type: str = 'sub', message_url=None, message_content=None):
        output_channel = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)
        if not output_channel: return

        embed_timestamp = timestamp or discord.utils.utcnow()
        visit_ts = int(embed_timestamp.timestamp()) if hasattr(embed_timestamp, 'timestamp') else int(discord.utils.utcnow().timestamp())
        guild = self.bot.get_guild(Config.GUILD_ID)
        if guild is None:
            logger.error(f"[FLIGHT] Guild {Config.GUILD_ID} unavailable, aborting log_result for {ign}")
            return
        guild_id = guild.id

        ambiguous_members = found_members if len(found_members) > 1 else []
        resolved_authorized_target = None
        if ambiguous_members:
            authorized_target = await self._get_recent_authorized_target(ign, guild_id=guild_id)
            authorized_member = resolve_authorized_ambiguous_member(ambiguous_members, authorized_target)
            if authorized_member is not None:
                resolved_authorized_target = authorized_target
                logger.info(
                    "[FLIGHT] Resolved ambiguous match for %s to recent authorized user_id=%s.",
                    ign,
                    int(authorized_target["user_id"]),
                )
                found_members = [authorized_member]
                ambiguous_members = []

        if len(found_members) == 1:
            mentions = " ".join([m.mention for m in found_members])
            logger.info(f"[FLIGHT] Match: {ign} | {mentions}")
            visit_id = await self.record_island_visit(ign, island, destination, found_members, guild_id, visit_ts, island_type=island_type)

            # Post authorized-traveler log to the xlog channel.
            xlog_channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
            if xlog_channel:
                guild_icon = guild.icon.url if guild and guild.icon else None
                member_line = "\n".join(_format_user_for_embed(m) for m in found_members)
                destination_link = self.get_island_channel_link(destination)

                desc_lines = []
                if message_url:
                    content_preview = message_content.strip() if message_content else "View original message"
                    if len(content_preview) > 100:
                        content_preview = content_preview[:97] + "..."
                    desc_lines.append(f"[{content_preview}]({message_url})")
                desc_lines.append(f"Member Linked: {member_line}")

                # Add roles/subscription info for the matched member
                member = found_members[0]
                # Ensure we have up-to-date roles (member cache can be stale/partial).
                try:
                    member = await guild.fetch_member(member.id)
                except Exception:
                    pass
                member_roles = [r for r in member.roles if r.name != "@everyone"]
                has_access = any(r.id == Config.ISLAND_ACCESS_ROLE for r in member_roles)
                
                sub_roles = {}
                dest_clean = clean_text(destination)
                channel_id = self.island_map.get(dest_clean)
                dest_channel = guild.get_channel(channel_id) if channel_id else None
                
                if dest_channel:
                    for target, overwrite in dest_channel.overwrites.items():
                        can_view = (getattr(overwrite, "view_channel", None) is True) or (getattr(overwrite, "read_messages", None) is True)
                        if isinstance(target, discord.Role) and can_view:
                            if target.name != "@everyone":
                                sub_roles[target.id] = target.name
                
                # Subscription Analysis
                cog = self
                await self._ensure_sub_roles_loaded()
                ign_opts, island_opts = self.parse_member_nick(member.display_name)
                max_identities = self.calculate_max_identities(member.display_name)

                all_member_subs = [r for r in member.roles if r.id in cog.all_sub_roles]
                current_island_subs = [r for r in member.roles if r.id in sub_roles]
                other_subs = [r for r in all_member_subs if r.id not in sub_roles]
                required_destination_roles = [
                    role for role in (guild.get_role(role_id) for role_id in sub_roles)
                    if role is not None
                ]
                allowed_identities = self.get_allowed_identity_count(member, len(all_member_subs))
                recent_identity_events, _authorized_target = await self._get_actionable_identity_events(
                    member.id,
                    guild_id,
                    ign,
                )

                if max_identities > allowed_identities:
                    # SUSPICIOUS: Automatically trigger manual alert flow
                    await self._trigger_automatic_flag(
                        ign,
                        island,
                        destination,
                        member,
                        max_identities,
                        len(all_member_subs),
                        allowed_identities,
                        message_url,
                        message_content,
                        embed_timestamp,
                        island_type=island_type,
                        recent_identity_events=recent_identity_events,
                    )
                    return

                if sub_roles and all_member_subs and not current_island_subs:
                    await self._trigger_island_access_mismatch_flag(
                        ign,
                        island,
                        destination,
                        member,
                        visit_id,
                        required_destination_roles,
                        all_member_subs,
                        recent_identity_events,
                        message_url,
                        embed_timestamp,
                    )
                    return

                if recent_identity_events:
                    await self._trigger_recent_identity_flag(
                        ign,
                        island,
                        destination,
                        member,
                        recent_identity_events,
                        visit_id,
                        message_url,
                        embed_timestamp,
                    )
                    return

                # Design: Use a block with emoji and clear separation
                if has_access:
                    desc_lines.append(
                        f"<a:starpink:784055540321091584> **Has Island Access?** Yes"
                    )
                    if current_island_subs:
                        sub_lines = [r.mention for r in current_island_subs]
                        desc_lines.append(
                            f"<a:heartside:784055539881214002> **Subscription(s):**\n> " + "\n> ".join(sub_lines)
                        )
                    else:
                        desc_lines.append(
                            f"<a:CampWarning:1172346431542140961> **Subscription(s):**\n> None detected for this island."
                        )
                    
                    if other_subs:
                        other_lines = [r.mention for r in other_subs]
                        desc_lines.append(
                            f"<a:heartside:784055539881214002> **Other Subscription(s):**\n> " + "\n> ".join(other_lines)
                        )

                    if len(all_member_subs) > 1:
                        desc_lines.append(f"> <:ChoLove:818216528449241128> **Multiple Subscriptions Detected**")


                else:
                    desc_lines.append(
                        f"<a:CampWarning:1172346431542140961> **No Island Access Role**"
                    )

                desc_lines.append("\nLog details matched with a member.")

                embed = discord.Embed(
                    title=f"<:Cho_Check:1456715827213504593> Verified Flight",
                    description="\n".join(desc_lines),
                    color=COLOR_SUCCESS,
                    timestamp=embed_timestamp,
                )
                embed.add_field(name="IGN",         value=f"```yaml\n{ign}```",           inline=True)
                embed.add_field(name="Island Name", value=f"```yaml\n{island.title()}```", inline=True)
                embed.add_field(name="Destination", value=destination_link,               inline=True)
                if resolved_authorized_target is not None:
                    embed.add_field(
                        name="Visitor",
                        value=f"```yaml\n{ign} from {island.title()}```",
                        inline=True,
                    )
                    embed.add_field(name="Target", value=member_line, inline=True)
                    embed.add_field(name="Cleared By", value="Previous authorization", inline=True)
                    embed.add_field(name="Matched From", value=f"`#{resolved_authorized_target['id']}`", inline=True)
                if visit_id is not None:
                    embed.add_field(name="Visit ID", value=f"`#{visit_id}`",              inline=True)

                # Merge any pending dodo-code request from this member into the embed,
                # suppressing the separate "Dodo Code Requested" xlog entry.
                dodo_req = self.pop_pending_dodo_request(found_members[0].id)
                if dodo_req is not None:
                    embed.add_field(name="Dodo Requested", value=dodo_req['channel'].mention, inline=True)

                embed.set_image(url=Config.FOOTER_LINE)
                embed.set_footer(text="Chopaeng Camp™ • Match Log", icon_url=guild_icon)

                flag_view = VerifiedFlightFlagView(bot=self.bot)
                if message_url:
                    flag_view.add_item(discord.ui.Button(label="View Flight Standing", url=message_url, style=discord.ButtonStyle.link))
                if dodo_req is not None and dodo_req.get('reply_msg'):
                    flag_view.add_item(discord.ui.Button(label="View Dodo Request", url=dodo_req['reply_msg'].jump_url, style=discord.ButtonStyle.link))
                
                await xlog_channel.send(embed=embed, view=flag_view)
        else:
            destination_link = self.get_island_channel_link(destination)
            alert_ts = int(embed_timestamp.timestamp()) if hasattr(embed_timestamp, 'timestamp') else int(discord.utils.utcnow().timestamp())

            # Guard against a concurrent log_result call for the same IGN+destination.
            # Register the key synchronously (before any await) so a second
            # coroutine for the same island sees it immediately and returns without
            # inserting a duplicate DB row or sending a duplicate embed.
            ign_clean = clean_text(ign)
            dest_clean = clean_text(destination)
            alert_key = (ign_clean, dest_clean)
            if alert_key in self._creating_alerts:
                return
            self._creating_alerts.add(alert_key)
            try:
                # If this IGN was already authorized and has a linked target within the last 24 hours,
                # keep the verbose audit trail but do not create a new manual-review alert.
                authorized_target = await self._get_recent_authorized_target(ign, guild_id=guild_id)
                if authorized_target:
                    user_id = int(authorized_target["user_id"])
                    visit_id = await self.record_authorized_followup_visit(
                        ign,
                        island,
                        destination,
                        user_id,
                        guild_id,
                        alert_ts,
                        island_type=island_type,
                    )
                    xlog_channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
                    if xlog_channel:
                        guild_icon = guild.icon.url if guild and guild.icon else None
                        member_label = _format_user_for_embed(user_id=user_id)
                        try:
                            if guild:
                                member = await guild.fetch_member(user_id)
                                member_label = _format_user_for_embed(member)
                        except Exception:
                            pass

                        xlog_embed = discord.Embed(
                            title="<:Cho_Check:1456715827213504593> Authorized Flight",
                            description=(
                                f"Previously verified traveler detected for **{destination_link}**.\n"
                                "No new alert was created."
                            ),
                            color=COLOR_SUCCESS,
                            timestamp=embed_timestamp,
                        )
                        xlog_embed.add_field(name="Member Linked", value=member_label, inline=False)
                        xlog_embed.add_field(
                            name="Visitor",
                            value=f"```yaml\n{ign} from {island.title()}```",
                            inline=True,
                        )
                        xlog_embed.add_field(name="Target", value=member_label, inline=True)
                        xlog_embed.add_field(name="Cleared By", value="Previous authorization", inline=True)
                        xlog_embed.add_field(name="IGN",           value=f"```yaml\n{ign}```",           inline=True)
                        xlog_embed.add_field(name="Origin Island", value=f"```yaml\n{island.title()}```", inline=True)
                        xlog_embed.add_field(name="Destination",   value=destination_link,               inline=True)
                        if visit_id is not None:
                            xlog_embed.add_field(name="Visit ID", value=f"`#{visit_id}`", inline=True)
                        xlog_embed.add_field(name="Matched From", value=f"`#{authorized_target['id']}`", inline=True)
                        xlog_embed.set_image(url=Config.FOOTER_LINE)
                        xlog_embed.set_footer(text="Chopaeng Camp™ • Match Log", icon_url=guild_icon)

                        xlog_view = VerifiedFlightFlagView(
                            bot=self.bot,
                            ign=ign,
                            visit_id=visit_id,
                            message_url=message_url,
                        )
                        if message_url:
                            xlog_view.add_item(discord.ui.Button(label="View Flight Standing", url=message_url, style=discord.ButtonStyle.link))
                        dodo_req = self.pop_pending_dodo_request(user_id)
                        if dodo_req is not None and dodo_req.get('reply_msg'):
                            xlog_embed.add_field(name="Dodo Requested", value=dodo_req['channel'].mention, inline=True)
                            xlog_view.add_item(discord.ui.Button(label="View Dodo Request", url=dodo_req['reply_msg'].jump_url, style=discord.ButtonStyle.link))
                        await xlog_channel.send(embed=xlog_embed, view=xlog_view)

                    logger.info(f"[FLIGHT] Logged authorized follow-up xlog for {ign} linked to user_id={user_id}.")
                    return

                visit_id = await self.record_island_visit(ign, island, destination, [], guild_id, alert_ts, island_type=island_type)

                # Check if there is already a pending alert for this IGN+destination to avoid flooding the channel
                existing_msg = None
                pending_entry = self._pending_alerts.get(alert_key)
                existing_msg_id = None
                if pending_entry:
                    if isinstance(pending_entry, tuple):
                        cached_id, cached_ts = pending_entry
                        if (alert_ts - cached_ts) <= MAX_ALERT_MERGE_SECONDS:
                            existing_msg_id = cached_id
                        else:
                            self._pending_alerts.pop(alert_key, None)
                    else:
                        existing_msg_id = pending_entry

                if existing_msg_id:
                    try:
                        existing_msg = await output_channel.fetch_message(existing_msg_id)
                        # Only reuse the message if the alert is still pending (not yet resolved)
                        if existing_msg.embeds:
                            status_field = next(
                                (f for f in existing_msg.embeds[0].fields if f.name == "Status"),
                                None
                            )
                            if status_field is None or "PENDING REVIEW" not in status_field.value:
                                existing_msg = None
                                self._pending_alerts.pop(alert_key, None)
                    except discord.NotFound:
                        existing_msg = None
                        self._pending_alerts.pop(alert_key, None)

                xlog_channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
                guild_icon = guild.icon.url if guild and guild.icon else None

                if existing_msg:
                    # Update the existing alert with a re-join counter instead of spamming a new message
                    embed = existing_msg.embeds[0]
                    rejoin_count = 1
                    updated_fields = []
                    has_rejoin_field = False
                    for f in embed.fields:
                        if f.name == "Re-join Attempts":
                            has_rejoin_field = True
                            m = re.search(r"\*\*(\d+)\*\*", f.value)
                            if m:
                                rejoin_count = int(m.group(1)) + 1
                        else:
                            updated_fields.append((f.name, f.value, f.inline))
                    rejoin_field = ("Re-join Attempts", f"**{rejoin_count}** attempt(s)\nLast seen <t:{alert_ts}:R>", True)
                    if has_rejoin_field:
                        updated_fields.append(rejoin_field)
                    else:
                        # Insert the re-join field after "Detected"
                        new_fields = []
                        for name, value, inline in updated_fields:
                            new_fields.append((name, value, inline))
                            if name == "Detected":
                                new_fields.append(rejoin_field)
                        updated_fields = new_fields
                    embed.clear_fields()
                    for name, value, inline in updated_fields:
                        embed.add_field(name=name, value=value, inline=inline)
                    await existing_msg.edit(embed=embed)
                    self._pending_alerts[alert_key] = (existing_msg.id, alert_ts)
                    logger.info(f"[FLIGHT] Updated existing alert for {ign} (re-join attempt #{rejoin_count})")

                    # Post re-join notification to xlog channel
                    if xlog_channel:
                        xlog_embed = discord.Embed(
                            description=(
                                f"### {Config.EMOJI_FAIL} Unverified Flight — Re-join Detected\n"
                                f"An unregistered traveler re-joined **{destination_link}**."
                            ),
                            color=COLOR_INVESTIGATION,
                            timestamp=embed_timestamp,
                        )
                        xlog_embed.add_field(name="IGN",          value=f"```yaml\n{ign}```",           inline=True)
                        xlog_embed.add_field(name="Origin Island", value=f"```yaml\n{island.title()}```", inline=True)
                        xlog_embed.add_field(name="Destination",   value=destination_link,               inline=True)
                        xlog_embed.add_field(name="Re-join #",     value=f"**{rejoin_count}**",           inline=True)
                        xlog_embed.set_image(url=Config.FOOTER_LINE)
                        xlog_embed.set_footer(text="Chopaeng Camp™ • Flight Logger", icon_url=guild_icon)
                        xlog_view = discord.ui.View()
                        xlog_view.add_item(discord.ui.Button(label="View Alert", url=existing_msg.jump_url, style=discord.ButtonStyle.link))
                        dodo_jump = await self.lookup_dodo_reveal_jump_url(ign, destination)
                        if dodo_jump:
                            xlog_view.add_item(
                                discord.ui.Button(label="View Dodo Reveal", url=dodo_jump, style=discord.ButtonStyle.link)
                            )
                        island_jump = self.get_island_channel_browser_url(destination)
                        if island_jump:
                            xlog_view.add_item(
                                discord.ui.Button(label="View Dodo Request", url=island_jump, style=discord.ButtonStyle.link)
                            )
                        await xlog_channel.send(embed=xlog_embed, view=xlog_view)
                else:
                    is_ambiguous = bool(ambiguous_members)
                    if is_ambiguous:
                        description = (
                            f"### {Config.EMOJI_FAIL} Ambiguous Traveler Match\n"
                            f"Multiple members matched this flight for **{destination_link}**.\n"
                            f"Manual review is required before admitting."
                        )
                        embed_color = COLOR_INVESTIGATION
                    else:
                        description = (
                            f"### {Config.EMOJI_FAIL} Unknown Traveler Detected\n"
                            f"An unregistered visitor is attempting to join **{destination_link}**.\n"
                            f"Use the buttons below to take action."
                        )
                        embed_color = COLOR_ALERT

                    embed = discord.Embed(
                        description=description,
                        color=embed_color,
                        timestamp=embed_timestamp
                    )
                    embed.add_field(name="Traveler (IGN)", value=f"```yaml\n{ign}```", inline=True)
                    embed.add_field(name="Origin Island",  value=f"```yaml\n{island.title()}```", inline=True)
                    embed.add_field(name="Destination",    value=f"```yaml\n{destination.title()}```", inline=True)
                    if is_ambiguous:
                        candidate_lines = [
                            _format_user_for_embed(m)
                            for m in ambiguous_members[:15]
                        ]
                        if len(ambiguous_members) > 15:
                            candidate_lines.append(f"...and {len(ambiguous_members) - 15} more")
                        embed.add_field(name="Possible Matches", value="\n".join(candidate_lines), inline=False)
                    embed.add_field(name="Detected",       value=f"<t:{alert_ts}:R>", inline=True)
                    embed.add_field(name="Status",         value="<:Cho_Investigate:1474310726381338666> **PENDING REVIEW**", inline=True)
                    if visit_id is not None:
                        embed.add_field(name="Visit ID", value=f"`#{visit_id}`", inline=True)
                    embed.set_image(url=Config.FOOTER_LINE)
                    guild      = self.bot.get_guild(Config.GUILD_ID)
                    guild_icon = guild.icon.url if guild and guild.icon else None
                    embed.set_footer(text="Chopaeng Camp™ • Flight Logger", icon_url=guild_icon)

                    view = TravelerActionView(self.bot, ign, visit_id=visit_id)
                    sent_msg = await output_channel.send(embed=embed, view=view)
                    self._pending_alerts[alert_key] = (sent_msg.id, alert_ts)

                    # Post unknown traveler notification to xlog channel
                    if xlog_channel:
                        if is_ambiguous:
                            xlog_desc = (
                                f"### {Config.EMOJI_FAIL} Ambiguous Flight Match\n"
                                f"Multiple members matched this traveler for **{destination_link}**.\n"
                                f"Flagged for manual review."
                            )
                            xlog_color = COLOR_INVESTIGATION
                        else:
                            xlog_desc = (
                                f"### {Config.EMOJI_FAIL} Unverified Flight Detected\n"
                                f"An unregistered traveler was detected attempting to join **{destination_link}**.\n"
                                f"No matching member found."
                            )
                            xlog_color = COLOR_ALERT

                        xlog_embed = discord.Embed(
                            description=xlog_desc,
                            color=xlog_color,
                            timestamp=embed_timestamp,
                        )
                        xlog_embed.add_field(name="IGN",          value=f"```yaml\n{ign}```",           inline=True)
                        xlog_embed.add_field(name="Origin Island", value=f"```yaml\n{island.title()}```", inline=True)
                        xlog_embed.add_field(name="Destination",   value=destination_link,               inline=True)
                        if is_ambiguous:
                            candidate_lines = [
                                _format_user_for_embed(m)
                                for m in ambiguous_members[:10]
                            ]
                            if len(ambiguous_members) > 10:
                                candidate_lines.append(f"...and {len(ambiguous_members) - 10} more")
                            xlog_embed.add_field(name="Possible Matches", value="\n".join(candidate_lines), inline=False)
                        xlog_embed.add_field(name="Detected",      value=f"<t:{alert_ts}:R>",            inline=True)
                        if visit_id is not None:
                            xlog_embed.add_field(name="Visit ID",  value=f"`#{visit_id}`",               inline=True)
                        xlog_embed.set_image(url=Config.FOOTER_LINE)
                        xlog_embed.set_footer(text="Chopaeng Camp™ • Flight Logger", icon_url=guild_icon)
                        xlog_view = discord.ui.View()
                        if message_url:
                            xlog_view.add_item(discord.ui.Button(label="View Flight Standing", url=message_url, style=discord.ButtonStyle.link))
                        xlog_view.add_item(discord.ui.Button(label="View Alert", url=sent_msg.jump_url, style=discord.ButtonStyle.link))
                        dodo_jump = await self.lookup_dodo_reveal_jump_url(ign, destination)
                        if dodo_jump:
                            xlog_view.add_item(
                                discord.ui.Button(label="View Dodo Reveal", url=dodo_jump, style=discord.ButtonStyle.link)
                            )
                        island_jump = self.get_island_channel_browser_url(destination)
                        if island_jump:
                            xlog_view.add_item(
                                discord.ui.Button(label="View Dodo Request", url=island_jump, style=discord.ButtonStyle.link)
                            )
                        await xlog_channel.send(embed=xlog_embed, view=xlog_view)
            finally:
                self._creating_alerts.discard(alert_key)

    @commands.hybrid_command(name="recover_flights", aliases=["recoverflights"])
    @app_commands.describe(hours="How many hours to scan back (default: 48)", mode="Execution mode: 'dry' or 'run'")
    @commands.has_permissions(administrator=True)
    async def recover_flights(self, ctx, hours: int = 48, mode: str = "dry"):
        """
        Scrapes past logs chronologically (Oldest -> Newest).
        Usage: /recover_flights [hours_back] [dry/run] or !recover_flights [hours_back] [dry/run]
        """
        listen_channel = self.bot.get_channel(Config.FLIGHT_LISTEN_CHANNEL_ID)
        if not listen_channel:
            return await ctx.send(f"[ERR] Listener channel {Config.FLIGHT_LISTEN_CHANNEL_ID} not found.")

        dry_run = mode.lower() != "run"
        status_header = f"Scanning history for the last **{hours} hours**..."
        status_mode = "DRY RUN" if dry_run else "LIVE EXECUTION"
        status_msg = await ctx.send(f"**{status_header}**\nMode: {status_mode}")

        cutoff = discord.utils.utcnow() - datetime.timedelta(hours=hours)
        found_count = 0
        processed_count = 0

        # oldest_first=True ensures logs are posted in the order they happened (Past -> Present)
        async for message in listen_channel.history(after=cutoff, limit=None, oldest_first=True):
            if message.author == self.bot.user:
                continue

            match = self.join_pattern.search(message.content)
            if match:
                found_count += 1

                if not dry_run:
                    try:
                        ign_raw    = match.group(1).strip()
                        island_raw = match.group(2).strip()
                        dest_raw   = match.group(3).strip()

                        ign_norm = self.normalize_identity_text(ign_raw)
                        isl_norm = self.normalize_identity_text(island_raw)

                        found = await asyncio.to_thread(self.find_matching_members, message.guild, ign_norm, isl_norm)

                        # Trigger the log result
                        await self.log_result(found, "JOINING", ign_raw, island_raw, dest_raw, timestamp=message.created_at, message_url=message.jump_url, message_content=message.content)
                        logger.info(f"[RECOVER] Processed item #{processed_count} - {ign_raw}")

                        processed_count += 1
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        logger.error(f"[RECOVER] Failed to process message {message.id}: {e}", exc_info=True)

        if dry_run:
            await status_msg.edit(content=f"**Scan Complete (Dry Run)**\nFound: {found_count} matches.\n\nCommand to execute:\n`!recover_flights {hours} run`")
        else:
            await status_msg.edit(content=f"**Recovery Complete**\nProcessed: {processed_count} flights.")

    @commands.hybrid_command(name="flight_status", aliases=["flightstatus", "fstatus"])
    @commands.has_permissions(manage_messages=True)
    async def flight_status(self, ctx):
        """Diagnose connection, channels, and last activity."""

        listen_chan = self.bot.get_channel(Config.FLIGHT_LISTEN_CHANNEL_ID)
        log_chan = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)

        lines = []

        # Listener Status
        if listen_chan:
            perms = listen_chan.permissions_for(ctx.guild.me)
            if perms.read_messages:
                lines.append(f"[OK] Listener Channel: {listen_chan.name}")
            else:
                lines.append(f"[WARN] Listener Channel: {listen_chan.name} (No Read Access)")
        else:
            lines.append(f"[ERR] Listener Channel: Missing (ID: {Config.FLIGHT_LISTEN_CHANNEL_ID})")

        # Log Output Status
        if log_chan:
            perms = log_chan.permissions_for(ctx.guild.me)
            if perms.send_messages:
                lines.append(f"[OK] Log Channel: {log_chan.name}")
            else:
                lines.append(f"[WARN] Log Channel: {log_chan.name} (No Send Access)")
        else:
            lines.append(f"[ERR] Log Channel: Missing (ID: {Config.FLIGHT_LOG_CHANNEL_ID})")

        # Database Status
        if self._db_conn:
            lines.append("[OK] Database: Connected")
        else:
            lines.append("[WARN] Database: Disconnected (Connects on write)")

        # Last Activity
        if self.last_processed:
            ts = int(self.last_processed.timestamp())
            lines.append(f"[INFO] Last Flight: <t:{ts}:R>")
        else:
            lines.append("[INFO] Last Flight: None since restart")

        embed = discord.Embed(
            title="System Status",
            description="```ini\n" + "\n".join(lines) + "\n```",
            color=0x2b2d31  # Dark/Neutral
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="flightdebug", aliases=["fdebug"])
    @app_commands.describe(
        character_name="ACNH character name from the flight log",
        island_name="ACNH island name from the flight log",
    )
    @commands.has_permissions(manage_messages=True)
    async def flight_debug(self, ctx, character_name: str, island_name: str):
        """
        Test member-matching against a character and island name.
        Usage: /flightdebug character_name island_name
        """
        if not character_name or not island_name:
            return await ctx.send(
                "**Usage:** `/flightdebug character_name island_name`\n"
                "Prefix usage for names with spaces: `!fdebug \"Character Name\" \"Island Name\"`"
            )

        ign = character_name.strip()
        island = island_name.strip()
        ign_norm = self.normalize_identity_text(ign)
        isl_norm = self.normalize_identity_text(island)

        # Run member-matching in a thread (same as the live pipeline)
        found = await asyncio.to_thread(
            self.find_matching_members, ctx.guild, ign_norm, isl_norm
        )
        candidates = await asyncio.to_thread(
            self.find_all_candidates, ctx.guild, ign_norm, isl_norm
        )

        if found:
            member_lines = []
            found_has_identity_flags = False
            for m in found:
                events = await self.get_recent_identity_events(m.id, ctx.guild.id if ctx.guild else None)
                _, reasons = summarize_recent_identity_events(events)
                suffix = ""
                if reasons:
                    found_has_identity_flags = True
                    suffix = f" - FLAG: {', '.join(reasons)}"
                member_lines.append(f"{_format_user_for_embed(m)}{suffix}")
            embed = discord.Embed(
                title="<:Cho_Check:1456715827213504593> Match Found",
                description=(
                    "This log entry **would trigger manual review** due to recent identity activity."
                    if found_has_identity_flags
                    else "This log entry **would be verified** as a known traveler."
                ),
                color=COLOR_INVESTIGATION if found_has_identity_flags else COLOR_SUCCESS,
            )
            embed.add_field(name="IGN",         value=f"`{ign}`",            inline=True)
            embed.add_field(name="Island",      value=f"`{island}`",         inline=True)
            embed.add_field(name="Matched Member(s)", value="\n".join(member_lines), inline=False)
        else:
            embed = discord.Embed(
                title=f"{Config.EMOJI_FAIL} No Match",
                description="This character/island pair **would trigger an unknown traveler alert**.",
                color=COLOR_ALERT,
            )
            embed.add_field(name="IGN",         value=f"`{ign}`",            inline=True)
            embed.add_field(name="Island",      value=f"`{island}`",         inline=True)

            # Show partial candidates (IGN-only or island-only matches) to help diagnose
            partial = [c for c in candidates if c["ign_match"] or c["island_match"]][:MAX_DEBUG_CANDIDATES]
            if partial:
                cand_lines = []
                for c in partial:
                    flags = []
                    if c["ign_match"]:    flags.append("IGN ✓")
                    if c["island_match"]: flags.append("Island ✓")
                    events = await self.get_recent_identity_events(c["member"].id, ctx.guild.id if ctx.guild else None)
                    _, reasons = summarize_recent_identity_events(events)
                    if reasons:
                        flags.append(f"FLAG: {', '.join(reasons)}")
                    cand_lines.append(f"{_format_user_for_embed(c['member'])} — {', '.join(flags)}")
                embed.add_field(name="Closest Candidates", value="\n".join(cand_lines), inline=False)
            else:
                embed.add_field(name="Closest Candidates", value="None found.", inline=False)

        embed.set_footer(text="Debug only — no database writes or channel posts.")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="flighttest", aliases=["ftest"])
    @commands.has_permissions(manage_messages=True)
    async def flight_test(self, ctx):
        """
        End-to-end test of the flight logger pipeline.
        Sends a fake flight message, processes it through the full pipeline, then cleans up.
        Usage: !flighttest
        """
        await ctx.defer()
        logger.info(f"[FLIGHT-TEST] Debug flight test triggered by {ctx.author}")
        
        now = datetime.datetime.now()
        timestamp = now.strftime("%Y-%m-%d %I:%M:%S %p").lower()

        # Pick a random sub-island as the destination; fall back to a placeholder if none loaded yet
        if self.island_map:
            random_dest = random.choice(tuple(self.island_map.keys())).title()
        else:
            random_dest = "Aruga"

        test_message_content = f"[{timestamp}] 🛬 ChoBot from Treasure Island is joining {random_dest}."
        
        # Get channels
        listen_channel = self.bot.get_channel(Config.FLIGHT_LISTEN_CHANNEL_ID)
        log_channel = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)
        
        if not listen_channel:
            embed = discord.Embed(
                title="Flight Test Failed",
                description="Could not find the flight listen channel.",
                color=0xFF0000
            )
            return await ctx.send(embed=embed)
        
        test_msg = None
        success = True
        error_details = None
        
        try:
            # Step 1: Send the test message to the listen channel
            test_msg = await listen_channel.send(test_message_content)
            
            # Step 2: Parse the message and call log_result directly
            # (since the bot ignores its own messages in on_message)
            match = self.join_pattern.search(test_message_content)
            if match:
                ign_raw = match.group(1).strip()
                island_raw = match.group(2).strip()
                dest_raw = match.group(3).strip()
                
                # Clean and find matching members
                ign_norm = self.normalize_identity_text(ign_raw)
                isl_norm = self.normalize_identity_text(island_raw)
                found = await asyncio.to_thread(
                    self.find_matching_members, 
                    ctx.guild, 
                    ign_norm,
                    isl_norm
                )
                
                # Log the result (this simulates what on_message would do)
                await self.log_result(found, "JOINING", ign_raw, island_raw, dest_raw, message_url=test_msg.jump_url if test_msg else None, message_content=test_message_content)
                
            # Step 3: Wait 3 seconds to allow moderators to see the test message
            # and verify the alert appears in the log channel
            await asyncio.sleep(3)
                
        except discord.Forbidden:
            success = False
            error_details = "Permission denied. Bot may lack permissions to send/delete messages in the listen channel."
        except Exception as e:
            success = False
            error_details = f"Unexpected error: {str(e)}"
            logger.error(f"[FLIGHT-TEST] Error during flight test: {e}", exc_info=True)
        finally:
            # Step 4: Clean up the test message from the listen channel
            if test_msg:
                try:
                    await test_msg.delete()
                except discord.NotFound:
                    pass  # Message already deleted
                except discord.Forbidden:
                    logger.warning(f"[FLIGHT-TEST] Could not delete test message - permission denied")
                except Exception as e:
                    logger.warning(f"[FLIGHT-TEST] Could not delete test message: {e}")
        
        # Step 5: Send summary embed to the invoker
        if success:
            embed = discord.Embed(
                title="Flight Test Complete",
                description="The test flight message was sent, processed, and cleaned up successfully.",
                color=0x2ECC71  # Green
            )
        else:
            embed = discord.Embed(
                title="Flight Test Failed",
                description=error_details or "An error occurred during the test.",
                color=0xFF0000  # Red
            )
        
        embed.add_field(
            name="<:Cho_Notes:1474311464688029817> Test Message",
            value=f"```{test_message_content}```",
            inline=False
        )
        embed.add_field(
            name="Listen Channel",
            value=listen_channel.mention if listen_channel else "Not found",
            inline=True
        )
        embed.add_field(
            name="Log Channel",
            value=log_channel.mention if log_channel else "Not found",
            inline=True
        )
        embed.add_field(
            name="ℹ️ Note",
            value=f"Check {log_channel.mention if log_channel else 'the log channel'} to verify the bot logged the test flight (should show 'UNKNOWN TRAVELER' alert for DebugUser).",
            inline=False
        )
        embed.set_footer(text="🛠️ DEBUG ONLY — This message will be automatically deleted in 10 minutes.")
        
        await ctx.send(embed=embed, delete_after=600)

    @commands.hybrid_command(name="unwarn", aliases=["removewarn"])
    @app_commands.describe(user="The user to unwarn", reason="Reason for removing the warning (optional)")
    @commands.has_permissions(manage_messages=True)
    async def unwarn(self, ctx, user: discord.Member, *, reason: str = None):
        """Remove all warnings from a user."""
        is_slash = ctx.interaction is not None
        await self._unwarn_internal(ctx.interaction if is_slash else ctx, user, reason, is_slash=is_slash)

    async def _unwarn_internal(self, ctx_or_interaction, user: discord.Member, reason: str = None, is_slash: bool = True):
        """Internal method for unwarn logic."""
        # Handle both slash and prefix commands
        if is_slash:
            await ctx_or_interaction.response.defer(ephemeral=True)
            guild = ctx_or_interaction.guild
            mod = ctx_or_interaction.user
        else:
            guild = ctx_or_interaction.guild
            mod = ctx_or_interaction.author

        reason = reason or "No reason provided"

        # Remove all warnings
        removed_count = await self.remove_all_warnings(user.id, guild.id)
        
        if removed_count == 0:
            msg = f"**{user.display_name}** has no warnings to remove."
            if is_slash:
                await ctx_or_interaction.followup.send(msg, ephemeral=True)
            else:
                await ctx_or_interaction.send(msg)
            return

        # Generate case ID
        now = discord.utils.utcnow()
        case_id = f"FL-{now.strftime('%y%m')}-{hex(int(now.timestamp()))[2:][-4:].upper()}"

        # DM the user
        try:
            warning_text = "warning has" if removed_count == 1 else "warnings have"
            dm_embed = discord.Embed(
                title="<:Cho_Check:1456715827213504593> Chobot Notification",
                description=f"{removed_count} {warning_text} been removed from your account in **{guild.name}**.",
                color=COLOR_SUCCESS,
                timestamp=discord.utils.utcnow()
            )
            dm_embed.add_field(name="Reason for Removal", value=reason, inline=False)
            dm_embed.set_footer(text=f"Case ID: {case_id}")
            if guild.icon:
                dm_embed.set_thumbnail(url=guild.icon.url)
            
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass  # DM closed

        # Log to sub-mod channel (green embed similar to Sapphire style)
        log_embed = self._create_unwarn_log(user, mod, reason, case_id, removed_count)
        sub_mod_channel = guild.get_channel(Config.SUB_MOD_CHANNEL_ID)
        
        if sub_mod_channel:
            await sub_mod_channel.send(content=user.mention, embed=log_embed)
            msg = f"Case `{case_id}`: Removed {removed_count} warning(s), logged in {sub_mod_channel.mention}"
        else:
            msg = f"Removed {removed_count} warning(s) (Case `{case_id}`), but log channel is missing."

        if is_slash:
            await ctx_or_interaction.followup.send(msg, ephemeral=True)
        else:
            await ctx_or_interaction.send(msg)

    def _create_unwarn_log(self, member: discord.Member, mod: discord.Member, reason: str, case_id: str, removed_count: int):
        """Creates a green log embed for unwarn action.
        
        Args:
            member: The member who was unwarned
            mod: The moderator who performed the unwarn
            reason: Reason for the unwarn
            case_id: The case ID for this action
            removed_count: Number of warnings removed
        """
        now = discord.utils.utcnow()
        mod_role_name = mod.top_role.name if hasattr(mod, 'top_role') and mod.top_role else "Moderator"
        
        desc_lines = [
            f"> **{_format_user_for_embed(member)}** has been unwarned!",
            f"> **Reason:** {reason}",
            f"> **Warnings Removed:** {removed_count}",
            f"> **Remaining Count:** 0",
            f"> **Responsible:** {_format_user_for_embed(mod)} ({mod_role_name})",
        ]
        
        embed = discord.Embed(
            title=f"**Unwarned Case ID: {case_id}**",
            description="\n".join(desc_lines),
            color=COLOR_SUCCESS,
            timestamp=now
        )
        embed.set_thumbnail(url="https://i.ibb.co/HXyRH3R/2668-Siren.gif")
        embed.set_footer(text=f"Mod: {mod.display_name}", icon_url=mod.display_avatar.url)
        return embed

    @commands.hybrid_command(name="warnings", aliases=["warnlist"])
    @app_commands.describe(user="The user to check", days="Number of days to look back (default: 30)")
    @commands.has_permissions(manage_messages=True)
    async def warnings(self, ctx, user: discord.Member, days: int = 30):
        """List recent warnings for a user."""
        is_slash = ctx.interaction is not None
        await self._warnings_internal(ctx.interaction if is_slash else ctx, user, days, is_slash=is_slash)

    async def _warnings_internal(self, ctx_or_interaction, user: discord.Member, days: int = 30, is_slash: bool = True):
        """Internal method for listing warnings."""
        # Handle both slash and prefix commands
        if is_slash:
            await ctx_or_interaction.response.defer(ephemeral=True)
            guild = ctx_or_interaction.guild
        else:
            guild = ctx_or_interaction.guild

        # Get warnings
        warnings = await self.get_warnings(user.id, guild.id, days)
        
        if not warnings:
            msg = f"**{user.display_name}** has no warnings in the last {days} days."
            if is_slash:
                await ctx_or_interaction.followup.send(msg, ephemeral=True)
            else:
                await ctx_or_interaction.send(msg)
            return

        # Build embed
        embed = discord.Embed(
            title=f"Warnings for {user.display_name}",
            description=f"Showing warnings from the last {days} days",
            color=COLOR_WARN,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        for i, warn in enumerate(warnings, 1):
            mod_id = warn['mod_id']
            mod = await self._resolve_member(guild, mod_id)
            mod_text = _format_user_for_embed(mod) if mod else f"ID: {mod_id}"
            
            timestamp = warn['timestamp']
            reason = warn['reason']

            visit_line = ""
            if warn.get('visit_id') and warn.get('visit_ign'):
                visit_ts = warn['visit_ts']
                origin = (warn.get('visit_origin') or '?').title()
                dest = (warn.get('visit_destination') or '?').title()
                visit_line = f"\n🏝️ **Linked Visit:** {warn['visit_ign']} · {origin} → {dest} (<t:{visit_ts}:R>)"

            embed.add_field(
                name=f"#{i} - <t:{timestamp}:R>",
                value=f"**Moderator:** {mod_text}\n**Reason:** {reason}{visit_line}",
                inline=False
            )

        if is_slash:
            await ctx_or_interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await ctx_or_interaction.send(embed=embed)

    @commands.hybrid_command(name="flighthistory", aliases=["fhistory"])
    @app_commands.describe(user="The user to check", days="Number of days to look back (default: 30)")
    @commands.has_permissions(manage_messages=True)
    async def flight_history(self, ctx, user: discord.Member, days: int = 30):
        """View a user's combined island visit and warning history."""
        is_slash = ctx.interaction is not None
        if is_slash:
            await ctx.interaction.response.defer(ephemeral=True)
            guild = ctx.interaction.guild
        else:
            guild = ctx.guild

        visits = await self.get_island_visits(user.id, guild.id, days)
        warnings = await self.get_warnings(user.id, guild.id, days)

        embed = discord.Embed(
            title=f"Flight History — {user.display_name}",
            description=f"Showing the last **{days}** days",
            color=COLOR_INVESTIGATION,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        # --- Island Visits ---
        if visits:
            lines = []
            for v in visits[:MAX_HISTORY_ENTRIES]:
                status_icon = "✅" if v['authorized'] else "🔴"
                dest = (v.get('destination') or '?').title()
                lines.append(f"{status_icon} **{dest}** (<t:{v['timestamp']}:R>)")
            embed.add_field(
                name=f"✈️ Island Visits ({len(visits)} in {days}d)",
                value="\n".join(lines) + (f"\n…and {len(visits) - MAX_HISTORY_ENTRIES} more" if len(visits) > MAX_HISTORY_ENTRIES else ""),
                inline=False
            )
        else:
            embed.add_field(name="✈️ Island Visits", value=f"No visits recorded in the last {days} days.", inline=False)

        # --- Warnings ---
        if warnings:
            lines = []
            for w in warnings[:MAX_HISTORY_ENTRIES]:
                mod = await self._resolve_member(guild, w['mod_id'])
                mod_text = mod.display_name if mod else f"ID: {w['mod_id']}"
                visit_tag = f" · visit #{w['visit_id']}" if w.get('visit_id') else ""
                lines.append(f"⚠️ <t:{w['timestamp']}:R> by **{mod_text}**{visit_tag}")
            embed.add_field(
                name=f"⚠️ Warnings ({len(warnings)} in {days}d)",
                value="\n".join(lines) + (f"\n…and {len(warnings) - MAX_HISTORY_ENTRIES} more" if len(warnings) > MAX_HISTORY_ENTRIES else ""),
                inline=False
            )
        else:
            embed.add_field(name="⚠️ Warnings", value=f"No warnings recorded in the last {days} days.", inline=False)

        if is_slash:
            await ctx.interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="profile")
    @app_commands.describe(user="The user to check")
    @commands.has_permissions(manage_messages=True)
    async def profile(self, ctx, user: discord.Member):
        """View a user's trust profile and timeline."""
        is_slash = ctx.interaction is not None
        if is_slash:
            await ctx.interaction.response.defer(ephemeral=True)
        
        user_id = str(user.id)
        guild_id = str(ctx.guild.id)
        
        async with connect_async_db() as db:
            params = [user_id, guild_id]
            guild_clause = " AND guild_id = ?"
            
            visit_summary_cur = await db.execute(
                "SELECT COUNT(*) AS total_visits, "
                "SUM(CASE WHEN authorized = 1 THEN 1 ELSE 0 END) AS authorized_visits, "
                "SUM(CASE WHEN authorized = 0 THEN 1 ELSE 0 END) AS unauthorized_visits, "
                "MAX(timestamp) AS last_visit_at "
                f"FROM island_visits WHERE user_id = ?{guild_clause}",
                params,
            )
            visit_summary = await visit_summary_cur.fetchone()
            
            warning_summary_cur = await db.execute(
                "SELECT COUNT(*) AS total_actions, "
                "SUM(CASE WHEN UPPER(action_type) = 'WARN' THEN 1 ELSE 0 END) AS warnings, "
                "SUM(CASE WHEN UPPER(action_type) = 'KICK' THEN 1 ELSE 0 END) AS kicks, "
                "SUM(CASE WHEN UPPER(action_type) = 'BAN' THEN 1 ELSE 0 END) AS bans, "
                "MAX(timestamp) AS last_action_at "
                f"FROM warnings WHERE user_id = ?{guild_clause}",
                params,
            )
            warning_summary = await warning_summary_cur.fetchone()
            
            recent_visits_cur = await db.execute(
                "SELECT ign, destination, authorized, timestamp "
                f"FROM island_visits WHERE user_id = ?{guild_clause} "
                "ORDER BY timestamp DESC LIMIT 30",
                params,
            )
            recent_visits = await recent_visits_cur.fetchall()
            
            recent_actions_cur = await db.execute(
                "SELECT action_type, reason, mod_id, timestamp "
                f"FROM warnings WHERE user_id = ?{guild_clause} "
                "ORDER BY timestamp DESC LIMIT 30",
                params,
            )
            recent_actions = await recent_actions_cur.fetchall()
            
            dodo_reveals_cur = await db.execute(
                "SELECT island_clean, message_url, username, nickname, created_at "
                "FROM dodo_reveal_messages WHERE user_id = ? ORDER BY created_at DESC LIMIT 30",
                (user_id,),
            )
            dodo_reveals = await dodo_reveals_cur.fetchall()
            
            identity_events_cur = await db.execute(
                "SELECT event_type, old_display_name, new_display_name, created_at "
                f"FROM member_identity_events WHERE user_id = ?{guild_clause} "
                "ORDER BY created_at DESC LIMIT 30",
                params,
            )
            identity_events = await identity_events_cur.fetchall()
            
            latest_auth_cur = await db.execute(
                "SELECT MAX(timestamp) AS authorized_at "
                f"FROM island_visits WHERE user_id = ?{guild_clause} AND authorized = 1",
                params,
            )
            latest_authorized_visit = await latest_auth_cur.fetchone()
            
            known_igns_cur = await db.execute(
                "SELECT ign, COUNT(*) AS visit_count, MAX(timestamp) AS last_seen_at "
                f"FROM island_visits WHERE user_id = ?{guild_clause} "
                "GROUP BY ign ORDER BY visit_count DESC, last_seen_at DESC LIMIT 10",
                params,
            )
            known_igns = await known_igns_cur.fetchall()
            
        def _get_val(row, key, default=0):
            if not row: return default
            if isinstance(row, dict):
                return row.get(key) or default
            elif hasattr(row, 'keys'):
                return row[key] if key in row.keys() and row[key] is not None else default
            return default

        total_visits = int(_get_val(visit_summary, "total_visits", 0))
        total_actions = int(_get_val(warning_summary, "total_actions", 0))
        warnings_count = int(_get_val(warning_summary, "warnings", 0))
        kicks_count = int(_get_val(warning_summary, "kicks", 0))
        bans_count = int(_get_val(warning_summary, "bans", 0))
        unauthorized_count = int(_get_val(visit_summary, "unauthorized_visits", 0))
        
        risk_score = min(
            100,
            warnings_count * 20
            + kicks_count * 35
            + bans_count * 60
            + unauthorized_count * 5,
        )
        
        risk_flags = []
        if warnings_count >= 2:
            risk_flags.append("repeat_warning")
        if kicks_count > 0:
            risk_flags.append("has_kick_action")
        if bans_count > 0:
            risk_flags.append("has_ban_action")
        if unauthorized_count > 0:
            risk_flags.append("unauthorized_visit_history")
            
        latest_authorized_at = int(_get_val(latest_authorized_visit, "authorized_at", 0))
        actionable_identity_events = [
            row for row in identity_events
            if int(_get_val(row, "created_at", 0)) > latest_authorized_at
        ]
        
        if actionable_identity_events:
            risk_flags.append("recent_identity_activity")
            
        if bans_count or risk_score >= 80:
            trust_state = "restricted"
        elif kicks_count or risk_score >= 45:
            trust_state = "watch"
        elif warnings_count or unauthorized_count:
            trust_state = "warned"
        elif total_visits >= 5:
            trust_state = "trusted"
        else:
            trust_state = "new"
            
        timeline = []
        for row in recent_visits:
            timeline.append({
                "type": "visit",
                "label": "Authorized visit" if _get_val(row, "authorized") else "Unknown visit",
                "title": f"{_get_val(row, 'ign')} visited {_get_val(row, 'destination')}",
                "timestamp": f"<t:{_get_val(row, 'timestamp')}:R>",
                "timestamp_raw": _get_val(row, "timestamp"),
                "severity": "info" if _get_val(row, "authorized") else "warning",
                "payload": {
                    "ign": _get_val(row, "ign"),
                    "destination": _get_val(row, "destination"),
                    "authorized": bool(_get_val(row, "authorized")),
                },
            })
        for row in recent_actions:
            action = (_get_val(row, "action_type") or "WARN").upper()
            timeline.append({
                "type": "moderation",
                "label": action,
                "title": _get_val(row, "reason") or action,
                "timestamp": f"<t:{_get_val(row, 'timestamp')}:R>",
                "timestamp_raw": _get_val(row, "timestamp"),
                "severity": "critical" if action == "BAN" else "warning" if action in {"WARN", "KICK"} else "attention",
                "payload": {
                    "mod_id": _get_val(row, "mod_id"),
                    "reason": _get_val(row, "reason"),
                    "action_type": action,
                },
            })
        for row in dodo_reveals:
            row_dict = {}
            if hasattr(row, 'keys'):
                for k in row.keys(): row_dict[k] = row[k]
            else:
                row_dict = dict(row)
                
            timeline.append({
                "type": "dodo_reveal",
                "label": "Dodo reveal",
                "title": f"Revealed {_get_val(row, 'island_clean')}",
                "timestamp": f"<t:{_get_val(row, 'created_at')}:R>",
                "timestamp_raw": _get_val(row, "created_at"),
                "severity": "info",
                "payload": row_dict,
            })
        for row in identity_events:
            created_at = _get_val(row, "created_at")
            cleared = bool(latest_authorized_at and int(created_at or 0) <= latest_authorized_at)
            
            old_name = _get_val(row, 'old_display_name') or 'Unknown'
            new_name = _get_val(row, 'new_display_name') or 'Unknown'
            
            row_dict = {}
            if hasattr(row, 'keys'):
                for k in row.keys(): row_dict[k] = row[k]
            else:
                row_dict = dict(row)
                
            payload = dict(row_dict)
            payload["cleared_by_authorization"] = cleared
            payload["cleared_authorized_at"] = latest_authorized_at if cleared else None
            timeline.append({
                "type": "identity",
                "label": _get_val(row, "event_type"),
                "title": f"{old_name} -> {new_name}",
                "timestamp": f"<t:{created_at}:R>",
                "timestamp_raw": created_at,
                "severity": "info" if cleared else "attention",
                "payload": payload,
            })
            
        timeline.sort(key=lambda item: int(item.get("timestamp_raw") or 0), reverse=True)
        
        # Build Base Embed
        embed_color = 0x2ECC71 # Green
        if trust_state == "restricted": embed_color = 0x992D22 # Red
        elif trust_state == "watch": embed_color = 0xE67E22 # Orange
        elif trust_state == "warned": embed_color = 0xF1C40F # Yellow
        elif trust_state == "new": embed_color = 0x3498DB # Blue

        embed = discord.Embed(
            title=f"Trust Profile — {user.display_name}",
            description=f"Status: **{trust_state.replace('_', ' ').title()}**",
            color=embed_color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        # Summary Fields
        embed.add_field(name="Visits", value=f"Total: {total_visits}\nAuth: {int(_get_val(visit_summary, 'authorized_visits', 0))}\nUnauth: {unauthorized_count}", inline=True)
        embed.add_field(name="Moderation", value=f"Total: {total_actions}\nWarn/Kick/Ban: {warnings_count}/{kicks_count}/{bans_count}", inline=True)
        
        flags_text = ", ".join(f"`{f}`" for f in risk_flags) if risk_flags else "None"
        embed.add_field(name="Risk Score", value=f"Score: **{risk_score}/100**\nFlags: {flags_text}", inline=False)
        
        if known_igns:
            ign_text = ", ".join(f"`{_get_val(r, 'ign')}` ({_get_val(r, 'visit_count')})" for r in known_igns)
            if len(ign_text) > 1024:
                ign_text = ign_text[:1021] + "..."
            embed.add_field(name="Known IGNs", value=ign_text, inline=False)

        view = ProfileTimelineView(timeline, embed, items_per_page=5)
        
        if is_slash:
            await ctx.interaction.followup.send(embed=view._build_embed(), view=view, ephemeral=True)
        else:
            await ctx.send(embed=view._build_embed(), view=view)

async def setup(bot):
    await bot.add_cog(FlightLoggerCog(bot))
    await bot.add_cog(FreeFlightCog(bot))


# ===========================================================================
# FREE ISLAND FLIGHT COG
# A lightweight listener for the free-island flight channel.
# Records visits to the database with island_type='free'.
# Does NOT post any alerts or embeds to Discord — website tracking only.
# ===========================================================================

class FreeFlightCog(commands.Cog, name="FreeFlightLogger"):
    """Silently records free-island flight arrivals into island_visits."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await init_db()

    @commands.Cog.listener()
    async def on_message(self, message):
        listen_id = Config.FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID
        if not listen_id:
            return
        if message.author == self.bot.user or message.channel.id != listen_id:
            return
        match = JOIN_PATTERN.search(message.content)
        if not match:
            return

        ign_raw    = match.group(1).strip()
        island_raw = match.group(2).strip()
        dest_raw   = match.group(3).strip()
        visit_ts   = int(message.created_at.timestamp())
        guild      = self.bot.get_guild(Config.GUILD_ID)
        guild_id   = guild.id if guild else None

        # Delegate to FlightLoggerCog.record_island_visit to avoid duplicating
        # DB logic; fall back to a direct insert if the cog is not loaded.
        flight_cog = self.bot.get_cog("FlightLoggerCog")
        if flight_cog is not None:
            await flight_cog.record_island_visit(
                ign_raw, island_raw, dest_raw, [], guild_id, visit_ts,
                island_type='free',
            )
        else:
            async with connect_async_db() as db:
                await db.execute(
                    "INSERT INTO island_visits "
                    "(ign, origin_island, destination, user_id, guild_id, authorized, timestamp, island_type) "
                    "VALUES (?, ?, ?, NULL, ?, 1, ?, 'free')",
                    (ign_raw, island_raw, dest_raw, guild_id, visit_ts),
                )
                await db.commit()
        logger.info(f"[FREE-FLIGHT] Recorded visit: {ign_raw} → {dest_raw}")
