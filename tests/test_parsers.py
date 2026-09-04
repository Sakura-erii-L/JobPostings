import pytest

from app.parsers import detect_image_suffix, extract_html, extract_file, fetch_public_url, is_link_message, is_system_message, parse_message_payload


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


def test_public_account_link_content_data_is_parsed():
    text, metadata = parse_message_payload({
        "type": "公众号链接",
        "text": "",
        "contentData": {"type": "share", "title": "锦浪科技校招", "des": "欢迎投递", "url": "https://example.com/recruit"},
    })
    assert "锦浪科技校招" in text
    assert metadata["url"] == "https://example.com/recruit"


def test_message_url_is_treated_as_public_web_source():
    assert is_link_message("普通文本", {"url": "https://example.com/recruit"})


def test_html_extraction_collects_image_resources():
    result = extract_html('<meta property="og:image" content="/poster.png"><img data-src="/poster-2.jpg"><p>招聘正文</p>')
    assert result["images"] == ["/poster.png", "/poster-2.jpg"]


def test_image_signature_is_detected_without_content_type():
    assert detect_image_suffix(b"\x89PNG\r\n\x1a\nposter") == ".png"


def test_system_messages_are_detected_without_filtering_recruitment_invitation_text():
    assert is_system_message("公众号链接", '"甲"邀请"乙"加入了群聊')
    assert is_system_message("系统消息", '"甲"撤回了一条消息')
    assert not is_system_message("普通文本", "欢迎加入我们团队，招聘算法工程师")
