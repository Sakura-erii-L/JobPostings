from app.security import hash_password, hash_value, redact_text, verify_password


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("AdminPass123!")
    second = hash_password("AdminPass123!")
    assert first != second
    assert verify_password("AdminPass123!", first)
    assert not verify_password("wrong-pass", first)
    assert not verify_password("AdminPass123!", "invalid")


def test_hash_is_stable():
    assert hash_value("abc") == hash_value("abc")
    assert hash_value("abc") != hash_value("abd")


def test_redaction():
    value = redact_text("联系电话 13812345678，邮箱 test@example.com，身份证 110101199001011234", b"key")
    assert "13812345678" not in value
    assert "test@example.com" not in value
    assert "110101199001011234" not in value
    assert "[PHONE_" in value
    assert "[EMAIL_" in value
    assert "[ID_" in value
