"""
webapp/app.py

The web UI you described: run a command, open a URL, log in, pick a
broker from a list, enter that broker's credentials (stored encrypted in
the vault, never in .env), and flip a connect toggle that runs the real
OAuth flow — Fyers' own login page, redirect back to us, token stored.

Scope note, stated plainly: this delivers login + credential management +
real OAuth connect/disconnect + a live connectivity check ("see what's
happening" = confirmed authenticated + reachable). It does NOT yet start
the actual tick/strategy/order pipeline (main.py's TradingSession) from
this UI — wiring "toggle on" to "now trading" is the natural next step,
not something to bolt on without equal care.

Auth model: one shared admin login for now (WEBAPP_ADMIN_USER /
WEBAPP_ADMIN_PASSWORD_HASH in .env), with all data already isolated by
user_id in the vault. Swapping in real per-client accounts later is a
users table + registration flow — this UI's structure doesn't need to
change for that, just the login route.
"""
from __future__ import annotations

import functools
import logging
import os
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from flask import Flask, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from brokers.base import ConnectionCheckResult
from brokers.registry import available_brokers, get_adapter_class
from webapp import credential_vault as credential_vault_module
from webapp.credential_vault import CredentialVault, VaultError

logger = logging.getLogger(__name__)

TOKEN_STORE_DIR = Path("secrets/tokens")


