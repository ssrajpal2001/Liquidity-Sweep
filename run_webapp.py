"""
run_webapp.py

Usage: python run_webapp.py

Starts the web UI. No .env setup required — the two infra secrets it
needs (Flask session key, vault encryption key) auto-generate into local
files on first run (see webapp/secrets_bootstrap.py). Everything else —
user accounts, broker credentials, connection state — lives in
secrets/credentials.db, created automatically too.

EC2 (and most servers) have no display, so this prints the URL to open
manually rather than calling webbrowser.open(), which would just fail
silently or error on a headless box.
"""
from __future__ import annotations

import logging
import os

from config.logging_setup import setup_logging

setup_logging(level="INFO")
logger = logging.getLogger("run_webapp")

if __name__ == "__main__":
    from webapp.app import create_app

    app = create_app()
    host = os.environ.get("WEBAPP_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBAPP_PORT", "5000"))

    logger.info("Starting web UI...")
    print(f"\nOpen this URL in your browser:\n\n    http://<your-ec2-public-ip>:{port}/register\n")
    print(
        "First time here: register an account, then log in.\n"
        f"If this doesn't load: check the EC2 security group allows inbound "
        f"traffic on port {port}.\n"
        "\n"
        "Note on OAuth brokers (Fyers): the broker's redirect URI generally "
        "needs to be https://, and this dev server only speaks http:// — "
        "see README 'HTTPS note' before trying to Connect one of those.\n"
    )
    app.run(host=host, port=port, debug=False)
