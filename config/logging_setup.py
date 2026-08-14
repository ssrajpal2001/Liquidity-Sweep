"""
config/logging_setup.py

Centralized logging configuration for the whole bot. Every entrypoint
(main.py, auth.py CLI, future backtest/replay runner, future dashboard)
calls setup_logging() once at startup instead of calling
logging.basicConfig() directly — that was a gap in the Phase 0/1 scaffold:
logs only went to the console and nothing was persisted, so there was
nothing to share after the fact.

Output:
- Console: human-readable, INFO and above by default.
- File: logs/bot_YYYY-MM-DD.log, one file per calendar day (IST), same
  format, kept for LOG_RETENTION_DAYS days. This is what you'd zip up or
  paste excerpts from to hand me a record of what happened during a live
  session — I can't watch your server in real time, so the file is the
  hand-off mechanism.

Every log line includes: timestamp (IST), level, logger name (which module
emitted it), and the message — e.g.:

    2026-08-15 09:16:03 IST INFO     auth: New Fyers access token stored...
    2026-08-15 09:16:04 IST INFO     data_feed.fyers_rest_client: Fyers connection OK...
"""
from __future__ import annotations

import logging
import logging.handlers
from datetime import timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
LOG_RETENTION_DAYS = 30

_LOG_FORMAT = "%(asctime)s IST %(levelname)-8s %(name)s: %(message)s"


class _ISTFormatter(logging.Formatter):
    """Formats timestamps in IST regardless of the host machine's timezone —
    server clocks are frequently UTC, and mixing UTC log timestamps with an
    IST-based trading session makes logs much harder to read."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        from datetime import datetime

        dt = datetime.fromtimestamp(record.created, tz=IST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


_configured = False


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str = DEFAULT_LOG_DIR,
    console: bool = True,
) -> Path:
    """Idempotent — safe to call from every entrypoint. Returns the path to
    today's log file so the caller can print it (e.g. "logs are at ...")."""
    global _configured

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    formatter = _ISTFormatter(_LOG_FORMAT)

    if not _configured:
        # File handler: rotates at midnight IST-ish (system local time — see
        # note below), keeps LOG_RETENTION_DAYS days, one physical file per day.
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=log_dir / "bot.log",
            when="midnight",
            backupCount=LOG_RETENTION_DAYS,
            encoding="utf-8",
            utc=False,
        )
        # Default rotation suffix is "%Y-%m-%d", giving files like
        # bot.log (today, currently being written) and
        # bot.log.2026-08-14 (yesterday, once rotated at midnight).
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

        if console:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            root.addHandler(console_handler)

        _configured = True

    return log_dir / "bot.log"


if __name__ == "__main__":
    path = setup_logging()
    logger = logging.getLogger("logging_setup.selftest")
    logger.info("Logging initialized. Writing to: %s", path)
    logger.warning("This is a warning-level test line.")
    logger.error("This is an error-level test line.")
    print(f"\nCurrent log file: {path}")
    print("Rotated (previous day) files will appear alongside it as bot.log.YYYY-MM-DD")
