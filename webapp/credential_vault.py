"""
webapp/credential_vault.py

Stores broker credentials (client_id, secret_key, redirect_uri, etc.)
encrypted at rest in a local SQLite database, keyed by (user_id,
broker_name). This is what "credentials in a DB, not .env" means
concretely: .env now holds exactly one new secret — the encryption key —
and every broker credential for every user lives here instead, encrypted.

Threat model this addresses: if the SQLite file alone leaks (backup,
misconfigured permissions, etc.), the credentials inside it are useless
without the separate encryption key. It does NOT protect against the
encryption key itself leaking — treat WEBAPP_ENCRYPTION_KEY with the same
care as any master secret; it belongs in `.env` (gitignored) and nowhere
else.

This is intentionally simple (single SQLite file, single Fernet key) —
adequate for a handful of trusted clients on one server, not a substitute
for a real secrets manager (Vault, AWS Secrets Manager) if this ever runs
multi-tenant at real scale. Worth revisiting if the client count or
threat model grows.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

DB_PATH = Path("secrets/credentials.db")


class VaultError(RuntimeError):
    pass


def generate_encryption_key() -> str:
    """Run once: `python -c "from webapp.credential_vault import generate_encryption_key;
    print(generate_encryption_key())"` and put the result in .env as
    WEBAPP_ENCRYPTION_KEY. Losing this key means every stored credential
    becomes permanently unreadable — back it up somewhere safe, separately
    from the SQLite file."""
    return Fernet.generate_key().decode()


class CredentialVault:
    def __init__(self, encryption_key: str, db_path: Optional[Path] = None):
        if not encryption_key:
            raise VaultError(
                "WEBAPP_ENCRYPTION_KEY is not set. Generate one with:\n"
                '  python -c "from webapp.credential_vault import generate_encryption_key; '
                'print(generate_encryption_key())"\n'
                "and add it to .env."
            )
        try:
            self._fernet = Fernet(encryption_key.encode())
        except Exception as exc:  # noqa: BLE001
            raise VaultError(f"WEBAPP_ENCRYPTION_KEY is invalid: {exc}") from exc

        # Resolved at call time, not bound as a mutable default parameter —
        # a default like `db_path: Path = DB_PATH` would capture whatever
        # DB_PATH was at import time and silently ignore later reassignment
        # (e.g. in tests redirecting storage to a temp dir).
        self.db_path = Path(db_path) if db_path is not None else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_credentials (
                    user_id TEXT NOT NULL,
                    broker_name TEXT NOT NULL,
                    encrypted_fields BLOB NOT NULL,
                    connected INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, broker_name)
                )
                """
            )

    def save_credentials(self, user_id: str, broker_name: str, fields: dict[str, str]) -> None:
        encrypted = self._fernet.encrypt(json.dumps(fields).encode())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO broker_credentials (user_id, broker_name, encrypted_fields, connected)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(user_id, broker_name)
                DO UPDATE SET encrypted_fields = excluded.encrypted_fields
                """,
                (user_id, broker_name, encrypted),
            )

    def get_credentials(self, user_id: str, broker_name: str) -> Optional[dict[str, str]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT encrypted_fields FROM broker_credentials WHERE user_id = ? AND broker_name = ?",
                (user_id, broker_name),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(self._fernet.decrypt(row[0]).decode())
        except InvalidToken as exc:
            raise VaultError(
                f"Could not decrypt stored credentials for {broker_name} — "
                "WEBAPP_ENCRYPTION_KEY may have changed since they were saved."
            ) from exc

    def set_connected(self, user_id: str, broker_name: str, connected: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE broker_credentials SET connected = ? WHERE user_id = ? AND broker_name = ?",
                (1 if connected else 0, user_id, broker_name),
            )

    def list_broker_status(self, user_id: str, broker_names: list[str]) -> dict[str, dict]:
        status = {name: {"has_credentials": False, "connected": False} for name in broker_names}
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT broker_name, connected FROM broker_credentials WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        for broker_name, connected in rows:
            if broker_name in status:
                status[broker_name] = {"has_credentials": True, "connected": bool(connected)}
        return status

    def delete_credentials(self, user_id: str, broker_name: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM broker_credentials WHERE user_id = ? AND broker_name = ?",
                (user_id, broker_name),
            )
