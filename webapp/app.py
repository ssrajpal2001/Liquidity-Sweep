"""
webapp/app.py

The web UI: run a command, open a URL, register a new client account,
log in, pick a broker from a list, enter that broker's credentials
(stored encrypted in the vault, never in .env), and flip a connect
toggle that either runs the real OAuth flow (Fyers) or logs in directly
(AngelOne's TOTP-based login needs no browser round trip).

Zero required .env setup: the two infra secrets this needs (Flask
session key, vault encryption key) auto-generate into local files on
first run — see webapp/secrets_bootstrap.py. Everything else — user
accounts, broker credentials, connection state — lives in
secrets/credentials.db.

Scope note, stated plainly: this delivers registration + login +
credential management + connect/disconnect (OAuth or direct) + a live
connectivity check ("see what's happening" = confirmed authenticated +
reachable). It does NOT yet start the actual tick/strategy/order
pipeline (main.py's TradingSession) from this UI — wiring "toggle on" to
"now trading" is the natural next step, not something to bolt on without
equal care.
"""
from __future__ import annotations

import functools
import logging
import secrets
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from brokers.base import AuthType, ConnectionCheckResult
from brokers.registry import available_brokers, get_adapter_class
from webapp import credential_vault as credential_vault_module
from webapp.credential_vault import CredentialVault
from webapp.secrets_bootstrap import get_or_create_encryption_key, get_or_create_secret_key
from webapp.user_store import UserStore, UserStoreError

logger = logging.getLogger(__name__)

TOKEN_STORE_DIR = Path("secrets/tokens")


def _build_env_like(fields: dict[str, str], token_store_path: Path) -> SimpleNamespace:
    """FyersAuth/AngelOneBrokerAdapter/etc. only ever access attributes by
    name (client_id, secret_key, redirect_uri for Fyers; api_key,
    client_code, pin, totp_secret for AngelOne; whatever the next broker
    needs) — they don't require a real config.config_loader.EnvConfig
    instance. Building the namespace generically from whatever fields the
    vault has for THIS broker (rather than hardcoding one broker's field
    names) is what makes adding a broker not require touching this
    function — a bug caught during testing when AngelOne's fields
    (different from Fyers') hit a namespace that only had Fyers' three
    attributes."""
    return SimpleNamespace(
        **fields,
        paper_mode=True,  # the web UI only ever toggles broker CONNECTION, not live trading
        auth_code=None,
        token_store_path=token_store_path,
    )


def _adapter_for(user_id: str, broker_name: str, vault: CredentialVault):
    fields = vault.get_credentials(user_id, broker_name)
    if fields is None:
        return None
    adapter_class = get_adapter_class(broker_name)
    token_path = TOKEN_STORE_DIR / f"{user_id}__{broker_name}_token_store.json"
    env_like = _build_env_like(fields, token_path)
    return adapter_class(env_like, paper_mode=True)


