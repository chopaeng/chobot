"""
ChoBot Web Dashboard
Mod-only web interface for island management, XLog reports, and analytics.
Access is protected by a secret key (DASHBOARD_SECRET env var).
"""

import json
import os
import re
import csv
import contextlib
import io
import secrets
import logging
import mimetypes
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import wraps

import boto3
from botocore.client import Config as BotocoreConfig
from botocore.exceptions import ClientError, NoCredentialsError

from flask import (
    Blueprint, request, redirect,
    url_for, session, jsonify, abort, g, Response, send_from_directory,
)

from utils.config import Config
from utils.auth_tokens import get_auth_user, revoke_auth_token, update_auth_user
from utils.database import connect_db, get_backend
from utils.discord_http import request as discord_request
from utils.discord_membership import (
    DiscordMembershipUnavailable,
    DiscordNotGuildMember,
    REFRESH_SECONDS,
    STALE_GRACE_SECONDS,
    is_beyond_stale_grace,
    refresh_user_payload,
    should_refresh,
)
from utils.db_migration import (
    backup_sqlite_database,
    dry_run_sqlite_to_mariadb,
    inspect_sqlite_source,
    migrate_sqlite_to_mariadb_detailed,
)
from utils.helpers import clean_text
from utils.ops_status import (
    backup_dir_path,
    build_health_payload,
    get_active_data_manager,
    list_backups,
    update_maintenance_settings,
)
from utils import island_access

logger = logging.getLogger("Dashboard")

# ---------------------------------------------------------------------------
# Blueprint setup
# ---------------------------------------------------------------------------
dashboard = Blueprint(
    "dashboard",
    __name__,
)


@dashboard.app_template_filter("intcomma")
def _intcomma(value):
    """Format a number with thousands comma separators (e.g. 2000 → 2,000)."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value

# Absolute path to the shared SQLite database
_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "chobot.db",
)

ALLOWED_CATEGORIES = ("public", "member", "order")
ALLOWED_THEMES     = ("pink", "teal", "purple", "gold")
ALLOWED_STATUSES   = ("ONLINE", "SUB ONLY", "REFRESHING", "OFFLINE")

# Dodo code value that signals a gate-refresh is in progress
REFRESHING_DODO_CODE = "GETTIN'"

# Display status keys (derived from live fields, not the stored status column)
STATUS_ONLINE     = "ONLINE"
STATUS_REFRESHING = "REFRESHING"
STATUS_OFFLINE    = "OFFLINE"

# Senior Mod role ID used during Discord OAuth login
ADMIN_ROLE_ID = Config.ADMIN_ROLE_ID

# Day-of-week label order (SQLite strftime('%w'): 0=Sunday … 6=Saturday)
_DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Max map upload size: 5 MB
MAX_MAP_SIZE      = 5 * 1024 * 1024
ALLOWED_MAP_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_mariadb_migration_lock = threading.Lock()
_mariadb_migration_last_result: dict | None = None


# ---------------------------------------------------------------------------
# Discord user resolution
# ---------------------------------------------------------------------------
# Cache: maps user_id → (display_name, cache_time)
_discord_user_cache: dict[str, tuple[str, float]] = {}
_discord_user_cache_lock = threading.Lock()
_DISCORD_CACHE_TTL = 3600  # seconds — refresh names after 1 hour

# User-Agent sent with every Discord API request.
# Discord (via Cloudflare) blocks requests that use the default Python-urllib
# User-Agent (error 1010).  The DiscordBot format is the accepted convention.
_DISCORD_USER_AGENT = "DiscordBot (https://github.com/bitress/chobot, 1.0)"

# Discord permission bit for the built-in Administrator privilege.
# Guild members with this bit set bypass role-ID checks and always get
# full admin access to the dashboard.
_ADMINISTRATOR_PERM = 0x8


def _resolve_discord_username(user_id) -> str:
    """Return the display name for a Discord user ID.

    Calls GET /api/v10/users/{id} using the Bot token and caches results for
    up to one hour.  Falls back to the raw ID string on any failure or when
    the token is not configured.
    """
    if not user_id:
        return "—"
    uid = str(user_id)
    with _discord_user_cache_lock:
        cached = _discord_user_cache.get(uid)
        if cached and (time.monotonic() - cached[1]) < _DISCORD_CACHE_TTL:
            return cached[0]
    token = Config.DISCORD_TOKEN
    if not token:
        return uid
    try:
        resp = discord_request(
            f"https://discord.com/api/v10/users/{uid}",
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent":    _DISCORD_USER_AGENT,
            },
            timeout=5,
        )
        data = json.loads(resp.body)
        name = data.get("global_name") or data.get("username") or uid
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            logger.debug("Discord user lookup HTTP 403 for %s (user inaccessible)", uid)
        else:
            logger.warning("Discord user lookup HTTP %s for %s", exc.code, uid)
        name = uid
    except Exception as exc:
        logger.debug("Discord user lookup failed for %s: %s", uid, exc)
        name = uid
    with _discord_user_cache_lock:
        _discord_user_cache[uid] = (name, time.monotonic())
    return name


def _resolve_discord_usernames(user_ids) -> dict[str, str]:
    """Resolve a collection of Discord user IDs to display names in one pass.

    Deduplicates the input so each distinct ID is fetched at most once per
    call.  Returns a mapping of id → display name.
    """
    result: dict[str, str] = {}
    for uid in dict.fromkeys(str(i) for i in user_ids if i):
        result[uid] = _resolve_discord_username(uid)
    return result


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    """Return a synchronous configured database connection."""
    return connect_db()


def init_dashboard_db():
    """Create dashboard-specific tables if they do not already exist."""
    try:
        conn = get_db()

        # Full IslandData-compatible table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS islands (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                type           TEXT NOT NULL DEFAULT '',
                items          TEXT NOT NULL DEFAULT '[]',
                theme          TEXT NOT NULL DEFAULT 'teal',
                cat            TEXT NOT NULL DEFAULT 'public',
                description    TEXT NOT NULL DEFAULT '',
                seasonal       TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT 'OFFLINE',
                visitors       INTEGER NOT NULL DEFAULT 0,
                dodo_code      TEXT,
                map_url        TEXT,
                updated_at     TEXT,
                required_roles TEXT NOT NULL DEFAULT '[]',
                channel_id     TEXT,
                display_name   TEXT,
                is_visible     INTEGER NOT NULL DEFAULT 1
            )
        """)

        # Migrate: add required_roles column if it was created without it
        try:
            conn.execute("ALTER TABLE islands ADD COLUMN required_roles TEXT NOT NULL DEFAULT '[]'")
            conn.commit()
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE islands ADD COLUMN channel_id TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE islands ADD COLUMN display_name TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE islands ADD COLUMN is_visible INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Core bot tables are created by SQLAlchemy/create_all, but older
        # deployments need additive migrations for analytics fields.
        try:
            conn.execute("ALTER TABLE island_visits ADD COLUMN island_type TEXT NOT NULL DEFAULT 'sub'")
            conn.commit()
        except Exception:
            pass  # Column already exists or table is not ready yet

        try:
            conn.execute("ALTER TABLE island_visits ADD COLUMN has_island_access INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except Exception:
            pass  # Column already exists or table is not ready yet

        try:
            conn.execute("ALTER TABLE warnings ADD COLUMN action_type TEXT NOT NULL DEFAULT 'WARN'")
            conn.commit()
        except Exception:
            pass  # Column already exists or table is not ready yet

        # Live island bot presence, written by the Discord bot's monitor loop
        conn.execute("""
            CREATE TABLE IF NOT EXISTS island_bot_status (
                island_id   TEXT PRIMARY KEY,
                island_name TEXT NOT NULL,
                is_online   INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS website_login_events (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           TEXT NOT NULL,
                username          TEXT,
                discord_name      TEXT,
                global_name       TEXT,
                account_name      TEXT,
                nickname          TEXT,
                avatar            TEXT,
                roles             TEXT NOT NULL DEFAULT '[]',
                role_count        INTEGER NOT NULL DEFAULT 0,
                is_admin          INTEGER NOT NULL DEFAULT 0,
                is_mod            INTEGER NOT NULL DEFAULT 0,
                ip_address        TEXT,
                user_agent        TEXT,
                return_to         TEXT,
                discord_message_id TEXT,
                discord_channel_id TEXT,
                discord_guild_id TEXT,
                created_at        TEXT NOT NULL
            )
        """)

        try:
            conn.execute("ALTER TABLE website_login_events ADD COLUMN discord_guild_id TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Legacy table kept for backward compatibility
        conn.execute("""
            CREATE TABLE IF NOT EXISTS island_metadata (
                name       TEXT PRIMARY KEY,
                category   TEXT NOT NULL DEFAULT 'public',
                theme      TEXT NOT NULL DEFAULT 'teal',
                notes      TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_audit_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id TEXT,
                actor_name    TEXT,
                action        TEXT NOT NULL,
                target        TEXT,
                details       TEXT NOT NULL,
                ip_address    TEXT,
                created_at    INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS command_search_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                command          TEXT NOT NULL,
                query            TEXT NOT NULL,
                normalized_query TEXT NOT NULL,
                source           TEXT,
                user_id          TEXT,
                channel_id       TEXT,
                found            INTEGER NOT NULL DEFAULT 0,
                result_count     INTEGER NOT NULL DEFAULT 0,
                created_at       INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_aliases (
                alias      TEXT NOT NULL,
                target     TEXT NOT NULL,
                kind       TEXT NOT NULL DEFAULT 'item',
                created_by TEXT,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (alias, kind)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS dodo_queue (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                island_clean TEXT NOT NULL,
                island_name  TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                username     TEXT,
                status       TEXT NOT NULL DEFAULT 'waiting',
                note         TEXT,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS incident_workflow (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind   TEXT NOT NULL,
                source_id     TEXT NOT NULL,
                title         TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'open',
                severity      TEXT NOT NULL DEFAULT 'attention',
                assigned_to   TEXT,
                note          TEXT,
                actor_user_id TEXT,
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL,
                UNIQUE(source_kind, source_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pocket_bundles (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                description  TEXT,
                category     TEXT NOT NULL DEFAULT 'Popular',
                icon         TEXT NOT NULL DEFAULT 'fa-box-open',
                is_official  INTEGER NOT NULL DEFAULT 0,
                created_by   TEXT,
                order_items  TEXT NOT NULL DEFAULT '[]',
                drop_items   TEXT NOT NULL DEFAULT '[]',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_pockets (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL DEFAULT 'ACNH Pocket',
                order_items  TEXT NOT NULL DEFAULT '[]',
                drop_items   TEXT NOT NULL DEFAULT '[]',
                created_by   TEXT,
                views        INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_bot_queue (
                id                TEXT PRIMARY KEY,
                user_id           TEXT NOT NULL,
                username          TEXT,
                command           TEXT NOT NULL,
                order_type        TEXT NOT NULL DEFAULT 'order',
                status            TEXT NOT NULL DEFAULT 'queued',
                queue_position    INTEGER DEFAULT 1,
                estimated_minutes INTEGER DEFAULT 2,
                dodo_code         TEXT,
                island_name       TEXT DEFAULT 'Sinta',
                message           TEXT,
                created_at        INTEGER NOT NULL,
                updated_at        INTEGER NOT NULL
            )
        """)



        # Community Loadouts table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS community_loadouts (
                id          TEXT PRIMARY KEY,
                short_code  TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                tags        TEXT NOT NULL DEFAULT '[]',
                category    TEXT NOT NULL DEFAULT 'General',
                order_items TEXT NOT NULL DEFAULT '[]',
                drop_items  TEXT NOT NULL DEFAULT '[]',
                user_id     TEXT,
                created_by  TEXT NOT NULL DEFAULT 'Community',
                upvotes     INTEGER NOT NULL DEFAULT 0,
                views       INTEGER NOT NULL DEFAULT 0,
                is_official INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)

        # Upvotes Tracking table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS community_loadout_upvotes (
                loadout_id  TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (loadout_id, user_id)
            )
        """)

        # User Favorite Islands table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_favorite_islands (
                user_id     TEXT NOT NULL,
                island_id   TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (user_id, island_id)
            )
        """)

        # User Saved In-Game Characters table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_saved_characters (
                user_id     TEXT NOT NULL,
                id          TEXT NOT NULL,
                ign         TEXT NOT NULL,
                island_name TEXT NOT NULL,
                title       TEXT,
                icon        TEXT,
                is_default  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (user_id, id)
            )
        """)

        # User Public Passport & Profile Customizer table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_public_passports (
                user_id                   VARCHAR(64) PRIMARY KEY,
                username                  VARCHAR(255) NOT NULL,
                is_public                 INTEGER NOT NULL DEFAULT 0,
                show_character_and_island INTEGER NOT NULL DEFAULT 1,
                pronouns                  VARCHAR(64),
                birth_day                 VARCHAR(16),
                birth_month               VARCHAR(32),
                native_fruit              VARCHAR(32),
                favourite_colour          VARCHAR(32),
                favourite_song            VARCHAR(128),
                country                   VARCHAR(128),
                language                  VARCHAR(64),
                personality               VARCHAR(64),
                hobbies                   VARCHAR(255),
                favourite_shows_films     VARCHAR(255),
                about_you                 TEXT,
                favourite_villagers       TEXT,
                primary_ign               VARCHAR(64),
                primary_island            VARCHAR(64),
                avatar_url                TEXT,
                updated_at                VARCHAR(64) NOT NULL
            )
        """)

        # User Custom Presets table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_custom_presets (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                title       TEXT NOT NULL,
                description TEXT,
                category    TEXT,
                tags        TEXT,
                order_items TEXT,
                drop_items  TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)

        # User Real-Time Online Presence & Community Radar table
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

        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_user_custom_presets_user ON user_custom_presets (user_id, updated_at)")
        except Exception:
            pass

        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_pocket_bundles_cat ON pocket_bundles (category, is_official)")
        except Exception:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_order_bot_queue_user_status ON order_bot_queue (user_id, status, created_at)")
        except Exception:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_community_loadouts_upvotes ON community_loadouts (upvotes, created_at)")
        except Exception:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_loadout_upvotes_user ON community_loadout_upvotes (user_id)")
        except Exception:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_user_fav_islands ON user_favorite_islands (user_id)")
        except Exception:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_user_saved_characters ON user_saved_characters (user_id)")
        except Exception:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_user_public_passports_username ON user_public_passports (username COLLATE NOCASE)")
        except Exception:
            pass

        conn.commit()
        conn.close()

        logger.info("Dashboard DB initialised with pocket bundles, order queue, and favorite islands")
    except Exception as exc:
        logger.warning("Could not initialise dashboard DB: %s", exc)


# ---------------------------------------------------------------------------
# R2 / S3 helpers
# ---------------------------------------------------------------------------
def _get_r2_client():
    """Return a boto3 S3 client pointed at Cloudflare R2, or None if unconfigured."""
    if not (Config.R2_ACCOUNT_ID and Config.R2_ACCESS_KEY_ID and Config.R2_SECRET_ACCESS_KEY):
        return None
    endpoint = f"https://{Config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=Config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=Config.R2_SECRET_ACCESS_KEY,
        config=BotocoreConfig(signature_version="s3v4"),
        region_name="auto",
    )


def _upload_map_to_r2(file_bytes: bytes, content_type: str, island_id: str) -> str:
    """Upload map image bytes to R2 and return the public URL."""
    client = _get_r2_client()
    if client is None:
        raise RuntimeError(
            "R2 is not configured — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, and R2_PUBLIC_URL in .env"
        )
    ext = mimetypes.guess_extension(content_type) or ".png"
    ext = {".jpe": ".jpg", ".jfif": ".jpg"}.get(ext, ext)
    key = f"maps/{island_id}{ext}"

    # Delete any pre-existing map files for this island (different extension)
    existing = client.list_objects_v2(
        Bucket=Config.R2_BUCKET_NAME,
        Prefix=f"maps/{island_id}",
    )
    for obj in existing.get("Contents", []):
        if obj["Key"] != key:
            client.delete_object(Bucket=Config.R2_BUCKET_NAME, Key=obj["Key"])

    client.put_object(
        Bucket=Config.R2_BUCKET_NAME,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
    )
    base = Config.R2_PUBLIC_URL.rstrip("/")
    return f"{base}/{key}"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _check_session():
    if not session.get("mod_logged_in"):
        return False
    user_id = session.get("discord_user_id")
    if not user_id:
        return True
    checked_at = int(session.get("discord_checked_at") or 0)
    if time.time() - checked_at < REFRESH_SECONDS:
        return True
    try:
        refreshed = refresh_user_payload({
            "user_id": user_id,
            "username": session.get("discord_username", ""),
            "avatar": session.get("discord_avatar_url", ""),
            "discord_checked_at": checked_at,
        })
    except DiscordNotGuildMember:
        logger.info("Clearing dashboard OAuth session for user_id=%s: no longer in guild", user_id)
        _clear_dashboard_session()
        return False
    except DiscordMembershipUnavailable as exc:
        logger.warning("Could not refresh dashboard OAuth session for %s: %s", user_id, exc)
        if not checked_at or time.time() - checked_at >= STALE_GRACE_SECONDS:
            _clear_dashboard_session()
            return False
        return True

    if not refreshed.get("is_admin"):
        logger.info("Clearing dashboard OAuth session for user_id=%s: admin access removed", user_id)
        _clear_dashboard_session()
        return False

    session["mod_logged_in"] = True
    session["mod_role"] = "admin"
    session["discord_username"] = refreshed.get("username", "")
    session["discord_avatar_url"] = refreshed.get("avatar", "")
    session["discord_checked_at"] = refreshed.get("discord_checked_at", int(time.time()))
    return True


def _get_session_role():
    """Return the current session role (always 'admin' for authenticated sessions)."""
    return session.get("mod_role", "admin")


def _check_bearer():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and Config.DASHBOARD_SECRET:
        return secrets.compare_digest(auth[len("Bearer "):], Config.DASHBOARD_SECRET)
    return False


def _dashboard_bearer_user() -> dict | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):]
    user = get_auth_user(token)
    if not user:
        return None
    if not should_refresh(user):
        return user
    try:
        refreshed = refresh_user_payload(user)
        update_auth_user(token, refreshed)
        return refreshed
    except DiscordNotGuildMember:
        logger.info("Revoking dashboard bearer token for user_id=%s: no longer in guild", user.get("user_id"))
        revoke_auth_token(token)
        return None
    except DiscordMembershipUnavailable as exc:
        logger.warning("Could not refresh dashboard bearer user %s: %s", user.get("user_id"), exc)
        if is_beyond_stale_grace(user):
            revoke_auth_token(token)
            return None
        return user


