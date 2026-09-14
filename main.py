"""
Main Entry Point
Unified Bot Application Runner

Starts selected services:
- Flask API Server (thread)
- Twitch Bot (thread w/ its own asyncio loop)
- Discord Command Bot (main asyncio loop)
- Flight Logger only (Discord bot with FlightLoggerCog only)
- Twitch Find only (Twitch bot in lightweight/find mode)

Usage:
    python main.py                      # Run ALL services
    python main.py all                  # Run ALL services
    python main.py flask                # Flask API only
    python main.py twitch               # Twitch bot (full)
    python main.py twitch-find          # Twitch bot (find commands only)
    python main.py discord              # Discord bot (with all cogs)
    python main.py discord-find         # Discord bot (find/search cogs only)
    python main.py flight-logger        # Discord bot with FlightLoggerCog only
    python main.py flask twitch-find    # Flask + Twitch find only
    ... any combination
"""

import os
import sys
import time
import asyncio
import threading
import logging
import traceback
import signal
import contextlib
from typing import Optional, Set

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import Config, DataManager
from utils.db_migration import migrate_sqlite_to_mariadb_detailed
from utils.ops_status import backup_dir_path, record_service_status, start_backup_scheduler
from bots import TwitchBot, DiscordCommandBot
from bots.flight_logger import FlightLoggerCog, FreeFlightCog
from api import run_flask_app, set_data_manager

# ============================================================================
# CONSTANTS
# ============================================================================
VALID_SERVICES = {
    "all", "flask",
    "twitch", "twitch-find",
    "discord", "discord-find", "flight-logger",
    "chorder", "order-bot",
    "migrate-mariadb",
}

SERVICE_DESCRIPTIONS = {
    "all":            "All services (Flask + Twitch + Discord w/ all cogs)",
    "flask":          "Flask API server",
    "twitch":         "Twitch bot (full, all commands)",
    "twitch-find":    "Twitch bot (find/search commands only)",
    "discord":        "Discord bot (all cogs including FlightLogger & Chorder)",
    "discord-find":   "Discord bot (find/search cogs only)",
    "flight-logger":  "Discord bot with FlightLoggerCog only",
    "chorder":        "Standalone SysBot Order Bot & Order Panel (/orderpanel)",
    "order-bot":      "Alias for chorder",
    "migrate-mariadb": "Migrate local SQLite database to MariaDB",
}

# ============================================================================
# LOGGING
# ============================================================================
_log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_str, logging.INFO)
if os.getenv("DEBUG", "false").lower() in ("true", "1", "yes") or os.getenv("CHOPAENG_AI_DEBUG", "false").lower() in ("true", "1", "yes"):
    _log_level = logging.DEBUG

_log_handlers = [
    logging.FileHandler("bot.log", encoding="utf-8"),
    logging.StreamHandler(),
]
for _h in _log_handlers:
    _h.setLevel(_log_level)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=_log_handlers,
)

# Enable configured log level on all bot-specific modules
for _mod_name in [
    "Main", "ChopaengAI", "DiscordCommandBot", "FlightLogger",
    "TwitchBot", "DataManager", "IslandAccess", "DiscordMembership",
    "DiscordHTTP", "DBMigration", "AuthTokens", "NookipediaClient",
    "FlaskAPI", "Dashboard",
]:
    logging.getLogger(_mod_name).setLevel(_log_level)

# Keep noisy third-party libraries manageable unless explicitly requested
if os.getenv("VERBOSE_LIBS", "false").lower() not in ("true", "1", "yes"):
    for _lib in ("discord.gateway", "discord.http", "websockets", "httpcore", "httpx", "asyncio"):
        logging.getLogger(_lib).setLevel(logging.INFO)
    for _lib in ("urllib3", "urllib3.connectionpool"):
        logging.getLogger(_lib).setLevel(logging.ERROR)

logger = logging.getLogger("Main")


