"""
Configuration Module
Loads and validates all environment variables
"""

import os
import logging
import secrets
from dotenv import load_dotenv

load_dotenv()

if not os.getenv('TWITCH_TOKEN') and os.getenv('\ufeffTWITCH_TOKEN'):
    for key in list(os.environ.keys()):
        if key.startswith('\ufeff'):
            clean_key = key.lstrip('\ufeff')
            os.environ[clean_key] = os.environ.pop(key)

class Config:
    """Application configuration"""

    @staticmethod
    def _get_int(key, default=None):
        """Helper to safely fetch and convert env vars to int"""
        val = os.getenv(key)
        if val and val.strip().isdigit():
            return int(val)
        return default

    @staticmethod
    def _clean_path(val):
        """Clean file paths, stripping surrounding quotes and repairing \\b backspace escapes from .env"""
        if not val:
            return val
        s = val.strip().strip('"').strip("'")
        if "\x08" in s:
            s = s.replace("\x08", "\\b")
        return s

    # General Config
    IS_PRODUCTION = os.getenv('IS_PRODUCTION', 'true').lower() == 'true'

    # Auth Tokens
    TWITCH_TOKEN = os.getenv('TWITCH_TOKEN')
    TWITCH_CHANNEL = os.getenv('TWITCH_CHANNEL')
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

    # Discord IDs (Safe Integer Casting)
    GUILD_ID = _get_int('GUILD_ID')
    CATEGORY_ID = _get_int('SUB_CATEGORY_ID')
    FREE_CATEGORY_ID = _get_int('FREE_CATEGORY_ID')
    LOG_CHANNEL_ID = _get_int('CHANNEL_ID')
    ISLAND_ACCESS_ROLE = _get_int('ISLAND_ACCESS_ROLE', 788749941949464577)
    FIND_BOT_CHANNEL_ID = _get_int('FIND_BOT_CHANNEL_ID', 1450554092626903232)
    AI_LEARN_CHANNEL_ID = _get_int('AI_LEARN_CHANNEL_ID', 907642922906845264)
    FREE_DODO_BOARD_CHANNEL_ID = _get_int('FREE_DODO_BOARD_CHANNEL_ID', 1500493205672825056)
    AUTOREPLY_CHANNELS = [907642922906845264, 1175875039954993306]
    ALWAYS_AUTOREPLY_CHANNELS = [1515569228453773482]
    ISLAND_STATUS_CHANNEL_ID = _get_int('ISLAND_STATUS_CHANNEL_ID', 1530536651338809415)


    # Environment Specific Channels
    if IS_PRODUCTION:
        FLIGHT_LISTEN_CHANNEL_ID = _get_int('FLIGHT_LISTEN_CHANNEL_ID')
        FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID = _get_int('FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID')
        FLIGHT_LOG_CHANNEL_ID = _get_int('FLIGHT_LOG_CHANNEL_ID')
        IGNORE_CHANNEL_ID = _get_int('IGNORE_CHANNEL_ID')
        SUB_MOD_CHANNEL_ID = _get_int('SUB_MOD_CHANNEL_ID')
        XLOG_VERBOSE_CHANNEL_ID = _get_int('XLOG_VERBOSE_CHANNEL_ID', 1486899475631968367)
    else:
        # Development / Fallback IDs
        FLIGHT_LISTEN_CHANNEL_ID = 1473286697461616732
        FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID = None
        FLIGHT_LOG_CHANNEL_ID = 1473286727224524915
        IGNORE_CHANNEL_ID = 809295405128089611
        SUB_MOD_CHANNEL_ID = 1473286794995830845
        XLOG_VERBOSE_CHANNEL_ID = 1491101080430579964

    # Patreon
    PATREON_TOKEN = os.getenv("PATREON_TOKEN")
    PATREON_CAMPAIGN_ID = os.getenv("PATREON_CAMPAIGN_ID")

    # Nookipedia
    NOOKIPEDIA_KEY = os.getenv("NOOKIPEDIA_KEY")

    # AI providers (optional)
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
    AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().lower()
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # Web Dashboard (mod-only)
    DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")
    DASHBOARD_FRONTEND_URL = os.getenv("DASHBOARD_FRONTEND_URL", "").strip().rstrip("/")

    # Discord webhook for logging dodo code reveals on the website
    DODO_LOG_WEBHOOK_URL = os.getenv("DODO_LOG_WEBHOOK_URL", "")

    _FLASK_SECRET_ENV = os.getenv("FLASK_SECRET_KEY", "").strip()
    FLASK_SECRET_KEY: str = _FLASK_SECRET_ENV or DASHBOARD_SECRET or secrets.token_hex(32)
    FLASK_SESSION_DAYS = _get_int("FLASK_SESSION_DAYS", 30)
    AUTH_TOKEN_TTL_DAYS = _get_int("AUTH_TOKEN_TTL_DAYS", 30)
    AUTH_DISCORD_REFRESH_SECONDS = _get_int("AUTH_DISCORD_REFRESH_SECONDS", 300)
    AUTH_DISCORD_STALE_GRACE_SECONDS = _get_int("AUTH_DISCORD_STALE_GRACE_SECONDS", 3600)
    BACKUP_DIR = os.getenv("BACKUP_DIR", "backups").strip() or "backups"
    BACKUP_INTERVAL_HOURS = _get_int("BACKUP_INTERVAL_HOURS", 24)
    HEALTH_CACHE_MAX_AGE_SECONDS = _get_int("HEALTH_CACHE_MAX_AGE_SECONDS", 7200)
    SERVICE_HEARTBEAT_INTERVAL_SECONDS = _get_int("SERVICE_HEARTBEAT_INTERVAL_SECONDS", 60)

    DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
    DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
    WEBSITE_LOGIN_LOG_CHANNEL_ID = _get_int("WEBSITE_LOGIN_LOG_CHANNEL_ID", 1510672689998860440)
    API_BASE_URL = os.getenv("API_BASE_URL", "https://console.chopaeng.com").strip().rstrip("/")
    _FRONTEND_ORIGINS_ENV = (
        os.getenv("FRONTEND_ORIGINS")
        or os.getenv("FLASK_ALLOWED_ORIGINS")
        or "https://www.chopaeng.com,https://chopaeng.com,http://localhost:5173,http://localhost:3000"
    )
    FRONTEND_ORIGINS = [
        origin.strip()
        for origin in _FRONTEND_ORIGINS_ENV.split(",")
        if origin.strip()
    ]

    ADMIN_ROLE_ID = _get_int("ADMIN_ROLE_ID")

    R2_ACCOUNT_ID       = os.getenv("R2_ACCOUNT_ID", "")
    R2_ACCESS_KEY_ID    = os.getenv("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
    R2_BUCKET_NAME      = os.getenv("R2_BUCKET_NAME", "chobot-maps")
    R2_PUBLIC_URL       = os.getenv("R2_PUBLIC_URL", "")

    DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").strip().lower()
    SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "")
    DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

    MYSQL_HOST = os.getenv("MYSQL_HOST") or os.getenv("MARIADB_HOST", "localhost")
    MYSQL_PORT = _get_int("MYSQL_PORT", _get_int("MARIADB_PORT", 3306))
    MYSQL_USER = os.getenv("MYSQL_USER") or os.getenv("MARIADB_USER", "chobot")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD") or os.getenv("MARIADB_PASSWORD", "")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE") or os.getenv("MARIADB_DATABASE", "chobot")

    MARIADB_HOST = MYSQL_HOST
    MARIADB_PORT = MYSQL_PORT
    MARIADB_USER = MYSQL_USER
    MARIADB_PASSWORD = MYSQL_PASSWORD
    MARIADB_DATABASE = MYSQL_DATABASE
    MARIADB_TRUNCATE_BEFORE_IMPORT = os.getenv("MARIADB_TRUNCATE_BEFORE_IMPORT", "true").strip().lower() == "true"

    # Google Sheets
    WORKBOOK_NAME = os.getenv('WORKBOOK_NAME')
    JSON_KEYFILE = 'service_account.json'
    CACHE_REFRESH_HOURS = 1
    # Data Source (nhl | sheets | auto)
    ITEM_DATA_SOURCE = os.getenv('ITEM_DATA_SOURCE', 'nhl').strip().lower()

    VILLAGERS_DIR = _clean_path(os.getenv('VILLAGERS_DIR'))
    TWITCH_VILLAGERS_DIR = _clean_path(os.getenv('TWITCH_VILLAGERS_DIR'))
    ORDER_BOT_DIR = _clean_path(os.getenv('ORDER_BOT_DIR') or os.getenv('ORDER_BOR_DIR'))
    ORDER_BOT_ISLAND = os.getenv('ORDER_BOT_ISLAND', 'SYSBOT-ACNH-ORDERS')
    ORDER_BOT_CHANNEL_ID = _get_int('ORDER_BOT_CHANNEL_ID', 1175672083183829075)
    ORDER_BOT_DISCORD_ID = _get_int('ORDER_BOT_DISCORD_ID', 1175671093445537802)
    ORDER_BOT_TWITCH_CHANNEL = os.getenv('ORDER_BOT_TWITCH_CHANNEL') or TWITCH_CHANNEL

    # ChoPaeng Console Order API (https://console.chopaeng.com/api/order)
    ORDER_API_URL = (os.getenv('ORDER_API_URL') or '').rstrip('/') or f"{API_BASE_URL}/api/order"

    # SysBot.ACNHOrders HTTP REST API
    # Set SYSBOT_API_URL to the address where SysBot is running, e.g. http://localhost:5202
    # Leave blank to disable live SysBot API integration.
    SYSBOT_API_URL = (os.getenv('SYSBOT_API_URL') or '').rstrip('/')
    # Optional: matches the ApiKey set in SysBot's server.json. Leave blank if no key is configured.
    SYSBOT_API_KEY = os.getenv('SYSBOT_API_KEY', '')

    @classmethod
    def is_order_api_configured(cls) -> bool:
        """Return True if ORDER_API_URL is configured."""
        return bool(cls.ORDER_API_URL)

    @classmethod
    def is_sysbot_configured(cls) -> bool:
        """Return True if SYSBOT_API_URL is set."""
        return bool(cls.SYSBOT_API_URL)

    DIR_FREE = TWITCH_VILLAGERS_DIR
    DIR_VIP = VILLAGERS_DIR
    DIR_ORDER = ORDER_BOT_DIR

    # Island Lists (fallback defaults; dynamically updated at runtime from Discord sub-category)
    SUB_ISLANDS = [
        "Adhika", "Alapaap", "Aruga", "Bahaghari", "Bituin", "Bonita", "Dakila",
        "Dalisay", "Diwa", "Gabay", "Galak", "Giliw", "Hiraya", "Kalangitan",
        "Lakan", "Likha", "Malaya", "Marahuyo", "Pangarap", "Tagumpay"
    ]

    TWITCH_SUB_ISLANDS = SUB_ISLANDS  

    FREE_ISLANDS = [
        "Kakanggata", "Kalawakan", "Kundiman", "Kilig", "Bathala", "Dalangin",
        "Gunita", "Kaulayaw", "Tala", "Sinagtala", "Tadhana", "Maharlika",
        "Pagsamo", "Harana", "Pagsuyo", "Matahom", "Paraluman", "Babaylan",
        "Amihan", "Silakbo", "Dangal", "Kariktan", "Tinig", "Banaag",
        "Sinag", "Giting", "Marilag"
    ]

    ORDER_BOT_ISLANDS = [ORDER_BOT_ISLAND] if ORDER_BOT_ISLAND else []

    ISLAND_BOT_ROLE_ID = _get_int('ISLAND_BOT_ROLE_ID')

    # Mod roles â€” members with these bypass all island access restrictions.
    SENIOR_MOD_ROLE_ID = _get_int("SENIOR_MOD_ROLE_ID")
    BABY_MOD_ROLE_ID   = _get_int("BABY_MOD_ROLE_ID")


    # Discord Embed Assets
    EMOJI_SEARCH = "<a:heartside:784055539881214002>"
    EMOJI_FAIL = "<a:CampWarning:1172346431542140961>"
    STAR_PINK = "<a:starpink:784055540321091584>"
    FOOTER_LINE = "https://i.ibb.co/wybN7Xn/lg4jVMT.gif"
    INDENT = "<a:starsparkle1:766724172474220574>"
    DROPBOT_INFO = "Try using <@&807096897453031425> to drop the specific item.\nCheck <#782872507551055892> for help."
    DEFAULT_PFP = "https://static-cdn.jtvnw.net/jtv_user_pictures/cf6b6d6c-f9b6-4bad-b034-391d7d32b9c3-profile_image-70x70.png"

    @classmethod
    def validate(cls, needs_twitch: bool = True):
        """Validate required environment variables exist and are not empty"""
        required_vars = [
            'DISCORD_TOKEN',
            'WORKBOOK_NAME', 'GUILD_ID', 'CATEGORY_ID',
            'PATREON_TOKEN', 'PATREON_CAMPAIGN_ID'
        ]
        if needs_twitch:
            required_vars.extend(['TWITCH_TOKEN', 'TWITCH_CHANNEL'])

        missing = []
        for var in required_vars:
            val = getattr(cls, var, None)
            
            # Check for None (Missing)
            if val is None:
                missing.append(var)
            # Check for Empty Strings (if it's a string)
            elif isinstance(val, str) and not val.strip():
                missing.append(var)

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        if cls.IS_PRODUCTION and not cls.DASHBOARD_SECRET:
            raise ValueError("DASHBOARD_SECRET is required in production")

        if cls.IS_PRODUCTION and not (cls._FLASK_SECRET_ENV or cls.DASHBOARD_SECRET):
            raise ValueError("FLASK_SECRET_KEY or DASHBOARD_SECRET is required in production")

        if cls.DB_BACKEND not in {"sqlite", "mysql", "mariadb"}:
            raise ValueError("DB_BACKEND must be one of: sqlite, mysql, mariadb")

        if cls.DB_BACKEND in {"mysql", "mariadb"} and not cls.DATABASE_URL:
            mysql_missing = [
                name for name, value in {
                    "MYSQL_HOST": cls.MYSQL_HOST,
                    "MYSQL_USER": cls.MYSQL_USER,
                    "MYSQL_DATABASE": cls.MYSQL_DATABASE,
                }.items()
                if not value
            ]
            if mysql_missing:
                raise ValueError(f"Missing MySQL database settings: {', '.join(mysql_missing)}")

        return True

