"""
risk_controls/daily_guard.py

The thing that stops a bug or a bad regime from compounding. Every module
that would place a new entry MUST call DailyGuard.can_trade() first and
respect a False return — this is enforced by convention (main.py wires it
in front of the state machine's signal output), not by this module
reaching into the rest of the bot.

Two independent trip conditions, either one halts new entries for the
rest of the day:
  - max_daily_loss_pct of total capital realized as loss
  - max_trades_per_day reached (win or loss — caps overtrading)

Also exposes a manual kill switch (`halt()`), separate from the automatic
trip conditions, for the "I want to stop this right now" case — it does
NOT close open positions, only blocks new entries, since force-closing
positions is a decision that deserves an explicit, separate action.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)


@dataclass
class DailyGuardState:
    trade_date: date
    trades_taken: int = 0
    realized_pnl_inr: float = 0.0
    manually_halted: bool = False


class DailyGuard:
    def __init__(self, total_capital_inr: float, max_daily_loss_pct: float, max_trades_per_day: int):
        self.total_capital_inr = total_capital_inr
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_trades_per_day = max_trades_per_day
        self._state = DailyGuardState(trade_date=date.today())

    def _roll_day_if_needed(self) -> None:
        today = date.today()
        if self._state.trade_date != today:
            logger.info(
                "New trading day (%s) — resetting daily guard (prior day: %d trades, PnL %.2f)",
                today, self._state.trades_taken, self._state.realized_pnl_inr,
            )
            self._state = DailyGuardState(trade_date=today)

    def can_trade(self) -> tuple[bool, str]:
        self._roll_day_if_needed()

        if self._state.manually_halted:
            return False, "Manually halted via kill switch."

        max_loss_inr = self.total_capital_inr * (self.max_daily_loss_pct / 100)
        if self._state.realized_pnl_inr <= -max_loss_inr:
            return False, (
                f"Daily max loss breached: {self._state.realized_pnl_inr:.2f} "
                f"<= -{max_loss_inr:.2f} ({self.max_daily_loss_pct}% of capital)"
            )

        if self._state.trades_taken >= self.max_trades_per_day:
            return False, f"Daily max trades reached: {self._state.trades_taken}/{self.max_trades_per_day}"

        return True, "OK"

    def record_trade_closed(self, realized_pnl_inr: float) -> None:
        self._roll_day_if_needed()
        self._state.trades_taken += 1
        self._state.realized_pnl_inr += realized_pnl_inr
        logger.info(
            "Trade recorded: pnl=%.2f | day totals: trades=%d pnl=%.2f",
            realized_pnl_inr, self._state.trades_taken, self._state.realized_pnl_inr,
        )
        can_trade, reason = self.can_trade()
        if not can_trade:
            logger.warning("Daily guard tripped after this trade: %s", reason)

    def halt(self) -> None:
        self._state.manually_halted = True
        logger.warning("Daily guard: manual kill switch engaged. New entries blocked.")

    def resume(self) -> None:
        self._state.manually_halted = False
        logger.info("Daily guard: manual kill switch released.")

    @property
    def state(self) -> DailyGuardState:
        self._roll_day_if_needed()
        return self._state
