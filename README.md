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

## 4. Web UI — login, broker credentials, connect toggle

```bash
# One-time setup:
python -c "import secrets; print(secrets.token_hex(32))"
# -> paste into .env as WEBAPP_SECRET_KEY

python -c "from webapp.credential_vault import generate_encryption_key; print(generate_encryption_key())"
# -> paste into .env as WEBAPP_ENCRYPTION_KEY (back this up separately — losing it
#    makes every stored broker credential permanently unreadable)

python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password-here'))"
# -> paste into .env as WEBAPP_ADMIN_PASSWORD_HASH
# also set WEBAPP_ADMIN_USER in .env to whatever username you want

# Run it:
python run_webapp.py
```

This prints a URL (`http://<ec2-public-ip>:5000/login`) — open it in a
browser. Log in, pick a broker from the list, enter that broker's
credentials (client_id/secret_key/redirect_uri for Fyers — the form
fields are broker-specific, defined once per adapter), and hit
**Connect**. That runs the real broker login in your browser and comes
back here with a live-verified connection.

**Credentials never touch `.env`** — they're stored encrypted in
`secrets/credentials.db` (Fernet, key from `WEBAPP_ENCRYPTION_KEY`).

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

## 5. Verify connectivity and run the trading loop directly (bypassing the web UI)

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

## 6. Known gaps to watch for

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

## 7. Run tests

```bash
pytest tests/ -v
```

36 tests, fully offline (no Fyers API calls) — config validation, token
expiry/storage math, candle aggregation, sweep/displacement/FVG detection,
the rolling-base feedback loop and void-state reset paths, risk math
against the original worked example, daily guard trip conditions, and a
full position-lifecycle integration test (entry → fill → Target1 →
breakeven → trailing stop → exit).

## 8. Repo layout

```
config/          settings.yaml + config_loader.py + logging_setup.py    (done)
auth/            auth.py — Fyers OAuth login/token lifecycle            (done)
brokers/         base.py (BrokerAdapter interface), fyers_adapter.py,
                 registry.py — the plug-and-play broker layer            (done)
webapp/          app.py, credential_vault.py, templates/ — login,
                 per-broker credential form, OAuth connect toggle        (done, trading loop not yet wired to it)
data_feed/       fyers_rest_client.py, fyers_ws_client.py,
                 candle_aggregator.py                                    (done, tick shape unverified live)
strategy/        rolling_base.py, state_store.py, sweep_detector.py,
                 displacement.py, retest_trigger.py, filters.py,
                 state_machine.py                                        (done, unit-tested, broker-agnostic)
execution/       expiry_resolver.py, option_selector.py, risk_engine.py,
                 order_manager.py, position_manager.py, greeks_engine.py (done — Delta computed locally, broker-independent)
risk_controls/   daily_guard.py                                         (done, unit-tested)
backtest/        replay_engine.py                                       (done — replays real strategy code)
monitoring/      dashboard.py                                           (Phase 5.5 — not yet built)
state/           local-only strategy state (rolling base, void flags), gitignored
secrets/         local-only token storage + credentials.db, gitignored
tests/           74 unit + integration tests
run_webapp.py    entrypoint for the web UI
```

## 9. Not investment advice

This is a technical build project. Sweep-based option buying carries fast,
full-premium loss risk; strategy quality only shows up after real
paper/live testing across different market regimes.