def _is_dashboard_mod_user(user: dict | None) -> bool:
    if not user:
        return False
    if user.get("is_admin") or user.get("is_mod"):
        return True
    roles = {str(role) for role in user.get("roles", [])}
    mod_role_ids = {
        str(Config.ADMIN_ROLE_ID or ""),
        str(Config.SENIOR_MOD_ROLE_ID or ""),
        str(Config.BABY_MOD_ROLE_ID or ""),
    } - {"", "0", "None"}
    return bool(roles & mod_role_ids)


def login_required(f):
    """Decorator for web routes — redirects to /dashboard/login if not authenticated."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not _check_session():
            return redirect(url_for("dashboard.login"))
        return f(*args, **kwargs)
    return _decorated


def admin_required(f):
    """Decorator for admin-only web routes — redirects to login if not authenticated,
    or returns 403 Forbidden if authenticated but lacking admin privileges."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not _check_session():
            return redirect(url_for("dashboard.login"))
        if _get_session_role() != "admin":
            abort(403)
        return f(*args, **kwargs)
    return _decorated


def api_auth_required(f):
    """Decorator for JSON API routes; accepts dashboard secret, session, or mod bearer token."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if _check_bearer() or _check_session():
            return f(*args, **kwargs)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401

        user = _dashboard_bearer_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if not _is_dashboard_mod_user(user):
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return _decorated


def _csrf_token() -> str:
    """Return the current CSRF token, creating one for this browser session."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_is_valid() -> bool:
    expected = session.get("csrf_token", "")
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    return bool(expected and provided and secrets.compare_digest(expected, provided))


# ---------------------------------------------------------------------------
# Template context processor — injects current_role into every page
# ---------------------------------------------------------------------------
@dashboard.context_processor
def _inject_user():
    return {
        "current_role":       session.get("mod_role", "admin"),
        "discord_username":   session.get("discord_username", ""),
        "discord_user_id":    session.get("discord_user_id", ""),
        "discord_avatar_url": session.get("discord_avatar_url", ""),
        "oauth_configured":   bool(Config.DISCORD_CLIENT_ID),
        "csrf_token":         _csrf_token(),
    }


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def _read_file(folder_path, filename):
    try:
        with open(os.path.join(folder_path, filename), "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except (FileNotFoundError, IOError, UnicodeDecodeError):
        return None


def _write_file(folder_path, filename, content):
    with open(os.path.join(folder_path, filename), "w", encoding="utf-8") as fh:
        fh.write(content)


def _parse_visitor_value(raw):
    """Normalize the content of Visitors.txt.

    The C# SysBot may write the file as a plain number ("3") or with a label
    ("Visitors: 3").  This strips any leading label so callers always receive
    the bare value ("3", "FULL", etc.).
    """
    if not raw:
        return raw
    cleaned = re.sub(r'(?i)^\s*visitors\s*:\s*', '', raw).strip()
    return cleaned if cleaned else None


def _parse_visitor_list(raw):
    """Parse Visitors.txt content into a (count, names) tuple.

    Handles the multi-line format produced by the C# SysBot::

        The following visitors are on {TownName}:
        #1: PlayerName
        #2: Available slot
        ...

    Also handles the legacy single-value format ("3", "Visitors: 3", "FULL").

    Returns:
        (visitor_count: int, visitor_names: list[str])
    """
    if not raw:
        return 0, []

    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]

    # New multi-line format from C# bot
    if lines and lines[0].lower().startswith("the following visitors are on"):
        names = []
        for line in lines[1:]:
            m = re.match(r'^#\d+:\s*(.+)$', line)
            if m:
                name = m.group(1).strip()
                if name.lower() != "available slot":
                    names.append(name)
        return len(names), names

    # Legacy single-value format
    cleaned = _parse_visitor_value(raw)
    if not cleaned:
        return 0, []
    if cleaned.isdigit():
        return int(cleaned), []
    if cleaned.upper() == "FULL":
        return 7, []
    return 0, []


def _collect_fs_islands():
    """Return a dict keyed by uppercase island name with live filesystem data."""
    result = {}

    def _scan(directory, itype):
        if not directory or not os.path.exists(directory):
            return
        if itype == "Order" and os.path.isdir(directory):
            direct_files = [
                os.path.join(directory, "Dodo.txt"),
                os.path.join(directory, "Visitors.txt"),
                os.path.join(directory, "Villagers.txt"),
            ]
            configured_name = getattr(Config, "ORDER_BOT_ISLAND", None) or os.path.basename(directory)
            basename_matches = clean_text(os.path.basename(directory)) in {clean_text(configured_name), clean_text("SYSBOT-ACNH-ORDERS")}
            if basename_matches or any(os.path.exists(path) for path in direct_files):
                uname = configured_name.upper()
                result[uname] = {
                    "name":        uname,
                    "fs_path":     directory,
                    "fs_type":     itype,
                    "fs_dodo":     _read_file(directory, "Dodo.txt"),
                    "fs_visitors": _parse_visitor_value(_read_file(directory, "Visitors.txt")),
                }
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir():
                    uname = entry.name.upper()
                    result[uname] = {
                        "name":        uname,
                        "fs_path":     entry.path,
                        "fs_type":     itype,
                        "fs_dodo":     _read_file(entry.path, "Dodo.txt"),
                        "fs_visitors": _parse_visitor_value(_read_file(entry.path, "Visitors.txt")),
                    }

    _scan(Config.DIR_FREE, "Free")
    _scan(Config.DIR_VIP,  "VIP")
    _scan(getattr(Config, "DIR_ORDER", None), "Order")
    return result


def _ts_to_str(ts):
    """Convert a Unix timestamp int to a human-readable UTC string."""
    if ts is None:
        return "\u2014"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return str(ts)


def _where_clause(conditions: list) -> str:
    """Build a safe WHERE clause from a list of predefined SQL fragment strings.

    Only hardcoded SQL condition strings (containing '?' placeholders) may be
    passed here — never raw user input.  User-supplied values must be passed
    separately as a params list to the db.execute() call.
    """
    return ("WHERE " + " AND ".join(conditions)) if conditions else ""


def _optional_rows(db, sql: str, params=()) -> list:
    """Return rows for optional feature tables, or [] before that feature has created them."""
    try:
        return db.execute(sql, params).fetchall()
    except Exception as exc:
        if "no such table" in str(exc).lower() or "doesn't exist" in str(exc).lower():
            return []
        raise


def row_to_island_dict(row: dict) -> dict:
    """Decode JSON columns and return a plain dict."""
    try:
        row["items"] = json.loads(row.get("items") or "[]")
    except (ValueError, TypeError):
        row["items"] = []
    try:
        row["required_roles"] = json.loads(row.get("required_roles") or "[]")
    except (ValueError, TypeError):
        row["required_roles"] = []
    row["is_visible"] = bool(row.get("is_visible", 1))
    return row


def _load_bot_status_map(conn) -> dict:
    """Return a dict of island_id → bool (is_online) from island_bot_status."""
    try:
        rows = conn.execute("SELECT island_id, is_online FROM island_bot_status").fetchall()
        return {r["island_id"]: bool(r["is_online"]) for r in rows}
    except Exception:
        return {}


def _effective_status(isl: dict) -> str:
    """Derive display status from live fields, ignoring the stored status key.

    Rules (in priority order):
      1. dodo_code == REFRESHING_DODO_CODE  → STATUS_REFRESHING
      2. discord_bot_online                  → STATUS_ONLINE
      3. otherwise                           → STATUS_OFFLINE
    """
    if (isl.get("dodo_code") or "").strip().upper() == REFRESHING_DODO_CODE:
        return STATUS_REFRESHING
    if isl.get("discord_bot_online"):
        return STATUS_ONLINE
    return STATUS_OFFLINE


# Backward-compatible alias for internal callers
_row_to_island_dict = row_to_island_dict

# Canonical fields exposed by the public API (in consistent order)
_API_ISLAND_FIELDS = (
    "cat", "description", "discord_bot_online", "dodo_code", "id", "items",
    "map_url", "name", "display_name", "is_visible", "required_roles", "seasonal", "status", "theme",
    "type", "updated_at", "visitors", "channel_id", "access_source",
)


def _island_api_dict(isl: dict) -> dict:
    """Return a clean API-facing dict containing only canonical island fields."""
    return {field: isl.get(field) for field in _API_ISLAND_FIELDS}


def _json_bool(data: dict, key: str, fallback: bool = True) -> bool:
    """Read a JSON boolean-ish value without treating omitted fields as false."""
    if key not in data:
        return fallback
    value = data.get(key)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _island_access_status(isl: dict, *, force_refresh: bool = False) -> dict:
    """Return dashboard-facing access status for one island."""
    info = island_access.resolved_island_required_roles(
        isl.get("name"),
        isl.get("cat"),
        isl.get("required_roles") or [],
        isl.get("type"),
        isl.get("channel_id"),
        force_refresh=force_refresh,
    )
    role_names = island_access.get_guild_role_names()
    is_member = island_access.is_member_island(isl.get("cat"), isl.get("type"))
    warnings = []
    if is_member and not info.channel_id:
        warnings.append("missing_channel_id")
    if is_member and info.role_count == 0:
        warnings.append("no_view_roles")
    if is_member and info.access_source != "discord_channel":
        warnings.append("using_database_fallback")
    return {
        "id": isl.get("id"),
        "name": isl.get("name"),
        "cat": isl.get("cat"),
        "type": isl.get("type"),
        "is_member": is_member,
        "channel_id": info.channel_id,
        "access_source": info.access_source,
        "required_roles": [island_access.role_payload(role_id, role_names) for role_id in info.required_roles],
        "required_role_ids": info.required_roles,
        "role_count": info.role_count,
        "warnings": warnings,
        "ok": not warnings,
    }


def _event_severity(kind: str) -> str:
    if kind in {"ban", "repeat_offender"}:
        return "critical"
    if kind in {"unknown_traveler", "no_island_access", "recent_nickname_change", "dodo_without_flight"}:
        return "warning"
    if kind in {"active_warning", "investigation"}:
        return "attention"
    return "info"


