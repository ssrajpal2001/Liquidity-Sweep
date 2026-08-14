"""
backtest/run_backtest.py — the "back file" for the liquidity sweep strategy.

Fetches real historical 1-minute spot candles from a connected broker,
resamples them to the configured LTF/HTF timeframes (backtest/
candle_resampler.py), and runs them through the EXACT SAME strategy code
that runs live (strategy/state_machine.py, via backtest/replay_engine.py)
— this is what makes it a real backtest rather than a second, unverified
copy of the rules.

WHAT THIS TELLS YOU: how many valid signals the ruleset would have fired,
in which direction, and when — not P&L. Option premium isn't simulated
(see backtest/replay_engine.py's docstring), so this validates signal
LOGIC and TIMING against real spot data, not profitability.

DEFAULTS (per your request — weekly vs monthly expiry cycle length):
    NIFTY:     last 7 calendar days   (weekly expiry -> one cycle)
    BANKNIFTY: 2026-08-01 to today    (monthly expiry -> the full month so far)
Override either with --from/--to.

Usage:
    python -m backtest.run_backtest --broker fyers --instrument NIFTY
    python -m backtest.run_backtest --broker fyers --instrument BANKNIFTY
    python -m backtest.run_backtest --broker fyers --instrument NIFTY --from 2026-08-01 --to 2026-08-14
    python -m backtest.run_backtest --broker angelone --username ssrajpal2001 --instrument NIFTY

Broker credentials:
    --username <name>  loads credentials from the web UI's vault
                        (secrets/credentials.db) — same as run_webapp.py
    (omit)              falls back to .env's FYERS_* config, Fyers only
                        (the direct main.py-style path)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from backtest.candle_resampler import resample_candles
from backtest.replay_engine import replay
from config.logging_setup import setup_logging

logger = logging.getLogger("run_backtest")

# Fyers symbol format, confirmed against real sample code.
FYERS_SYMBOLS = {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX"}

# AngelOne interval names, confirmed against real forum-posted request
# samples: ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, TEN_MINUTE,
# FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY. No native 75-minute
# here either — same 1-minute-then-resample approach as Fyers.
ANGELONE_INTERVAL_1MIN = "ONE_MINUTE"

DEFAULT_SESSION_CONFIG = {
    "high_probability_windows": [["09:15", "10:30"], ["13:15", "14:45"]],
    "blocked_window": [["11:30", "13:00"]],
}
SWEEP_BUFFER_POINTS = {"NIFTY": 4, "BANKNIFTY": 8}


def _get_fyers_model(username: str | None):
    if username:
        from pathlib import Path
        from webapp.app import TOKEN_STORE_DIR, _build_env_like
        from webapp.credential_vault import CredentialVault
        from webapp.secrets_bootstrap import get_or_create_encryption_key
        from brokers.fyers_adapter import FyersBrokerAdapter

        vault = CredentialVault(get_or_create_encryption_key())
        fields = vault.get_credentials(username, "fyers")
        if fields is None:
            raise SystemExit(f"No Fyers credentials saved for '{username}'. Add them via the web UI first.")
        env_like = _build_env_like(fields, TOKEN_STORE_DIR / f"{username}__fyers_token_store.json")
        adapter = FyersBrokerAdapter(env_like, paper_mode=True)
        if not adapter.is_authenticated():
            raise SystemExit(f"'{username}' has saved Fyers credentials but isn't connected — Connect via the web UI first.")
        return adapter.rest_client.model

    from auth.auth import FyersAuth
    from config.config_loader import load_settings
    from data_feed.fyers_rest_client import FyersRestClient

    settings = load_settings()
    auth = FyersAuth(settings.env)
    if not auth.is_authenticated():
        raise SystemExit("Not authenticated. Run: python -m auth.auth login")
    return FyersRestClient(settings.env, auth=auth).model


def _fetch_fyers_1min_candles(model, symbol: str, from_date: date, to_date: date) -> list[tuple]:
    """Returns (epoch_seconds, open, high, low, close, volume) tuples.
    Fyers' history() response shape confirmed against real usage code:
    response['candles'] = [[epoch, o, h, l, c, v], ...]."""
    payload = {
        "symbol": symbol, "resolution": "1", "date_format": "1",
        "range_from": from_date.isoformat(), "range_to": to_date.isoformat(),
        "cont_flag": "1",
    }
    response = model.history(data=payload)
    if not isinstance(response, dict) or response.get("s") != "ok":
        logger.error("Fyers history() failed for %s: %s", symbol, response)
        return []
    return [tuple(row) for row in response.get("candles", [])]


def _get_angelone_adapter(username: str | None):
    if not username:
        raise SystemExit(
            "--broker angelone requires --username (AngelOne credentials only "
            "live in the web UI vault, there's no .env fallback for it)."
        )
    from webapp.app import TOKEN_STORE_DIR, _build_env_like
    from webapp.credential_vault import CredentialVault
    from webapp.secrets_bootstrap import get_or_create_encryption_key
    from brokers.angelone_adapter import AngelOneBrokerAdapter

    vault = CredentialVault(get_or_create_encryption_key())
    fields = vault.get_credentials(username, "angelone")
    if fields is None:
        raise SystemExit(f"No AngelOne credentials saved for '{username}'. Add them via the web UI first.")
    env_like = _build_env_like(fields, TOKEN_STORE_DIR / f"{username}__angelone_token_store.json")
    adapter = AngelOneBrokerAdapter(env_like, paper_mode=True)
    # AngelOne's TOTP login is cheap and safe to re-run — no browser
    # round trip needed, so just log in fresh rather than requiring the
    # web UI to already show "Connected".
    check = adapter.login()
    if not check.ok:
        raise SystemExit(f"AngelOne login failed: {check.detail}")
    return adapter


def _fetch_angelone_1min_candles(adapter, instrument: str, from_date: date, to_date: date) -> list[tuple]:
    """Returns (epoch_seconds, open, high, low, close, volume) tuples.
    Response shape confirmed against real forum-posted samples:
    response['data'] = [[datetime_str, o, h, l, c, v], ...]."""
    from execution.angelone_instrument_master import AngelOneInstrumentMaster

    token = AngelOneInstrumentMaster.spot_index_token(instrument, "NSE")
    if token is None:
        raise SystemExit(
            f"No known AngelOne spot index token for {instrument} — see "
            "execution/angelone_instrument_master.py's KNOWN_INDEX_SPOT_TOKENS "
            "(community-reported, not independently verified)."
        )

    smart = adapter._get_smart()  # noqa: SLF001 — script reaching into adapter internals deliberately
    params = {
        "exchange": "NSE", "symboltoken": token, "interval": ANGELONE_INTERVAL_1MIN,
        "fromdate": from_date.strftime("%Y-%m-%d 09:15"),
        "todate": to_date.strftime("%Y-%m-%d 15:30"),
    }
    response = smart.getCandleData(params)
    if not response or not response.get("status"):
        logger.error("AngelOne getCandleData failed for %s: %s", instrument, response)
        return []

    candles = []
    for row in response.get("data", []):
        try:
            dt_str, o, h, l, c, v = row
            ts = datetime.fromisoformat(dt_str)
            candles.append((ts.timestamp(), o, h, l, c, v))
        except (ValueError, TypeError):
            logger.warning("Skipping malformed AngelOne candle row: %s", row)
    return candles


def run(broker: str, instrument: str, username: str | None, from_date: date, to_date: date) -> None:
    if instrument not in FYERS_SYMBOLS:  # same instrument set for both brokers
        raise SystemExit(f"Unsupported instrument '{instrument}'. Use NIFTY or BANKNIFTY.")

    logger.info(
        "[BACKTEST_START] instrument=%s broker=%s range=%s to %s",
        instrument, broker, from_date, to_date,
    )

    if broker == "fyers":
        model = _get_fyers_model(username)
        symbol = FYERS_SYMBOLS[instrument]
        raw_candles = _fetch_fyers_1min_candles(model, symbol, from_date, to_date)
    elif broker == "angelone":
        adapter = _get_angelone_adapter(username)
        raw_candles = _fetch_angelone_1min_candles(adapter, instrument, from_date, to_date)
    else:
        raise SystemExit(f"Unsupported broker '{broker}'. Use fyers or angelone.")

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
    print(f"BACKTEST RESULT — {instrument} ({from_date} to {to_date})")
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
