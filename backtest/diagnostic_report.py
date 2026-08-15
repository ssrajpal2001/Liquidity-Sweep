"""
backtest/diagnostic_report.py

Formats a completed diagnostic backtest run into the exact Markdown
report structure requested: a summary table (one row per triggered
trade), a detailed step-by-step audit for 5 representative trades, and a
performance metrics block. Pure formatting logic — no data fetching, no
strategy logic — so it's independently testable against a synthetic run.
"""
from __future__ import annotations

from typing import Optional

from backtest.spot_trade_simulator import SimulatedTrade, TradeOutcome


def _fmt_ts(ts) -> str:
    return ts.strftime("%Y-%m-%d %H:%M IST") if ts is not None else "—"


def _fmt_price(p) -> str:
    return f"{p:.2f}" if p is not None else "—"


def build_summary_table(trades: list[SimulatedTrade], entry_ltf_minutes: int) -> str:
    header = (
        "| # | Date & Timestamp (IST) | Entry TF | Setup | Swept Level | Sweep Candle TS | "
        "FVG Zone [low, high] | Retest Entry | Spot SL | Target 1 | Target 2 | Outcome | "
        "P&L (pts / R:R) |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = []
    for i, t in enumerate(trades, 1):
        setup = "Bull" if t.signal.direction == "bullish" else "Bear"
        fvg = t.signal.fvg
        rows.append(
            f"| {i} | {_fmt_ts(t.entry_time)} | {entry_ltf_minutes}m | {setup} | "
            f"{_fmt_price(t.signal.sweep.level)} | {_fmt_ts(t.signal.sweep.candle.open_time)} | "
            f"[{_fmt_price(fvg.gap_low)}, {_fmt_price(fvg.gap_high)}] | {_fmt_price(t.entry_price)} | "
            f"{_fmt_price(t.sl_price)} | {_fmt_price(t.target1_price)} | {_fmt_price(t.target2_price)} | "
            f"{t.outcome.value} | {t.points_pnl:+.1f} pts / {t.rr_achieved:+.2f}R |"
        )
    return header + "\n".join(rows)


def build_trade_audit(trade: SimulatedTrade, index: int, base_high: Optional[float] = None,
                       base_low: Optional[float] = None, base_time=None) -> str:
    sweep = trade.signal.sweep
    fvg = trade.signal.fvg
    lines = [f"### Trade #{index} — {trade.outcome.value} ({trade.signal.direction})", ""]

    if base_time is not None:
        lines.append(f"- **Rolling Base:** {_fmt_ts(base_time)} — high={_fmt_price(base_high)}, low={_fmt_price(base_low)}")
    lines.append(
        f"- **Sweep Detection:** {_fmt_ts(sweep.candle.open_time)} — "
        f"{sweep.direction.value} sweep of level {_fmt_price(sweep.level)}, "
        f"pierced {sweep.pierce_points:.2f} pts, closed back inside"
    )
    lines.append(
        f"- **Displacement & FVG:** FVG zone [{_fmt_price(fvg.gap_low)}, {_fmt_price(fvg.gap_high)}], "
        f"formed at {fvg.formed_at_candle_open_time}"
    )
    lines.append(f"- **Retest Trigger:** entry at {_fmt_ts(trade.entry_time)}, price {_fmt_price(trade.entry_price)}")
    if trade.target1_hit_time:
        lines.append(f"- **Target 1 Hit:** {_fmt_ts(trade.target1_hit_time)} at {_fmt_price(trade.target1_price)} — SL moved to breakeven")
    lines.append(
        f"- **Exit:** {trade.outcome.value} at {_fmt_ts(trade.exit_time)}, price {_fmt_price(trade.exit_price)} "
        f"— {trade.points_pnl:+.1f} points ({trade.rr_achieved:+.2f}R), Rs{trade.rupee_pnl:+.2f} "
        f"(lot size {trade.lot_size})"
    )
    return "\n".join(lines)


def build_performance_summary(trades: list[SimulatedTrade], false_sweeps_filtered: int) -> str:
    total = len(trades)
    wins = sum(1 for t in trades if t.outcome in (TradeOutcome.TARGET1_THEN_TARGET2, TradeOutcome.TARGET1_THEN_SL))
    win_rate = (wins / total * 100) if total else 0.0
    rr_values = [t.rr_achieved for t in trades if t.outcome != TradeOutcome.STALE]
    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += t.points_pnl
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    stale = sum(1 for t in trades if t.outcome == TradeOutcome.STALE)

    return (
        f"- **Total Trades Triggered:** {total}\n"
        f"- **Win Rate (T1/T2 completion):** {win_rate:.1f}%\n"
        f"- **Average R:R Achieved:** {avg_rr:+.2f}R\n"
        f"- **Max Drawdown:** {max_dd:.1f} points\n"
        f"- **Stale/Unresolved Trades:** {stale}\n"
        f"- **False Sweeps Filtered Out:** {false_sweeps_filtered} "
        f"(rejected for no displacement, no FVG, or invalid time window/bias)\n"
    )


def build_full_report(
    trades: list[SimulatedTrade],
    false_sweeps_filtered: int,
    entry_ltf_minutes: int,
    instrument: str,
    from_date, to_date,
) -> str:
    parts = [
        f"# Diagnostic Backtest Report - {instrument} ({from_date} to {to_date})",
        "",
        "**Model:** trading the SPOT INDEX directly (not options) - 1 lot = "
        f"{trades[0].lot_size if trades else '?'} units. See report footer for why.",
        "",
        "## 1. Trade Summary",
        "",
        build_summary_table(trades, entry_ltf_minutes) if trades else "_No trades triggered in this window._",
        "",
        "## 2. Representative Trade Audit",
        "",
    ]

    by_outcome: dict[TradeOutcome, list[SimulatedTrade]] = {}
    for t in trades:
        by_outcome.setdefault(t.outcome, []).append(t)
    representative: list[SimulatedTrade] = []
    for outcome in (TradeOutcome.TARGET1_THEN_TARGET2, TradeOutcome.SL_HIT_DIRECT,
                     TradeOutcome.TARGET1_THEN_SL, TradeOutcome.STALE):
        representative.extend(by_outcome.get(outcome, [])[:2])
    representative = representative[:5]

    for i, t in enumerate(representative, 1):
        parts.append(build_trade_audit(t, i))
        parts.append("")

    parts.append("## 3. Performance Metrics")
    parts.append("")
    parts.append(build_performance_summary(trades, false_sweeps_filtered))
    parts.append(
        "\n---\n_P&L model note: this backtest simulates trading NIFTY SPOT directly "
        "(not an option contract), because 2 years of historical option premium data "
        "isn't available. Real option P&L would differ due to theta decay, IV changes, "
        "and the Spot-Risk x Delta sizing formula used in live trading "
        "(execution/risk_engine.py) - this report validates SIGNAL LOGIC AND TIMING "
        "against real spot price action, not real option profitability._"
    )
    return "\n".join(parts)