def _incident_source_id(kind: str, payload: dict) -> str:
    """Build a stable workflow key for a derived incident signal."""
    for key in ("id", "visit_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    user_id = payload.get("user_id") or "unknown"
    timestamp = payload.get("timestamp") or payload.get("created_at") or payload.get("last_warning_at") or ""
    return f"{user_id}:{timestamp}"


def _load_incident_workflow_map(db, source_pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    if not source_pairs:
        return {}
    result = {}
    try:
        for kind, source_id in source_pairs:
            row = db.execute(
                "SELECT * FROM incident_workflow WHERE source_kind = ? AND source_id = ?",
                (kind, source_id),
            ).fetchone()
            if row:
                result[(kind, source_id)] = dict(row)
    except Exception:
        return {}
    return result


def _incident_event(kind: str, title: str, timestamp, user_id, payload: dict) -> dict:
    payload = dict(payload)
    source_id = _incident_source_id(kind, payload)
    return {
        "kind": kind,
        "source_id": source_id,
        "severity": _event_severity(kind),
        "title": title,
        "timestamp": _ts_to_str(timestamp),
        "timestamp_raw": timestamp,
        "user_id": user_id,
        "trust_profile_url": url_for("dashboard.trust", user_id=user_id) if user_id else None,
        "payload": payload,
    }


def _latest_authorization_after_identity_event(db, user_id, guild_id, created_at):
    if user_id is None or created_at is None:
        return None
    params = [user_id, int(created_at)]
    guild_clause = ""
    if guild_id is not None:
        guild_clause = " AND guild_id = ?"
        params.append(guild_id)
    row = db.execute(
        "SELECT MAX(timestamp) AS authorized_at "
        "FROM island_visits "
        "WHERE user_id = ? AND authorized = 1 AND user_id IS NOT NULL "
        f"AND timestamp >= ?{guild_clause}",
        params,
    ).fetchone()
    if not row:
        return None
    return row["authorized_at"]


def _identity_event_cleared_by_authorization(db, row) -> tuple[bool, int | None]:
    authorized_at = _latest_authorization_after_identity_event(
        db,
        row["user_id"],
        row["guild_id"] if "guild_id" in row.keys() else None,
        row["created_at"],
    )
    return authorized_at is not None, authorized_at


def _recent_incident_payload(limit: int = 25) -> dict:
    """Collect moderation signals for the incident center."""
    now = int(time.time())
    db = get_db()
    try:
        unknown = db.execute(
            "SELECT id, ign, destination, user_id, timestamp, island_type, has_island_access "
            "FROM island_visits WHERE authorized = 0 ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        warnings = db.execute(
            "SELECT w.*, iv.ign, iv.destination "
            "FROM warnings w LEFT JOIN island_visits iv ON w.visit_id = iv.id "
            "WHERE w.timestamp IS NOT NULL AND w.timestamp >= ? "
            "ORDER BY w.timestamp DESC LIMIT ?",
            (now - 3 * 86400, limit),
        ).fetchall()
        dodo_reveals = _optional_rows(
            db,
            "SELECT * FROM dodo_reveal_messages ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        identity_events = _optional_rows(
            db,
            "SELECT * FROM member_identity_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        queue = db.execute(
            "SELECT * FROM dodo_queue WHERE status IN ('waiting', 'called', 'investigating') "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        repeat_offenders = db.execute(
            "SELECT user_id, COUNT(*) AS warning_count, MAX(timestamp) AS last_warning_at "
            "FROM warnings WHERE user_id IS NOT NULL AND timestamp >= ? "
            "GROUP BY user_id HAVING COUNT(*) >= 2 "
            "ORDER BY warning_count DESC, last_warning_at DESC LIMIT ?",
            (now - 30 * 86400, limit),
        ).fetchall()
        workflow_rows = db.execute(
            "SELECT * FROM incident_workflow WHERE status IN ('open', 'investigating', 'watching') "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception as exc:
        db.close()
        return {"ok": False, "error": str(exc)}

    events = []
    for row in unknown:
        kind = "no_island_access" if row["has_island_access"] == 0 else "unknown_traveler"
        events.append(_incident_event(
            kind,
            f"{row['ign']} visited {row['destination']}",
            row["timestamp"],
            row["user_id"],
            dict(row),
        ))
    for row in warnings:
        action = (row["action_type"] or "WARN").lower()
        kind = "investigation" if action == "note" else "active_warning"
        events.append(_incident_event(
            kind,
            f"{row['action_type']} for {row['ign'] or row['user_id'] or 'unknown user'}",
            row["timestamp"],
            row["user_id"],
            dict(row),
        ))
    for row in dodo_reveals:
        events.append(_incident_event(
            "dodo_reveal",
            f"Dodo revealed for {row['island_clean']}",
            row["created_at"],
            row["user_id"],
            dict(row),
        ))
    for row in queue:
        events.append(_incident_event(
            "dodo_queue",
            f"{row['username'] or row['user_id']} is queued for {row['island_name']}",
            row["created_at"],
            row["user_id"],
            dict(row),
        ))
    actionable_identity_events = []
    suppressed_identity_events = []
    for row in identity_events:
        cleared, authorized_at = _identity_event_cleared_by_authorization(db, row)
        payload = dict(row)
        payload["cleared_by_authorization"] = cleared
        payload["cleared_authorized_at"] = authorized_at
        if cleared:
            suppressed_identity_events.append(payload)
            continue
        actionable_identity_events.append(payload)
        events.append(_incident_event(
            "recent_nickname_change",
            f"Identity event for {row['user_id']}",
            row["created_at"],
            row["user_id"],
            payload,
        ))
    for row in repeat_offenders:
        events.append(_incident_event(
            "repeat_offender",
            f"{row['user_id']} has {row['warning_count']} recent actions",
            row["last_warning_at"],
            row["user_id"],
            dict(row),
        ))

    source_pairs = [(event["kind"], event["source_id"]) for event in events]
    workflow_map = _load_incident_workflow_map(db, source_pairs)
    db.close()

    for event in events:
        workflow = workflow_map.get((event["kind"], event["source_id"]))
        if workflow:
            event["workflow"] = workflow
            event["status"] = workflow.get("status") or "open"
            event["assigned_to"] = workflow.get("assigned_to") or ""
            event["note"] = workflow.get("note") or ""
            event["severity"] = workflow.get("severity") or event["severity"]
        else:
            event["workflow"] = None
            event["status"] = "new"
            event["assigned_to"] = ""
            event["note"] = ""

    workflow_only = []
    for row in workflow_rows:
        key = (row["source_kind"], row["source_id"])
        if key not in workflow_map:
            workflow_only.append({
                "kind": row["source_kind"],
                "source_id": row["source_id"],
                "severity": row["severity"],
                "title": row["title"],
                "timestamp": _ts_to_str(row["updated_at"]),
                "timestamp_raw": row["updated_at"],
                "user_id": None,
                "trust_profile_url": None,
                "payload": {},
                "workflow": dict(row),
                "status": row["status"],
                "assigned_to": row["assigned_to"] or "",
                "note": row["note"] or "",
            })
    events.extend(workflow_only)

    return {
        "ok": True,
        "summary": {
            "unknown_travelers": len(unknown),
            "active_warnings": len(warnings),
            "recent_dodo_reveals": len(dodo_reveals),
            "recent_identity_events": len(actionable_identity_events),
            "suppressed_identity_events": len(suppressed_identity_events),
            "open_queue_entries": len(queue),
            "repeat_offenders": len(repeat_offenders),
            "workflow_open": sum(1 for row in workflow_rows if row["status"] in {"open", "investigating", "watching"}),
        },
        "events": sorted(events, key=lambda item: item.get("timestamp_raw") or 0, reverse=True)[:limit],
        "unknown_travelers": [dict(row) for row in unknown],
        "active_warnings": [dict(row) for row in warnings],
        "recent_dodo_reveals": [dict(row) for row in dodo_reveals],
        "recent_identity_events": actionable_identity_events,
        "suppressed_identity_events": suppressed_identity_events,
        "open_queue": [dict(row) for row in queue],
        "repeat_offenders": [dict(row) for row in repeat_offenders],
        "workflow": [dict(row) for row in workflow_rows],
    }


def _command_analytics_payload(days: int = 30, limit: int = 15) -> dict:
    cutoff = int(time.time()) - max(days, 1) * 86400
    db = get_db()
    try:
        top_queries = db.execute(
            "SELECT command, normalized_query, COUNT(*) AS count, "
            "SUM(CASE WHEN found = 0 THEN 1 ELSE 0 END) AS failed_count "
            "FROM command_search_events WHERE created_at >= ? "
            "GROUP BY command, normalized_query ORDER BY count DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        failed = db.execute(
            "SELECT command, normalized_query, COUNT(*) AS count "
            "FROM command_search_events WHERE created_at >= ? AND found = 0 "
            "GROUP BY command, normalized_query ORDER BY count DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        channels = db.execute(
            "SELECT channel_id, COUNT(*) AS count "
            "FROM command_search_events WHERE created_at >= ? AND channel_id IS NOT NULL AND channel_id != '' "
            "GROUP BY channel_id ORDER BY count DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        totals = db.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN found = 0 THEN 1 ELSE 0 END) AS failed "
            "FROM command_search_events WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()
        busiest_islands = db.execute(
            "SELECT destination, COUNT(*) AS count FROM island_visits WHERE timestamp >= ? "
            "GROUP BY destination ORDER BY count DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        peak_hours = db.execute(
            "SELECT CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS hour, COUNT(*) AS count "
            "FROM island_visits WHERE timestamp >= ? GROUP BY hour ORDER BY count DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()

    total = int(totals["total"] or 0) if totals else 0
    failed_total = int(totals["failed"] or 0) if totals else 0
    return {
        "ok": True,
        "days": days,
        "summary": {
            "total_searches": total,
            "failed_searches": failed_total,
            "success_rate_pct": round((total - failed_total) * 100 / total, 1) if total else None,
        },
        "top_queries": [dict(row) for row in top_queries],
        "failed_queries": [dict(row) for row in failed],
        "top_channels": [dict(row) for row in channels],
        "busiest_islands": [dict(row) for row in busiest_islands],
        "peak_hours": [dict(row) for row in peak_hours],
    }


def _load_dashboard_islands() -> list[dict]:
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        return _merge_dashboard_fs_islands([_row_to_island_dict(dict(r)) for r in rows])
    finally:
        db.close()


def _fs_island_stub(fs: dict) -> dict:
    """Build dashboard metadata for an island folder that has no DB row yet."""
    name = fs.get("name", "")
    fs_type = fs.get("fs_type") or ""
    cat = "member" if fs_type == "VIP" else "order" if fs_type == "Order" else "public"
    island_type = "Order Bot" if fs_type == "Order" else fs_type
    channel_id = str(Config.ORDER_BOT_CHANNEL_ID or "") if fs_type == "Order" else None
    return {
        "id": name.lower(),
        "name": name,
        "display_name": None,
        "is_visible": True,
        "type": island_type,
        "items": [],
        "theme": "teal",
        "cat": cat,
        "description": "",
        "seasonal": "Year-Round" if fs_type == "Order" else "",
        "status": "OFFLINE",
        "visitors": 0,
        "dodo_code": None,
        "map_url": None,
        "updated_at": None,
        "required_roles": [],
        "channel_id": channel_id,
    }


def _merge_dashboard_fs_islands(db_islands: list[dict]) -> list[dict]:
    """Return DB islands plus filesystem-only islands such as Sinta/order bot."""
    fs_map = _collect_fs_islands()
    merged = []
    seen = set()

    for isl in db_islands:
        uname = str(isl.get("name") or isl.get("id") or "").upper()
        if uname:
            seen.add(uname)
        merged.append(_merge_island(isl, fs_map.get(uname)))

    for uname, fs in fs_map.items():
        if uname in seen:
            continue
        merged.append(_merge_island(_fs_island_stub(fs), fs))

    merged.sort(key=lambda item: str(item.get("name") or item.get("id") or ""))
    return merged


def _find_island_filesystem_meta(island_id: str, display_name: str | None = None) -> dict:
    """Return filesystem metadata used by the legacy island detail page."""
    upper = (display_name or island_id).upper()
    fs_path = fs_type = None
    for directory, itype in [(Config.DIR_FREE, "Free"), (Config.DIR_VIP, "VIP"), (getattr(Config, "DIR_ORDER", None), "Order")]:
        if not directory:
            continue
        if itype == "Order" and os.path.isdir(directory):
            order_key = (getattr(Config, "ORDER_BOT_ISLAND", None) or os.path.basename(directory)).upper()
            if upper == order_key or island_id.lower() == order_key.lower():
                basename_matches = clean_text(os.path.basename(directory)) in {clean_text(order_key), clean_text("SYSBOT-ACNH-ORDERS")}
                has_order_files = any(os.path.exists(os.path.join(directory, fname)) for fname in ("Dodo.txt", "Visitors.txt", "Villagers.txt"))
                if basename_matches or has_order_files:
                    fs_path, fs_type = directory, itype
                    break
        for candidate_name in [upper, island_id]:
            candidate = os.path.join(directory, candidate_name)
            if os.path.isdir(candidate):
                fs_path, fs_type = candidate, itype
                break
        if fs_path:
            break
    return {
        "fs_path": fs_path,
        "fs_type": fs_type,
        "fs_dodo": _read_file(fs_path, "Dodo.txt") if fs_path else None,
        "fs_visitors": _parse_visitor_value(_read_file(fs_path, "Visitors.txt")) if fs_path else None,
    }


def _island_sparkline_7d(destination: str) -> list[dict]:
    """Return per-island visit counts for the last seven days."""
    db_sp = get_db()
    try:
        return [
            dict(r) for r in db_sp.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                "WHERE LOWER(destination) = LOWER(?) "
                "AND timestamp > strftime('%s','now','-7 days') "
                "GROUP BY day ORDER BY day",
                (destination,),
            ).fetchall()
        ]
    except Exception:
        return []
    finally:
        db_sp.close()


def _island_detail_api_dict(isl: dict) -> dict:
    """Return the React dashboard island editor payload."""
    payload = _island_api_dict(isl)
    payload.update(_find_island_filesystem_meta(isl.get("id", ""), isl.get("name")))
    payload.update({
        "allowed_categories": list(ALLOWED_CATEGORIES),
        "allowed_themes": list(ALLOWED_THEMES),
        "allowed_statuses": list(ALLOWED_STATUSES),
        "r2_configured": bool(
            Config.R2_ACCOUNT_ID
            and Config.R2_ACCESS_KEY_ID
            and Config.R2_SECRET_ACCESS_KEY
            and Config.R2_BUCKET_NAME
            and Config.R2_PUBLIC_URL
        ),
        "sparkline_7d": _island_sparkline_7d(isl.get("name") or isl.get("id", "")),
    })
    return payload


def _merge_island(db_row: dict, fs: dict | None) -> dict:
    """Overlay live filesystem data (Dodo / Visitors) onto a DB island record."""
    db_row["fs_dodo"]     = fs["fs_dodo"]     if fs else None
    db_row["fs_visitors"] = fs["fs_visitors"] if fs else None
    db_row["fs_type"]     = fs["fs_type"]     if fs else None
    db_row["fs_path"]     = fs["fs_path"]     if fs else None
    return db_row


# ===========================================================================
# WEB ROUTES
# ===========================================================================

# ---------------------------------------------------------------------------
# Domain restriction — dashboard is served from console.chopaeng.com,
# and localhost for local development.
# ---------------------------------------------------------------------------
_ALLOWED_DASHBOARD_HOSTS = {"console.chopaeng.com", "localhost", "127.0.0.1"}


def _dashboard_notice(message: str, category: str = "info") -> None:
    """Log retired dashboard page notices that used to be Flask flash messages."""
    logger.info("Dashboard UI notice [%s]: %s", category, message)


def _dashboard_frontend_response(*_args, **_kwargs):
    """Send dashboard page requests to the React frontend app."""
    frontend = getattr(Config, "DASHBOARD_FRONTEND_URL", "").strip().rstrip("/")
    if frontend:
        path = request.path
        if path.startswith("/dashboard"):
            path = path[len("/dashboard"):] or "/"
        query = f"?{request.query_string.decode()}" if request.query_string else ""
        return redirect(f"{frontend}{path}{query}")
    return jsonify({
        "error": "Dashboard UI is served by the React frontend",
        "api": "/dashboard/api",
        "path": request.path,
    }), 404


def _is_dashboard_frontend_request() -> bool:
    """True for dashboard UI routes owned by the React frontend."""
    path = request.path.rstrip("/") or "/dashboard"
    if path.startswith("/dashboard/api"):
        return False
    if path.startswith("/dashboard/static"):
        return False
    if path.startswith("/dashboard/oauth2"):
        return False
    if path.endswith("/analytics/export.csv"):
        return False

    exact_pages = {
        "/dashboard",
        "/dashboard/login",
        "/dashboard/auth-log",
        "/dashboard/forbidden",
        "/dashboard/islands",
        "/dashboard/logs",
        "/dashboard/status",
        "/dashboard/analytics",
        "/dashboard/database",
        "/dashboard/ops",
        "/dashboard/incidents",
        "/dashboard/trust",
    }
    return path in exact_pages or path.startswith("/dashboard/islands/")


@dashboard.before_request
def _restrict_to_console_domain():
    """Return 404 for any request that did not arrive via an allowed dashboard host."""
    host = request.host.split(":")[0]  # strip optional port
    if host not in _ALLOWED_DASHBOARD_HOSTS:
        abort(404)
    if _is_dashboard_frontend_request():
        return _dashboard_frontend_response()
    if request.method in {"POST", "PUT", "DELETE"} and not request.path.startswith("/dashboard/api/"):
        if not _csrf_is_valid():
            abort(403)


@dashboard.errorhandler(403)
def _forbidden(_e):
    return jsonify({"error": "Forbidden"}), 403

@dashboard.errorhandler(500)
def _internal_server_error(e):
    logger.exception("Internal server error: %s", e)
    return jsonify({"error": "Internal server error"}), 500


@dashboard.route("/api/session", methods=["GET"])
def api_session():
    """Return the current dashboard session state for a React dashboard."""
    user = _dashboard_bearer_user()
    bearer_mod = _is_dashboard_mod_user(user)
    session_auth = _check_session()
    role = _get_session_role() if session_auth else ("admin" if bearer_mod else None)
    return jsonify({
        "authenticated": bool(session_auth or bearer_mod or _check_bearer()),
        "role": role,
        "frontend_owned": True,
        "csrf_token": _csrf_token() if session_auth else None,
        "user": user if bearer_mod else {
            "id": session.get("discord_user_id"),
            "username": session.get("discord_username"),
            "avatar": session.get("discord_avatar_url"),
        } if session_auth else None,
    })


@dashboard.route("/api/login", methods=["POST"])
def api_login():
    """Secret-key login endpoint for a React dashboard."""
    payload = request.get_json(silent=True) or {}
    secret = payload.get("secret") or request.form.get("secret", "")
    if secret and Config.DASHBOARD_SECRET and secrets.compare_digest(secret, Config.DASHBOARD_SECRET):
        session["mod_logged_in"] = True
        session["mod_role"] = "admin"
        session.permanent = True
        return jsonify({
            "ok": True,
            "role": "admin",
            "csrf_token": _csrf_token(),
        })
    return jsonify({"ok": False, "error": "Invalid secret key"}), 401


@dashboard.route("/api/logout", methods=["POST"])
def api_logout():
    """Clear the browser dashboard session for a React dashboard."""
    _clear_dashboard_session()
    return jsonify({"ok": True})


def _clear_dashboard_session() -> None:
    session.pop("mod_logged_in",       None)
    session.pop("mod_role",            None)
    session.pop("discord_user_id",     None)
    session.pop("discord_username",    None)
    session.pop("discord_avatar_url",  None)
    session.pop("discord_checked_at",  None)
    session.pop("oauth_state",         None)
    session.pop("csrf_token",          None)


@dashboard.route("/login", methods=["GET", "POST"])
def login():
    return _dashboard_frontend_response()


@dashboard.route("/logout")
def logout():
    _clear_dashboard_session()
    return redirect(url_for("dashboard.login"))


# ---------------------------------------------------------------------------
# Discord OAuth2 routes
# ---------------------------------------------------------------------------

@dashboard.route("/oauth2/redirect")
def oauth2_redirect():
    """Redirect the user to Discord's authorization page."""
    if not Config.DISCORD_CLIENT_ID:
        return redirect(url_for("dashboard.login"))
    if not Config.GUILD_ID:
        return redirect(url_for("dashboard.login"))
    state = secrets.token_hex(16)
    session["oauth_state"] = state
    # Derive the callback URL from the current request so operators don't need
    # to set a DISCORD_REDIRECT_URI env var — just register this URL in the
    # Discord application's OAuth2 Redirects list:
    #   https://your-domain/dashboard/oauth2/callback
    callback_url = url_for("dashboard.oauth2_callback", _external=True)
    params = urllib.parse.urlencode({
        "client_id":     Config.DISCORD_CLIENT_ID,
        "redirect_uri":  callback_url,
        "response_type": "code",
        "scope":         "identify guilds.members.read",
        "state":         state,
    })
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")


@dashboard.route("/oauth2/callback")
def oauth2_callback():
    """Handle the OAuth2 callback from Discord."""
    error = request.args.get("error")
    if error:
        return redirect(url_for("dashboard.login"))

    state = request.args.get("state", "")
    if state != session.pop("oauth_state", ""):
        return redirect(url_for("dashboard.login"))

    code = request.args.get("code", "")
    if not code:
        return redirect(url_for("dashboard.login"))

    # Exchange authorization code for access token
    # The redirect_uri must exactly match what was sent during the authorization request.
    callback_url = url_for("dashboard.oauth2_callback", _external=True)
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
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent":   _DISCORD_USER_AGENT,
            },
            method="POST",
            timeout=10,
        )
        token_resp = json.loads(resp.body)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            pass
        logger.error(
            "OAuth token exchange HTTP %s — redirect_uri=%s — Discord response: %s",
            exc.code, callback_url, body,
        )
        return redirect(url_for("dashboard.login"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.error("OAuth token exchange failed: %s", exc)
        return redirect(url_for("dashboard.login"))

    access_token = token_resp.get("access_token")
    if not access_token:
        return redirect(url_for("dashboard.login"))

    # Fetch the user's guild-member record (includes roles and computed permissions)
    role = None
    member_perms = 0
    try:
        resp = discord_request(
            f"https://discord.com/api/users/@me/guilds/{Config.GUILD_ID}/member",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent":    _DISCORD_USER_AGENT,
            },
            timeout=10,
        )
        member_data = json.loads(resp.body)
        member_roles = [str(r) for r in member_data.get("roles", [])]
        try:
            member_perms = int(member_data.get("permissions", "0") or 0)
        except (ValueError, TypeError):
            member_perms = 0
        # Guild administrators (ADMINISTRATOR permission bit) always get admin access,
        # regardless of whether ADMIN_ROLE_ID is configured.
        if member_perms & _ADMINISTRATOR_PERM:
            role = "admin"
        elif ADMIN_ROLE_ID and str(ADMIN_ROLE_ID) in member_roles:
            role = "admin"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.warning("OAuth member fetch returned 404")
        else:
            logger.error("OAuth member fetch HTTP error %s", exc.code)
        return redirect(url_for("dashboard.login"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.error("OAuth member fetch failed: %s", exc)
        return redirect(url_for("dashboard.login"))

    if role is None:
        logger.warning(
            "OAuth role check: no qualifying role — "
            "member_roles=%s, admin_id=%s, permissions=%s",
            member_roles, ADMIN_ROLE_ID, member_perms,
        )
        return redirect(url_for("dashboard.login"))

    # Fetch basic user info for display
    discord_username   = ""
    discord_user_id    = ""
    discord_avatar_url = ""
    try:
        resp = discord_request(
            "https://discord.com/api/users/@me",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent":    _DISCORD_USER_AGENT,
            },
            timeout=10,
        )
        user_data = json.loads(resp.body)
        discord_user_id  = str(user_data.get("id", ""))
        discord_username = user_data.get("global_name") or user_data.get("username", "")
        avatar_hash      = user_data.get("avatar") or ""
        # Discord avatar hashes are lowercase hex strings (32 chars) or
        # animated variants prefixed with 'a_'.  Validate before using.
        if (discord_user_id and avatar_hash
                and re.fullmatch(r"(?:a_)?[0-9a-f]{32}", avatar_hash)):
            discord_avatar_url = (
                f"https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.png?size=64"
            )
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        pass  # Non-critical — display info is optional

    session["mod_logged_in"]      = True
    session["mod_role"]           = role
    session["discord_user_id"]    = discord_user_id
    session["discord_username"]   = discord_username
    session["discord_avatar_url"] = discord_avatar_url
    session["discord_checked_at"] = int(time.time())
    session.permanent          = True
    logger.info("OAuth login: user=%s role=%s", discord_username, role)
    return redirect(url_for("dashboard.index"))


@dashboard.route("/")
@admin_required
def index():
    db = get_db()
    try:
        total_visits   = db.execute("SELECT COUNT(*) FROM island_visits").fetchone()[0]
        total_warnings = db.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
        visits_today   = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours')"
        ).fetchone()[0]
        visits_week    = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        warnings_week  = db.execute(
            "SELECT COUNT(*) FROM warnings "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        recent_raw     = db.execute(
            "SELECT ign, destination, authorized, timestamp, user_id "
            "FROM island_visits ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        top_islands_raw = db.execute(
            "SELECT destination, COUNT(*) AS visit_count "
            "FROM island_visits "
            "GROUP BY destination "
            "ORDER BY visit_count DESC LIMIT 5"
        ).fetchall()
        top_travelers_raw = db.execute(
            "SELECT ign, COUNT(*) AS visit_count "
            "FROM island_visits "
            "GROUP BY ign "
            "ORDER BY visit_count DESC LIMIT 5"
        ).fetchall()
        trend_raw = db.execute(
            "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
            "FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days') "
            "GROUP BY day ORDER BY day"
        ).fetchall()
    except Exception:
        total_visits = total_warnings = visits_today = visits_week = warnings_week = 0
        recent_raw = []
        top_islands_raw = []
        top_travelers_raw = []
        trend_raw = []
    finally:
        db.close()

    recent_user_ids = [r["user_id"] for r in recent_raw if r["user_id"]]
    recent_name_map = _resolve_discord_usernames(recent_user_ids) if recent_user_ids else {}

    recent = [
        {
            "ign":         r["ign"],
            "destination": r["destination"],
            "authorized":  bool(r["authorized"]),
            "timestamp":   _ts_to_str(r["timestamp"]),
            "user_name":   recent_name_map.get(str(r["user_id"])) if r["user_id"] else None,
        }
        for r in recent_raw
    ]

    top_islands  = [{"name": r["destination"], "count": r["visit_count"]} for r in top_islands_raw]
    top_travelers = [{"ign": r["ign"], "count": r["visit_count"]} for r in top_travelers_raw]

    # Build a complete 7-day trend (fill gaps with 0)
    trend_map = {r["day"]: r["count"] for r in trend_raw}
    today_dt  = datetime.now(timezone.utc)
    trend_labels = []
    trend_counts = []
    for offset in range(6, -1, -1):
        d = (today_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        trend_labels.append(d[-5:])  # "MM-DD"
        trend_counts.append(trend_map.get(d, 0))

    warn_rate_7d = round(warnings_week / visits_week * 100, 1) if visits_week > 0 else 0

    db2 = get_db()
    try:
        rows2        = db2.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands2  = [_row_to_island_dict(dict(r)) for r in rows2]
        bot_status2  = _load_bot_status_map(db2)
    except Exception:
        db_islands2 = []
        bot_status2 = {}
    finally:
        db2.close()

    for isl in db_islands2:
        isl["discord_bot_online"] = bot_status2.get(isl.get("id", ""))

    island_count = len(db_islands2)
    status_map: dict[str, int] = {STATUS_ONLINE: 0, STATUS_REFRESHING: 0, STATUS_OFFLINE: 0}
    for isl in db_islands2:
        s = _effective_status(isl)
        status_map[s] = status_map.get(s, 0) + 1

    online_count = status_map[STATUS_ONLINE]

    return _dashboard_frontend_response(
        "dashboard/index.html",
        total_visits=total_visits,
        total_warnings=total_warnings,
        visits_today=visits_today,
        visits_week=visits_week,
        warnings_week=warnings_week,
        warn_rate_7d=warn_rate_7d,
        recent=recent,
        island_count=island_count,
        status_map=status_map,
        online_count=online_count,
        top_islands=top_islands,
        top_travelers=top_travelers,
        trend_labels=trend_labels,
        trend_counts=trend_counts,
    )


@dashboard.route("/islands")
@admin_required
def islands():
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
    except Exception:
        db_islands = []
    finally:
        db.close()

    merged = _merge_dashboard_fs_islands(db_islands)
    return _dashboard_frontend_response("dashboard/islands.html", islands=merged)


@dashboard.route("/islands/<name>", methods=["GET", "POST"])
@admin_required
def island_detail(name):
    island_id = name.lower()
    upper     = name.upper()

    db = get_db()
    try:
        row  = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
        meta = _row_to_island_dict(dict(row)) if row else None
    finally:
        db.close()

    fs_meta = _find_island_filesystem_meta(island_id, upper)
    fs_path = fs_meta["fs_path"]
    fs_type = fs_meta["fs_type"]

    if request.method == "POST":
        isl_type         = request.form.get("type", "").strip()
        isl_seasonal     = request.form.get("seasonal", "").strip()
        isl_desc         = request.form.get("description", "").strip()
        isl_cat          = request.form.get("cat", "public")
        isl_theme        = request.form.get("theme", "teal")
        isl_status       = request.form.get("status", "OFFLINE")
        # required_roles comes as a JSON array from the hidden input
        roles_raw = request.form.get("required_roles_json", "") or "[]"
        try:
            isl_required_roles = json.loads(roles_raw) if roles_raw.startswith("[") else []
            # Only keep string role IDs to avoid injecting arbitrary data
            isl_required_roles = [str(r) for r in isl_required_roles if str(r).isdigit()]
        except (ValueError, TypeError):
            isl_required_roles = []
        isl_dodo         = meta["dodo_code"] if meta else (_read_file(fs_path, "Dodo.txt") if fs_path else None)
        _fs_visitors_raw = _parse_visitor_value(_read_file(fs_path, "Visitors.txt")) if not meta and fs_path else None
        isl_visitors_raw = str(meta["visitors"]) if meta else (_fs_visitors_raw or "0")

        # items come as a JSON array from the hidden input
        items_raw = request.form.get("items_json", "") or request.form.get("items", "")
        try:
            items_list = json.loads(items_raw) if items_raw.startswith("[") else [
                i.strip() for i in items_raw.split(",") if i.strip()
            ]
        except (ValueError, TypeError):
            items_list = []

        errors = []
        if isl_cat    not in ALLOWED_CATEGORIES: errors.append("Invalid category.")
        if isl_theme  not in ALLOWED_THEMES:     errors.append("Invalid theme.")
        if isl_status not in ALLOWED_STATUSES:   errors.append("Invalid status.")

        try:
            isl_visitors = int(isl_visitors_raw)
        except ValueError:
            isl_visitors = 0

        if errors:
            for e in errors:
                _dashboard_notice(e, "error")
        else:
            # dodo_code and visitors are managed by island bots; do not write to filesystem

            db2 = get_db()
            try:
                # We do NOT include `required_roles` in the DO UPDATE SET clause
                # so that we do not overwrite the background sync performed by the bot.
                # It is included in INSERT so that new records get the default '[]'.
                db2.execute(
                    """INSERT INTO islands
                           (id, name, display_name, is_visible, type, items, theme, cat, description, seasonal,
                            status, visitors, dodo_code, map_url, updated_at, required_roles)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                           name=excluded.name, type=excluded.type, items=excluded.items,
                           theme=excluded.theme, cat=excluded.cat,
                           description=excluded.description, seasonal=excluded.seasonal,
                           status=excluded.status, visitors=excluded.visitors,
                           dodo_code=excluded.dodo_code, updated_at=excluded.updated_at""",
                    (
                        island_id, upper, meta.get("display_name") if meta else None,
                        int(bool(meta.get("is_visible", True) if meta else True)),
                        isl_type, json.dumps(items_list),
                        isl_theme, isl_cat, isl_desc, isl_seasonal,
                        isl_status, isl_visitors, isl_dodo,
                        meta["map_url"] if meta else None,
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps(isl_required_roles),
                    ),
                )
                db2.commit()
            finally:
                db2.close()

            _dashboard_notice(f'Island "{upper}" saved successfully.', "success")
            return redirect(url_for("dashboard.islands"))

    island = meta or {
        "id": island_id, "name": upper, "type": "", "items": [],
        "theme": "teal", "cat": "public", "description": "", "seasonal": "",
        "status": "OFFLINE", "visitors": 0, "dodo_code": None,
        "map_url": None, "updated_at": None, "required_roles": [],
    }
    island["fs_path"]     = fs_path
    island["fs_type"]     = fs_type
    island["fs_dodo"]     = _read_file(fs_path, "Dodo.txt")     if fs_path else None
    island["fs_visitors"] = _parse_visitor_value(_read_file(fs_path, "Visitors.txt")) if fs_path else None
    island["items_text"]  = ", ".join(island["items"]) if isinstance(island.get("items"), list) else ""

    r2_configured = bool(Config.R2_ACCOUNT_ID and Config.R2_ACCESS_KEY_ID and Config.R2_SECRET_ACCESS_KEY)

    return _dashboard_frontend_response(
        "dashboard/island_detail.html",
        island=island,
        allowed_categories=ALLOWED_CATEGORIES,
        allowed_themes=ALLOWED_THEMES,
        allowed_statuses=ALLOWED_STATUSES,
        r2_configured=r2_configured,
        sparkline_7d=_island_sparkline_7d(upper),
    )


_ALLOWED_SORT_COLS = {"ign", "destination", "timestamp"}

@dashboard.route("/logs")
@admin_required
def logs():
    page              = request.args.get("page", 1, type=int)
    per_page          = 25
    island_filter     = request.args.get("island", "").strip()
    authorized_filter = request.args.get("authorized", "")
    category_filter   = request.args.get("category", "")
    sort_by           = request.args.get("sort_by", "timestamp")
    sort_order        = request.args.get("sort_order", "desc")
    log_type          = request.args.get("type", "flights")
    ign_filter        = request.args.get("ign", "").strip()
    _ALLOWED_ACTION_TYPES = {"WARN", "KICK", "BAN", "DISMISS", "NOTE", "ADMIT"}
    action_type_filter = request.args.get("action_type", "").strip().upper()
    if action_type_filter not in _ALLOWED_ACTION_TYPES:
        action_type_filter = ""

    # Sanitise sort params
    if sort_by not in _ALLOWED_SORT_COLS:
        sort_by = "timestamp"
    sort_order = "asc" if sort_order == "asc" else "desc"

    db = get_db()
    try:
        # Fetch island list for dropdown (used in flights filter UI)
        island_names = [
            r[0] for r in db.execute(
                "SELECT name FROM islands ORDER BY name"
            ).fetchall()
        ]

        if log_type == "warnings":
            conditions, params = [], []
            if ign_filter:
                conditions.append("LOWER(iv.ign) LIKE LOWER(?)")
                params.append(f"%{ign_filter}%")
            if action_type_filter:
                conditions.append("UPPER(w.action_type) = ?")
                params.append(action_type_filter)
            where = _where_clause(conditions)
            total = db.execute(
                f"SELECT COUNT(*) FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id "
                f"{where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT w.*, iv.ign, iv.destination "
                f"FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id "
                f"{where} ORDER BY w.timestamp DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page],
            ).fetchall()
            name_map = _resolve_discord_usernames(
                [r["user_id"] for r in rows if r["user_id"]] + [r["mod_id"] for r in rows if r["mod_id"]]
            )
            entries = [
                {
                    "user_id":     r["user_id"],
                    "user_name":   name_map.get(str(r["user_id"]), str(r["user_id"])) if r["user_id"] else "—",
                    "reason":      r["reason"],
                    "mod_id":      r["mod_id"],
                    "mod_name":    name_map.get(str(r["mod_id"]), str(r["mod_id"])) if r["mod_id"] else "—",
                    "timestamp":   _ts_to_str(r["timestamp"]),
                    "ign":         r["ign"],
                    "destination": r["destination"],
                    "action_type": r["action_type"],
                }
                for r in rows
            ]
        else:
            conditions, params = [], []
            use_island_join = bool(category_filter in ("public", "member"))

            if island_filter:
                col = "iv.destination" if use_island_join else "destination"
                conditions.append(f"LOWER({col}) = LOWER(?)")
                params.append(island_filter)
            if ign_filter:
                col = "iv.ign" if use_island_join else "ign"
                conditions.append(f"LOWER({col}) LIKE LOWER(?)")
                params.append(f"%{ign_filter}%")
            if authorized_filter in ("0", "1"):
                col = "iv.authorized" if use_island_join else "authorized"
                conditions.append(f"{col} = ?")
                params.append(int(authorized_filter))
            if use_island_join:
                conditions.append("isl.cat = ?")
                params.append(category_filter)

            if use_island_join:
                join_sql = (
                    "FROM island_visits iv "
                    "JOIN islands isl ON LOWER(iv.destination) = isl.id"
                )
                order_sql = f"iv.{sort_by} {sort_order.upper()}"
                where = _where_clause(conditions)
                total = db.execute(
                    f"SELECT COUNT(*) {join_sql} {where}", params
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT iv.* {join_sql} {where} "
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    params + [per_page, (page - 1) * per_page],
                ).fetchall()
            else:
                where = _where_clause(conditions)
                order_sql = f"{sort_by} {sort_order.upper()}"
                total = db.execute(
                    f"SELECT COUNT(*) FROM island_visits {where}", params
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT * FROM island_visits {where} "
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    params + [per_page, (page - 1) * per_page],
                ).fetchall()

            entries = [
                {
                    "id":            r["id"],
                    "ign":           r["ign"],
                    "origin_island": r["origin_island"],
                    "destination":   r["destination"],
                    "authorized":    bool(r["authorized"]),
                    "timestamp":     _ts_to_str(r["timestamp"]),
                    "user_id":       r["user_id"],
                }
                for r in rows
            ]
            flight_name_map = _resolve_discord_usernames([r["user_id"] for r in rows if r["user_id"]])
            for e in entries:
                e["user_name"] = flight_name_map.get(str(e["user_id"])) if e["user_id"] else None
    except Exception:
        total, entries, island_names = 0, [], []
    finally:
        db.close()

    return _dashboard_frontend_response(
        "dashboard/logs.html",
        entries=entries,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, (total + per_page - 1) // per_page),
        island_filter=island_filter,
        authorized_filter=authorized_filter,
        category_filter=category_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        log_type=log_type,
        island_names=island_names,
        ign_filter=ign_filter,
        action_type_filter=action_type_filter,
    )


@dashboard.route("/status")
@admin_required
def island_status():
    """Dedicated Island Status Breakdown page."""
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = _merge_dashboard_fs_islands([_row_to_island_dict(dict(r)) for r in rows])
    except Exception:
        db_islands = []
    finally:
        db.close()

    island_count = len(db_islands)

    # Load live bot-presence data and annotate each island
    db2 = get_db()
    try:
        bot_status = _load_bot_status_map(db2)
    except Exception:
        bot_status = {}
    finally:
        db2.close()

    for isl in db_islands:
        isl["discord_bot_online"] = bot_status.get(isl.get("id", ""))

    # Derive counts from live fields (discord_bot_online / dodo_code)
    online_count    = 0
    refreshing_count = 0
    offline_count   = 0
    grouped: dict[str, list] = {STATUS_ONLINE: [], STATUS_REFRESHING: [], STATUS_OFFLINE: []}
    for isl in db_islands:
        s = _effective_status(isl)
        grouped[s].append(isl)
        if s == STATUS_ONLINE:
            online_count += 1
        elif s == STATUS_REFRESHING:
            refreshing_count += 1
        else:
            offline_count += 1

    def _pct(count):
        return round(count * 100 / island_count) if island_count else 0

    online_pct     = _pct(online_count)
    refreshing_pct = _pct(refreshing_count)
    off_pct        = _pct(offline_count)

    return _dashboard_frontend_response(
        "dashboard/status.html",
        island_count=island_count,
        online_count=online_count,
        refreshing_count=refreshing_count,
        offline_count=offline_count,
        online_pct=online_pct,
        refreshing_pct=refreshing_pct,
        off_pct=off_pct,
        grouped=grouped,
    )


@dashboard.route("/analytics")
@admin_required
def analytics():
    # ── Island-type filter (free / sub / all) ──────────────────────────────
    island_type_filter = request.args.get("island_type", "").lower()
    if island_type_filter not in ("free", "sub"):
        island_type_filter = ""

    # SQL fragment appended to WHERE clauses in island_visits queries
    it_clause = " AND island_type = ?" if island_type_filter else ""
    it_params = [island_type_filter] if island_type_filter else []

    db = get_db()
    try:
        top_islands = [
            dict(r) for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY destination "
                "ORDER BY visit_count DESC LIMIT 10",
                it_params,
            ).fetchall()
        ]
        top_travelers = [
            dict(r) for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY ign "
                "ORDER BY visit_count DESC LIMIT 10",
                it_params,
            ).fetchall()
        ]
        visits_by_day = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause} "
                "GROUP BY day ORDER BY day",
                it_params,
            ).fetchall()
        ]
        visits_by_day_30 = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause} "
                "GROUP BY day ORDER BY day",
                it_params,
            ).fetchall()
        ]
        visits_by_hour = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS hour, "
                "COUNT(*) AS count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY hour ORDER BY hour",
                it_params,
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count "
            f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "GROUP BY authorized",
            it_params,
        ).fetchall()
        # Visits by island category (public vs member/VIP)
        cat_raw = db.execute(
            "SELECT isl.cat, COUNT(*) AS visit_count "
            "FROM island_visits iv "
            "JOIN islands isl ON LOWER(iv.destination) = isl.id "
            f"{'WHERE iv.island_type = ?' if island_type_filter else ''} "
            "GROUP BY isl.cat",
            it_params,
        ).fetchall()
        # Top users per action type (WARN, KICK, BAN, NOTE)
        _VALID_COUNT_KEYS = {"warn_count", "kick_count", "ban_count", "note_count"}

        def _top_by_action(action: str, count_key: str):
            if count_key not in _VALID_COUNT_KEYS:
                raise ValueError(f"Invalid count_key: {count_key!r}")
            if island_type_filter:
                rows = db.execute(
                    f"SELECT w.user_id, COUNT(*) AS {count_key} "
                    "FROM warnings w "
                    "JOIN island_visits iv ON w.visit_id = iv.id "
                    "WHERE w.user_id IS NOT NULL AND iv.island_type = ? AND UPPER(w.action_type) = ? "
                    f"GROUP BY w.user_id ORDER BY {count_key} DESC LIMIT 10",
                    (island_type_filter, action),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT user_id, COUNT(*) AS {count_key} "
                    "FROM warnings WHERE user_id IS NOT NULL AND UPPER(action_type) = ? "
                    f"GROUP BY user_id ORDER BY {count_key} DESC LIMIT 10",
                    (action,),
                ).fetchall()
            return [dict(r) for r in rows]

        top_warned  = _top_by_action("WARN",    "warn_count")
        top_kicked  = _top_by_action("KICK",    "kick_count")
        top_banned  = _top_by_action("BAN",     "ban_count")
        top_noted   = _top_by_action("NOTE",    "note_count")

        all_action_user_ids = (
            [r["user_id"] for r in top_warned]
            + [r["user_id"] for r in top_kicked]
            + [r["user_id"] for r in top_banned]
            + [r["user_id"] for r in top_noted]
        )
        action_name_map = _resolve_discord_usernames(all_action_user_ids)
        for collection in (top_warned, top_kicked, top_banned, top_noted):
            for row in collection:
                row["user_name"] = action_name_map.get(str(row["user_id"]), str(row["user_id"]))
        # Quick summary stats
        visits_today = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours'){it_clause}",
            it_params,
        ).fetchone()[0]
        visits_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        if island_type_filter:
            warnings_week = db.execute(
                "SELECT COUNT(*) FROM warnings w "
                "JOIN island_visits iv ON w.visit_id = iv.id "
                "WHERE w.timestamp > strftime('%s','now','-7 days') "
                "AND iv.island_type = ?",
                it_params,
            ).fetchone()[0]
        else:
            warnings_week = db.execute(
                "SELECT COUNT(*) FROM warnings "
                "WHERE timestamp > strftime('%s','now','-7 days')"
            ).fetchone()[0]
        # Day-of-week breakdown (0=Sunday … 6=Saturday)
        dow_raw = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%w', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS dow, "
                "COUNT(*) AS count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY dow ORDER BY dow",
                it_params,
            ).fetchall()
        ]
        # New vs returning travelers (7d and 30d)
        new_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit "
            f"  FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "  GROUP BY ign"
            f") WHERE first_visit > strftime('%s','now','-7 days')",
            it_params,
        ).fetchone()[0]
        total_unique_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        new_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit "
            f"  FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "  GROUP BY ign"
            f") WHERE first_visit > strftime('%s','now','-30 days')",
            it_params,
        ).fetchone()[0]
        total_unique_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        # All-time unique travelers and islands
        total_unique_travelers = db.execute(
            f"SELECT COUNT(DISTINCT ign) FROM island_visits"
            f"{' WHERE island_type = ?' if island_type_filter else ''}",
            it_params,
        ).fetchone()[0]
        total_unique_islands = db.execute(
            f"SELECT COUNT(DISTINCT destination) FROM island_visits"
            f"{' WHERE island_type = ?' if island_type_filter else ''}",
            it_params,
        ).fetchone()[0]
        # Visits in the previous week (7–14 days ago) for week-over-week delta
        visits_prev_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-14 days') "
            f"AND timestamp <= strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        # Warnings issued today
        if island_type_filter:
            warnings_today = db.execute(
                "SELECT COUNT(*) FROM warnings w "
                "JOIN island_visits iv ON w.visit_id = iv.id "
                "WHERE w.timestamp > strftime('%s','now','+8 hours','start of day','-8 hours') "
                "AND iv.island_type = ?",
                it_params,
            ).fetchone()[0]
        else:
            warnings_today = db.execute(
                "SELECT COUNT(*) FROM warnings "
                "WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours')"
            ).fetchone()[0]
        # Peak hour (hour with the most visits all-time, in UTC+8)
        peak_hour_row = db.execute(
            "SELECT CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS hour, "
            "COUNT(*) AS cnt "
            f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "GROUP BY hour ORDER BY cnt DESC LIMIT 1",
            it_params,
        ).fetchone()
        peak_hour = peak_hour_row["hour"] if peak_hour_row else None
        # Average visits per day over the last 30 days
        avg_visits_30d_row = db.execute(
            "SELECT COUNT(*) * 1.0 / 30 AS avg FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause}",
            it_params,
        ).fetchone()
        avg_visits_30d = round(avg_visits_30d_row["avg"] or 0, 1)
    except Exception:
        top_islands = top_travelers = visits_by_day = visits_by_day_30 = []
        visits_by_hour = []
        auth_raw = []
        cat_raw = []
        top_warned = []
        top_kicked = []
        top_banned = []
        top_noted = []
        visits_today = visits_week = warnings_week = 0
        dow_raw = []
        new_7d = total_unique_7d = new_30d = total_unique_30d = 0
        total_unique_travelers = total_unique_islands = 0
        visits_prev_week = warnings_today = 0
        peak_hour = None
        avg_visits_30d = 0.0
    finally:
        db.close()

    auth_map   = {r["authorized"]: r["count"] for r in auth_raw}
    auth_stats = {"authorized": auth_map.get(1, 0), "unauthorized": auth_map.get(0, 0)}
    cat_map    = {r["cat"]: r["visit_count"] for r in cat_raw}
    cat_stats  = {"public": cat_map.get("public", 0), "member": cat_map.get("member", 0)}

    # Build full 24-hour array (fill missing hours with 0)
    hour_map = {r["hour"]: r["count"] for r in visits_by_hour}
    visits_by_hour_full = [{"hour": h, "count": hour_map.get(h, 0)} for h in range(24)]

    # Build full 7-day-of-week array (fill missing days with 0)
    dow_map = {r["dow"]: r["count"] for r in dow_raw}
    visits_by_dow = [{"dow": d, "label": _DOW_LABELS[d], "count": dow_map.get(d, 0)} for d in range(7)]

    returning_7d  = max(total_unique_7d  - new_7d,  0)
    returning_30d = max(total_unique_30d - new_30d, 0)
    new_returning = {
        "new_7d":  new_7d,  "returning_7d":  returning_7d,  "total_7d":  total_unique_7d,
        "new_30d": new_30d, "returning_30d": returning_30d, "total_30d": total_unique_30d,
    }

    total_visits = auth_stats["authorized"] + auth_stats["unauthorized"]
    auth_rate_pct = round(auth_stats["authorized"] / total_visits * 100) if total_visits else None
    warn_rate_week = round(warnings_week / visits_week * 100, 1) if visits_week else 0.0

    return _dashboard_frontend_response(
        "dashboard/analytics.html",
        top_islands=top_islands,
        top_travelers=top_travelers,
        visits_by_day=visits_by_day,
        visits_by_day_30=visits_by_day_30,
        visits_by_hour=visits_by_hour_full,
        visits_by_dow=visits_by_dow,
        auth_stats=auth_stats,
        cat_stats=cat_stats,
        top_warned=top_warned,
        top_kicked=top_kicked,
        top_banned=top_banned,
        top_noted=top_noted,
        visits_today=visits_today,
        visits_week=visits_week,
        warnings_week=warnings_week,
        warnings_today=warnings_today,
        new_returning=new_returning,
        island_type_filter=island_type_filter,
        total_unique_travelers=total_unique_travelers,
        total_unique_islands=total_unique_islands,
        visits_prev_week=visits_prev_week,
        peak_hour=peak_hour,
        avg_visits_30d=avg_visits_30d,
        auth_rate_pct=auth_rate_pct,
        warn_rate_week=warn_rate_week,
    )