def _build_env_like(fields: dict[str, str], token_store_path: Path) -> SimpleNamespace:
    """FyersAuth/FyersRestClient only ever access attributes (client_id,
    secret_key, redirect_uri, paper_mode, auth_code, token_store_path) —
    they don't require a real config.config_loader.EnvConfig instance, so
    a plain namespace built from vault-stored fields works without
    touching auth.py at all."""
    return SimpleNamespace(
        client_id=fields.get("client_id"),
        secret_key=fields.get("secret_key"),
        redirect_uri=fields.get("redirect_uri"),
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
    app.secret_key = os.environ.get("WEBAPP_SECRET_KEY") or secrets.token_hex(32)
    if not os.environ.get("WEBAPP_SECRET_KEY"):
        logger.warning(
            "WEBAPP_SECRET_KEY not set — using a random key for this process only. "
            "Sessions will not survive a restart. Set WEBAPP_SECRET_KEY in .env for production use."
        )

    encryption_key = os.environ.get("WEBAPP_ENCRYPTION_KEY", "")
    # Read db_path via the module attribute, not a bound default, so tests
    # (or any caller) reassigning credential_vault_module.DB_PATH before
    # create_app() actually take effect — see credential_vault.py's
    # __init__ docstring for why the naive default-parameter approach
    # silently doesn't work here.
    vault = CredentialVault(encryption_key, db_path=credential_vault_module.DB_PATH) if encryption_key else None

    admin_user = os.environ.get("WEBAPP_ADMIN_USER")
    admin_password_hash = os.environ.get("WEBAPP_ADMIN_PASSWORD_HASH")

    def login_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    def _vault_or_error():
        if vault is None:
            return None, render_template(
                "error.html",
                message="WEBAPP_ENCRYPTION_KEY is not set in .env. Generate one with:\n"
                        'python -c "from webapp.credential_vault import generate_encryption_key; '
                        'print(generate_encryption_key())"',
            )
        return vault, None

    # -- auth --------------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if (
                admin_user and admin_password_hash
                and username == admin_user
                and check_password_hash(admin_password_hash, password)
            ):
                session["user_id"] = username
                return redirect(url_for("dashboard"))
            error = "Invalid username or password."
            if not admin_user or not admin_password_hash:
                error = "WEBAPP_ADMIN_USER / WEBAPP_ADMIN_PASSWORD_HASH not configured in .env."
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # -- dashboard -----------------------------------------------------------
    @app.route("/")
    @login_required
    def dashboard():
        v, err = _vault_or_error()
        if err:
            return err
        broker_names = available_brokers()
        status = v.list_broker_status(session["user_id"], broker_names)
        return render_template("dashboard.html", brokers=broker_names, status=status)

    # -- credentials ---------------------------------------------------------
    @app.route("/brokers/<broker_name>/credentials", methods=["GET", "POST"])
    @login_required
    def broker_credentials(broker_name: str):
        v, err = _vault_or_error()
        if err:
            return err
        try:
            adapter_class = get_adapter_class(broker_name)
        except ValueError as exc:
            return render_template("error.html", message=str(exc)), 404

        fields_spec = adapter_class.required_credential_fields()

        if request.method == "POST":
            existing_now = v.get_credentials(session["user_id"], broker_name) or {}
            values = {}
            for name, _, is_secret in fields_spec:
                submitted = request.form.get(name, "")
                if is_secret and not submitted and name in existing_now:
                    values[name] = existing_now[name]  # blank secret field = keep existing value
                else:
                    values[name] = submitted
            v.save_credentials(session["user_id"], broker_name, values)
            return redirect(url_for("dashboard"))

        existing = v.get_credentials(session["user_id"], broker_name) or {}
        return render_template(
            "credentials_form.html", broker_name=broker_name,
            fields_spec=fields_spec, existing=existing,
        )

    # -- connect (OAuth) toggle -----------------------------------------------
    @app.route("/brokers/<broker_name>/connect")
    @login_required
    def broker_connect(broker_name: str):
        v, err = _vault_or_error()
        if err:
            return err
        adapter = _adapter_for(session["user_id"], broker_name, v)
        if adapter is None:
            return redirect(url_for("broker_credentials", broker_name=broker_name))

        state = secrets.token_urlsafe(16)
        session[f"oauth_state_{broker_name}"] = state
        login_url = adapter.build_login_url(state=state)
        logger.info("[BROKER_CONNECT_INITIATED] user=%s broker=%s", session["user_id"], broker_name)
        return redirect(login_url)

    @app.route("/brokers/<broker_name>/callback")
    @login_required
    def broker_callback(broker_name: str):
        v, err = _vault_or_error()
        if err:
            return err
        adapter = _adapter_for(session["user_id"], broker_name, v)
        if adapter is None:
            return render_template("error.html", message="No credentials saved for this broker."), 400

        try:
            adapter.exchange_code(request.url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[BROKER_CONNECT_FAILED] user=%s broker=%s", session["user_id"], broker_name)
            return render_template("error.html", message=f"Connection failed: {exc}"), 400

        check: ConnectionCheckResult = adapter.test_connection()
        v.set_connected(session["user_id"], broker_name, check.ok)
        logger.info(
            "[BROKER_CONNECT_%s] user=%s broker=%s user_name=%s",
            "OK" if check.ok else "FAILED", session["user_id"], broker_name, check.user_name,
        )
        return redirect(url_for("dashboard"))

    @app.route("/brokers/<broker_name>/disconnect")
    @login_required
    def broker_disconnect(broker_name: str):
        v, err = _vault_or_error()
        if err:
            return err
        v.set_connected(session["user_id"], broker_name, False)
        token_path = TOKEN_STORE_DIR / f"{session['user_id']}__{broker_name}_token_store.json"
        if token_path.exists():
            token_path.unlink()
        logger.info("[BROKER_DISCONNECTED] user=%s broker=%s", session["user_id"], broker_name)
        return redirect(url_for("dashboard"))

    # -- live status check (what "see what's happening" shows today) -----------
    @app.route("/brokers/<broker_name>/status")
    @login_required
    def broker_status(broker_name: str):
        from flask import jsonify

        v, err = _vault_or_error()
        if err:
            return {"error": "vault not configured"}, 500
        adapter = _adapter_for(session["user_id"], broker_name, v)
        if adapter is None or not adapter.is_authenticated():
            return jsonify({"connected": False, "detail": "Not connected."})
        check = adapter.test_connection()
        return jsonify({
            "connected": check.ok, "detail": check.detail,
            "user_name": check.user_name, "user_id": check.user_id,
        })

    return app
