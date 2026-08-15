"""
backtest/run_backtest.py — the "back file" for the liquidity sweep strategy.

Fetches real historical 1-minute spot candles from a connected broker via
BrokerAdapter.get_historical_candles() (broker-agnostic — Fyers and
AngelOne both implement it, adding a third broker needs zero changes
here), resamples to the configured LTF/HTF timeframes
(data_feed/candle_resampler.py), and runs them through the EXACT SAME
strategy code that runs live (strategy/state_machine.py, via
backtest/replay_engine.py).

WHAT THIS TELLS YOU: how many valid signals the ruleset would have fired,
in which direction, and when — not P&L. Option premium isn't simulated,
so this validates signal LOGIC and TIMING against real spot data, not
profitability.

DEFAULTS (per your request — weekly vs monthly expiry cycle length):
    NIFTY:     last 7 calendar days   (weekly expiry -> one cycle)
    BANKNIFTY: 2026-08-01 to today    (monthly expiry -> the full month so far)
Override either with --from/--to.

Usage:
    python -m backtest.run_backtest --broker fyers --username alice --instrument NIFTY
    python -m backtest.run_backtest --broker angelone --username alice --instrument BANKNIFTY
    python -m backtest.run_backtest --broker fyers --instrument NIFTY --from 2026-08-01 --to 2026-08-14

Broker credentials:
    --username <name>  loads credentials + connects via the web UI's vault
                        (secrets/credentials.db) — works for any broker.
    (omit)              falls back to .env's FYERS_* config (Fyers only,
                        the direct main.py-style path).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from backtest.replay_engine import replay
from config.logging_setup import setup_logging
from data_feed.candle_resampler import resample_candles

logger = logging.getLogger("run_backtest")

# The symbol/instrument name each broker's get_historical_candles() expects.
INSTRUMENT_SYMBOLS = {
    "fyers": {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX"},
    "angelone": {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY"},
}

DEFAULT_SESSION_CONFIG = {
    "high_probability_windows": [["09:15", "10:30"], ["13:15", "14:45"]],
    "blocked_window": [["11:30", "13:00"]],
}
SWEEP_BUFFER_POINTS = {"NIFTY": 4, "BANKNIFTY": 8}


def _get_adapter(broker: str, username: str | None):
    """Returns a connected BrokerAdapter, either from the web UI vault
    (--username) or, for Fyers only, from .env (the direct main.py-style
    path with no web UI involved)."""
    if username:
        from webapp.broker_session_builder import BrokerSessionError, build_connected_adapter
        try:
            return build_connected_adapter(username, broker, paper_mode=True)
        except BrokerSessionError as exc:
            raise SystemExit(str(exc)) from None

    if broker != "fyers":
        raise SystemExit(f"--broker {broker} requires --username (no .env fallback path for it).")
    from auth.auth import FyersAuth
    from brokers.fyers_adapter import FyersBrokerAdapter
    from config.config_loader import load_settings

    settings = load_settings()
    auth = FyersAuth(settings.env)
    if not auth.is_authenticated():
        raise SystemExit("Not authenticated. Run: python -m auth.auth login")
    return FyersBrokerAdapter(settings.env, paper_mode=True)


def run(broker: str, instrument: str, username: str | None, from_date: date, to_date: date) -> None:
    symbols_for_broker = INSTRUMENT_SYMBOLS.get(broker)
    if symbols_for_broker is None or instrument not in symbols_for_broker:
        raise SystemExit(f"Unsupported broker/instrument combo: {broker}/{instrument}.")

    logger.info(
        "[BACKTEST_START] instrument=%s broker=%s range=%s to %s",
        instrument, broker, from_date, to_date,
    )

    adapter = _get_adapter(broker, username)
    symbol = symbols_for_broker[instrument]
    raw_candles = adapter.get_historical_candles(symbol, from_date, to_date)
    if not raw_candles:
        logger.error("[BACKTEST_ABORTED] No candle data returned — check date range and market hours.")
        return
    logger.info("[BACKTEST_DATA_FETCHED] %d 1-minute candles for %s", len(raw_candles), instrument)

    ltf_candles = resample_candles(raw_candles, instrument, target_minutes=3)
    htf_candles = resample_candles(raw_candles, instrument, target_minutes=75)
    logger.info(
        "[BACKTEST_RESAMPLED] %d LTF (3m) candles, %d HTF (75m) candles",
        len(ltf_candles), len(htf_candles),
    )

    result = replay(
        instrument_key=instrument,
        htf_candles=htf_candles,
        ltf_candles=ltf_candles,
        session_config=DEFAULT_SESSION_CONFIG,
        sweep_buffer_points=SWEEP_BUFFER_POINTS.get(instrument, 5),
    )

    print(f"\n{'=' * 60}")
    print(f"BACKTEST RESULT — {instrument} via {broker} ({from_date} to {to_date})")
    print(f"{'=' * 60}")
    print(f"HTF candles processed: {result.htf_candles_processed}")
    print(f"LTF candles processed: {result.ltf_candles_processed}")
    print(f"Signals generated:     {len(result.signals)}")
    for i, sig in enumerate(result.signals, 1):
        print(
            f"  {i}. {sig.entry_candle.open_time} | {sig.direction.upper()} | "
            f"spot_sl={sig.spot_structural_sl:.2f} | "
            f"sweep_level={sig.sweep.level:.2f} pierce={sig.sweep.pierce_points:.2f}pts"
        )
    if not result.signals:
        print("  (no signals — could mean a genuinely quiet period, or that filters "
              "are too strict for this window; try a longer date range)")
    print(f"{'=' * 60}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--broker", default="fyers", choices=["fyers", "angelone"])
    parser.add_argument("--instrument", required=True, choices=["NIFTY", "BANKNIFTY"])
    parser.add_argument("--username", default=None, help="Load credentials from the web UI vault")
    parser.add_argument("--from", dest="from_date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", default=None, help="YYYY-MM-DD")
    args = parser.parse_args()

    setup_logging(level="INFO")

    today = date.today()
    if args.from_date and args.to_date:
        from_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        to_date = datetime.strptime(args.to_date, "%Y-%m-%d").date()
    elif args.instrument == "NIFTY":
        from_date, to_date = today - timedelta(days=7), today
    else:  # BANKNIFTY
        from_date, to_date = date(today.year, today.month, 1), today

    run(args.broker, args.instrument, args.username, from_date, to_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