@dashboard.route("/database")
@admin_required
def database():
    """Admin database tools: inspect DB backend and run SQLite -> MariaDB copy."""
    return _dashboard_frontend_response(
        "dashboard/database.html",
        db_backend=get_backend(),
        mariadb=_mariadb_settings_payload(),
    )


@dashboard.route("/ops")
@admin_required
def ops():
    """Operations dashboard page."""
    return _dashboard_frontend_response("dashboard/ops.html")


@dashboard.route("/incidents")
@admin_required
def incidents():
    """Incident center page."""
    return _dashboard_frontend_response("dashboard/incidents.html")


@dashboard.route("/trust")
@admin_required
def trust():
    """User trust profile lookup page."""
    return _dashboard_frontend_response("dashboard/trust.html", initial_user_id=(request.args.get("user_id") or "").strip())


@dashboard.route("/analytics/export.csv")
@admin_required
def analytics_export_csv():
    """Export visit log data as a CSV download."""
    return _analytics_csv_response()


@dashboard.route("/api/analytics/export.csv")
@api_auth_required
def api_analytics_export_csv():
    """Export visit log data as CSV for the React dashboard."""
    return _analytics_csv_response()


def _analytics_csv_response():
    """Build a CSV export response for dashboard analytics."""
    island_type_filter = request.args.get("island_type", "").lower()
    if island_type_filter not in ("free", "sub"):
        island_type_filter = ""

    it_clause = " AND island_type = ?" if island_type_filter else ""
    it_params = [island_type_filter] if island_type_filter else []

    db = get_db()
    try:
        # Limit to 10 000 rows to keep response size and memory usage reasonable.
        rows = db.execute(
            "SELECT ign, origin_island, destination, island_type, authorized, "
            "datetime(timestamp, 'unixepoch', '+8 hours') AS visit_time "
            f"FROM island_visits WHERE 1=1{it_clause} "
            "ORDER BY timestamp DESC LIMIT 10000",
            it_params,
        ).fetchall()
    except Exception:
        rows = []
    finally:
        db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["IGN", "Origin Island", "Destination", "Island Type", "Authorized", "Visit Time (UTC+8)"])
    for r in rows:
        writer.writerow([
            r["ign"],
            r["origin_island"],
            r["destination"],
            r["island_type"],
            "Yes" if r["authorized"] else "No",
            r["visit_time"],
        ])

    filename = f"chobot_visits{'_' + island_type_filter if island_type_filter else ''}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ===========================================================================
