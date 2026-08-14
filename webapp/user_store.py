"""
webapp/user_store.py

Real per-client accounts: register with a username/password, log in
against those stored credentials. Lives in the same local SQLite file as
CredentialVault (separate table) — password hashes only, never
plaintext. Hashing alone (werkzeug's scrypt-based generate_password_hash)
makes plain SQLite storage fine here, unlike the broker-credentials table
which needs REVERSIBLE encryption (Fernet, in credential_vault.py)
because the app needs the actual client_id/secret_key back out to log
into a broker — passwords never need to come back out, only be checked.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = Path("secrets/credentials.db")  # shared file with CredentialVault, separate table
MIN_PASSWORD_LENGTH = 8


class UserStoreError(RuntimeError):
    pass


@dataclass
class User:
    username: str
    created_at: str


class UserStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path is not None else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def register(self, username: str, password: str, confirm_password: str) -> None:
        username = username.strip()
        if not username:
            raise UserStoreError("Username is required.")
        if password != confirm_password:
            raise UserStoreError("Passwords do not match.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise UserStoreError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

        password_hash = generate_password_hash(password)
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, password_hash, datetime.now(IST).isoformat()),
                )
        except sqlite3.IntegrityError:
            raise UserStoreError(f"Username '{username}' is already taken.") from None

    def verify(self, username: str, password: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()
        if row is None:
            return False
        return check_password_hash(row[0], password)

    def exists(self, username: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()
        return row is not None