def configure_ssl_cert_bundle() -> None:
    """Ensure Python/aiohttp can find a CA bundle for TLS verification."""
    if os.getenv("SSL_CERT_FILE"):
        return
    try:
        import certifi  # lazy import

        ca_path = certifi.where()
        if ca_path and os.path.exists(ca_path):
            os.environ["SSL_CERT_FILE"] = ca_path
            logger.info("[SSL] Using certifi CA bundle for outbound HTTPS.")
    except Exception as exc:
        logger.warning(f"[SSL] Could not configure certifi CA bundle: {exc}")

# ============================================================================
# SHARED STOP FLAG
# ============================================================================
STOP_EVENT = threading.Event()

# ============================================================================
# PID LOCK FILE  (prevents duplicate instances after !update)
# ============================================================================
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chobot.pid")
# How long (seconds) to wait for the previous instance to clean up after SIGTERM.
_SIGTERM_GRACE_SECONDS = 2


def acquire_pid_lock() -> None:
    """Write the current PID to the lock file, killing any prior instance first."""
    current_pid = os.getpid()

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
        except (ValueError, OSError):
            old_pid = None

        if old_pid and old_pid != current_pid:
            try:
                os.kill(old_pid, signal.SIGTERM)
                logger.info(f"[MAIN] Sent SIGTERM to previous instance (PID {old_pid}).")
                # Give the old process a moment to clean up before we proceed.
                time.sleep(_SIGTERM_GRACE_SECONDS)
            except ProcessLookupError:
                pass  # Already gone – that's fine.
            except OSError as exc:
                logger.warning(f"[MAIN] Could not terminate previous instance: {exc}")

    try:
        with open(PID_FILE, "w") as f:
            f.write(str(current_pid))
        logger.info(f"[MAIN] PID lock acquired (PID {current_pid} → {PID_FILE}).")
    except OSError as exc:
        logger.warning(f"[MAIN] Could not write PID file: {exc}")


def release_pid_lock() -> None:
    """Remove the PID lock file when shutting down cleanly."""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                stored_pid = int(f.read().strip())
            # Only remove the file if it still refers to *this* process.
            if stored_pid == os.getpid():
                os.remove(PID_FILE)
                logger.info("[MAIN] PID lock released.")
    except (OSError, ValueError):
        pass


# ============================================================================
# CLI PARSING
# ============================================================================
def parse_services(args: list[str]) -> Set[str]:
    """Parse CLI arguments into a set of services to run."""
    if len(args) <= 1:
        return {"all"}

    requested = set()
    for arg in args[1:]:
        service = arg.lower().strip()
        if service in ("--help", "-h"):
            print_help()
            sys.exit(0)
        if service not in VALID_SERVICES:
            print(f"✗ Unknown service: '{service}'")
            print(f"  Valid options: {', '.join(sorted(VALID_SERVICES))}")
            print(f"  Run with --help for details.")
            sys.exit(1)
        requested.add(service)

    # "all" overrides everything
    if "all" in requested:
        return {"all"}

    # Resolve conflicts: full mode supersedes limited mode
    if "discord" in requested:
        for subset in ("flight-logger", "discord-find"):
            if subset in requested:
                logger.info(f"[CLI] 'discord' already includes '{subset}'; ignoring.")
                requested.discard(subset)

    if "twitch" in requested and "twitch-find" in requested:
        logger.info("[CLI] 'twitch' already includes 'twitch-find'; ignoring.")
        requested.discard("twitch-find")

    return requested


def print_help():
    print(__doc__)
    print("Available services:")
    for name, desc in SERVICE_DESCRIPTIONS.items():
        print(f"  {name:<18} {desc}")
    print()
    print("Examples:")
    print("  python main.py                        # all services")
    print("  python main.py flask twitch-find       # Flask API + Twitch find only")
    print("  python main.py flight-logger           # Discord with FlightLogger only")
    print("  python main.py discord-find twitch-find # Both bots in find-only mode")


def expand_services(requested: Set[str]) -> dict:
    """Expand service set into structured flags."""
    if "all" in requested:
        return {
            "flask": True,
            "twitch": True,
            "twitch_find_only": False,
            "discord": True,
            "discord_find_only": False,
            "flight_logger_only": False,
        }

    return {
        "flask": "flask" in requested,
        "twitch": "twitch" in requested,
        "twitch_find_only": "twitch-find" in requested,
        "discord": bool({"discord", "chorder", "order-bot"} & requested),
        "discord_find_only": "discord-find" in requested,
        "flight_logger_only": "flight-logger" in requested,
    }


