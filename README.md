# liquidity-sweep-bot

Automated Nifty/Sensex index-option trading bot based on Spot liquidity
sweeps (signal on Spot, execute on Options), using Upstox as the data
feeder and broker. See the full architecture in the accompanying
`upstox_liquidity_sweep_bot_workplan.md` and `workflow_diagram.mermaid`.

**Status:** Phase 0 (project scaffold) and Phase 1 (config + auth + REST
connectivity) are implemented and tested below. Phases 2-10 are stubbed
out (see the docstring in each file under `data_feed/`, `strategy/`,
`execution/`, `risk_controls/`, `backtest/`, `monitoring/`) and will be
built out incrementally.

## 1. Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create your local secrets file:

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `UPSTOX_API_KEY` / `UPSTOX_API_SECRET` — from your app at
  https://developer.upstox.com
- `UPSTOX_REDIRECT_URI` — must exactly match the redirect URI registered on
  that app
- `UPSTOX_SANDBOX` — `true` while developing, `false` only when you're
  intentionally going live

`.env` and everything under `secrets/` are gitignored — **never commit
real credentials or tokens.**

Non-secret settings (capital, risk %, instruments, time windows, etc.) live
in `config/settings.yaml` and are safe to commit.

## 2. Daily login (required — Upstox tokens are not refreshable)

Upstox does not issue refresh tokens. Every access token expires at the
next 3:30 AM IST after it was generated, so authentication is a **daily
manual step**:

```bash
python -m auth.auth login
```

This prints a login URL, you complete Upstox's own login in a browser, and
paste back the redirected URL (or just the `code` value). The token is
then stored locally at the path in `TOKEN_STORE_PATH` (default
`secrets/token_store.json`, `chmod 600`).

Check whether you currently have a valid token:

```bash
python -m auth.auth check
```

## 3. Verify connectivity

```bash
python main.py
```

This loads config, confirms a valid token exists, connects the live
WebSocket feed, and runs the full pipeline: tick -> candle -> rolling base
-> sweep -> displacement -> FVG -> retest -> filters -> entry -> fill ->
SL/TSL -> target -> exit. **Hard-gated to sandbox**: it refuses to start
unless `settings.yaml` has `app.environment: sandbox` AND `.env` has
`UPSTOX_SANDBOX=true` — both, not just one.

Every meaningful stage logs a distinct `[TAG]` line to `logs/bot.log`, specifically so a session can be reviewed after the fact:

```
[CONNECTIVITY_OK]        [HTF_CANDLE_CLOSE]       [SIGNAL_PASSED_FILTERS]
[STRIKE_SELECTED]        [RISK_PLAN]              [ENTRY_ORDER_PLACED]
[ENTRY_FILLED]           [TARGET1_HIT]            [SL_MOVED_TO_BREAKEVEN]
[TSL_UPDATE]             [SL_HIT] / [TSL_HIT]     [TARGET2_HIT]
[POSITION_CLOSED]        [WS_RECONNECT_RESYNC]    [FEED_STALE]
```

`grep '\[TAG\]' logs/bot.log` to trace any one stage end to end, or paste
the whole file back for review.

## 4. Known gaps to watch for during tomorrow's sandbox run

- **Feed shape**: `data_feed/protobuf_decoder.py` was written from
  documented/sample payloads, not a live message (no network path to
  Upstox from the build environment). If you see `[Unrecognized feed
  shape]` warnings in the log, that's the decoder needing a field-name
  adjustment — share the warning line.
- **REST resync on reconnect** logs a placeholder — actual Historical
  Candle V3 backfill isn't implemented, so a candle spanning a disconnect
  may be based on partial data. Treat any signal immediately after a
  `[WS_RECONNECT_RESYNC]` line with extra scrutiny.
- Option-leg price tracking (SL/TSL/Target after entry) IS wired in as of
  tonight: `[OPTION_LEG_SUBSCRIBED]` confirms the contract's own live
  price joined the feed right after `[ENTRY_FILLED]`.

## 5. Run tests

```bash
pytest tests/ -v
```

Phase 0/1 tests cover config validation and the token expiry/storage logic
and run fully offline (no Upstox API calls).

## 6. Repo layout

```
config/          settings.yaml + config_loader.py + logging_setup.py   (done)
auth/            auth.py — OAuth login/token lifecycle                 (done)
data_feed/       upstox_rest_client.py, upstox_ws_client.py,
                 protobuf_decoder.py, candle_aggregator.py             (done, feed shape unverified live)
strategy/        rolling_base.py, state_store.py, sweep_detector.py,
                 displacement.py, retest_trigger.py, filters.py,
                 state_machine.py                                       (done, unit-tested)
execution/       expiry_resolver.py, option_selector.py, risk_engine.py,
                 order_manager.py, position_manager.py                  (done, option-leg tick wiring pending)
risk_controls/   daily_guard.py                                         (done, unit-tested)
backtest/        replay_engine.py                                       (done — replays real strategy code)
monitoring/      dashboard.py                                           (Phase 5.5 — not yet built)
state/           local-only strategy state (rolling base, void flags), gitignored
secrets/         local-only token storage, gitignored
tests/           32 unit tests across candles, sweep/displacement/FVG,
                 rolling base + void-state, risk math, daily guard
```

## 7. Not investment advice

This is a technical build project. Sweep-based option buying carries fast,
full-premium loss risk; strategy quality only shows up after real
paper/live testing across different market regimes.
