"""
main.py — orchestrator entrypoint.

Wires together every phase: config -> auth -> REST client -> WS feed ->
candle aggregator -> per-instrument state machine -> daily guard ->
expiry resolver -> option selector -> risk engine -> order manager ->
position manager (entry/SL/TSL/target1/target2/exit).

HARD SAFETY GATE: refuses to start unless settings.yaml's
app.environment is "sandbox" AND .env's UPSTOX_SANDBOX is true. This is
deliberate — going live is a separate, explicit decision after Phase 10's
paper-trading checklist, not something that should happen because one of
two flags got out of sync.

Every meaningful stage logs a distinct [TAG] (see execution/
position_manager.py's docstring for the full list) specifically so
tomorrow's sandbox run produces a log you can paste back to me and I can
confirm exactly which stages fired: tick -> candle -> signal -> sweep ->
displacement -> FVG -> retest -> filters -> entry -> fill -> SL/TSL ->
target -> exit.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import timedelta, timezone

from auth.auth import UpstoxAuth
from config.config_loader import ConfigError, Settings, load_settings
from config.logging_setup import setup_logging
from data_feed.candle_aggregator import Candle, CandleAggregator
from data_feed.upstox_rest_client import UpstoxRestClient
from data_feed.upstox_ws_client import UpstoxWSClient
from execution.expiry_resolver import ExpiryResolver
from execution.option_selector import OptionSelector
from execution.order_manager import OrderManager
from execution.position_manager import Position, PositionManager
from execution.risk_engine import compute_risk_plan
from risk_controls.daily_guard import DailyGuard
from strategy.state_machine import InstrumentStateMachine, SignalDecision
from strategy.state_store import StateStore

logger = logging.getLogger("main")

IST = timezone(timedelta(hours=5, minutes=30))


def _assert_sandbox_gate(settings: Settings) -> None:
    yaml_env = settings.raw["app"]["environment"]
    env_sandbox = settings.env.sandbox
    if yaml_env != "sandbox" or not env_sandbox:
        raise RuntimeError(
            "Refusing to start: paper trading (Phase 10) hasn't been "
            "completed yet. settings.yaml app.environment must be "
            f"'sandbox' (currently '{yaml_env}') AND .env UPSTOX_SANDBOX "
            f"must be true (currently {env_sandbox}). Going live is a "
            "deliberate step — see README 'Going live'."
        )


class TradingSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth = UpstoxAuth(settings.env)
        self.rest_client = UpstoxRestClient(settings.env, auth=self.auth)
        self.store = StateStore()

        self.daily_guard = DailyGuard(
            total_capital_inr=settings.capital["total_capital_inr"],
            max_daily_loss_pct=settings.capital["max_daily_loss_pct"],
            max_trades_per_day=settings.capital["max_trades_per_day"],
        )

        self.expiry_resolver = ExpiryResolver(self.rest_client.api_client)
        self.order_manager = OrderManager(
            self.rest_client.api_client, settings.execution["limit_offset_rupees"]
        )
        self.position_manager = PositionManager(
            order_manager=self.order_manager,
            get_order_status_fn=self.order_manager.get_order_status,
            trail_distance_points=settings.risk.get("trail_distance_points", 5.0),
            target1_booking_pct=settings.execution["target1_booking_pct"],
            on_closed=self._on_position_closed,
        )

        self.instruments = settings.instruments
        htf = settings.raw["timeframes"]["htf_minutes"]
        self.primary_ltf = settings.raw["timeframes"]["ltf_minutes"][0]

        self.candle_agg = CandleAggregator(on_close=self._on_candle_close)
        self.state_machines: dict[str, InstrumentStateMachine] = {}
        self.option_selectors: dict[str, OptionSelector] = {}
        self.open_positions: dict[str, Position] = {}  # instrument_key -> Position (spot key, not option key)
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
            self.option_selectors[key] = OptionSelector(
                self.rest_client.api_client,
                underlying_instrument_key=key,
                strike_interval=inst["strike_interval"],
                target_delta_min=settings.raw["option_selection"]["target_delta_min"],
                target_delta_max=settings.raw["option_selection"]["target_delta_max"],
                cache_refresh_seconds=settings.raw["option_selection"]["delta_cache_refresh_seconds"],
            )

        self.ws_client: UpstoxWSClient | None = None
        self._latest_spot: dict[str, float] = {}
        self.positions_by_option_key: dict[str, Position] = {}

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

        expiry = self.expiry_resolver.nearest_weekly_expiry(instrument_key)
        if expiry is None:
            logger.error("[SIGNAL_SKIPPED] %s no resolvable expiry.", instrument_key)
            return

        spot_price = self._latest_spot.get(instrument_key)
        if spot_price is None:
            logger.error("[SIGNAL_SKIPPED] %s no spot price available yet.", instrument_key)
            return

        selected = self.option_selectors[instrument_key].select(expiry, spot_price, option_type)
        if selected is None:
            logger.error("[SIGNAL_SKIPPED] %s no strike found for delta target.", instrument_key)
            return
        logger.info(
            "[STRIKE_SELECTED] %s %s strike=%.0f instrument=%s delta=%.2f ask=%.2f",
            instrument_key, option_type, selected.strike_price,
            selected.instrument_key, selected.delta, selected.ask_price,
        )

        plan = compute_risk_plan(
            entry_premium=selected.ask_price,
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
            selected.instrument_key, decision.direction, plan, selected.ask_price,
            tag=f"{instrument_key}-{decision.direction}",
        )
        if position is not None:
            self.open_positions[instrument_key] = position
            self.positions_by_option_key[selected.instrument_key] = position
            subscribed = self.ws_client.subscribe([selected.instrument_key], mode="ltpc")
            logger.info(
                "[OPTION_LEG_SUBSCRIBED] %s subscribed=%s — SL/TSL/Target now tracking this contract's live price.",
                selected.instrument_key, subscribed,
            )

    def _on_position_closed(self, position: Position) -> None:
        self.daily_guard.record_trade_closed(position.realized_pnl_inr)
        for spot_key, pos in list(self.open_positions.items()):
            if pos is position:
                del self.open_positions[spot_key]
        self.positions_by_option_key.pop(position.instrument_key, None)
        if self.ws_client is not None:
            self.ws_client.unsubscribe([position.instrument_key])

    # -- reconnect resync ------------------------------------------------------
    def _rest_resync_on_reconnect(self) -> None:
        logger.info(
            "[WS_RECONNECT_RESYNC] Historical Candle V3 backfill not yet implemented — "
            "candles closed during the disconnect gap may be incomplete. "
            "Flagging here so any signal right after a reconnect gets extra scrutiny."
        )

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        instrument_keys = [i["spot_key"] for i in self.instruments]

        def on_tick(tick):
            # Route by whether this tick belongs to a spot index (feeds the
            # candle/signal pipeline) or an open position's option contract
            # (feeds SL/TSL/Target checks) — a single WS connection carries
            # both once a position's leg has been dynamically subscribed.
            position = self.positions_by_option_key.get(tick.instrument_key)
            if position is not None:
                self.position_manager.on_price_update(position, tick.ltp)
                return

            self.candle_agg.ingest_tick(tick.instrument_key, tick.ltp, tick.ltt_epoch_ms)
            self._latest_spot[tick.instrument_key] = tick.ltp

        self.ws_client = UpstoxWSClient(
            api_client=self.rest_client.api_client,
            instrument_keys=instrument_keys,
            on_tick=on_tick,
            on_reconnect=self._rest_resync_on_reconnect,
            mode="full",
        )
        self.ws_client.start()
        logger.info("[SESSION_STARTED] instruments=%s", instrument_keys)

    def stop(self) -> None:
        if self.ws_client is not None:
            self.ws_client.stop()
        logger.info("[SESSION_STOPPED]")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    log_path = setup_logging(level=settings.raw.get("logging", {}).get("level", "INFO"))
    logger.info("Logging to console and to: %s", log_path)

    try:
        _assert_sandbox_gate(settings)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    auth = UpstoxAuth(settings.env)
    if not auth.is_authenticated():
        logger.error("Not authenticated. Run: python -m auth.auth login")
        return 1

    rest_client = UpstoxRestClient(settings.env, auth=auth)
    check = rest_client.test_connection()
    if not check.ok:
        logger.error("Upstox connectivity check failed: %s", check.detail)
        return 1
    logger.info(
        "[CONNECTIVITY_OK] user=%s sandbox=%s", check.user_name, settings.env.sandbox
    )

    session = TradingSession(settings)
    session.start()

    stop_requested = {"flag": False}

    def _handle_signal(signum, frame):
        logger.info("[SHUTDOWN_SIGNAL] received signal %s", signum)
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Bot running (sandbox). Press Ctrl+C to stop.")
    try:
        while not stop_requested["flag"]:
            time.sleep(1)
            stale = session.ws_client.seconds_since_last_message() if session.ws_client else None
            if stale is not None and stale > 60:
                logger.warning("[FEED_STALE] no WS messages received in %.0fs", stale)
    finally:
        session.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
