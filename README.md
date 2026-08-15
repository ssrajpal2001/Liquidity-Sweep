# liquidity-sweep-bot

Automated Nifty/Sensex index-option trading bot based on Spot liquidity
sweeps (signal on Spot, execute on Options), supporting multiple brokers
(Fyers, AngelOne, more to come) via a plug-and-play adapter interface.
See the full architecture in the accompanying
`upstox_liquidity_sweep_bot_workplan.md` and `workflow_diagram.mermaid`
(filenames predate the broker abstraction; the strategy logic they
describe is unchanged and broker-agnostic).

**Status:** Phases 0-8 are implemented and tested (95 passing tests),
plus a web UI for registration/login/broker credential management/
connect. Phase 5.5 (dashboard) and Phase 9 (fuller backtest fill
simulation) are not yet built, and the web UI's "Connect" doesn't yet
start the actual trading pipeline (see section 4).

**Two ways to run this — pick based on what you're doing:**
- **Web UI** (`python run_webapp.py`) — register an account, add broker
  credentials through a form, connect. No `.env` setup at all. This is
  the multi-client-friendly path. See section 4.
- **Direct** (`python main.py`) — a single hardcoded broker (Fyers)
  configured entirely via `.env`, no web UI involved. See section 5.

## 1. Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

If you're using the **web UI**, skip straight to section 4 — no `.env`
needed.

If you're running `main.py` **directly** (single Fyers broker, no web
UI), create `.env`:

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

## 2. Daily login (only for the direct `main.py` path)

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

## 4. Web UI — register, log in, pick a broker, connect

```bash
python run_webapp.py
```

That's it — **no `.env` setup required.** The two infra secrets it needs
(Flask session key, vault encryption key) auto-generate into
`secrets/webapp_secret.key` / `secrets/webapp_encryption.key` on first
run and are reused forever after. Everything else — accounts, broker
credentials, connection state — lives in `secrets/credentials.db`,
created automatically too.

This prints a URL (`http://<ec2-public-ip>:5000/register`) — open it in
a browser:

1. **Register** a new account (username + password, 8+ characters).
2. **Log in.**
3. Dashboard lists brokers from the registry (`fyers`, `angelone`, more
   as they're added). Pick one, **Add credentials** — the form fields
   are broker-specific, defined once per adapter.
4. Hit **Connect**.

**Two connect flows, depending on the broker:**
- **OAuth redirect** (Fyers, and future Zerodha/Upstox): Connect sends
  your browser to the broker's own login page, which redirects back here
  once you log in there.
- **Direct credentials** (AngelOne): Connect logs in immediately, right
  here, using the client code / PIN / TOTP secret you entered — no
  browser redirect at all. This also means AngelOne can re-authenticate
  itself headlessly if a session goes stale, with no daily manual step.

Either way you land back on the dashboard with a live-verified
connection, polled every 10s.

**Multiple clients:** each registered account only ever sees its own
broker credentials and connection state — verified by a cross-user
isolation test (`tests/test_webapp_flow.py`).

**Credentials never touch `.env`** — they're stored encrypted in
`secrets/credentials.db` (Fernet, key from `secrets/webapp_encryption.key`
— back that file up separately; losing it makes every stored credential
permanently unreadable).

### HTTPS note — read this before trying to connect a broker

Fyers' redirect URI (and most brokers') generally needs to be `https://`,
and the Flask dev server here only speaks plain `http://`. Two ways to
handle this on EC2:
- Put a reverse proxy (nginx/Caddy) with a real TLS cert in front of port
  5000, and register `https://your-domain/brokers/fyers/callback` as the
  redirect URI on your Fyers app, **exactly** matching what you enter in
  the credentials form.
- Or use a tunnel (e.g. `ngrok http 5000`) during testing to get a
  temporary `https://` URL, and register that as the redirect URI instead.

Either way, the `redirect_uri` field you enter in the web form MUST
exactly match what's registered on the broker's app dashboard —
character for character, including trailing slash.

### Scope note