# JSON CRUD API  (Bearer token OR active browser session)
# ===========================================================================

def _parse_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _parse_positive_int(value, default: int) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def _request_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or ""


def _dashboard_actor() -> tuple[str | None, str | None]:
    user_id = session.get("discord_user_id")
    name = session.get("discord_username") or session.get("discord_global_name")
    bearer_user = _dashboard_bearer_user()
    if bearer_user:
        user_id = user_id or str(bearer_user.get("user_id") or bearer_user.get("id") or "")
        name = name or bearer_user.get("username") or bearer_user.get("global_name")
    return (str(user_id) if user_id else None, str(name) if name else None)


def _record_audit_event(action: str, target: str | None = None, details: dict | None = None) -> None:
    db = get_db()
    try:
        actor_user_id, actor_name = _dashboard_actor()
        db.execute(
            """
            INSERT INTO dashboard_audit_events
            (actor_user_id, actor_name, action, target, details, ip_address, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                actor_name,
                action,
                target,
                json.dumps(details or {}, sort_keys=True),
                _request_ip(),
                int(time.time()),
            ),
        )
        db.commit()
    except Exception as exc:
        logger.debug("Audit event insert failed: %s", exc)
    finally:
        db.close()


def _sqlite_table_counts() -> dict[str, int]:
    source = inspect_sqlite_source(_DB_PATH)
    return {name: int(meta["rows"] or 0) for name, meta in source["tables"].items()}


def _mariadb_settings_payload() -> dict:
    missing = []
    if not Config.MYSQL_HOST:
        missing.append("MYSQL_HOST")
    if not Config.MYSQL_USER:
        missing.append("MYSQL_USER")
    if not Config.MYSQL_DATABASE:
        missing.append("MYSQL_DATABASE")

    return {
        "configured": not missing,
        "missing": missing,
        "host": Config.MYSQL_HOST,
        "port": Config.MYSQL_PORT,
        "user": Config.MYSQL_USER,
        "database": Config.MYSQL_DATABASE,
        "default_truncate_before_import": Config.MARIADB_TRUNCATE_BEFORE_IMPORT,
    }


@dashboard.route("/api/mariadb-migration/status", methods=["GET"])
@api_auth_required
def api_mariadb_migration_status():
    """Return SQLite source counts and MariaDB migration configuration status."""
    try:
        source = inspect_sqlite_source(_DB_PATH)
        source_tables = {name: int(meta["rows"] or 0) for name, meta in source["tables"].items()}
    except Exception as exc:
        return jsonify({"error": f"Could not inspect SQLite database: {exc}"}), 500

    return jsonify({
        "runtime_database": get_backend(),
        "sqlite_path": _DB_PATH,
        "sqlite_exists": os.path.exists(_DB_PATH),
        "source_tables": source_tables,
        "source_total_rows": source["total_rows"],
        "persistent_total_rows": source["persistent_rows"],
        "skipped_tables": [name for name, meta in source["tables"].items() if meta["skipped"]],
        "mariadb": _mariadb_settings_payload(),
        "migration_running": _mariadb_migration_lock.locked(),
        "last_result": _mariadb_migration_last_result,
        "note": "Use DB_BACKEND=mysql to run ChoBot against MariaDB/MySQL after migrating data.",
    })


@dashboard.route("/api/mariadb-migration", methods=["POST"])
@api_auth_required
def api_mariadb_migration_run():
    """Copy existing SQLite data into MariaDB without changing the running SQLite flow."""
    global _mariadb_migration_last_result

    data = request.get_json(silent=True) or {}
    dry_run = _parse_bool(data.get("dry_run"), False)
    truncate_before_import = _parse_bool(
        data.get("truncate_before_import"),
        Config.MARIADB_TRUNCATE_BEFORE_IMPORT,
    )

    if not _mariadb_migration_lock.acquire(blocking=False):
        return jsonify({"error": "MariaDB migration is already running"}), 409

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        if dry_run:
            report = dry_run_sqlite_to_mariadb(
                sqlite_path=_DB_PATH,
                host=Config.MARIADB_HOST,
                port=Config.MARIADB_PORT,
                user=Config.MARIADB_USER,
                password=Config.MARIADB_PASSWORD,
                database=Config.MARIADB_DATABASE,
            )
            source_tables = {
                name: int(meta["rows"] or 0)
                for name, meta in report["source"]["tables"].items()
            }
            _mariadb_migration_last_result = {
                "ok": True,
                "dry_run": True,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "source_tables": source_tables,
                "source_total_rows": report["source"]["total_rows"],
                "persistent_total_rows": report["source"]["persistent_rows"],
                "target_database_exists": report["target_database_exists"],
                "target_tables": report["target_tables"],
                "schema_drift": report["schema_drift"],
                "warnings": report["warnings"],
                "mariadb": _mariadb_settings_payload(),
            }
            _record_audit_event(
                "mariadb_migration_dry_run",
                Config.MARIADB_DATABASE,
                {
                    "source_total_rows": report["source"]["total_rows"],
                    "schema_drift_tables": sorted(report["schema_drift"]),
                },
            )
            return jsonify(_mariadb_migration_last_result)

        summary = migrate_sqlite_to_mariadb_detailed(
            sqlite_path=_DB_PATH,
            host=Config.MARIADB_HOST,
            port=Config.MARIADB_PORT,
            user=Config.MARIADB_USER,
            password=Config.MARIADB_PASSWORD,
            database=Config.MARIADB_DATABASE,
            truncate_before_import=truncate_before_import,
            backup_dir=backup_dir_path(),
        )
        _mariadb_migration_last_result = {
            **summary,
            "runtime_database": get_backend(),
            "sqlite_preserved": True,
            "note": "Migration copied data to MariaDB. Set DB_BACKEND=mysql to use it as the app database.",
        }
        _record_audit_event(
            "mariadb_migration_run",
            Config.MARIADB_DATABASE,
            {
                "truncate_before_import": truncate_before_import,
                "total_rows_copied": summary["total_rows_copied"],
                "backup_path": summary["backup_path"],
                "validation_ok": bool(summary.get("validation", {}).get("ok")),
            },
        )
        return jsonify(_mariadb_migration_last_result)
    except Exception as exc:
        logger.exception("MariaDB migration failed")
        _mariadb_migration_last_result = {
            "ok": False,
            "dry_run": dry_run,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
        return jsonify(_mariadb_migration_last_result), 500
    finally:
        _mariadb_migration_lock.release()


@dashboard.route("/api/database/maintenance", methods=["POST"])
@api_auth_required
def api_database_maintenance():
    """Prune volatile and stale operational rows."""
    data = request.get_json(silent=True) or {}
    warning_days = _parse_positive_int(data.get("warning_days"), 3)
    reveal_days = _parse_positive_int(data.get("reveal_days"), 30)
    audit_days = _parse_positive_int(data.get("audit_days"), 180)
    now = int(time.time())
    backup_file = None
    if get_backend() == "sqlite":
        try:
            backup_file = os.path.basename(backup_sqlite_database(_DB_PATH, backup_dir_path()))
        except Exception as exc:
            logger.warning("Could not create pre-maintenance backup: %s", exc)

    deleted: dict[str, int] = {}
    db = get_db()
    try:
        cur = db.execute("DELETE FROM command_claims")
        deleted["command_claims"] = max(cur.rowcount, 0)

        cur = db.execute(
            "DELETE FROM warnings WHERE timestamp IS NOT NULL AND timestamp < ?",
            (now - warning_days * 86400,),
        )
        deleted["expired_warnings"] = max(cur.rowcount, 0)

        cur = db.execute(
            "DELETE FROM dodo_reveal_messages WHERE created_at < ?",
            (now - reveal_days * 86400,),
        )
        deleted["stale_dodo_reveals"] = max(cur.rowcount, 0)

        cur = db.execute(
            "DELETE FROM dashboard_audit_events WHERE created_at < ?",
            (now - audit_days * 86400,),
        )
        deleted["old_audit_events"] = max(cur.rowcount, 0)

        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Database maintenance failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    _record_audit_event(
        "database_maintenance",
        "database",
        {
            "deleted": deleted,
            "warning_days": warning_days,
            "reveal_days": reveal_days,
            "audit_days": audit_days,
            "backup_file": backup_file,
        },
    )
    return jsonify({"ok": True, "deleted": deleted, "backup_file": backup_file})


@dashboard.route("/api/runtime-status", methods=["GET"])
@api_auth_required
def api_runtime_status():
    """Return authenticated operational health and integration details."""
    return jsonify(build_health_payload(
        data_manager=get_active_data_manager(),
        include_private=True,
    ))


@dashboard.route("/api/backups", methods=["GET"])
@api_auth_required
def api_backups():
    """Return recent SQLite database backups without exposing filesystem paths."""
    limit = min(max(request.args.get("limit", 25, type=int), 1), 100)
    return jsonify(list_backups(limit=limit))


@dashboard.route("/api/backups/<filename>", methods=["GET"])
@api_auth_required
def api_backup_download(filename):
    """Download a named backup file from the configured backup directory."""
    safe_name = os.path.basename(filename)
    if safe_name != filename or not safe_name.endswith(".db"):
        return jsonify({"ok": False, "error": "Invalid backup filename"}), 400
    backup_dir = backup_dir_path()
    if not os.path.exists(os.path.join(backup_dir, safe_name)):
        return jsonify({"ok": False, "error": "Backup not found"}), 404
    _record_audit_event("backup_download", safe_name, {})
    return send_from_directory(backup_dir, safe_name, as_attachment=True)


@dashboard.route("/api/maintenance-mode", methods=["POST"])
@api_auth_required
def api_maintenance_mode():
    """Update maintenance-mode switches stored in the settings table."""
    data = request.get_json(silent=True) or {}
    try:
        settings = update_maintenance_settings(data)
    except Exception as exc:
        logger.exception("Maintenance mode update failed")
        return jsonify({"ok": False, "error": str(exc)}), 500

    _record_audit_event(
        "maintenance_mode_update",
        "settings",
        {
            "maintenance_mode": settings["maintenance_mode"],
            "disable_dodo_reveals": settings["disable_dodo_reveals"],
            "disable_refresh": settings["disable_refresh"],
            "disable_commands": settings.get("disable_commands", False),
            "island_count": len(settings.get("islands") or {}),
            "has_message": bool(settings["message"]),
        },
    )
    return jsonify({"ok": True, "maintenance": settings})


@dashboard.route("/api/audit-events", methods=["GET"])
@api_auth_required
def api_audit_events():
    """Return recent dashboard/system audit entries."""
    limit = min(max(request.args.get("limit", 25, type=int), 1), 100)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM dashboard_audit_events ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "entries": []}), 500
    finally:
        db.close()

    entries = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item.get("details") or "{}")
        except (TypeError, ValueError):
            item["details"] = {}
        item["created_at_text"] = _ts_to_str(item.get("created_at"))
        entries.append(item)

    return jsonify({"ok": True, "entries": entries})


@dashboard.route("/api/incidents", methods=["GET", "POST", "PATCH"])
@api_auth_required
def api_incidents():
    """Return incident-center moderation queues and alert levels."""
    if request.method == "GET":
        limit = min(max(request.args.get("limit", 25, type=int), 1), 100)
        payload = _recent_incident_payload(limit=limit)
        status = 200 if payload.get("ok") else 500
        return jsonify(payload), status

    data = request.get_json(silent=True) or {}
    source_kind = str(data.get("kind") or data.get("source_kind") or "").strip()
    source_id = str(data.get("source_id") or "").strip()
    title = str(data.get("title") or "").strip()[:240]
    status_value = str(data.get("status") or ("open" if request.method == "POST" else "")).strip().lower()
    severity = str(data.get("severity") or "attention").strip().lower()
    assigned_to = str(data.get("assigned_to") or "").strip()[:80]
    note = str(data.get("note") or "").strip()[:1000]
    allowed_statuses = {"open", "investigating", "watching", "resolved", "dismissed"}
    allowed_severities = {"critical", "warning", "attention", "info"}

    if not source_kind or not source_id:
        return jsonify({"ok": False, "error": "kind/source_kind and source_id are required"}), 400
    if status_value and status_value not in allowed_statuses:
        return jsonify({"ok": False, "error": f"status must be one of {sorted(allowed_statuses)}"}), 400
    if severity not in allowed_severities:
        return jsonify({"ok": False, "error": f"severity must be one of {sorted(allowed_severities)}"}), 400

    actor_user_id, _actor_name = _dashboard_actor()
    now = int(time.time())
    db = get_db()
    try:
        if request.method == "POST":
            if not title:
                title = f"{source_kind.replace('_', ' ').title()} {source_id}"
            db.execute(
                """INSERT INTO incident_workflow
                   (source_kind, source_id, title, status, severity, assigned_to, note, actor_user_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_kind, source_id) DO UPDATE SET
                       title=excluded.title,
                       status=excluded.status,
                       severity=excluded.severity,
                       assigned_to=excluded.assigned_to,
                       note=excluded.note,
                       actor_user_id=excluded.actor_user_id,
                       updated_at=excluded.updated_at""",
                (source_kind, source_id, title, status_value, severity, assigned_to, note, actor_user_id, now, now),
            )
        else:
            row = db.execute(
                "SELECT * FROM incident_workflow WHERE source_kind = ? AND source_id = ?",
                (source_kind, source_id),
            ).fetchone()
            if not row:
                title = title or f"{source_kind.replace('_', ' ').title()} {source_id}"
                db.execute(
                    """INSERT INTO incident_workflow
                       (source_kind, source_id, title, status, severity, assigned_to, note, actor_user_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source_kind, source_id, title, status_value or "open", severity, assigned_to, note, actor_user_id, now, now),
                )
            else:
                db.execute(
                    """UPDATE incident_workflow
                       SET status = COALESCE(NULLIF(?, ''), status),
                           severity = ?,
                           assigned_to = ?,
                           note = ?,
                           actor_user_id = ?,
                           updated_at = ?
                       WHERE source_kind = ? AND source_id = ?""",
                    (status_value, severity, assigned_to, note, actor_user_id, now, source_kind, source_id),
                )
        db.commit()
        row = db.execute(
            "SELECT * FROM incident_workflow WHERE source_kind = ? AND source_id = ?",
            (source_kind, source_id),
        ).fetchone()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    _record_audit_event(
        "incident_workflow_update",
        f"{source_kind}:{source_id}",
        {"status": status_value, "severity": severity, "assigned": bool(assigned_to), "has_note": bool(note)},
    )
    return jsonify({"ok": True, "incident": dict(row) if row else None})


