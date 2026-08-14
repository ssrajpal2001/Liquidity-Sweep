"""
run_webapp.py

Usage: python run_webapp.py

Starts the credential/connection web UI. EC2 (and most servers) have no
display, so this prints the URL to open manually rather than calling
webbrowser.open() — that call would just fail silently or error on a
headless box.

Required in .env before this will work (see README "Web UI setup"):
  WEBAPP_SECRET_KEY          - Flask session signing key
  WEBAPP_ENCRYPTION_KEY      - Fernet key for the credential vault
  WEBAPP_ADMIN_USER          - login username
  WEBAPP_ADMIN_PASSWORD_HASH - login password, hashed (see README)
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

from config.logging_setup import setup_logging

load_dotenv()
setup_logging(level="INFO")
logger = logging.getLogger("run_webapp")

if __name__ == "__main__":
    missing = [
        var for var in (
            "WEBAPP_SECRET_KEY", "WEBAPP_ENCRYPTION_KEY",
            "WEBAPP_ADMIN_USER", "WEBAPP_ADMIN_PASSWORD_HASH",
        )
        if not os.environ.get(var)
    ]
    if missing:
        logger.error(
            "Missing required .env variable(s) for the web UI: %s\n"
            "See README 'Web UI setup' for how to generate each one.",
            ", ".join(missing),
        )
        raise SystemExit(1)

    from webapp.app import create_app

    app = create_app()
    host = os.environ.get("WEBAPP_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBAPP_PORT", "5000"))

    logger.info("Starting web UI...")
    print(f"\nOpen this URL in your browser:\n\n    http://<your-ec2-public-ip>:{port}/login\n")
    print(
        "If this doesn't load: check the EC2 security group allows inbound "
        f"traffic on port {port}, and see README 'HTTPS note' — most brokers "
        "require an https:// redirect URI, which this dev server does not "
        "provide on its own.\n"
    )
    app.run(host=host, port=port, debug=False)
