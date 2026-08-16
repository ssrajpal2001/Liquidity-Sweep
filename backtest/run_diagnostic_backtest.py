"""
backtest/run_diagnostic_backtest.py

The full diagnostic backtest: real historical NIFTY spot data, run
through the exact live strategy code (with the diagnostic on_event hook
capturing every stage - rolling base updates, every sweep whether it led
to a trade or not, displacement, FVG, retest arm/trigger/stale, and
filter outcomes), producing the Markdown report format requested:
trade summary table, 5 representative trade audits, performance metrics.

P&L MODEL: trades NIFTY SPOT directly, NOT options (backtest/
spot_trade_simulator.py) - because 2 years of historical option premium
data isn't available. 1 lot = 65 units (current NIFTY lot size).

CHUNKED FETCH: 2 years of 1-minute candles is far more than any broker's
historical API returns in one call (Fyers/AngelOne both cap the range
per request). This fetches in 30-day windows and concatenates, logging
progress - a 2-year run will make ~24 API calls and take a few minutes.

Usage:
    python -m backtest.run_diagnostic_backtest --broker fyers --username alice
    python -m backtest.run_diagnostic_backtest --broker angelone --username alice --from 2024-08-15 --to 2026-08-15
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from backtest.diagnostic_report import build_full_report
from backtest.run_backtest import INSTRUMENT_SYMBOLS, _get_adapter
from backtest.spot_trade_simulator import simulate_spot_trade
from config.logging_setup import setup_logging
from data_feed.candle_resampler import resample_candles
from strategy.state_machine import InstrumentStateMachine, SignalDecision
from strategy.state_store import StateStore

logger = logging.getLogger("run_diagnostic_backtest")

DEFAULT_SESSION_CONFIG = {
    "high_probability_windows": [["09:15", "10:30"], ["13:15", "14:45"]],
    "blocked_window": [["11:30", "13:00"]],
}
CHUNK_DAYS = 30  # per-request window for historical fetch - conservative, works for both brokers
LOT_SIZE = 65


RATE_LIMIT_DELAY_SECONDS = 1.0   # pause between chunk requests, well under AngelOne's rate limit
RATE_LIMIT_MAX_RETRIES = 3       # retries specifically for rate-limit errors, with backoff
RATE_LIMIT_BACKOFF_SECONDS = 5.0  # wait before retrying a rate-limited chunk


def _is_rate_limit_error(exc: Exception) -> bool:
    return "exceeding access rate" in str(exc).lower() or "access denied" in str(exc).lower()


def _fetch_chunked(adapter, symbol: str, from_date: date, to_date: date) -> list[tuple]:
    """Real bug fixed here, found live: firing all ~25 chunk requests
    back-to-back with zero delay hit AngelOne's rate limiter almost
    immediately, silently dropping most of a 2-year run's data (12+ of
    25 chunks failed with 'Access denied because of exceeding access
    rate' within about 2 seconds of the run starting). Now pauses
    between every chunk, and retries rate-limit-specific failures with
    backoff before giving up on that window."""
    all_candles: list[tuple] = []
    chunk_start = from_date
    chunk_num = 0
    total_chunks = (to_date - from_date).days // CHUNK_DAYS + 1

    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), to_date)
        chunk_num += 1
        logger.info("[FETCH_CHUNK %d/%d] %s to %s", chunk_num, total_chunks, chunk_start, chunk_end)

        candles: list[tuple] = []
        for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 2):  # +1 initial try, then retries
            try:
                candles = adapter.get_historical_candles(symbol, chunk_start, chunk_end)
                break
            except Exception as exc:  # noqa: BLE001
                if _is_rate_limit_error(exc) and attempt <= RATE_LIMIT_MAX_RETRIES:
                    wait = RATE_LIMIT_BACKOFF_SECONDS * attempt
                    logger.warning(
                        "[FETCH_CHUNK %d/%d] Rate limited (attempt %d/%d) - waiting %.0fs before retry.",
                        chunk_num, total_chunks, attempt, RATE_LIMIT_MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue
                logger.exception(
                    "Chunk fetch failed for %s to %s after %d attempt(s) - skipping this window.",
                    chunk_start, chunk_end, attempt,
                )
                candles = []
                break

        all_candles.extend(candles)
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(RATE_LIMIT_DELAY_SECONDS)  # pace every request, not just ones that failed

    all_candles.sort(key=lambda c: c[0])
    return all_candles


def run(broker: str, username: str | None, from_date: date, to_date: date, output_path: str) -> None:
    instrument = "NIFTY"
    symbol = INSTRUMENT_SYMBOLS[broker][instrument]

    logger.info("[DIAGNOSTIC_BACKTEST_START] %s via %s, %s to %s", instrument, broker, from_date, to_date)

    adapter = _get_adapter(broker, username)
    raw_candles = _fetch_chunked(adapter, symbol, from_date, to_date)
    if not raw_candles:
        logger.error("[DIAGNOSTIC_BACKTEST_ABORTED] No candle data returned for the entire range.")
        return
    logger.info("[DIAGNOSTIC_BACKTEST_DATA] %d total 1-minute candles fetched.", len(raw_candles))

    ltf_minutes = 3
    ltf_candles = resample_candles(raw_candles, instrument, target_minutes=ltf_minutes)
    htf_candles = resample_candles(raw_candles, instrument, target_minutes=75)
    logger.info(
        "[DIAGNOSTIC_BACKTEST_RESAMPLED] %d LTF (%dm) candles, %d HTF (75m) candles",
        len(ltf_candles), ltf_minutes, len(htf_candles),
    )

    false_sweeps_filtered = {"count": 0}

    def on_event(event_type: str, data: dict) -> None:
        if event_type in ("sweep_rejected", "signal_filtered"):
            false_sweeps_filtered["count"] += 1

    tmp_state = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_state.close()
    Path(tmp_state.name).unlink()  # StateStore creates it fresh; avoids an empty-file JSONDecodeError
    store = StateStore(Path(tmp_state.name))

    machine = InstrumentStateMachine(
        instrument_key=instrument, store=store, session_config=DEFAULT_SESSION_CONFIG,
        sweep_buffer_points=4, on_event=on_event,
    )

    merged = sorted(
        [(c, "htf") for c in htf_candles] + [(c, "ltf") for c in ltf_candles],
        key=lambda pair: pair[0].open_time,
    )

    signals: list[SignalDecision] = []
    for candle, kind in merged:
        if kind == "htf":
            machine.on_htf_candle_close(candle)
        else:
            decision = machine.on_ltf_candle_close(candle)
            if decision is not None:
                signals.append(decision)

    logger.info("[DIAGNOSTIC_BACKTEST_SIGNALS] %d signals passed all filters.", len(signals))

    trades = []
    for signal in signals:
        forward_candles = [c for c in ltf_candles if c.open_time > signal.entry_candle.open_time]
        trade = simulate_spot_trade(signal, forward_candles, lot_size=LOT_SIZE)
        trades.append(trade)

    report = build_full_report(
        trades, false_sweeps_filtered=false_sweeps_filtered["count"],
        entry_ltf_minutes=ltf_minutes, instrument=instrument, from_date=from_date, to_date=to_date,
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("[DIAGNOSTIC_BACKTEST_COMPLETE] Report written to %s", output_path)
    print(report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--broker", default="fyers", choices=["fyers", "angelone"])
    parser.add_argument("--username", default=None)
    parser.add_argument("--from", dest="from_date", default=None, help="YYYY-MM-DD, default: 2 years ago")
    parser.add_argument("--to", dest="to_date", default=None, help="YYYY-MM-DD, default: today")
    parser.add_argument("--output", default="diagnostic_backtest_report.md")
    args = parser.parse_args()

    setup_logging(level="INFO")

    today = date.today()
    from_date = (datetime.strptime(args.from_date, "%Y-%m-%d").date() if args.from_date
                 else date(today.year - 2, today.month, today.day))
    to_date = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else today

    run(args.broker, args.username, from_date, to_date, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