This delivers login, credential management, and a real OAuth
connect/disconnect toggle with a live connectivity check ("Connected" =
confirmed reachable right now, polled every 10s). It does **not** yet
start the tick/strategy/order pipeline (`main.py`'s `TradingSession`)
from a toggle — that wiring (one running session per connected
client+broker) is the natural next step, not something folded in here
without equal care.

## 5. Backtesting — real historical data, real strategy code

```bash
# NIFTY: last 7 days (weekly expiry -> one cycle)
python -m backtest.run_backtest --broker fyers --instrument NIFTY

# BANKNIFTY: full current month so far (monthly expiry -> one cycle)
python -m backtest.run_backtest --broker fyers --instrument BANKNIFTY

# Or an explicit range for either:
python -m backtest.run_backtest --broker fyers --instrument NIFTY --from 2026-08-01 --to 2026-08-14

# Using web-UI-saved credentials instead of .env:
python -m backtest.run_backtest --broker fyers --username ssrajpal2001 --instrument NIFTY

# AngelOne works too (--username required — AngelOne credentials only
# live in the vault, there's no .env fallback path for it):
python -m backtest.run_backtest --broker angelone --username ssrajpal2001 --instrument NIFTY
```

Fetches real 1-minute historical candles from Fyers, resamples them to
the configured LTF (3m) and HTF (75m) timeframes
(`backtest/candle_resampler.py`), and runs them through the exact same
strategy code (`strategy/state_machine.py`) that runs live — not a
second, separately-maintained copy of the rules.

**What this tells you:** how many valid signals the ruleset would have
fired, in which direction, and when. **What it doesn't tell you:** P&L —
option premium isn't simulated, only spot-level signal detection. A
premium-aware fill simulator is a reasonable next addition.

**A real bug this surfaced and fixed:** 75-minute HTF candles were
bucketing to midnight-anchored boundaries (08:45-10:00, 10:00-11:15...)
instead of market-open-anchored ones (09:15-10:30, 10:30-11:45...) — this
affected live trading too, not just backtesting, since
`strategy/rolling_base.py`'s HTF levels come from these same candles.
Fixed in `data_feed/candle_aggregator.py`; verified the fix doesn't
change 3/5/15-minute bucketing at all (mathematically guaranteed, since
09:15 divides evenly into those).

## 6. Verify connectivity and run the trading loop directly (bypassing the web UI)

```bash
python main.py
```

**Broker-agnostic** — `main.py`'s `TradingSession` depends only on the
`BrokerAdapter` interface, never on a concrete broker class. This
particular entrypoint is still wired to Fyers-via-`.env` for the
single-client, no-web-UI path; for any broker connected through the web
UI (Fyers or AngelOne), use section 7 instead.

## 7. Multi-client — run every connected client's broker simultaneously

```bash
python -m orchestration.session_manager
```

Scans every registered web UI account, finds every broker each one has
marked **Connected**, and starts one fully independent trading session
per (client, broker) pair — own broker connection, own isolated state
file (`state/{user}__{broker}_strategy_state.json`), own daily loss
guard. Two clients both trading NIFTY never share state or interfere
with each other.

Re-scans for newly-connected clients every 5 minutes by default
(`--poll-seconds` to change it) — connect a new broker via the web UI
while this is running and it'll pick it up on the next scan without a
restart.

**Scope, stated plainly:** strategy configuration (instruments, capital,
risk %, timeframes) currently comes from one shared
`config/settings.yaml` for every client — true per-client strategy/
capital configuration is a further step, not built yet. What IS isolated
per client today: broker connection, rolling-base/void state, and trade
execution.

## 8. WS-reconnect resync

On a WebSocket disconnect/reconnect, `main.py` and the session manager
both fetch recent 1-minute historical candles via
`BrokerAdapter.get_historical_candles()` and feed them through
`CandleAggregator.bootstrap_instrument()`, which resamples to every
registered timeframe (3m/5m LTF, 75m HTF) and repairs whatever candle
was truncated by the gap. This is a real implementation now, not a
placeholder — verified with tests covering successful resync, empty
broker responses, broker exceptions, and the recent-lookback filter.

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

## 9. Known gaps to watch for

- **Delta may not be available from Fyers' option chain API.** Not a problem anymore —
  `execution/greeks_engine.py` computes Delta locally (Black-Scholes from the
  option's own live LTP) for any strike a broker doesn't supply one for. Watch
  for `[DELTA_COMPUTED_LOCALLY]` in logs — that's expected, not an error.
- **AngelOne — status after live verification (2026-08-14):**
  - WebSocket tick feed: **confirmed working live** (see
    `scripts/test_angelone_feed.py` output) — `token`/`last_traded_price`
    fields are exactly as assumed.
  - Option-chain/expiry/order-token resolution now goes through
    `execution/angelone_instrument_master.py`, which downloads and caches
    AngelOne's public scrip-master file. The JSON schema and the
    "strike is stored ×100" quirk are confirmed against multiple
    independent real samples and unit-tested against those exact
    samples — but the actual live download (network path to
    angelbroking.com) hasn't been exercised from this build environment.
    Run it once against the real file before trusting real strikes.
  - Index spot tokens (`KNOWN_INDEX_SPOT_TOKENS`) are community-reported,
    not independently verified — confirm before relying on them.
- **Tick/message shape for Fyers unverified live**: same caveat as before,
  watch for `[Unrecognized feed shape]`.
- **Expiry response shape unverified live** for Fyers: watch for
  `[No expiryData in option chain response]`.
- **`main.py`'s Fyers-via-.env path is unchanged** for the single-client
  no-web-UI case, but `TradingSession` itself is now fully broker-
  agnostic (verified: the identical class constructs and runs correctly
  against both Fyers and AngelOne adapters). Section 7's session manager
  is the multi-broker/multi-client entrypoint.
- **Per-client strategy config not yet built** — `config/settings.yaml`
  is shared across every client in the session manager. Per-client
  capital/risk/instrument overrides are a natural follow-up.

## 10. Run tests

```bash
pytest tests/ -v
```

36 tests, fully offline (no Fyers API calls) — config validation, token
expiry/storage math, candle aggregation, sweep/displacement/FVG detection,
the rolling-base feedback loop and void-state reset paths, risk math
against the original worked example, daily guard trip conditions, and a
full position-lifecycle integration test (entry → fill → Target1 →
breakeven → trailing stop → exit).

## 11. Repo layout

```
config/          settings.yaml + config_loader.py + logging_setup.py    (done)
auth/            auth.py — Fyers OAuth login/token lifecycle            (done)
brokers/         base.py (BrokerAdapter interface, both auth styles),
                 fyers_adapter.py, angelone_adapter.py, registry.py       (done — 2 brokers live, get_historical_candles on both)
orchestration/   session_manager.py — N clients x N brokers, isolated    (done)
webapp/          app.py, credential_vault.py, templates/ — login,
                 per-broker credential form, OAuth connect toggle        (done, trading loop not yet wired to it)
data_feed/       fyers_rest_client.py, fyers_ws_client.py,
                 candle_aggregator.py                                    (done, tick shape unverified live)
strategy/        rolling_base.py, state_store.py, sweep_detector.py,
                 displacement.py, retest_trigger.py, filters.py,
                 state_machine.py                                        (done, unit-tested, broker-agnostic)
execution/       expiry_resolver.py, option_selector.py, generic_option_selector.py,
                 risk_engine.py, order_manager.py, position_manager.py, greeks_engine.py (done)
risk_controls/   daily_guard.py                                         (done, unit-tested)
backtest/        replay_engine.py, candle_resampler.py, run_backtest.py    (done — replays real strategy code against real historical data)
monitoring/      dashboard.py                                           (Phase 5.5 — not yet built)
state/           local-only strategy state (rolling base, void flags), gitignored
secrets/         local-only token storage + credentials.db, gitignored
tests/           74 unit + integration tests
run_webapp.py    entrypoint for the web UI
```

## 12. Not investment advice

This is a technical build project. Sweep-based option buying carries fast,
full-premium loss risk; strategy quality only shows up after real
paper/live testing across different market regimes.
