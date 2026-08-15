"""
main.py — orchestrator entrypoint, broker-agnostic.

This runs against ANY BrokerAdapter (Fyers, AngelOne, or a future
broker) — nothing in TradingSession imports a broker-specific class.
Every broker-facing call goes through the BrokerAdapter interface
(brokers/base.py): start_feed/subscribe/unsubscribe, get_option_chain,
nearest_expiry, place_entry_buy/sell, get_order_status,
get_historical_candles. Swapping which broker this runs against is a
one-line change at the bottom of main() — the entire TradingSession
class doesn't know or care which one it got.

Wires together: config -> auth -> broker adapter -> WS feed -> candle
aggregator -> per-instrument state machine -> daily guard -> expiry
resolution -> option selection -> risk engine -> order manager ->
position manager (entry/SL/TSL/target1/target2/exit).

HARD SAFETY GATE: refuses to start unless settings.yaml's
app.environment is "paper" AND .env's PAPER_MODE is true — see
_assert_paper_gate(). Going live is a deliberate, separate step.

WS-RECONNECT RESYNC (previously a placeholder, now real): on reconnect,
fetches recent 1-minute history via broker.get_historical_candles() and
feeds it through CandleAggregator.bootstrap_instrument(), which
resamples to every registered timeframe and repairs whatever candle was
truncated by the gap — see data_feed/candle_aggregator.py's docstring.

Every meaningful stage logs a distinct [TAG] — see execution/
position_manager.py's docstring for the full list.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import date, timedelta, timezone

from auth.auth import FyersAuth
from brokers.base import BrokerAdapter
from brokers.fyers_adapter import FyersBrokerAdapter
from config.config_loader import ConfigError, Settings, load_settings
from config.logging_setup import setup_logging
from data_feed.candle_aggregator import Candle, CandleAggregator
from execution.generic_option_selector import GenericOptionSelector
from execution.position_manager import Position, PositionManager
from execution.risk_engine import compute_risk_plan
from risk_controls.daily_guard import DailyGuard
from strategy.state_machine import InstrumentStateMachine, SignalDecision
from strategy.state_store import StateStore

logger = logging.getLogger("main")

IST = timezone(timedelta(hours=5, minutes=30))

WS_STALE_WARNING_SECONDS = 60
WS_FORCE_RECONNECT_SECONDS = 120
RESYNC_LOOKBACK_MINUTES = 180  # how far back to fetch on reconnect — comfortably covers one 75m HTF bucket


def _assert_paper_gate(settings: Settings) -> None:
    yaml_env = settings.raw["app"]["environment"]
    env_paper = settings.env.paper_mode
    if yaml_env != "paper" or not env_paper:
        raise RuntimeError(
            "Refusing to start: paper trading (Phase 10) hasn't been "
            "completed yet. settings.yaml app.environment must be "
            f"'paper' (currently '{yaml_env}') AND .env PAPER_MODE must "
            f"be true (currently {env_paper}). Going live is a "
            "deliberate step — see README 'Going live'."
        )


class TradingSession:
    """Broker-agnostic — depends only on brokers.base.BrokerAdapter,
    never on a concrete Fyers/AngelOne class."""

    def __init__(self, settings: Settings, broker: BrokerAdapter, session_id: str | None = None):
        self.settings = settings
        self.broker = broker
        self.session_id = session_id or broker.broker_name
        # Per-session state path — critical for multi-client: without
        # this, two clients both trading NIFTY would share one rolling-
        # base/void-state file and silently corrupt each other's state.
        from pathlib import Path
        state_path = Path(f"state/{self.session_id}_strategy_state.json")
        self.store = StateStore(state_path)

        self.daily_guard = DailyGuard(
            total_capital_inr=settings.capital["total_capital_inr"],
            max_daily_loss_pct=settings.capital["max_daily_loss_pct"],
            max_trades_per_day=settings.capital["max_trades_per_day"],
        )
        self.position_manager = PositionManager(
            order_manager=broker,  # BrokerAdapter already exposes place_entry_buy/place_exit_sell/get_order_status
            get_order_status_fn=broker.get_order_status,
            trail_distance_points=settings.risk.get("trail_distance_points", 5.0),
            target1_booking_pct=settings.execution["target1_booking_pct"],
            on_closed=self._on_position_closed,
        )

        self.instruments = settings.instruments
        htf = settings.raw["timeframes"]["htf_minutes"]
        self.primary_ltf = settings.raw["timeframes"]["ltf_minutes"][0]

        self.candle_agg = CandleAggregator(on_close=self._on_candle_close)
        self.state_machines: dict[str, InstrumentStateMachine] = {}
        self.option_selectors: dict[str, GenericOptionSelector] = {}
        self.open_positions: dict[str, Position] = {}
        self.positions_by_option_symbol: dict[str, Position] = {}
        self.instrument_by_key = {i["spot_key"]: i for i in self.instruments}

        for inst in self.instruments:
            key = inst["spot_key"]
            self.candle_agg.register(key, htf)
            for ltf in settings.raw["timeframes"]["ltf_minutes"]:
                self.candle_agg.register(key, ltf)

            self.state_machines[key] = InstrumentStateMachine(
                instrument_key=key,
                store=self.store,
                session_config=settings.session,
                sweep_buffer_points=settings.risk["sweep_buffer_points"].get(inst["name"], 5),
                atr_multiplier=settings.risk["atr_displacement_multiplier"],
            )
            self.option_selectors[key] = GenericOptionSelector(
                broker,
                underlying_symbol=key,
                strike_interval=inst["strike_interval"],
                target_delta_min=settings.raw["option_selection"]["target_delta_min"],
                target_delta_max=settings.raw["option_selection"]["target_delta_max"],
                cache_refresh_seconds=settings.raw["option_selection"]["delta_cache_refresh_seconds"],
            )

        self._latest_spot: dict[str, float] = {}

    # -- candle pipeline -----------------------------------------------------
    def _on_candle_close(self, candle: Candle) -> None:
        machine = self.state_machines.get(candle.instrument_key)
        if machine is None:
            return

        htf = self.settings.raw["timeframes"]["htf_minutes"]

        if candle.timeframe_minutes == htf:
            logger.debug("[HTF_CANDLE_CLOSE] %s %s", candle.instrument_key, candle.open_time)
            machine.on_htf_candle_close(candle)
            return

        if candle.timeframe_minutes == self.primary_ltf:
            self._latest_spot[candle.instrument_key] = candle.close
            decision = machine.on_ltf_candle_close(candle)
            if decision is not None:
                logger.info(
                    "[SIGNAL_PASSED_FILTERS] %s direction=%s spot_sl=%.2f",
                    candle.instrument_key, decision.direction, decision.spot_structural_sl,
                )
                self._handle_signal(candle.instrument_key, decision)

    # -- signal -> execution --------------------------------------------------
    def _handle_signal(self, instrument_key: str, decision: SignalDecision) -> None:
        if instrument_key in self.open_positions:
            logger.info("[SIGNAL_SKIPPED] %s already has an open position.", instrument_key)
            return

        can_trade, reason = self.daily_guard.can_trade()
        if not can_trade:
            logger.warning("[SIGNAL_SKIPPED] %s daily guard: %s", instrument_key, reason)
            return

        inst_config = self.instrument_by_key[instrument_key]
        option_type = "PE" if decision.direction == "bearish" else "CE"

        expiry = self.broker.nearest_expiry(instrument_key)
        if expiry is None:
            logger.error("[SIGNAL_SKIPPED] %s no resolvable expiry.", instrument_key)
            return

        spot_price = self._latest_spot.get(instrument_key)
        if spot_price is None:
            logger.error("[SIGNAL_SKIPPED] %s no spot price available yet.", instrument_key)
            return

        selected = self.option_selectors[instrument_key].select(expiry, spot_price, option_type)
        if selected is None:
            logger.error("[SIGNAL_SKIPPED] %s no strike found for %s.", instrument_key, option_type)
            return
        logger.info(
            "[STRIKE_SELECTED] %s %s strike=%.0f symbol=%s delta=%.2f%s ltp=%.2f",
            instrument_key, option_type, selected.strike_price, selected.symbol,
            selected.delta, " (estimated)" if selected.delta_is_estimated else "", selected.ltp,
        )

        plan = compute_risk_plan(
            entry_premium=selected.ltp,
            spot_entry=spot_price,
            spot_structural_sl=decision.spot_structural_sl,
            delta=selected.delta,
            lot_size=inst_config["lot_size"],
            total_capital_inr=self.settings.capital["total_capital_inr"],
            risk_per_trade_pct=self.settings.capital["risk_per_trade_pct"],
            target1_rr=self.settings.execution["target1_rr"],
            direction=decision.direction,
        )
        logger.info(
            "[RISK_PLAN] %s lots=%d qty=%d premium_sl=%.2f target1=%.2f capital_at_risk=%.2f",
            instrument_key, plan.lots, plan.quantity, plan.premium_sl_price,
            plan.target1_price, plan.capital_at_risk_inr,
        )

        position = self.position_manager.open_position(
            selected.symbol, decision.direction, plan, selected.ltp,
            tag=f"{instrument_key}-{decision.direction}",
        )
        if position is not None:
            self.open_positions[instrument_key] = position
            self.positions_by_option_symbol[selected.symbol] = position
            subscribed = self.broker.subscribe([selected.symbol])
            logger.info(
                "[OPTION_LEG_SUBSCRIBED] %s subscribed=%s — SL/TSL/Target now tracking this contract's live price.",
                selected.symbol, subscribed,
            )

    def _on_position_closed(self, position: Position) -> None:
        self.daily_guard.record_trade_closed(position.realized_pnl_inr)
        for spot_key, pos in list(self.open_positions.items()):
            if pos is position:
                del self.open_positions[spot_key]
        self.positions_by_option_symbol.pop(position.instrument_key, None)
        self.broker.unsubscribe([position.instrument_key])

    # -- WS-reconnect resync (real implementation, not a placeholder) -----------
    def _rest_resync_on_reconnect(self) -> None:
        today = date.today()
        for inst in self.instruments:
            key = inst["spot_key"]
            try:
                raw = self.broker.get_historical_candles(key, today, today)
            except Exception:  # noqa: BLE001
                logger.exception("[WS_RECONNECT_RESYNC] Failed to fetch resync data for %s", key)
                continue
            if not raw:
                logger.warning("[WS_RECONNECT_RESYNC] No candle data returned for %s — gap not repaired.", key)
                continue
            cutoff = time.time() - RESYNC_LOOKBACK_MINUTES * 60
            recent = [c for c in raw if c[0] >= cutoff]
            self.candle_agg.bootstrap_instrument(key, recent)
            logger.info(
                "[WS_RECONNECT_RESYNC] %s: repaired from %d recent 1-min candles.",
                key, len(recent),
            )

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        symbols = [i["spot_key"] for i in self.instruments]

        def on_tick(symbol: str, ltp: float):
            position = self.positions_by_option_symbol.get(symbol)
            if position is not None:
                self.position_manager.on_price_update(position, ltp)
                return
            self.candle_agg.ingest_tick(symbol, ltp, epoch_ms=None)
            self._latest_spot[symbol] = ltp

        self.broker.start_feed(symbols, on_tick=on_tick, on_reconnect=self._rest_resync_on_reconnect)
        logger.info("[SESSION_STARTED] session_id=%s instruments=%s broker=%s",
                    self.session_id, symbols, self.broker.broker_name)

    def stop(self) -> None:
        self.broker.stop_feed()
        logger.info("[SESSION_STOPPED] session_id=%s", self.session_id)

    def get_status(self) -> dict:
        """Everything the web UI's live dashboard needs — positions,
        daily P&L, guard state, feed health. Reads directly from
        in-memory state (this session runs in the same process as the
        Flask app that calls this), no separate IPC needed."""
        can_trade, guard_reason = self.daily_guard.can_trade()
        guard_state = self.daily_guard.state
        return {
            "session_id": self.session_id,
            "broker": self.broker.broker_name,
            "feed_open": self.broker.is_feed_open,
            "seconds_since_last_message": self.broker.seconds_since_last_message(),
            "open_positions": [
                {
                    "instrument": spot_key,
                    "option_symbol": pos.instrument_key,
                    "direction": pos.direction,
                    "status": pos.status.value,
                    "entry_price": pos.plan.entry_price,
                    "current_sl": pos.current_sl,
                    "target1_price": pos.plan.target1_price,
                    "remaining_quantity": pos.remaining_quantity,
                    "realized_pnl_inr": round(pos.realized_pnl_inr, 2),
                }
                for spot_key, pos in self.open_positions.items()
            ],
            "daily_trades": guard_state.trades_taken,
            "daily_realized_pnl_inr": round(guard_state.realized_pnl_inr, 2),
            "can_trade": can_trade,
            "guard_reason": guard_reason,
        }


def _get_broker_adapter(settings: Settings) -> BrokerAdapter:
    """Fyers-via-.env only, for now — the direct main.py-style path.
    Running against AngelOne (or any web-UI-connected broker) needs a
    per-client BrokerAdapter built from vault credentials instead — that
    plumbing lives in webapp/broker_session_builder.py and is what
    orchestration/session_manager.py uses, not duplicated here."""
    if not (settings.env.client_id and settings.env.secret_key and settings.env.redirect_uri):
        raise RuntimeError(
            "This direct-run path (python main.py) requires FYERS_CLIENT_ID, "
            "FYERS_SECRET_KEY, and FYERS_REDIRECT_URI in .env — they're not "
            "required globally anymore (config_loader.py), only here, since "
            "the web UI / session manager path gets broker credentials from "
            "the vault instead. Either fill these into .env, or use "
            "`python run_webapp.py` / `python -m orchestration.session_manager` instead."
        )
    auth = FyersAuth(settings.env)
    if not auth.is_authenticated():
        raise RuntimeError("Not authenticated. Run: python -m auth.auth login")
    adapter = FyersBrokerAdapter(settings.env, paper_mode=settings.env.paper_mode)
    check = adapter.test_connection()
    if not check.ok:
        raise RuntimeError(f"Broker connectivity check failed: {check.detail}")
    logger.info("[CONNECTIVITY_OK] broker=%s user=%s paper_mode=%s",
                adapter.broker_name, check.user_name, settings.env.paper_mode)
    return adapter


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    log_path = setup_logging(level=settings.raw.get("logging", {}).get("level", "INFO"))
    logger.info("Logging to console and to: %s", log_path)

    try:
        _assert_paper_gate(settings)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    try:
        broker = _get_broker_adapter(settings)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    session = TradingSession(settings, broker)
    session.start()

    stop_requested = {"flag": False}

    def _handle_signal(signum, frame):
        logger.info("[SHUTDOWN_SIGNAL] received signal %s", signum)
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Bot running (paper mode, broker=%s). Press Ctrl+C to stop.", broker.broker_name)
    last_forced_reconnect = 0.0
    try:
        while not stop_requested["flag"]:
            time.sleep(1)
            stale = broker.seconds_since_last_message()
            if stale is not None and stale > WS_STALE_WARNING_SECONDS:
                logger.warning("[FEED_STALE] no WS messages received in %.0fs", stale)
            if (
                not broker.is_feed_open
                and stale is not None
                and stale > WS_FORCE_RECONNECT_SECONDS
                and time.time() - last_forced_reconnect > WS_FORCE_RECONNECT_SECONDS
                and hasattr(broker, "force_outer_reconnect")
            ):
                logger.warning("[FEED_STALE] forcing outer reconnect after %.0fs closed.", stale)
                broker.force_outer_reconnect()
                last_forced_reconnect = time.time()
    finally:
        session.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
