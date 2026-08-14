from __future__ import annotations

import pytest

from webapp.credential_vault import CredentialVault, VaultError, generate_encryption_key


@pytest.fixture()
def vault(tmp_path):
    key = generate_encryption_key()
    return CredentialVault(key, db_path=tmp_path / "test.db")


def test_save_and_get_credentials_round_trip(vault):
    vault.save_credentials("user1", "fyers", {"client_id": "XC-100", "secret_key": "s3cr3t"})
    creds = vault.get_credentials("user1", "fyers")
    assert creds == {"client_id": "XC-100", "secret_key": "s3cr3t"}


def test_get_credentials_returns_none_when_absent(vault):
    assert vault.get_credentials("user1", "fyers") is None


def test_credentials_are_encrypted_on_disk(tmp_path):
    key = generate_encryption_key()
    db_path = tmp_path / "test.db"
    vault = CredentialVault(key, db_path=db_path)
    vault.save_credentials("user1", "fyers", {"secret_key": "very-secret-value-12345"})

    raw = db_path.read_bytes()
    assert b"very-secret-value-12345" not in raw


def test_wrong_encryption_key_cannot_decrypt(tmp_path):
    db_path = tmp_path / "test.db"
    vault1 = CredentialVault(generate_encryption_key(), db_path=db_path)
    vault1.save_credentials("user1", "fyers", {"secret_key": "s3cr3t"})

    vault2 = CredentialVault(generate_encryption_key(), db_path=db_path)  # different key
    with pytest.raises(VaultError):
        vault2.get_credentials("user1", "fyers")


def test_save_credentials_upserts_without_losing_connected_state(vault):
    vault.save_credentials("user1", "fyers", {"client_id": "A"})
    vault.set_connected("user1", "fyers", True)

    vault.save_credentials("user1", "fyers", {"client_id": "B"})  # update creds
    status = vault.list_broker_status("user1", ["fyers"])
    assert status["fyers"]["connected"] is True  # NOT reset by the credential update
    assert vault.get_credentials("user1", "fyers")["client_id"] == "B"


def test_list_broker_status_covers_brokers_with_no_credentials(vault):
    vault.save_credentials("user1", "fyers", {"client_id": "A"})
    status = vault.list_broker_status("user1", ["fyers", "upstox"])
    assert status["fyers"]["has_credentials"] is True
    assert status["upstox"]["has_credentials"] is False
    assert status["upstox"]["connected"] is False


def test_delete_credentials(vault):
    vault.save_credentials("user1", "fyers", {"client_id": "A"})
    vault.delete_credentials("user1", "fyers")
    assert vault.get_credentials("user1", "fyers") is None


def test_credentials_isolated_per_user(vault):
    vault.save_credentials("user1", "fyers", {"client_id": "USER1_ID"})
    vault.save_credentials("user2", "fyers", {"client_id": "USER2_ID"})
    assert vault.get_credentials("user1", "fyers")["client_id"] == "USER1_ID"
    assert vault.get_credentials("user2", "fyers")["client_id"] == "USER2_ID"


def test_missing_encryption_key_raises_clear_error(tmp_path):
    with pytest.raises(VaultError, match="WEBAPP_ENCRYPTION_KEY"):
        CredentialVault("", db_path=tmp_path / "test.db")


def test_invalid_encryption_key_raises_clear_error(tmp_path):
    with pytest.raises(VaultError, match="invalid"):
        CredentialVault("not-a-valid-fernet-key", db_path=tmp_path / "test.db")
