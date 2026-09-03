import pytest

from app.parsers import extract_html, extract_file, fetch_public_url


def test_html_extraction():
    result = extract_html("<html><title>招聘</title><script>x</script><p>研发工程师</p></html>")
    assert "研发工程师" in result["text"]
    assert "x" not in result["text"]


def test_text_file_extraction():
    result = extract_file("notice.txt", "招聘岗位：测试工程师".encode("utf-8"))
    assert "测试工程师" in result["text"]


def test_private_url_is_rejected():
    with pytest.raises(ValueError, match="blocked"):
        fetch_public_url("http://127.0.0.1/internal")