def create_app() -> Flask:
    app = Flask(__name__)
    # Zero required .env setup: both infra secrets auto-generate into
    # local files on first run and are reused forever after — see
    # webapp/secrets_bootstrap.py's docstring for why these two
    # specifically can't live in the database itself.
    app.secret_key = get_or_create_secret_key()

    encryption_key = get_or_create_encryption_key()
    vault = CredentialVault(encryption_key, db_path=credential_vault_module.DB_PATH)
    users = UserStore()

    # The trading engine runs INSIDE this process now — one process-wide
    # SessionManager, started here so Start/Stop routes and status polling
    # all talk to the same in-memory running sessions. This is what makes
    # "merge web app + trading engine" concrete rather than aspirational:
    # a session started via a button click is the exact same TradingSession
    # object a status route reads from, no IPC/shared-file layer needed.
    from orchestration.session_manager import SessionManager
    session_manager = SessionManager()

    def login_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    # -- auth: real per-client registration + login ---------------------------
    @app.route("/register", methods=["GET", "POST"])
    def register():
        error = None
        if request.method == "POST":
            try:
                users.register(
                    request.form.get("username", ""),
                    request.form.get("password", ""),
                    request.form.get("confirm_password", ""),
                )
            except UserStoreError as exc:
                error = str(exc)
            else:
                session["user_id"] = request.form.get("username", "").strip()
                return redirect(url_for("dashboard"))
        return render_template("register.html", error=error)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if users.verify(username, password):
                session["user_id"] = username.strip()
                return redirect(url_for("dashboard"))
            error = "Invalid username or password."
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # -- dashboard -----------------------------------------------------------
    @app.route("/")
    @login_required
    def dashboard():
        broker_names = available_brokers()
        status = vault.list_broker_status(session["user_id"], broker_names)
        return render_template("dashboard.html", brokers=broker_names, status=status)

    # -- credentials ---------------------------------------------------------
    @app.route("/brokers/<broker_name>/credentials", methods=["GET", "POST"])
    @login_required
    def broker_credentials(broker_name: str):
        try:
            adapter_class = get_adapter_class(broker_name)
        except ValueError as exc:
            return render_template("error.html", message=str(exc)), 404

        fields_spec = adapter_class.required_credential_fields()

        if request.method == "POST":
            existing_now = vault.get_credentials(session["user_id"], broker_name) or {}
            values = {}
            for name, _, is_secret in fields_spec:
                submitted = request.form.get(name, "")
                if is_secret and not submitted and name in existing_now:
                    values[name] = existing_now[name]  # blank secret field = keep existing value
                else:
                    values[name] = submitted
            vault.save_credentials(session["user_id"], broker_name, values)
            return redirect(url_for("dashboard"))

        existing = vault.get_credentials(session["user_id"], broker_name) or {}
        return render_template(
            "credentials_form.html", broker_name=broker_name,
            fields_spec=fields_spec, existing=existing,
        )

    # -- connect (branches on auth_type) ---------------------------------------
    @app.route("/brokers/<broker_name>/connect")
    @login_required
    def broker_connect(broker_name: str):
        adapter = _adapter_for(session["user_id"], broker_name, vault)
        if adapter is None:
            return redirect(url_for("broker_credentials", broker_name=broker_name))

        if adapter.auth_type == AuthType.DIRECT_CREDENTIALS:
            # No browser round trip needed — log in right here with the
            # already-stored credentials and show the result immediately.
            check = adapter.login()
            vault.set_connected(session["user_id"], broker_name, check.ok)
            logger.info(
                "[BROKER_CONNECT_%s] user=%s broker=%s (direct login) user_name=%s",
                "OK" if check.ok else "FAILED", session["user_id"], broker_name, check.user_name,
            )
            if not check.ok:
                return render_template("error.html", message=f"Login failed: {check.detail}"), 400
            return redirect(url_for("dashboard"))

        state = secrets.token_urlsafe(16)
        session[f"oauth_state_{broker_name}"] = state
        login_url = adapter.build_login_url(state=state)
        logger.info("[BROKER_CONNECT_INITIATED] user=%s broker=%s", session["user_id"], broker_name)
        return redirect(login_url)

    @app.route("/brokers/<broker_name>/callback")
    @login_required
    def broker_callback(broker_name: str):
        adapter = _adapter_for(session["user_id"], broker_name, vault)
        if adapter is None:
            return render_template("error.html", message="No credentials saved for this broker."), 400

        try:
            adapter.exchange_code(request.url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[BROKER_CONNECT_FAILED] user=%s broker=%s", session["user_id"], broker_name)
            return render_template("error.html", message=f"Connection failed: {exc}"), 400

        check: ConnectionCheckResult = adapter.test_connection()
        vault.set_connected(session["user_id"], broker_name, check.ok)
        logger.info(
            "[BROKER_CONNECT_%s] user=%s broker=%s user_name=%s",
            "OK" if check.ok else "FAILED", session["user_id"], broker_name, check.user_name,
        )
        return redirect(url_for("dashboard"))

    @app.route("/brokers/<broker_name>/disconnect")
    @login_required
    def broker_disconnect(broker_name: str):
        # Stop trading first if it's running — disconnecting the broker
        # out from under a live session would leave it holding a dead
        # connection rather than failing cleanly.
        if session_manager.get_status(session["user_id"], broker_name).get("running"):
            session_manager.stop_one(session["user_id"], broker_name)
        vault.set_connected(session["user_id"], broker_name, False)
        token_path = TOKEN_STORE_DIR / f"{session['user_id']}__{broker_name}_token_store.json"
        if token_path.exists():
            token_path.unlink()
        logger.info("[BROKER_DISCONNECTED] user=%s broker=%s", session["user_id"], broker_name)
        return redirect(url_for("dashboard"))

    # -- live status check (what "see what's happening" shows today) -----------
    @app.route("/brokers/<broker_name>/status")
    @login_required
    def broker_status(broker_name: str):
        adapter = _adapter_for(session["user_id"], broker_name, vault)
        if adapter is None or not adapter.is_authenticated():
            return jsonify({"connected": False, "detail": "Not connected."})
        check = adapter.test_connection()
        return jsonify({
            "connected": check.ok, "detail": check.detail,
            "user_name": check.user_name, "user_id": check.user_id,
        })

    # -- trading engine control (merged into this process) ---------------------
    @app.route("/brokers/<broker_name>/start_trading", methods=["POST"])
    @login_required
    def start_trading(broker_name: str):
        status = vault.list_broker_status(session["user_id"], [broker_name])
        if not status[broker_name]["connected"]:
            return redirect(url_for("broker_credentials", broker_name=broker_name))
        ok, message = session_manager.start_one(session["user_id"], broker_name)
        logger.info("[UI_START_TRADING] user=%s broker=%s ok=%s: %s",
                    session["user_id"], broker_name, ok, message)
        return redirect(url_for("dashboard"))

    @app.route("/brokers/<broker_name>/stop_trading", methods=["POST"])
    @login_required
    def stop_trading(broker_name: str):
        ok, message = session_manager.stop_one(session["user_id"], broker_name)
        logger.info("[UI_STOP_TRADING] user=%s broker=%s ok=%s: %s",
                    session["user_id"], broker_name, ok, message)
        return redirect(url_for("dashboard"))

    @app.route("/brokers/<broker_name>/trading_status")
    @login_required
    def trading_status(broker_name: str):
        return jsonify(session_manager.get_status(session["user_id"], broker_name))

    # -- in-browser log viewer -------------------------------------------------
    @app.route("/logs")
    @login_required
    def logs_page():
        return render_template("logs.html")

    @app.route("/logs/tail")
    @login_required
    def logs_tail():
        from config.logging_setup import DEFAULT_LOG_DIR

        log_path = DEFAULT_LOG_DIR / "bot.log"
        if not log_path.exists():
            return jsonify({"lines": ["(no log file yet — logs appear once the trading engine has started)"]})
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return jsonify({"lines": [f"(error reading log file: {exc})"]})
        return jsonify({"lines": lines[-200:]})  # last 200 lines — enough context without shipping the whole file every poll

    return app