@dashboard.route("/api/command-analytics", methods=["GET"])
@api_auth_required
def api_command_analytics():
    """Return command/search analytics for dashboard reporting."""
    days = min(max(request.args.get("days", 30, type=int), 1), 365)
    limit = min(max(request.args.get("limit", 15, type=int), 1), 100)
    payload = _command_analytics_payload(days=days, limit=limit)
    status = 200 if payload.get("ok") else 500
    return jsonify(payload), status


@dashboard.route("/api/search-aliases", methods=["GET", "POST"])
@api_auth_required
def api_search_aliases():
    """List or upsert typo/synonym aliases for item and villager search."""
    db = get_db()
    try:
        if request.method == "GET":
            rows = db.execute(
                "SELECT alias, target, kind, created_by, created_at FROM search_aliases ORDER BY kind, alias"
            ).fetchall()
            return jsonify({"ok": True, "items": [dict(row) for row in rows]})

        data = request.get_json(silent=True) or {}
        alias = re.sub(r"\s+", " ", str(data.get("alias") or "").lower()).strip()
        target = re.sub(r"\s+", " ", str(data.get("target") or "").lower()).strip()
        kind = (data.get("kind") or "item").strip().lower()
        if kind not in {"item", "villager"}:
            return jsonify({"ok": False, "error": "kind must be item or villager"}), 400
        if not alias or not target:
            return jsonify({"ok": False, "error": "alias and target are required"}), 400
        now = int(time.time())
        db.execute(
            "INSERT INTO search_aliases (alias, target, kind, created_by, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(alias, kind) DO UPDATE SET target=excluded.target, created_by=excluded.created_by, created_at=excluded.created_at",
            (alias, target, kind, session.get("discord_user_id") or "", now),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    _record_audit_event("search_alias_upsert", alias, {"target": target, "kind": kind})
    return jsonify({"ok": True, "alias": alias, "target": target, "kind": kind})


def _send_dodo_queue_dm(user_id: str, island_name: str):
    token = str(Config.DISCORD_TOKEN or "").strip()
    if not token:
        return
    try:
        auth = token if token.lower().startswith("bot ") else f"Bot {token}"
        headers = {"Authorization": auth, "Content-Type": "application/json", "User-Agent": _DISCORD_USER_AGENT}
        
        resp = discord_request(
            "https://discord.com/api/v10/users/@me/channels",
            method="POST",
            headers=headers,
            data=json.dumps({"recipient_id": user_id}).encode("utf-8"),
            timeout=5,
        )
        ch_id = json.loads(resp.body).get("id")
        if not ch_id:
            return
            
        discord_request(
            f"https://discord.com/api/v10/channels/{ch_id}/messages",
            method="POST",
            headers=headers,
            data=json.dumps({
                "content": f"✈️ **It's your turn!** You have been called for **{island_name}**. Please check the dashboard to reveal your Dodo code now."
            }).encode("utf-8"),
            timeout=5,
        )
    except Exception as e:
        logger.warning("Failed to send queue DM to %s: %s", user_id, e)


@dashboard.route("/api/dodo-queue", methods=["GET", "PATCH"])
@api_auth_required
def api_dodo_queue():
    """List and moderate Dodo queue entries."""
    db = get_db()
    try:
        if request.method == "GET":
            status_filter = (request.args.get("status") or "waiting,called,investigating").strip()
            statuses = [item.strip() for item in status_filter.split(",") if item.strip()]
            if not statuses:
                statuses = ["waiting", "called", "investigating"]
            placeholders = ",".join("?" for _ in statuses)
            rows = db.execute(
                f"SELECT * FROM dodo_queue WHERE status IN ({placeholders}) ORDER BY created_at ASC LIMIT 100",
                statuses,
            ).fetchall()
            return jsonify({"ok": True, "items": [dict(row) for row in rows]})

        data = request.get_json(silent=True) or {}
        entry_id = int(data.get("id") or 0)
        status = (data.get("status") or "").strip().lower()
        note = (data.get("note") or "").strip()[:500]
        if not entry_id or status not in {"waiting", "called", "done", "cancelled", "investigating"}:
            return jsonify({"ok": False, "error": "id and valid status are required"}), 400
            
        row = db.execute("SELECT * FROM dodo_queue WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "entry not found"}), 404
        prev_status = row.get("status")

        cur = db.execute(
            "UPDATE dodo_queue SET status = ?, note = ?, updated_at = ? WHERE id = ?",
            (status, note, int(time.time()), entry_id),
        )
        db.commit()
        
        if status == "called" and prev_status != "called":
            uid = str(row.get("user_id") or "")
            iname = str(row.get("island_name") or "")
            if uid and iname:
                threading.Thread(
                    target=_send_dodo_queue_dm,
                    args=(uid, iname),
                    daemon=True,
                ).start()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    _record_audit_event("dodo_queue_update", str(entry_id), {"status": status, "note": bool(note)})
    return jsonify({"ok": True, "updated": max(cur.rowcount, 0)})


@dashboard.route("/api/access-simulator", methods=["POST"])
@api_auth_required
def api_access_simulator():
    """Alias for the role/access simulator with audit logging."""
    response = api_island_test_access()
    _record_audit_event("access_simulator_run", None, {"payload_keys": sorted((request.get_json(silent=True) or {}).keys())})
    return response


@dashboard.route("/api/status-summary", methods=["GET"])
@api_auth_required
def api_status_summary():
    """Return live island status counts and per-island effective statuses."""
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = _merge_dashboard_fs_islands([_row_to_island_dict(dict(r)) for r in rows])
        bot_status = _load_bot_status_map(db)
    except Exception:
        db_islands = []
        bot_status = {}
    finally:
        db.close()

    island_count     = len(db_islands)
    online_count     = 0
    refreshing_count = 0
    offline_count    = 0
    islands_out      = []
    access_problem_count = 0

    for isl in db_islands:
        isl["discord_bot_online"] = bot_status.get(isl.get("id", ""))
        s = _effective_status(isl)
        access_status = _island_access_status(isl)
        if access_status["warnings"]:
            access_problem_count += 1
        islands_out.append({
            "id": isl.get("id", ""),
            "name": isl.get("name", ""),
            "status": s,
            "access_source": access_status["access_source"],
            "role_count": access_status["role_count"],
            "access_warnings": access_status["warnings"],
        })
        if s == STATUS_ONLINE:
            online_count += 1
        elif s == STATUS_REFRESHING:
            refreshing_count += 1
        else:
            offline_count += 1

    def _pct(count):
        return round(count * 100 / island_count) if island_count else 0

    return jsonify({
        "island_count":     island_count,
        "online_count":     online_count,
        "refreshing_count": refreshing_count,
        "offline_count":    offline_count,
        "online_pct":       _pct(online_count),
        "refreshing_pct":   _pct(refreshing_count),
        "off_pct":          _pct(offline_count),
        "access_problem_count": access_problem_count,
        "islands":          islands_out,
    })


@dashboard.route("/api/island-health", methods=["GET"])
@api_auth_required
def api_island_health():
    """Return per-island operational health signals."""
    stale_minutes = max(request.args.get("stale_minutes", 15, type=int), 1)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = _merge_dashboard_fs_islands([_row_to_island_dict(dict(r)) for r in rows])
        bot_status = _load_bot_status_map(db)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "islands": []}), 500
    finally:
        db.close()

    items = []
    for island in db_islands:
        updated_raw = island.get("updated_at") or ""
        updated_dt = None
        if updated_raw:
            with contextlib.suppress(Exception):
                updated_dt = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
        warnings = []
        status = _effective_status(island)
        bot_online = bot_status.get(island.get("id", ""))
        if status == STATUS_OFFLINE:
            warnings.append("offline")
        if bot_online is False:
            warnings.append("bot_offline")
        if updated_dt and updated_dt < stale_cutoff:
            warnings.append("stale_update")
        access_status = _island_access_status(island)
        warnings.extend(access_status["warnings"])
        items.append({
            "id": island.get("id", ""),
            "name": island.get("name", ""),
            "status": status,
            "visitors": island.get("visitors") or 0,
            "discord_bot_online": bot_online,
            "updated_at": updated_raw,
            "access_source": access_status["access_source"],
            "warnings": warnings,
            "ok": not warnings,
        })

    return jsonify({
        "ok": True,
        "stale_minutes": stale_minutes,
        "problem_count": sum(1 for item in items if item["warnings"]),
        "islands": items,
    })


@dashboard.route("/api/user-trust-profile", methods=["GET"])
@api_auth_required
def api_user_trust_profile():
    """Return a compact moderation/trust profile for a Discord user."""
    user_id = (request.args.get("user_id") or "").strip()
    guild_id = (request.args.get("guild_id") or str(Config.GUILD_ID or "")).strip()
    if not user_id:
        return jsonify({"ok": False, "error": "user_id is required"}), 400

    db = get_db()
    try:
        params = [user_id]
        guild_clause = ""
        if guild_id:
            guild_clause = " AND guild_id = ?"
            params.append(guild_id)

        visit_summary = db.execute(
            "SELECT COUNT(*) AS total_visits, "
            "SUM(CASE WHEN authorized = 1 THEN 1 ELSE 0 END) AS authorized_visits, "
            "SUM(CASE WHEN authorized = 0 THEN 1 ELSE 0 END) AS unauthorized_visits, "
            "MAX(timestamp) AS last_visit_at "
            f"FROM island_visits WHERE user_id = ?{guild_clause}",
            params,
        ).fetchone()
        warning_summary = db.execute(
            "SELECT COUNT(*) AS total_actions, "
            "SUM(CASE WHEN UPPER(action_type) = 'WARN' THEN 1 ELSE 0 END) AS warnings, "
            "SUM(CASE WHEN UPPER(action_type) = 'KICK' THEN 1 ELSE 0 END) AS kicks, "
            "SUM(CASE WHEN UPPER(action_type) = 'BAN' THEN 1 ELSE 0 END) AS bans, "
            "MAX(timestamp) AS last_action_at "
            f"FROM warnings WHERE user_id = ?{guild_clause}",
            params,
        ).fetchone()
        recent_visits = db.execute(
            "SELECT ign, destination, authorized, timestamp "
            f"FROM island_visits WHERE user_id = ?{guild_clause} "
            "ORDER BY timestamp DESC LIMIT 10",
            params,
        ).fetchall()
        recent_actions = db.execute(
            "SELECT action_type, reason, mod_id, timestamp "
            f"FROM warnings WHERE user_id = ?{guild_clause} "
            "ORDER BY timestamp DESC LIMIT 10",
            params,
        ).fetchall()
        dodo_reveals = _optional_rows(
            db,
            "SELECT island_clean, message_url, username, nickname, created_at "
            "FROM dodo_reveal_messages WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
            (user_id,),
        )
        identity_events = _optional_rows(
            db,
            "SELECT event_type, old_display_name, new_display_name, created_at "
            f"FROM member_identity_events WHERE user_id = ?{guild_clause} "
            "ORDER BY created_at DESC LIMIT 10",
            params,
        )
        latest_authorized_visit = db.execute(
            "SELECT MAX(timestamp) AS authorized_at "
            f"FROM island_visits WHERE user_id = ?{guild_clause} AND authorized = 1",
            params,
        ).fetchone()
        known_igns = db.execute(
            "SELECT ign, COUNT(*) AS visit_count, MAX(timestamp) AS last_seen_at "
            f"FROM island_visits WHERE user_id = ?{guild_clause} "
            "GROUP BY ign ORDER BY visit_count DESC, last_seen_at DESC LIMIT 10",
            params,
        ).fetchall()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    total_visits = int(visit_summary["total_visits"] or 0)
    total_actions = int(warning_summary["total_actions"] or 0)
    warnings_count = int(warning_summary["warnings"] or 0)
    kicks_count = int(warning_summary["kicks"] or 0)
    bans_count = int(warning_summary["bans"] or 0)
    unauthorized_count = int(visit_summary["unauthorized_visits"] or 0)
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
    latest_authorized_at = int(latest_authorized_visit["authorized_at"] or 0) if latest_authorized_visit else 0
    actionable_identity_events = [
        row
        for row in identity_events
        if int(row["created_at"] or 0) > latest_authorized_at
    ]
    suppressed_identity_events = len(identity_events) - len(actionable_identity_events)
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
            "label": "Authorized visit" if row["authorized"] else "Unknown visit",
            "title": f"{row['ign']} visited {row['destination']}",
            "timestamp": _ts_to_str(row["timestamp"]),
            "timestamp_raw": row["timestamp"],
            "severity": "info" if row["authorized"] else "warning",
            "payload": {
                "ign": row["ign"],
                "destination": row["destination"],
                "authorized": bool(row["authorized"]),
            },
        })
    for row in recent_actions:
        action = (row["action_type"] or "WARN").upper()
        timeline.append({
            "type": "moderation",
            "label": action,
            "title": row["reason"] or action,
            "timestamp": _ts_to_str(row["timestamp"]),
            "timestamp_raw": row["timestamp"],
            "severity": "critical" if action == "BAN" else "warning" if action in {"WARN", "KICK"} else "attention",
            "payload": {
                "mod_id": row["mod_id"],
                "reason": row["reason"],
                "action_type": action,
            },
        })
    for row in dodo_reveals:
        timeline.append({
            "type": "dodo_reveal",
            "label": "Dodo reveal",
            "title": f"Revealed {row['island_clean']}",
            "timestamp": _ts_to_str(row["created_at"]),
            "timestamp_raw": row["created_at"],
            "severity": "info",
            "payload": dict(row),
        })
    for row in identity_events:
        cleared = bool(latest_authorized_at and int(row["created_at"] or 0) <= latest_authorized_at)
        payload = dict(row)
        payload["cleared_by_authorization"] = cleared
        payload["cleared_authorized_at"] = latest_authorized_at if cleared else None
        timeline.append({
            "type": "identity",
            "label": row["event_type"],
            "title": f"{row['old_display_name'] or 'Unknown'} -> {row['new_display_name'] or 'Unknown'}",
            "timestamp": _ts_to_str(row["created_at"]),
            "timestamp_raw": row["created_at"],
            "severity": "info" if cleared else "attention",
            "payload": payload,
        })
    timeline.sort(key=lambda item: int(item.get("timestamp_raw") or 0), reverse=True)

    return jsonify({
        "ok": True,
        "user_id": user_id,
        "guild_id": guild_id,
        "user_name": _resolve_discord_username(user_id),
        "risk_score": risk_score,
        "trust_state": trust_state,
        "status_label": trust_state.replace("_", " ").title(),
        "risk_flags": risk_flags,
        "summary": {
            "total_visits": total_visits,
            "authorized_visits": int(visit_summary["authorized_visits"] or 0),
            "unauthorized_visits": unauthorized_count,
            "total_actions": total_actions,
            "warnings": warnings_count,
            "kicks": kicks_count,
            "bans": bans_count,
            "dodo_reveals": len(dodo_reveals),
            "known_igns": len(known_igns),
            "recent_identity_events": len(actionable_identity_events),
            "suppressed_identity_events": suppressed_identity_events,
            "last_visit_at": _ts_to_str(visit_summary["last_visit_at"]),
            "last_action_at": _ts_to_str(warning_summary["last_action_at"]),
        },
        "timeline": timeline[:30],
        "known_igns": [
            {
                "ign": row["ign"],
                "visit_count": row["visit_count"],
                "last_seen_at": _ts_to_str(row["last_seen_at"]),
            }
            for row in known_igns
        ],
        "recent_visits": [
            {
                "ign": row["ign"],
                "destination": row["destination"],
                "authorized": bool(row["authorized"]),
                "timestamp": _ts_to_str(row["timestamp"]),
            }
            for row in recent_visits
        ],
        "recent_actions": [
            {
                "action_type": row["action_type"],
                "reason": row["reason"],
                "mod_id": row["mod_id"],
                "timestamp": _ts_to_str(row["timestamp"]),
            }
            for row in recent_actions
        ],
        "recent_dodo_reveals": [
            {
                "island": row["island_clean"],
                "message_url": row["message_url"],
                "username": row["username"],
                "nickname": row["nickname"],
                "created_at": _ts_to_str(row["created_at"]),
            }
            for row in dodo_reveals
        ],
        "recent_identity_events": [
            {
                "event_type": row["event_type"],
                "old_display_name": row["old_display_name"],
                "new_display_name": row["new_display_name"],
                "created_at": _ts_to_str(row["created_at"]),
                "cleared_by_authorization": bool(latest_authorized_at and int(row["created_at"] or 0) <= latest_authorized_at),
            }
            for row in identity_events
        ],
    })


