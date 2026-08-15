"""
orchestration/session_manager.py — N clients, N brokers, running concurrently.

Per the request: any number of registered web-UI clients, each with any
number of connected brokers, each broker acting as an independent data
feed + order-placement channel for that specific client — never shared
across clients.

Discovery: scans webapp.user_store.UserStore for every registered
account, and for each one, webapp.credential_vault.CredentialVault for
every broker marked connected=True. One TradingSession per (user,
broker) pair, each with:
  - its own BrokerAdapter instance (own WS connection, own auth)
  - its own StateStore file (state/{user}__{broker}_strategy_state.json
    — see main.py's TradingSession.__init__ session_id parameter)
  - its own DailyGuard (capital/risk tracked independently per session)
All run in the same process. Each broker adapter's WS client already
manages its own background thread internally (confirmed for both Fyers
and AngelOne adapters), so this doesn't need its own threading beyond
what TradingSession.start() already sets in motion.

SCOPE NOTE, stated plainly: strategy config (instruments, capital, risk
%, timeframes) currently comes from ONE shared config/settings.yaml —
true per-client strategy/capital configuration is a further step, not
built here. What IS per-client here: broker connection, state isolation,
and trade execution.

Usage:
    python -m orchestration.session_manager
    python -m orchestration.session_manager --poll-seconds 60
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass

from brokers.registry import available_brokers
from config.config_loader import ConfigError, load_settings
from config.logging_setup import setup_logging
from main import TradingSession, _assert_paper_gate
from webapp.broker_session_builder import BrokerSessionError, build_connected_adapter
from webapp.credential_vault import CredentialVault
from webapp.secrets_bootstrap import get_or_create_encryption_key
from webapp.user_store import UserStore

logger = logging.getLogger("session_manager")

DEFAULT_POLL_SECONDS = 300  # how often to re-scan for newly-connected clients while running


@dataclass
class RunningSession:
    user: str
    broker: str
    trading_session: TradingSession


def discover_connected_pairs() -> list[tuple[str, str]]:
    """Every (username, broker_name) pair where that broker is currently
    marked connected in the vault. Iterates the users table directly
    (UserStore doesn't expose a "list all" method today, so this reads
    the shared SQLite file — same file, same schema, no new dependency)."""
    import sqlite3

    users = UserStore()
    with sqlite3.connect(users.db_path) as conn:
        usernames = [row[0] for row in conn.execute("SELECT username FROM users")]

    vault = CredentialVault(get_or_create_encryption_key())
    brokers = available_brokers()
    pairs = []
    for username in usernames:
        status = vault.list_broker_status(username, brokers)
        for broker_name, info in status.items():
            if info["connected"]:
                pairs.append((username, broker_name))
    return pairs


class SessionManager:
    def __init__(self, poll_seconds: int = DEFAULT_POLL_SECONDS):
        self.poll_seconds = poll_seconds
        self.running: dict[tuple[str, str], RunningSession] = {}
        self._stopping = False

    def _start_one(self, username: str, broker_name: str) -> None:
        key = (username, broker_name)
        if key in self.running:
            return

        try:
            settings = load_settings()
        except ConfigError as exc:
            logger.error("[SESSION_MANAGER] Config error, cannot start any session: %s", exc)
            return

        try:
            _assert_paper_gate(settings)
        except RuntimeError as exc:
            logger.error("[SESSION_MANAGER] Paper gate blocked startup: %s", exc)
            return

        try:
            adapter = build_connected_adapter(username, broker_name, paper_mode=True)
        except BrokerSessionError as exc:
            logger.error("[SESSION_START_FAILED] user=%s broker=%s: %s", username, broker_name, exc)
            return

        session_id = f"{username}__{broker_name}"
        trading_session = TradingSession(settings, adapter, session_id=session_id)
        try:
            trading_session.start()
        except Exception:  # noqa: BLE001
            logger.exception("[SESSION_START_FAILED] user=%s broker=%s", username, broker_name)
            return

        self.running[key] = RunningSession(username, broker_name, trading_session)
        logger.info("[SESSION_MANAGER_STARTED] user=%s broker=%s (total running: %d)",
                    username, broker_name, len(self.running))

    def sync(self) -> None:
        """Starts sessions for any newly-connected (user, broker) pairs.
        Does NOT auto-stop sessions whose vault status flips to
        disconnected mid-run — an open position shouldn't lose its
        tracking just because a dashboard toggle changed; stopping a
        live session is a deliberate separate action (see stop_all)."""
        try:
            pairs = discover_connected_pairs()
        except Exception:  # noqa: BLE001
            logger.exception("[SESSION_MANAGER] Failed to discover connected clients.")
            return

        logger.info("[SESSION_MANAGER_SYNC] %d connected (client, broker) pair(s) found.", len(pairs))
        for username, broker_name in pairs:
            self._start_one(username, broker_name)

    def stop_all(self) -> None:
        for key, running in list(self.running.items()):
            try:
                running.trading_session.stop()
            except Exception:  # noqa: BLE001
                logger.exception("[SESSION_STOP_FAILED] %s", key)
        self.running.clear()

    def run_forever(self) -> None:
        self.sync()
        if not self.running:
            logger.warning(
                "[SESSION_MANAGER] No connected clients found — nothing to run. "
                "Connect a broker via the web UI, then restart or wait for the next poll."
            )

        def _handle_signal(signum, frame):
            logger.info("[SESSION_MANAGER_SHUTDOWN] received signal %s", signum)
            self._stopping = True

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        last_sync = time.time()
        try:
            while not self._stopping:
                time.sleep(1)
                if time.time() - last_sync > self.poll_seconds:
                    self.sync()
                    last_sync = time.time()
        finally:
            self.stop_all()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS,
                         help="How often to re-scan for newly-connected clients while running.")
    args = parser.parse_args()

    setup_logging(level="INFO")
    logger.info("[SESSION_MANAGER] Starting — discovering connected clients...")
    manager = SessionManager(poll_seconds=args.poll_seconds)
    manager.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
