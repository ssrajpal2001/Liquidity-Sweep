"""
scripts/test_angelone_feed.py

Standalone smoke test for AngelOne's live tick feed, run against your
ACTUAL saved credentials (loaded from the same vault the web UI uses).
This exists specifically to verify — for real, against live data — the
one piece flagged as unconfirmed in brokers/angelone_adapter.py: whether
incoming WS messages actually have `token` and `last_traded_price` keys
as assumed, or something else.

Usage:
    python scripts/test_angelone_feed.py <your_username>

What it does:
    1. Loads your AngelOne credentials from secrets/credentials.db
       (the same ones you entered through the web UI).
    2. Logs in (fresh TOTP-based login — safe to run anytime).
    3. Subscribes to one well-known, always-liquid symbol (SBIN-EQ,
       NSE_CM token "3045" — appears repeatedly in AngelOne's own sample
       code, about as safe a smoke-test symbol as exists).
    4. Listens for 20 seconds, printing:
       - every RAW message received (so we can see the true shape)
       - what the current parser extracted from it (or failed to)
    5. Prints a clear pass/fail summary at the end.

If parsing fails, paste the RAW MESSAGE lines back — that's exactly
what's needed to fix brokers/angelone_adapter.py's _handle_data().
"""
from __future__ import annotations

import sys
import time

from webapp.credential_vault import CredentialVault
from webapp.secrets_bootstrap import get_or_create_encryption_key
from webapp.app import _build_env_like
from webapp.app import TOKEN_STORE_DIR
from brokers.angelone_adapter import AngelOneBrokerAdapter

TEST_SYMBOL = "1:3045"  # exchangeType 1 (NSE_CM) : token 3045 (SBIN-EQ)
LISTEN_SECONDS = 20


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_angelone_feed.py <your_username>")
        return 1
    username = sys.argv[1]

    print(f"Loading AngelOne credentials for user '{username}'...")
    vault = CredentialVault(get_or_create_encryption_key())
    fields = vault.get_credentials(username, "angelone")
    if fields is None:
        print(f"No AngelOne credentials saved for '{username}'. Add them via the web UI first.")
        return 1

    token_path = TOKEN_STORE_DIR / f"{username}__angelone_token_store.json"
    env_like = _build_env_like(fields, token_path)
    adapter = AngelOneBrokerAdapter(env_like, paper_mode=True)

    print("Logging in (fresh TOTP)...")
    check = adapter.login()
    if not check.ok:
        print(f"LOGIN FAILED: {check.detail}")
        return 1
    print(f"Login OK — user_name={check.user_name}")

    raw_messages: list = []
    parsed_ticks: list = []

    # Monkeypatch _handle_data to also capture the raw message, in
    # addition to running the real parser — this is deliberately reaching
    # into the adapter's internals FOR THIS SMOKE TEST ONLY, to see both
    # "what arrived" and "what the parser did with it" side by side.
    original_handle_data = adapter._handle_data

    def capturing_handle_data(wsapp, message):
        raw_messages.append(message)
        print(f"\n[RAW MESSAGE #{len(raw_messages)}] {message}")
        before = len(parsed_ticks)
        adapter._on_tick = lambda token, ltp: parsed_ticks.append((token, ltp))
        original_handle_data(wsapp, message)
        if len(parsed_ticks) > before:
            print(f"  -> PARSED OK: token={parsed_ticks[-1][0]} ltp={parsed_ticks[-1][1]}")
        else:
            print("  -> PARSE FAILED (see [Unrecognized AngelOne tick shape] warning above)")

    adapter._handle_data = capturing_handle_data

    print(f"\nStarting feed for {TEST_SYMBOL} (SBIN-EQ), listening {LISTEN_SECONDS}s...\n")
    adapter.start_feed([TEST_SYMBOL], on_tick=lambda t, p: None)

    time.sleep(LISTEN_SECONDS)
    adapter.stop_feed()

    print("\n" + "=" * 60)
    print(f"SUMMARY: {len(raw_messages)} raw message(s) received, "
          f"{len(parsed_ticks)} parsed successfully.")
    if len(raw_messages) == 0:
        print("No messages arrived at all — check: market hours (this only "
              "works 9:15-15:30 IST on a trading day), EC2 outbound network "
              "to AngelOne's WS endpoint, and that the WS actually opened "
              "(look for 'AngelOne WS connected' above).")
        return 1
    if len(parsed_ticks) == 0:
        print("Messages arrived but NONE parsed — paste the [RAW MESSAGE] "
              "lines above back to fix the parser in brokers/angelone_adapter.py.")
        return 1
    if len(parsed_ticks) < len(raw_messages):
        print("PARTIAL: some messages parsed, some didn't — paste the "
              "failing [RAW MESSAGE] lines back for a closer look.")
        return 0
    print("ALL MESSAGES PARSED CORRECTLY. Tick feed confirmed working live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
