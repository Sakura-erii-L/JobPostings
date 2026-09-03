import pytest

from app.backups import decrypt_backup, encrypt_backup


def test_backup_encryption_round_trip():
    encrypted = encrypt_backup(b"jobpostings database", "correct horse battery staple", {"snapshot_name": "test.jpe"})
    payload, metadata = decrypt_backup(encrypted, "correct horse battery staple")
    assert payload == b"jobpostings database"
    assert metadata["snapshot_name"] == "test.jpe"
    with pytest.raises(Exception):
        decrypt_backup(encrypted, "wrong password")

