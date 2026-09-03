from app.security import hash_value, redact_text


def test_hash_is_stable():
    assert hash_value("abc") == hash_value("abc")
    assert hash_value("abc") != hash_value("abd")


def test_redaction():
    value = redact_text("联系电话 13812345678，邮箱 test@example.com", b"key")
    assert "13812345678" not in value
    assert "test@example.com" not in value
    assert "[PHONE_" in value
    assert "[EMAIL_" in value