# ============================================================================
# THREAD RUNNERS
# ============================================================================
def run_flask(data_manager: DataManager):
    """Run Flask API server in a thread."""
    try:
        logger.info("[FLASK] Starting Flask API...")
        record_service_status("flask", mode="api", status="starting")
        set_data_manager(data_manager)
        record_service_status("flask", mode="api", status="running")
        run_flask_app(host="0.0.0.0", port=8100)
    except Exception as e:
        logger.error(f"[FLASK] Critical error: {e}")
        logger.error(traceback.format_exc())
        record_service_status("flask", mode="api", status="error", error=str(e))
        STOP_EVENT.set()


async def _safe_close_twitch_bot(twitch_bot: TwitchBot) -> None:
    """Close Twitch bot, tolerating TwitchIO close bug on failed initial connect."""
    try:
        await twitch_bot.close()
    except AttributeError as exc:
        # TwitchIO can raise: 'NoneType' object has no attribute 'cancel'
        # when close() runs before keeper task exists.
        if "cancel" in str(exc):
            logger.warning("[TWITCH] Ignoring known TwitchIO close bug after failed connect.")
            return
        raise


async def _run_twitch_lifecycle(twitch_bot: TwitchBot) -> None:
    """Run Twitch bot until it stops or a global stop signal is set."""
    stop_waiter = asyncio.create_task(asyncio.to_thread(STOP_EVENT.wait))
    start_task = asyncio.create_task(twitch_bot.start())

    done, pending = await asyncio.wait(
        {start_task, stop_waiter},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if stop_waiter in done and STOP_EVENT.is_set():
        logger.warning("[TWITCH] Stop signal received, closing bot...")
        await _safe_close_twitch_bot(twitch_bot)

    if start_task in done:
        # Propagate startup/runtime exceptions to caller.
        await start_task

    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def run_twitch(data_manager: DataManager, find_only: bool = False):
    """Run Twitch bot in a thread with its own event loop."""
    loop: Optional[asyncio.AbstractEventLoop] = None
    twitch_bot: Optional[TwitchBot] = None
    try:
        mode = "find-only" if find_only else "full"
        logger.info(f"[TWITCH] Starting Twitch bot ({mode} mode)...")
        record_service_status("twitch", mode=mode, status="starting")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        twitch_bot = TwitchBot(data_manager)
        record_service_status("twitch", mode=mode, status="running")
        loop.run_until_complete(_run_twitch_lifecycle(twitch_bot))

    except Exception as e:
        logger.error(f"[TWITCH] Critical error: {e}")
        logger.error(traceback.format_exc())
        record_service_status("twitch", mode="find-only" if find_only else "full", status="error", error=str(e))
        STOP_EVENT.set()
    finally:
        try:
            if loop and not loop.is_closed():
                if twitch_bot is not None:
                    loop.run_until_complete(_safe_close_twitch_bot(twitch_bot))
                loop.stop()
                loop.close()
        except Exception:
            pass
        record_service_status("twitch", mode="find-only" if find_only else "full", status="stopped")


# ============================================================================
# DISCORD RUNNERS
# ============================================================================
async def run_discord(
        data_manager: DataManager,
        flight_logger_only: bool = False,
        find_only: bool = False,
) -> bool:
    """Run Discord bot on the main asyncio loop.

    Returns True if the caller should restart the process (OTA update),
    False otherwise.
    """
    discord_bot: Optional[DiscordCommandBot] = None
    try:
        if flight_logger_only:
            mode = "FlightLogger-only"
        elif find_only:
            mode = "find-only"
        else:
            mode = "full"
        logger.info(f"[DISCORD] Starting Discord bot ({mode} mode)...")
        record_service_status("discord", mode=mode, status="starting")

        discord_bot = DiscordCommandBot(data_manager, load_command_cog=not flight_logger_only)

        if flight_logger_only:
            await discord_bot.add_cog(FlightLoggerCog(discord_bot))
            await discord_bot.add_cog(FreeFlightCog(discord_bot))
            logger.info("[DISCORD] Loaded cog: FlightLoggerCog + FreeFlightCog (only)")
        elif find_only:
            # Load only find/search-related cogs here
            # await discord_bot.add_cog(FindCog(discord_bot))
            logger.info("[DISCORD] Loaded find/search cogs only")
        else:
            # Full bot — load all cogs
            await discord_bot.add_cog(FlightLoggerCog(discord_bot))
            await discord_bot.add_cog(FreeFlightCog(discord_bot))
            # await discord_bot.add_cog(FindCog(discord_bot))
            # await discord_bot.add_cog(SomeOtherCog(discord_bot))
            logger.info("[DISCORD] Loaded all cogs ✓")

        async def stop_watcher():
            while not STOP_EVENT.is_set():
                await asyncio.sleep(0.5)
            logger.warning("[DISCORD] Stop signal received, closing bot...")
            await discord_bot.close()

        watcher_task = asyncio.create_task(stop_watcher())

        record_service_status("discord", mode=mode, status="running")
        await discord_bot.start(Config.DISCORD_TOKEN)

        watcher_task.cancel()
        record_service_status("discord", mode=mode, status="stopped")

    except Exception as e:
        logger.error(f"[DISCORD] Critical error: {e}")
        logger.error(traceback.format_exc())
        record_service_status("discord", mode=mode if "mode" in locals() else "unknown", status="error", error=str(e))
        STOP_EVENT.set()
        if discord_bot:
            try:
                await discord_bot.close()
            except Exception:
                pass

    return bool(discord_bot and discord_bot.restart_requested)


# ============================================================================
# MAIN
# ============================================================================
def main():
    configure_ssl_cert_bundle()
    data_manager: Optional[DataManager] = None

    # One-shot utility mode: migrate sqlite data into MariaDB and exit.
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() == "migrate-mariadb":
        sqlite_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chobot.db")
        try:
            summary = migrate_sqlite_to_mariadb_detailed(
                sqlite_path=sqlite_path,
                host=Config.MARIADB_HOST,
                port=Config.MARIADB_PORT,
                user=Config.MARIADB_USER,
                password=Config.MARIADB_PASSWORD,
                database=Config.MARIADB_DATABASE,
                truncate_before_import=Config.MARIADB_TRUNCATE_BEFORE_IMPORT,
                backup_dir=backup_dir_path(),
            )
            total_rows = summary["total_rows_copied"]
            logger.info(
                "[MIGRATE] Success: %d tables, %d rows copied. Backup: %s. Validation: %s.",
                len(summary["tables"]),
                total_rows,
                summary.get("backup_path") or "skipped",
                "passed" if summary.get("validation", {}).get("ok") else "check warnings",
            )
            return
        except Exception as exc:
            logger.critical(f"[MIGRATE] Failed: {exc}")
            logger.critical(traceback.format_exc())
            sys.exit(1)

    # ---- Parse CLI ---------------------------------------------------------
    services = parse_services(sys.argv)
    flags = expand_services(services)

    needs_discord = flags["discord"] or flags["discord_find_only"] or flags["flight_logger_only"]
    needs_twitch = flags["twitch"] or flags["twitch_find_only"]
    if not flags["flask"]:
        record_service_status("flask", mode="api", status="not_selected")
    if not needs_discord:
        record_service_status("discord", mode="none", status="not_selected")
    if not needs_twitch:
        record_service_status("twitch", mode="none", status="not_selected")

    logger.info("=" * 70)
    logger.info("CHOBOT STARTING")
    logger.info(f"  Services: {', '.join(sorted(services))}")
    logger.info("=" * 70)

    # ---- Single-instance lock (kill prior instance if still alive) ---------
    acquire_pid_lock()

    # ---- Validate config ---------------------------------------------------
    try:
        Config.validate(needs_twitch=needs_twitch)
        logger.info("[CONFIG] All environment variables validated ✓")
    except ValueError as e:
        logger.critical(f"[CONFIG] Configuration error: {e}")
        sys.exit(1)

    # ---- Init shared data manager ------------------------------------------
    logger.info("[DATA] Initializing data manager...")
    data_manager = DataManager(
        workbook_name=Config.WORKBOOK_NAME,
        json_keyfile=Config.JSON_KEYFILE,
        cache_refresh_hours=Config.CACHE_REFRESH_HOURS,
    )

    logger.info("[DATA] Loading initial cache...")
    if not data_manager.cache:
        logger.info("[DATA] No local cache found. Fetching from Google Sheets...")
        data_manager.update_cache()
    else:
        logger.info(
            f"[DATA] Local cache loaded successfully "
            f"({len(data_manager.cache)} items). Skipping initial fetch."
        )
    start_backup_scheduler(STOP_EVENT)
    logger.info(f"[DATA] Cache status: {len(data_manager.cache)} items ready ✓")

    # ---- Signal handling ---------------------------------------------------
    def _handle_signal(signum, frame):
        logger.warning(f"[MAIN] Signal {signum} received. Shutting down...")
        STOP_EVENT.set()

    try:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
    except Exception:
        pass

    # ---- Start requested services ------------------------------------------
    threads: list[threading.Thread] = []

    if flags["flask"]:
        flask_thread = threading.Thread(
            target=run_flask, args=(data_manager,), name="FlaskThread"
        )
        flask_thread.start()
        threads.append(flask_thread)
        logger.info("[MAIN] Flask API thread started ✓")

    if needs_twitch:
        twitch_find_only = flags["twitch_find_only"]
        twitch_thread = threading.Thread(
            target=run_twitch,
            args=(data_manager, twitch_find_only),
            name="TwitchThread",
        )
        twitch_thread.start()
        threads.append(twitch_thread)
        mode = "find-only" if twitch_find_only else "full"
        logger.info(f"[MAIN] Twitch bot thread started ({mode}) ✓")

    # ---- Discord / Flight-Logger (runs on main asyncio loop) ---------------
    restart_requested = False
    if needs_discord:
        try:
            restart_requested = asyncio.run(
                run_discord(
                    data_manager,
                    flight_logger_only=flags["flight_logger_only"],
                    find_only=flags["discord_find_only"],
                )
            )
        except KeyboardInterrupt:
            logger.info("[MAIN] Shutdown signal received (Ctrl+C)")
            STOP_EVENT.set()
        except Exception as e:
            logger.critical(f"[MAIN] Critical error: {e}")
            logger.critical(traceback.format_exc())
            STOP_EVENT.set()
    else:
        # No Discord — keep main thread alive until stop signal or Ctrl+C
        logger.info("[MAIN] No Discord service selected. Main thread waiting...")
        try:
            while not STOP_EVENT.is_set():
                STOP_EVENT.wait(timeout=1.0)
        except KeyboardInterrupt:
            logger.info("[MAIN] Shutdown signal received (Ctrl+C)")
            STOP_EVENT.set()

    # ---- Shutdown ----------------------------------------------------------
    STOP_EVENT.set()

    if data_manager is not None:
        data_manager.stop_auto_refresh()

    for t in threads:
        try:
            t.join(timeout=5)
        except Exception:
            pass

    logger.info("=" * 70)
    logger.info("APPLICATION SHUTDOWN COMPLETE")
    logger.info("=" * 70)

    # ---- Restart (OTA update) ----------------------------------------------
    # Perform os.execv() from the main thread *after* the event loop and all
    # helper threads have stopped.  Doing it here (rather than from a
    # background thread while the loop is still running) avoids the race where
    # the old process is still partially alive when the new one connects to
    # Discord, which caused duplicate responses on every subsequent restart.
    if restart_requested:
        logger.info("[MAIN] Restarting process for OTA update...")
        # Release the PID lock so the exec'd process (which re-acquires it
        # immediately) doesn't see our own PID and try to kill itself.
        release_pid_lock()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # Clean shutdown (no restart) — remove the PID lock file.
    release_pid_lock()


if __name__ == "__main__":
    main()
