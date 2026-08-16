from __future__ import annotations

from datetime import datetime, timezone

from backtest.diagnostic_report import build_full_report, build_performance_summary, build_summary_table
from backtest.spot_trade_simulator import simulate_spot_trade
from data_feed.candle_aggregator import Candle
from strategy.displacement import DisplacementDirection, FairValueGap
from strategy.state_machine import SignalDecision
from strategy.sweep_detector import SweepDirection, SweepEvent


def _candle(o, h, l, c, minute):
    return Candle(instrument_key="NIFTY", timeframe_minutes=3,
                  open_time=datetime(2026, 8, 10, 9, minute, tzinfo=timezone.utc), open=o, high=h, low=l, close=c)


def _make_trade(outcome_candles):
    entry_candle = _candle(25000, 25005, 24995, 25000, 15)
    sweep = SweepEvent(direction=SweepDirection.BULLISH, level=24950, candle=entry_candle, pierce_points=2)
    fvg = FairValueGap(direction=DisplacementDirection.BULLISH, gap_high=25010, gap_low=25000,
                        formed_at_candle_open_time="x")
    signal = SignalDecision(instrument_key="NIFTY", direction="bullish", sweep=sweep, fvg=fvg,
                             entry_candle=entry_candle, spot_structural_sl=24980)
    return simulate_spot_trade(signal, outcome_candles, lot_size=65)


def test_summary_table_has_correct_column_count():
    trade = _make_trade([_candle(25000, 25035, 24995, 25030, 18), _candle(25030, 25065, 25025, 25060, 21)])
    table = build_summary_table([trade], entry_ltf_minutes=3)
    header_line = table.split("\n")[0]
    assert header_line.count("|") == 14  # 13 columns -> 14 pipe characters


def test_summary_table_empty_when_no_trades():
    table = build_summary_table([], entry_ltf_minutes=3)
    assert "|" in table  # header still renders even with zero rows


def test_performance_summary_computes_win_rate_correctly():
    winner = _make_trade([_candle(25000, 25035, 24995, 25030, 18), _candle(25030, 25065, 25025, 25060, 21)])
    loser = _make_trade([_candle(25000, 25010, 24975, 24980, 18)])
    summary = build_performance_summary([winner, loser], false_sweeps_filtered=3)
    assert "Total Trades Triggered:** 2" in summary
    assert "Win Rate" in summary
    assert "50.0%" in summary  # 1 win out of 2
    assert "False Sweeps Filtered Out:** 3" in summary


def test_performance_summary_handles_zero_trades_without_crashing():
    summary = build_performance_summary([], false_sweeps_filtered=0)
    assert "Total Trades Triggered:** 0" in summary


def test_performance_summary_includes_rejection_breakdown_when_provided():
    """This IS the actual bottleneck diagnosis — sorted by count, highest
    first, so the dominant rejection reason is immediately visible
    without needing to grep a raw log file for event names that were
    never actually written to it as text."""
    summary = build_performance_summary(
        [], false_sweeps_filtered=1547,
        rejection_breakdown={"retest_stale": 900, "no_displacement": 400, "no_fvg": 247},
    )
    assert "retest_stale" in summary
    assert "58.2%" in summary  # 900/1547
    # sorted descending — retest_stale (highest count) must appear before no_fvg (lowest)
    assert summary.index("retest_stale") < summary.index("no_fvg")


def test_performance_summary_omits_breakdown_section_when_not_provided():
    """Backward compatible — callers that don't pass a breakdown still
    get a valid report, just without that extra section."""
    summary = build_performance_summary([], false_sweeps_filtered=10)
    assert "Rejection reason breakdown" not in summary


def test_full_report_includes_all_required_sections():
    trade = _make_trade([_candle(25000, 25035, 24995, 25030, 18), _candle(25030, 25065, 25025, 25060, 21)])
    report = build_full_report([trade], false_sweeps_filtered=5, entry_ltf_minutes=3,
                                instrument="NIFTY", from_date="2024-08-15", to_date="2026-08-15")
    assert "Trade Summary" in report
    assert "Representative Trade Audit" in report
    assert "Performance Metrics" in report
    assert "SPOT INDEX directly" in report
    assert "lot size 65" in report


def test_full_report_shows_at_most_two_examples_per_outcome_type():
    """With all-identical-outcome trades, showing 5 duplicates of the same
    scenario wouldn't be 'representative' — capped at 2 per outcome type,
    diversity across outcomes matters more than hitting exactly 5."""
    trades = [
        _make_trade([_candle(25000, 25035, 24995, 25030, 18), _candle(25030, 25065, 25025, 25060, 21)])
        for _ in range(10)
    ]
    report = build_full_report(trades, false_sweeps_filtered=0, entry_ltf_minutes=3,
                                instrument="NIFTY", from_date="x", to_date="y")
    assert report.count("### Trade #") == 2


def test_full_report_shows_up_to_five_with_diverse_outcomes():
    winner1 = _make_trade([_candle(25000, 25035, 24995, 25030, 18), _candle(25030, 25065, 25025, 25060, 21)])
    winner2 = _make_trade([_candle(25000, 25035, 24995, 25030, 18), _candle(25030, 25065, 25025, 25060, 21)])
    loser1 = _make_trade([_candle(25000, 25010, 24975, 24980, 18)])
    loser2 = _make_trade([_candle(25000, 25010, 24975, 24980, 18)])
    be_stop = _make_trade([_candle(25000, 25035, 24995, 25030, 18), _candle(25030, 25032, 24995, 25000, 21)])
    stale = _make_trade([_candle(25000, 25010, 24995, 25005, 18)])

    trades = [winner1, winner2, loser1, loser2, be_stop, stale]
    report = build_full_report(trades, false_sweeps_filtered=0, entry_ltf_minutes=3,
                                instrument="NIFTY", from_date="x", to_date="y")
    assert report.count("### Trade #") == 5
