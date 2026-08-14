# liquidity-sweep-bot

Automated Nifty/Sensex index-option trading bot based on Spot liquidity
sweeps (signal on Spot, execute on Options), using **Fyers** as the data
feeder and broker. See the full architecture in the accompanying
`upstox_liquidity_sweep_bot_workplan.md` and `workflow_diagram.mermaid`
(filenames predate the Fyers switch; the logic they describe is unchanged
and broker-agnostic — only the data feeder/broker layer changed).

**Status:** Phases 0-8 are implemented and tested (36 passing tests).
Phase 5.5 (dashboard) and Phase 9 (fuller backtest fill simulation) are
not yet built.

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
- `FYERS_CLIENT_ID` — the FULL app id from https://myapi.fyers.in/dashboard,
  including the `-100` suffix (e.g. `XC1234-100`)
- `FYERS_SECRET_KEY` — from the same dashboard
- `FYERS_REDIRECT_URI` — must exactly match the redirect URI registered on
  that app, character-for-character including trailing slash
- `PAPER_MODE` — `true` while testing, `false` only when intentionally
  going live (see "Paper mode vs live" below — this is NOT a broker-side
  sandbox)

`.env` and everything under `secrets/` are gitignored — **never commit
real credentials or tokens.**

Non-secret settings (capital, risk %, instruments, time windows, etc.) live
in `config/settings.yaml` and are safe to commit.

## 2. Daily login

Fyers access tokens are valid for about a day, and this project checks
validity live (via a `get_profile()` call) rather than trusting a fixed
clock time, since Fyers' exact expiry cutoff isn't consistently
documented. Log in each trading day:

```bash
python -m auth.auth login
```

This prints a login URL, you complete Fyers' own login in a browser, and
paste back the redirected URL (it'll contain `?auth_code=...`). The token
is stored locally at `TOKEN_STORE_PATH` (default `secrets/token_store.json`,
`chmod 600`).

Check whether you currently have a usable token:

```bash
python -m auth.auth check
```

(This only checks the local soft-expiry backstop, not whether Fyers still
accepts it — `python main.py`'s connectivity check is the real test.)

## 3. Paper mode vs live — read this before running

**Fyers has no confirmed broker-side sandbox** the way some other
brokers do (checked — community threads ask for one without a clear
answer). So "paper mode" here is **simulated locally by this bot**, not
by Fyers:

- Market data (spot ticks, option chain, quotes) is always REAL and live.
- When `PAPER_MODE=true` (and `settings.yaml` has `app.environment: paper`),
  `execution/order_manager.py` never calls Fyers' real order endpoint —
  it logs `[PAPER_ORDER_SIMULATED]` and simulates an immediate fill at
  the requested price, using the exact same code path live mode will use.
- `main.py` **refuses to start** unless BOTH flags say paper — a mismatch
  between `settings.yaml` and `.env` stops the bot rather than guessing
  which one you meant.

## 4. Verify connectivity and run

```bash
python main.py
```

Loads config, confirms the token is live-accepted, connects the
WebSocket feed, and runs the full pipeline: tick → candle → rolling base
→ sweep → displacement → FVG → retest → filters → entry → fill →
SL/TSL → target → exit.

Every meaningful stage logs a distinct `[TAG]` to `logs/bot.log`:

```
[CONNECTIVITY_OK]        [HTF_CANDLE_CLOSE]       [SIGNAL_PASSED_FILTERS]
[STRIKE_SELECTED]        [RISK_PLAN]              [PAPER_ORDER_SIMULATED] / [ENTRY_ORDER_PLACED]
[ENTRY_FILLED]           [OPTION_LEG_SUBSCRIBED]  [TARGET1_HIT]
[SL_MOVED_TO_BREAKEVEN]  [TSL_UPDATE]             [SL_HIT] / [TSL_HIT] / [TARGET2_HIT]
[POSITION_CLOSED]        [WS_RECONNECT_RESYNC]    [FEED_STALE]
```

`grep '\[TAG\]' logs/bot.log` to trace any one stage end to end, or paste
the whole file back for review.

## 5. Known gaps to watch for

- **Delta may not be available from Fyers' option chain API.** Multiple
  community threads (2024-2026) ask whether Fyers' `optionchain()` reliably
  returns Greeks at all. `execution/option_selector.py` tries real delta
  first; if none comes back it falls back to nearest-strike-to-spot with
  an assumed delta and logs `[DELTA_UNAVAILABLE_FALLBACK]`. If you see
  that tag, the risk math (Spot Risk × Delta = Premium SL) is running on
  an assumed delta, not a live one — worth checking your Fyers data plan
  includes Greeks.
- **Tick/message shape unverified live**: `data_feed/fyers_ws_client.py`'s
  field expectations (`symbol`, `ltp`) come from real community sample
  code, which is stronger than pure docs, but still hasn't been confirmed
  against a live connection from this build environment (no network path
  to Fyers here). Watch for `[Unrecognized tick shape]` warnings.
- **Expiry response shape unverified live**: same caveat for
  `execution/expiry_resolver.py`'s `expiryData` parsing — watch for
  `[No expiryData in option chain response]`.
- **REST resync on reconnect** logs a placeholder — historical-candle
  backfill on WS reconnect isn't implemented yet, so a candle spanning a
  disconnect may be based on partial data.

## 6. Run tests

```bash
pytest tests/ -v
```

36 tests, fully offline (no Fyers API calls) — config validation, token
expiry/storage math, candle aggregation, sweep/displacement/FVG detection,
the rolling-base feedback loop and void-state reset paths, risk math
against the original worked example, daily guard trip conditions, and a
full position-lifecycle integration test (entry → fill → Target1 →
breakeven → trailing stop → exit).

## 7. Repo layout

```
config/          settings.yaml + config_loader.py + logging_setup.py    (done)
auth/            auth.py — Fyers OAuth login/token lifecycle            (done)
data_feed/       fyers_rest_client.py, fyers_ws_client.py,
                 candle_aggregator.py                                    (done, tick shape unverified live)
strategy/        rolling_base.py, state_store.py, sweep_detector.py,
                 displacement.py, retest_trigger.py, filters.py,
                 state_machine.py                                        (done, unit-tested, broker-agnostic)
execution/       expiry_resolver.py, option_selector.py, risk_engine.py,
                 order_manager.py, position_manager.py                   (done, delta availability unverified live)
risk_controls/   daily_guard.py                                         (done, unit-tested)
backtest/        replay_engine.py                                       (done — replays real strategy code)
monitoring/      dashboard.py                                           (Phase 5.5 — not yet built)
state/           local-only strategy state (rolling base, void flags), gitignored
secrets/         local-only token storage, gitignored
tests/           36 unit + integration tests
```

## 8. Not investment advice

This is a technical build project. Sweep-based option buying carries fast,
full-premium loss risk; strategy quality only shows up after real
paper/live testing across different market regimes.
