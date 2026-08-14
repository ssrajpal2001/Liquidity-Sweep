"""
webapp/secrets_bootstrap.py

Auto-generates and persists the two infra secrets the web UI needs
(Flask session signing key, vault encryption key) to local files on first
run — zero required .env setup, per the request to keep everything out
of the environment and in the database instead.

Why these two specifically CAN'T live in the database: the encryption key
is what protects the credentials table — a key stored inside the thing it
encrypts is circular (you'd need the key to read the key). So these two
live in their own small local files under secrets/ instead: still never
in .env, still never committed (gitignored), just not in the DB either.

Generated once, then reused forever after — regenerating the encryption
key on every run would make every previously-stored broker credential
permanently unreadable, so these are read-if-exists, create-if-missing,
not regenerate-every-time.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

SECRET_KEY_PATH = Path("secrets/webapp_secret.key")
ENCRYPTION_KEY_PATH = Path("secrets/webapp_encryption.key")


def _get_or_create(path: Path, generator) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = generator()
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def get_or_create_secret_key(path: Path = SECRET_KEY_PATH) -> str:
    return _get_or_create(path, lambda: secrets.token_hex(32))


def get_or_create_encryption_key(path: Path = ENCRYPTION_KEY_PATH) -> str:
    return _get_or_create(path, lambda: Fernet.generate_key().decode())