@dashboard.route("/api/islands", methods=["GET"])
@api_auth_required
def api_islands_list():
    """List all islands."""
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = _merge_dashboard_fs_islands([_row_to_island_dict(dict(r)) for r in rows])
        bot_status = _load_bot_status_map(db)
    except Exception:
        db_islands = []
        bot_status = {}
    finally:
        db.close()

    result = []
    for isl in db_islands:
        isl["discord_bot_online"] = bot_status.get(isl.get("id", ""))
        isl["status"] = _effective_status(isl)
        access_info = island_access.resolved_island_required_roles(
            isl.get("name"),
            isl.get("cat"),
            isl.get("required_roles") or [],
            isl.get("type"),
            isl.get("channel_id"),
        )
        isl["required_roles"] = access_info.required_roles
        isl["channel_id"] = access_info.channel_id
        isl["access_source"] = access_info.access_source
        result.append(_island_api_dict(isl))
    return jsonify(result)


@dashboard.route("/api/islands/role-status", methods=["GET"])
@api_auth_required
def api_island_role_status():
    """Return Discord role-gating diagnostics for dashboard island rows."""
    force_refresh = request.args.get("refresh") in {"1", "true", "yes"}
    if force_refresh:
        island_access.clear_access_caches()
    islands = _load_dashboard_islands()
    statuses = [_island_access_status(isl, force_refresh=force_refresh) for isl in islands]
    problem_count = sum(1 for item in statuses if item["warnings"])
    return jsonify({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "discord_configured": bool(Config.DISCORD_TOKEN and Config.GUILD_ID),
        "category_id": str(Config.CATEGORY_ID or ""),
        "total": len(statuses),
        "member_islands": sum(1 for item in statuses if item["is_member"]),
        "problem_count": problem_count,
        "items": statuses,
    })


@dashboard.route("/api/islands/sync-roles", methods=["POST"])
@api_auth_required
def api_island_sync_roles():
    """Refresh stored island required_roles/channel_id from Discord channel permissions."""
    islands = _load_dashboard_islands()
    db = get_db()
    try:
        summary = island_access.sync_island_role_cache(db, islands, force_refresh=True)
        db.commit()
    finally:
        db.close()
    summary.update({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "discord_configured": bool(Config.DISCORD_TOKEN and Config.GUILD_ID),
    })
    return jsonify(summary)


@dashboard.route("/api/islands/test-access", methods=["POST"])
@api_auth_required
def api_island_test_access():
    """Test island access for a set of Discord role IDs or a Discord user ID."""
    data = request.get_json(silent=True) or {}
    roles = [str(role) for role in data.get("roles", []) if str(role)]
    user_id = str(data.get("user_id") or "").strip()
    is_admin = bool(data.get("is_admin"))
    is_mod_user = bool(data.get("is_mod"))

    if user_id and not roles:
        member = island_access.discord_api_json(f"/guilds/{Config.GUILD_ID}/members/{user_id}")
        if isinstance(member, dict):
            roles = [str(role) for role in member.get("roles", []) if str(role)]
        else:
            return jsonify({"error": "Discord member not found or unavailable"}), 404

    if not roles and not is_admin and not is_mod_user:
        return jsonify({"error": "Provide roles, user_id, is_mod, or is_admin"}), 400

    is_mod_user = is_mod_user or is_admin or island_access.is_mod(roles)
    role_names = island_access.get_guild_role_names()
    islands = _load_dashboard_islands()
    results = []
    for isl in islands:
        info = island_access.resolved_island_required_roles(
            isl.get("name"),
            isl.get("cat"),
            isl.get("required_roles") or [],
            isl.get("type"),
            isl.get("channel_id"),
        )
        accessible = island_access.has_island_access(roles, info.required_roles, is_mod_user)
        matched_role_ids = sorted(set(roles) & set(info.required_roles))
        results.append({
            "id": isl.get("id"),
            "name": isl.get("name"),
            "type": isl.get("type"),
            "cat": isl.get("cat"),
            "channel_id": info.channel_id,
            "access_source": info.access_source,
            "accessible": accessible,
            "required_roles": [island_access.role_payload(role_id, role_names) for role_id in info.required_roles],
            "matched_roles": [island_access.role_payload(role_id, role_names) for role_id in matched_role_ids],
        })

    return jsonify({
        "user_id": user_id or None,
        "roles": [island_access.role_payload(role_id, role_names) for role_id in roles],
        "is_mod": is_mod_user,
        "accessible_count": sum(1 for item in results if item["accessible"]),
        "items": results,
    })


@dashboard.route("/api/islands", methods=["POST"])
@api_auth_required
def api_island_create():
    """Create or upsert a full island record."""
    data      = request.get_json(silent=True) or {}
    island_id = (data.get("id") or data.get("name", "")).strip().lower()
    name      = (data.get("name") or island_id).strip().upper()
    display_name = (data.get("display_name") or data.get("displayName") or "").strip() or None
    is_visible = _json_bool(data, "is_visible", _json_bool(data, "isVisible", True))
    isl_type  = data.get("type", "")
    items     = data.get("items", [])
    theme     = data.get("theme", "teal")
    cat       = data.get("cat", "public")
    desc      = data.get("description", "")
    seasonal  = data.get("seasonal", "")
    status    = data.get("status", "OFFLINE")
    visitors  = int(data.get("visitors", 0))
    dodo_code = data.get("dodoCode") or data.get("dodo_code") or None
    map_url   = data.get("mapUrl")   or data.get("map_url")   or None

    if not island_id:
        return jsonify({"error": "id or name is required"}), 400
    if cat    not in ALLOWED_CATEGORIES: return jsonify({"error": f"cat must be one of {ALLOWED_CATEGORIES}"}),  400
    if theme  not in ALLOWED_THEMES:     return jsonify({"error": f"theme must be one of {ALLOWED_THEMES}"}),    400
    if status not in ALLOWED_STATUSES:   return jsonify({"error": f"status must be one of {ALLOWED_STATUSES}"}), 400

    if (dodo_code or "").strip().upper() == REFRESHING_DODO_CODE:
        status = STATUS_REFRESHING

    db = get_db()
    try:
        db.execute(
            """INSERT INTO islands
                   (id, name, display_name, is_visible, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, display_name=excluded.display_name,
                   is_visible=excluded.is_visible, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (island_id, name, display_name, int(is_visible), isl_type, json.dumps(items),
             theme, cat, desc, seasonal, status, visitors, dodo_code, map_url,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "ok", "id": island_id}), 201


@dashboard.route("/api/islands/<name>", methods=["GET"])
@api_auth_required
def api_island_get(name):
    """Get a single island record."""
    island_id = name.lower()
    db = get_db()
    try:
        row = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
    finally:
        db.close()
    if row:
        island = _row_to_island_dict(dict(row))
    else:
        fs_match = None
        for fs in _collect_fs_islands().values():
            if str(fs.get("name", "")).lower() == island_id:
                fs_match = fs
                break
        if not fs_match:
            return jsonify({"error": f'Island "{name}" not found'}), 404
        island = _merge_island(_fs_island_stub(fs_match), fs_match)
    access_info = island_access.resolved_island_required_roles(
        island.get("name"),
        island.get("cat"),
        island.get("required_roles") or [],
        island.get("type"),
        island.get("channel_id"),
    )
    island["required_roles"] = access_info.required_roles
    island["channel_id"] = access_info.channel_id
    island["access_source"] = access_info.access_source
    payload = _island_detail_api_dict(island)
    payload["access_status"] = _island_access_status(island)
    return jsonify(payload)


@dashboard.route("/api/islands/<name>", methods=["PUT"])
@api_auth_required
def api_island_update(name):
    """Update a single island record (partial or full)."""
    island_id = name.lower()
    data      = request.get_json(silent=True) or {}

    # Open the DB ONCE. Let Flask's app context teardown handle the closing.
    db = get_db()
    
    row      = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
    existing = _row_to_island_dict(dict(row)) if row else {}

    cat    = data.get("cat",    existing.get("cat",    "public"))
    theme  = data.get("theme",  existing.get("theme",  "teal"))
    status = data.get("status", existing.get("status", "OFFLINE"))

    if cat    not in ALLOWED_CATEGORIES: return jsonify({"error": f"cat must be one of {ALLOWED_CATEGORIES}"}),  400
    if theme  not in ALLOWED_THEMES:     return jsonify({"error": f"theme must be one of {ALLOWED_THEMES}"}),    400
    if status not in ALLOWED_STATUSES:   return jsonify({"error": f"status must be one of {ALLOWED_STATUSES}"}), 400

    items_in = data.get("items", existing.get("items", []))
    if isinstance(items_in, str):
        try:
            items_in = json.loads(items_in)
        except ValueError:
            items_in = [i.strip() for i in items_in.split(",") if i.strip()]

    display_name = data.get("display_name", data.get("displayName", existing.get("display_name")))
    display_name = (display_name or "").strip() or None
    
    is_visible = _json_bool(data, "is_visible", _json_bool(data, "isVisible", existing.get("is_visible", True)))

    dodo_code = data.get("dodoCode", data.get("dodo_code", existing.get("dodo_code")))
    if (dodo_code or "").strip().upper() == REFRESHING_DODO_CODE:
        status = STATUS_REFRESHING

    # Safely handle the visitors cast to avoid 500s on empty strings/None
    raw_visitors = data.get("visitors", existing.get("visitors", 0))
    try:
        visitors_count = int(raw_visitors) if raw_visitors not in [None, ""] else 0
    except (ValueError, TypeError):
        visitors_count = 0

    try:
        db.execute(
            """INSERT INTO islands
                   (id, name, display_name, is_visible, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, display_name=excluded.display_name,
                   is_visible=excluded.is_visible, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (
                island_id,
                data.get("name", existing.get("name", island_id)).upper(),
                display_name,
                1 if is_visible else 0, # Safer bool to int cast
                data.get("type", existing.get("type", "")),
                json.dumps(items_in),
                theme, 
                cat,
                data.get("description", existing.get("description", "")),
                data.get("seasonal",    existing.get("seasonal", "")),
                status,
                visitors_count,
                dodo_code,
                existing.get("map_url"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
    except Exception as e:
        # If it still 500s, this will expose exactly why in your server logs
        print(f"[!] DB Execute Error on Island Update: {e}")
        return jsonify({"error": "Database operation failed."}), 500
        
    return jsonify({"status": "ok", "id": island_id})


    
@dashboard.route("/api/islands/<name>", methods=["DELETE"])
@api_auth_required
def api_island_delete(name):
    """Delete stored metadata for an island (does not touch the filesystem)."""
    island_id = name.lower()
    db = get_db()
    try:
        db.execute("DELETE FROM islands WHERE id = ?", (island_id,))
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "deleted", "id": island_id})


@dashboard.route("/api/islands/<name>/map", methods=["POST"])
@api_auth_required
def api_island_upload_map(name):
    """Upload an island map image to Cloudflare R2 and store the URL."""
    island_id = name.lower()

    if "map" not in request.files:
        return jsonify({"error": "No file part named 'map'"}), 400
    file = request.files["map"]
    if not file or not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    file_bytes = file.read()
    if len(file_bytes) > MAX_MAP_SIZE:
        return jsonify({"error": f"File too large (max {MAX_MAP_SIZE // 1024 // 1024} MB)"}), 413

    content_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "image/png"
    if content_type not in ALLOWED_MAP_TYPES:
        return jsonify({"error": f"Unsupported type: {content_type}. Allowed: {sorted(ALLOWED_MAP_TYPES)}"}), 415

    try:
        map_url = _upload_map_to_r2(file_bytes, content_type, island_id)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except (ClientError, NoCredentialsError) as exc:
        logger.error("R2 upload failed for island %s: %s", island_id, exc)
        return jsonify({"error": "R2 upload failed", "details": str(exc)}), 502

    db = get_db()
    try:
        db.execute(
            "UPDATE islands SET map_url = ?, updated_at = ? WHERE id = ?",
            (map_url, datetime.now(timezone.utc).isoformat(), island_id),
        )
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            db.execute(
                "INSERT INTO islands (id, name, map_url, updated_at) VALUES (?,?,?,?)",
                (island_id, island_id.upper(), map_url, datetime.now(timezone.utc).isoformat()),
            )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "uploaded", "id": island_id, "map_url": map_url})


@dashboard.route("/api/islands/sync-maps", methods=["POST"])
@api_auth_required
def api_sync_maps():
    """Scan the R2 bucket for existing map images and back-fill map_url in the DB.

    For every object under the ``maps/`` prefix in the configured R2 bucket,
    derive the island id from the object key (e.g. ``maps/alapaap.jpg``
    → island id ``alapaap``), construct the public URL, and write it into the
    ``islands`` table.  Rows that already have a ``map_url`` are also updated
    so that any manually renamed/re-uploaded files are corrected.

    Returns a JSON summary ``{"synced": N, "skipped": N, "errors": [...]}``.
    """
    client = _get_r2_client()
    if client is None:
        return jsonify({"error": "R2 is not configured"}), 503

    base = (Config.R2_PUBLIC_URL or "").rstrip("/")
    if not base:
        return jsonify({"error": "R2_PUBLIC_URL is not configured"}), 503

    # Collect all objects under maps/ prefix (handle paginated responses)
    keys: list[str] = []
    kwargs: dict = {"Bucket": Config.R2_BUCKET_NAME, "Prefix": "maps/"}
    while True:
        try:
            resp = client.list_objects_v2(**kwargs)
        except (ClientError, NoCredentialsError) as exc:
            return jsonify({"error": "R2 list failed", "details": str(exc)}), 502
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
        if resp.get("IsTruncated"):
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        else:
            break

    synced = 0
    skipped = 0
    errors: list[str] = []
    now = datetime.now(timezone.utc).isoformat()

    db = get_db()
    try:
        for key in keys:
            # key looks like "maps/alapaap.jpg" or "maps/subdirectory/..." – skip nested
            parts = key.split("/")
            if len(parts) != 2:
                skipped += 1
                continue
            filename = parts[1]
            if not filename:
                skipped += 1
                continue
            # Strip extension to get island id
            island_id = filename.rsplit(".", 1)[0].lower()
            if not island_id:
                skipped += 1
                continue
            map_url = f"{base}/{key}"
            try:
                db.execute(
                    "UPDATE islands SET map_url = ?, updated_at = ? WHERE id = ?",
                    (map_url, now, island_id),
                )
                if db.execute("SELECT changes()").fetchone()[0] == 0:
                    # Island row doesn't exist yet — create a minimal one
                    db.execute(
                        "INSERT OR IGNORE INTO islands (id, name, map_url, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (island_id, island_id.upper(), map_url, now),
                    )
                synced += 1
            except Exception as exc:
                errors.append(f"{island_id}: {exc}")
        db.commit()
    finally:
        db.close()

    return jsonify({"synced": synced, "skipped": skipped, "errors": errors})


