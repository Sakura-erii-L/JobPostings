import pytest

from app.parsers import _major_name_catalog, detect_image_suffix, extract_event_datetime_candidates, extract_html, extract_file, extract_recruitment_catalog, fetch_public_url, is_access_challenge_page, is_link_message, is_major_like_title, is_major_requirement_heading, is_system_message, normalize_event_datetime, parse_message_payload, parse_message_time, recover_original_source_url


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
    assert metadata["source_url"] == "https://example.com/recruit"


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


def test_trace_memo_datetime_is_used_but_numeric_creation_time_is_ignored():
    assert parse_message_time({"datetime": "2026/8/31 11:16:30", "createTime": 1788146190}) == "2026-08-31T03:16:30+00:00"
    assert parse_message_time({"createTime": 1788146190}) is None


def test_wechat_environment_challenge_page_is_detected_only_for_wechat_hosts():
    body = "<html><body>当前环境异常，完成验证后即可继续访问</body></html>"
    assert is_access_challenge_page("https://mp.weixin.qq.com/s/example", body)
    assert not is_access_challenge_page("https://example.com/recruit", body)


def test_wechat_verification_redirect_recovers_original_source_url():
    original = "https://mp.weixin.qq.com/s/example?mid=123"
    redirected = "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=temporary&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fexample%3Fmid%3D123"
    assert recover_original_source_url(redirected) == original
    assert recover_original_source_url(original) == original


def test_relative_event_time_uses_source_message_date():
    reference = "2026-09-04T11:00:00+00:00"
    assert normalize_event_datetime("明日19点", "Asia/Shanghai", reference) == "2026-09-05T11:00:00+00:00"
    assert normalize_event_datetime("次日晚上7:30", "Asia/Shanghai", reference) == "2026-09-05T11:30:00+00:00"
    assert "2026-09-05T11:00:00+00:00" in extract_event_datetime_candidates("说明会：明日19点", "Asia/Shanghai", reference)


def test_numeric_month_day_time_uses_source_year_and_clock():
    reference = "2026-09-03T15:58:47+00:00"
    assert normalize_event_datetime("09-04 09:00", "Asia/Shanghai", reference) == "2026-09-04T01:00:00+00:00"
    assert normalize_event_datetime("09-04 10:20", "Asia/Shanghai", reference) == "2026-09-04T02:20:00+00:00"


def test_month_day_and_recruitment_catalog_are_extracted():
    reference = "2026-09-04T00:00:00+00:00"
    assert normalize_event_datetime("9月5号19:00", "Asia/Shanghai", reference) == "2026-09-05T11:00:00+00:00"
    catalog = extract_recruitment_catalog("""四、招聘岗位
航空发动机总体设计、航空发动机部件设计、传动系统设计
五、需求专业
航空航天类：航空宇航推进理论与工程、航空宇航科学与技术
能源动力类：动力工程、工程热物理
六、报名方式
请在线报名
""")
    assert catalog["job_titles"] == ["航空发动机总体设计", "航空发动机部件设计", "传动系统设计"]
    assert catalog["major_requirements"] == ["航空航天类：航空宇航推进理论与工程、航空宇航科学与技术", "能源动力类：动力工程、工程热物理"]


def test_recruitment_catalog_separates_major_names_from_mixed_job_section():
    catalog = extract_recruitment_catalog("""岗位需求
机械结构设计、电气控制设计、软件开发
软件工程、计算机科学与技术、人工智能
工作地点
西安
""")

    assert catalog["job_titles"] == ["机械结构设计", "电气控制设计", "软件开发"]
    assert catalog["major_requirements"] == ["软件工程", "计算机科学与技术", "人工智能"]
    assert is_major_requirement_heading("需求专业/方向")


def test_major_name_catalog_has_exact_names_without_swallowing_job_roles():
    assert len(_major_name_catalog()) == 1385
    assert is_major_like_title("逻辑学")
    assert is_major_like_title("学科教学（数学）")
    assert not is_major_like_title("软件工程师")
    assert not is_major_like_title("逻辑学研究员")
    assert not is_major_like_title("软件工程实习生")