@dashboard.route("/api/analytics", methods=["GET"])
@api_auth_required
def api_analytics():
    """Return full analytics dataset as JSON.

    Accepts an optional ``island_type`` query parameter (``free`` or ``sub``)
    to filter results to a specific island type.
    """
    island_type_filter = request.args.get("island_type", "").lower()
    if island_type_filter not in ("free", "sub"):
        island_type_filter = ""

    it_clause = " AND island_type = ?" if island_type_filter else ""
    it_params = [island_type_filter] if island_type_filter else []

    db = get_db()
    try:
        top_islands = [
            dict(r) for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY destination ORDER BY visit_count DESC LIMIT 10",
                it_params,
            ).fetchall()
        ]
        top_travelers = [
            dict(r) for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY ign ORDER BY visit_count DESC LIMIT 10",
                it_params,
            ).fetchall()
        ]
        visits_by_day = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause} "
                "GROUP BY day ORDER BY day",
                it_params,
            ).fetchall()
        ]
        visits_by_day_30 = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause} "
                "GROUP BY day ORDER BY day",
                it_params,
            ).fetchall()
        ]
        visits_by_hour = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS hour, "
                "COUNT(*) AS count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY hour ORDER BY hour",
                it_params,
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count "
            f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "GROUP BY authorized",
            it_params,
        ).fetchall()
        cat_raw = db.execute(
            "SELECT isl.cat, COUNT(*) AS visit_count "
            "FROM island_visits iv "
            "JOIN islands isl ON LOWER(iv.destination) = isl.id "
            f"{'WHERE iv.island_type = ?' if island_type_filter else ''} "
            "GROUP BY isl.cat",
            it_params,
        ).fetchall()
        dow_raw = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%w', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS dow, "
                "COUNT(*) AS count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY dow ORDER BY dow",
                it_params,
            ).fetchall()
        ]
        _VALID_COUNT_KEYS = {"warn_count", "kick_count", "ban_count", "note_count"}
        _VALID_ACTIONS    = {"WARN", "KICK", "BAN", "NOTE", "ADMIT", "DISMISS"}

        def _top_by_action(action: str, count_key: str):
            if count_key not in _VALID_COUNT_KEYS:
                raise ValueError(f"Invalid count_key: {count_key!r}")
            if action not in _VALID_ACTIONS:
                raise ValueError(f"Invalid action: {action!r}")
            if island_type_filter:
                rows = db.execute(
                    f"SELECT w.user_id, COUNT(*) AS {count_key} "
                    "FROM warnings w "
                    "JOIN island_visits iv ON w.visit_id = iv.id "
                    "WHERE w.user_id IS NOT NULL AND iv.island_type = ? AND UPPER(w.action_type) = ? "
                    f"GROUP BY w.user_id ORDER BY {count_key} DESC LIMIT 10",
                    (island_type_filter, action),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT user_id, COUNT(*) AS {count_key} "
                    "FROM warnings WHERE user_id IS NOT NULL AND UPPER(action_type) = ? "
                    f"GROUP BY user_id ORDER BY {count_key} DESC LIMIT 10",
                    (action,),
                ).fetchall()
            return [dict(r) for r in rows]

        top_warned = _top_by_action("WARN", "warn_count")
        top_kicked = _top_by_action("KICK", "kick_count")
        top_banned = _top_by_action("BAN",  "ban_count")
        top_noted  = _top_by_action("NOTE", "note_count")

        all_action_user_ids = (
            [r["user_id"] for r in top_warned]
            + [r["user_id"] for r in top_kicked]
            + [r["user_id"] for r in top_banned]
            + [r["user_id"] for r in top_noted]
        )
        action_name_map = _resolve_discord_usernames(all_action_user_ids)
        for collection in (top_warned, top_kicked, top_banned, top_noted):
            for row in collection:
                row["user_name"] = action_name_map.get(str(row["user_id"]), str(row["user_id"]))

        visits_today = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours'){it_clause}",
            it_params,
        ).fetchone()[0]
        visits_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        if island_type_filter:
            warnings_week = db.execute(
                "SELECT COUNT(*) FROM warnings w "
                "JOIN island_visits iv ON w.visit_id = iv.id "
                "WHERE w.timestamp > strftime('%s','now','-7 days') AND iv.island_type = ?",
                it_params,
            ).fetchone()[0]
            warnings_today = db.execute(
                "SELECT COUNT(*) FROM warnings w "
                "JOIN island_visits iv ON w.visit_id = iv.id "
                "WHERE w.timestamp > strftime('%s','now','+8 hours','start of day','-8 hours') "
                "AND iv.island_type = ?",
                it_params,
            ).fetchone()[0]
        else:
            warnings_week = db.execute(
                "SELECT COUNT(*) FROM warnings WHERE timestamp > strftime('%s','now','-7 days')"
            ).fetchone()[0]
            warnings_today = db.execute(
                "SELECT COUNT(*) FROM warnings "
                "WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours')"
            ).fetchone()[0]
        visits_prev_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-14 days') "
            f"AND timestamp <= strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        peak_hour_row = db.execute(
            "SELECT CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS hour, "
            "COUNT(*) AS cnt "
            f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "GROUP BY hour ORDER BY cnt DESC LIMIT 1",
            it_params,
        ).fetchone()
        peak_hour = peak_hour_row["hour"] if peak_hour_row else None
        avg_visits_30d_row = db.execute(
            "SELECT COUNT(*) * 1.0 / 30 AS avg FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause}",
            it_params,
        ).fetchone()
        avg_visits_30d = round(avg_visits_30d_row["avg"] or 0, 1)
        new_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit "
            f"  FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "  GROUP BY ign"
            f") WHERE first_visit > strftime('%s','now','-7 days')",
            it_params,
        ).fetchone()[0]
        total_unique_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        new_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit "
            f"  FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "  GROUP BY ign"
            f") WHERE first_visit > strftime('%s','now','-30 days')",
            it_params,
        ).fetchone()[0]
        total_unique_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        total_unique_travelers = db.execute(
            f"SELECT COUNT(DISTINCT ign) FROM island_visits"
            f"{' WHERE island_type = ?' if island_type_filter else ''}",
            it_params,
        ).fetchone()[0]
        total_unique_islands = db.execute(
            f"SELECT COUNT(DISTINCT destination) FROM island_visits"
            f"{' WHERE island_type = ?' if island_type_filter else ''}",
            it_params,
        ).fetchone()[0]
    except Exception as exc:
        logger.exception("Failed to build dashboard analytics payload")
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    auth_map  = {r["authorized"]: r["count"] for r in auth_raw}
    cat_map   = {r["cat"]: r["visit_count"] for r in cat_raw}
    hour_map  = {r["hour"]: r["count"] for r in visits_by_hour}
    dow_map   = {r["dow"]: r["count"] for r in dow_raw}

    auth_stats = {"authorized": auth_map.get(1, 0), "unauthorized": auth_map.get(0, 0)}
    cat_stats  = {"public": cat_map.get("public", 0), "member": cat_map.get("member", 0)}
    visits_by_hour_full = [{"hour": h, "count": hour_map.get(h, 0)} for h in range(24)]
    visits_by_dow = [{"dow": d, "label": _DOW_LABELS[d], "count": dow_map.get(d, 0)} for d in range(7)]

    total_visits  = auth_stats["authorized"] + auth_stats["unauthorized"]
    auth_rate_pct = round(auth_stats["authorized"] / total_visits * 100) if total_visits else None
    warn_rate_week = round(warnings_week / visits_week * 100, 1) if visits_week else 0.0

    returning_7d  = max(total_unique_7d  - new_7d,  0)
    returning_30d = max(total_unique_30d - new_30d, 0)

    return jsonify({
        "ok": True,
        # Basic summary (backward-compatible)
        "top_islands":         top_islands,
        "top_travelers":       top_travelers,
        "authorized_visits":   auth_stats["authorized"],
        "unauthorized_visits": auth_stats["unauthorized"],
        # Extended analytics
        "visits_by_day":       visits_by_day,
        "visits_by_day_30":    visits_by_day_30,
        "visits_by_hour":      visits_by_hour_full,
        "visits_by_dow":       visits_by_dow,
        "auth_stats":          auth_stats,
        "cat_stats":           cat_stats,
        "top_warned":          top_warned,
        "top_kicked":          top_kicked,
        "top_banned":          top_banned,
        "top_noted":           top_noted,
        "visits_today":        visits_today,
        "visits_week":         visits_week,
        "warnings_week":       warnings_week,
        "warnings_today":      warnings_today,
        "visits_prev_week":    visits_prev_week,
        "peak_hour":           peak_hour,
        "avg_visits_30d":      avg_visits_30d,
        "total_unique_travelers": total_unique_travelers,
        "total_unique_islands":   total_unique_islands,
        "auth_rate_pct":       auth_rate_pct,
        "warn_rate_week":      warn_rate_week,
        "new_returning": {
            "new_7d":        new_7d,
            "returning_7d":  returning_7d,
            "total_7d":      total_unique_7d,
            "new_30d":       new_30d,
            "returning_30d": returning_30d,
            "total_30d":     total_unique_30d,
        },
        "island_type_filter": island_type_filter,
    })


@dashboard.route("/api/website-logins", methods=["GET"])
@api_auth_required
def api_website_logins():
    """Return paginated Discord website login audit events."""
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 25, type=int), 1), 100)
    search = (request.args.get("q") or "").strip()
    access = (request.args.get("access") or "all").strip().lower()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    conditions = []
    params = []

    if search:
        like = f"%{search}%"
        conditions.append(
            "(user_id LIKE ? OR username LIKE ? OR discord_name LIKE ? OR "
            "global_name LIKE ? OR account_name LIKE ? OR nickname LIKE ? OR ip_address LIKE ?)"
        )
        params.extend([like, like, like, like, like, like, like])

    if access == "mod":
        conditions.append("is_mod = 1")
    elif access == "admin":
        conditions.append("is_admin = 1")
    elif access == "regular":
        conditions.append("is_mod = 0 AND is_admin = 0")

    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_at <= ?")
        params.append(f"{date_to}T23:59:59Z" if len(date_to) == 10 else date_to)

    where_sql = _where_clause(conditions)
    offset = (page - 1) * per_page

    db = get_db()
    try:
        total = db.execute(
            f"SELECT COUNT(*) AS count FROM website_login_events {where_sql}",
            params,
        ).fetchone()["count"]
        rows = db.execute(
            "SELECT * FROM website_login_events "
            f"{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
        summary = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN is_mod = 1 THEN 1 ELSE 0 END) AS mod_count, "
            "SUM(CASE WHEN is_admin = 1 THEN 1 ELSE 0 END) AS admin_count "
            "FROM website_login_events"
        ).fetchone()
    except Exception as exc:
        logger.exception("Failed to load website login events")
        return jsonify({"error": str(exc)}), 500
    finally:
        db.close()

    entries = []
    for row in rows:
        item = dict(row)
        try:
            item["roles"] = json.loads(item.get("roles") or "[]")
        except (TypeError, ValueError):
            item["roles"] = []
        item["is_admin"] = bool(item.get("is_admin"))
        item["is_mod"] = bool(item.get("is_mod"))
        entries.append(item)

    return jsonify({
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "entries": entries,
        "filters": {
            "q": search,
            "access": access,
            "from": date_from,
            "to": date_to,
        },
        "summary": {
            "total": int(summary["total"] or 0),
            "mod_count": int(summary["mod_count"] or 0),
            "admin_count": int(summary["admin_count"] or 0),
        },
    })


@dashboard.route("/api/logs", methods=["GET"])
@api_auth_required
def api_logs():
    """Return paginated flight-log or warning entries as JSON.

    Query parameters
    ----------------
    type            : ``flights`` (default) or ``warnings``
    page            : page number (default 1)
    per_page        : rows per page, capped at 100 (default 25)
    ign             : IGN substring filter
    island          : island name filter (flights only)
    authorized      : ``0`` or ``1`` (flights only)
    category        : ``public`` or ``member`` (flights only)
    sort_by         : ``timestamp`` (default), ``ign``, or ``destination`` (flights only)
    sort_order      : ``desc`` (default) or ``asc`` (flights only)
    action_type     : ``WARN``, ``KICK``, ``BAN``, ``DISMISS``, ``NOTE``, ``ADMIT`` (warnings only)
    """
    log_type          = request.args.get("type", "flights")
    page              = request.args.get("page", 1, type=int)
    per_page          = min(request.args.get("per_page", 25, type=int), 100)
    ign_filter        = request.args.get("ign", "").strip()
    island_filter     = request.args.get("island", "").strip()
    authorized_filter = request.args.get("authorized", "")
    category_filter   = request.args.get("category", "")
    sort_by           = request.args.get("sort_by", "timestamp")
    sort_order        = request.args.get("sort_order", "desc")
    _ALLOWED_ACTION_TYPES = {"WARN", "KICK", "BAN", "DISMISS", "NOTE", "ADMIT"}
    action_type_filter = request.args.get("action_type", "").strip().upper()
    if action_type_filter not in _ALLOWED_ACTION_TYPES:
        action_type_filter = ""
    if sort_by not in _ALLOWED_SORT_COLS:
        sort_by = "timestamp"
    sort_order = "asc" if sort_order == "asc" else "desc"

    db = get_db()
    try:
        island_names = [
            r[0] for r in db.execute("SELECT name FROM islands ORDER BY name").fetchall()
        ]
        if log_type == "warnings":
            conditions, params = [], []
            if ign_filter:
                conditions.append("LOWER(iv.ign) LIKE LOWER(?)")
                params.append(f"%{ign_filter}%")
            if action_type_filter:
                conditions.append("UPPER(w.action_type) = ?")
                params.append(action_type_filter)
            where = _where_clause(conditions)
            total = db.execute(
                f"SELECT COUNT(*) FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id {where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT w.*, iv.ign, iv.destination "
                f"FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id "
                f"{where} ORDER BY w.timestamp DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page],
            ).fetchall()
            name_map = _resolve_discord_usernames(
                [r["user_id"] for r in rows if r["user_id"]]
                + [r["mod_id"] for r in rows if r["mod_id"]]
            )
            entries = [
                {
                    "user_id":     r["user_id"],
                    "user_name":   name_map.get(str(r["user_id"]), str(r["user_id"])) if r["user_id"] else "—",
                    "reason":      r["reason"],
                    "mod_id":      r["mod_id"],
                    "mod_name":    name_map.get(str(r["mod_id"]), str(r["mod_id"])) if r["mod_id"] else "—",
                    "timestamp":   _ts_to_str(r["timestamp"]),
                    "ign":         r["ign"],
                    "destination": r["destination"],
                    "action_type": r["action_type"],
                }
                for r in rows
            ]
        else:
            conditions, params = [], []
            use_island_join = bool(category_filter in ("public", "member"))

            if island_filter:
                col = "iv.destination" if use_island_join else "destination"
                conditions.append(f"LOWER({col}) = LOWER(?)")
                params.append(island_filter)
            if ign_filter:
                col = "iv.ign" if use_island_join else "ign"
                conditions.append(f"LOWER({col}) LIKE LOWER(?)")
                params.append(f"%{ign_filter}%")
            if authorized_filter in ("0", "1"):
                col = "iv.authorized" if use_island_join else "authorized"
                conditions.append(f"{col} = ?")
                params.append(int(authorized_filter))
            if use_island_join:
                conditions.append("isl.cat = ?")
                params.append(category_filter)

            if use_island_join:
                join_sql   = ("FROM island_visits iv "
                              "JOIN islands isl ON LOWER(iv.destination) = isl.id")
                order_sql  = f"iv.{sort_by} {sort_order.upper()}"
                where      = _where_clause(conditions)
                total      = db.execute(
                    f"SELECT COUNT(*) {join_sql} {where}", params
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT iv.* {join_sql} {where} "
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    params + [per_page, (page - 1) * per_page],
                ).fetchall()
            else:
                where      = _where_clause(conditions)
                order_sql  = f"{sort_by} {sort_order.upper()}"
                total      = db.execute(
                    f"SELECT COUNT(*) FROM island_visits {where}", params
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT * FROM island_visits {where} "
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    params + [per_page, (page - 1) * per_page],
                ).fetchall()

            entries = [
                {
                    "id":            r["id"],
                    "ign":           r["ign"],
                    "origin_island": r["origin_island"],
                    "destination":   r["destination"],
                    "authorized":    bool(r["authorized"]),
                    "timestamp":     _ts_to_str(r["timestamp"]),
                    "user_id":       r["user_id"],
                }
                for r in rows
            ]
            flight_name_map = _resolve_discord_usernames([r["user_id"] for r in rows if r["user_id"]])
            for e in entries:
                e["user_name"] = flight_name_map.get(str(e["user_id"])) if e["user_id"] else None
    except Exception:
        total, entries, island_names = 0, [], []
    finally:
        db.close()

    return jsonify({
        "page":        page,
        "per_page":    per_page,
        "total":       total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "log_type":    log_type,
        "entries":     entries,
        "island_names": island_names,
    })


@dashboard.route("/api/overview", methods=["GET"])
@api_auth_required
def api_overview():
    """Return the data powering the Overview dashboard page as JSON."""
    db = get_db()
    try:
        total_visits   = db.execute("SELECT COUNT(*) FROM island_visits").fetchone()[0]
        total_warnings = db.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
        visits_today   = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours')"
        ).fetchone()[0]
        visits_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        warnings_week = db.execute(
            "SELECT COUNT(*) FROM warnings "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        recent_raw = db.execute(
            "SELECT ign, destination, authorized, timestamp, user_id "
            "FROM island_visits ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        top_islands_raw = db.execute(
            "SELECT destination, COUNT(*) AS visit_count "
            "FROM island_visits GROUP BY destination "
            "ORDER BY visit_count DESC LIMIT 5"
        ).fetchall()
        top_travelers_raw = db.execute(
            "SELECT ign, COUNT(*) AS visit_count "
            "FROM island_visits GROUP BY ign "
            "ORDER BY visit_count DESC LIMIT 5"
        ).fetchall()
        trend_raw = db.execute(
            "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
            "FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days') "
            "GROUP BY day ORDER BY day"
        ).fetchall()
    except Exception:
        total_visits = total_warnings = visits_today = visits_week = warnings_week = 0
        recent_raw = []
        top_islands_raw = []
        top_travelers_raw = []
        trend_raw = []
    finally:
        db.close()

    recent_user_ids = [r["user_id"] for r in recent_raw if r["user_id"]]
    recent_name_map = _resolve_discord_usernames(recent_user_ids) if recent_user_ids else {}

    recent = [
        {
            "ign":         r["ign"],
            "destination": r["destination"],
            "authorized":  bool(r["authorized"]),
            "timestamp":   _ts_to_str(r["timestamp"]),
            "user_name":   recent_name_map.get(str(r["user_id"])) if r["user_id"] else None,
        }
        for r in recent_raw
    ]

    top_islands  = [{"name": r["destination"], "count": r["visit_count"]} for r in top_islands_raw]
    top_travelers = [{"ign": r["ign"], "count": r["visit_count"]} for r in top_travelers_raw]

    trend_map = {r["day"]: r["count"] for r in trend_raw}
    today_dt  = datetime.now(timezone.utc)
    trend_labels, trend_counts = [], []
    for offset in range(6, -1, -1):
        d = (today_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        trend_labels.append(d[-5:])
        trend_counts.append(trend_map.get(d, 0))

    warn_rate_7d = round(warnings_week / visits_week * 100, 1) if visits_week > 0 else 0

    db2 = get_db()
    try:
        rows2       = db2.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands2 = [_row_to_island_dict(dict(r)) for r in rows2]
        bot_status2 = _load_bot_status_map(db2)
    except Exception:
        db_islands2 = []
        bot_status2 = {}
    finally:
        db2.close()

    for isl in db_islands2:
        isl["discord_bot_online"] = bot_status2.get(isl.get("id", ""))

    island_count = len(db_islands2)
    status_map: dict[str, int] = {STATUS_ONLINE: 0, STATUS_REFRESHING: 0, STATUS_OFFLINE: 0}
    for isl in db_islands2:
        s = _effective_status(isl)
        status_map[s] = status_map.get(s, 0) + 1

    online_count = status_map[STATUS_ONLINE]
    online_pct   = round(online_count / island_count * 100) if island_count else 0

    return jsonify({
        "total_visits":   total_visits,
        "total_warnings": total_warnings,
        "visits_today":   visits_today,
        "visits_week":    visits_week,
        "warnings_week":  warnings_week,
        "warn_rate_7d":   warn_rate_7d,
        "island_count":   island_count,
        "online_count":   online_count,
        "online_pct":     online_pct,
        "status_map":     status_map,
        "top_islands":    top_islands,
        "top_travelers":  top_travelers,
        "trend_labels":   trend_labels,
        "trend_counts":   trend_counts,
        "recent":         recent,
    })

