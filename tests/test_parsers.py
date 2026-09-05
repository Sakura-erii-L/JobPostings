import io
import zipfile

import pytest

from app.parsers import _major_name_catalog, detect_file_suffix, detect_image_suffix, extract_event_datetime_candidates, extract_html, extract_file, extract_recruitment_catalog, extract_recruitment_shared_details, fetch_public_url, is_access_challenge_page, is_file_message, is_link_message, is_major_like_title, is_major_requirement_heading, is_system_message, normalize_event_datetime, parse_message_payload, parse_message_time, recover_original_source_url


def test_html_extraction():
    result = extract_html("<html><title>招聘</title><script>x</script><p>研发工程师</p></html>")
    assert "研发工程师" in result["text"]
    assert "x" not in result["text"]


def test_text_file_extraction():
    result = extract_file("notice.txt", "招聘岗位：测试工程师".encode("utf-8"))
    assert "测试工程师" in result["text"]


def test_pdf_signature_is_detected_for_generic_attachment_name():
    assert detect_file_suffix("attachment.bin", b"%PDF-1.7\n", "application/octet-stream") == ".pdf"


def test_docx_zip_signature_is_detected_for_generic_attachment_name():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    assert detect_file_suffix("attachment.bin", buffer.getvalue()) == ".docx"


def test_pdf_binary_is_extracted_when_filename_and_mime_are_generic():
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Recruitment Test Engineer")
    result = extract_file("attachment.bin", document.tobytes(), mime_type="application/octet-stream")
    assert "Recruitment Test Engineer" in result["text"]


def test_docx_binary_is_extracted_when_filename_and_mime_are_generic():
    document_class = pytest.importorskip("docx").Document
    document = document_class()
    document.add_paragraph("Document Test Engineer")
    buffer = io.BytesIO()
    document.save(buffer)
    result = extract_file("attachment.bin", buffer.getvalue(), mime_type="application/octet-stream")
    assert "Document Test Engineer" in result["text"]


def test_shared_document_is_file_message_and_not_link_message():
    message = {"contentData": {"type": "share", "title": "岗位说明.docx"}}
    assert is_file_message("share", message)
    assert not is_link_message("share", message)
    text, _ = parse_message_payload(message)
    assert text == "岗位说明.docx"


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


def test_inline_recommended_job_categories_and_shared_details_are_extracted():
    source = """杭州长川科技2027届校招。【推荐岗位】：硬件开发类/软件算法类/机械能源类/仿真类/AI类/测试类/产品类/研发支持类/应用开发类/销服支持类/供应链类/综合职能类等；【岗位地点】：杭州/苏州/成都/上海/南京等；【对口专业】：电子类、机械类、软件类等专业；【年薪范围】：本科：13-25w；硕士：20-40w；博士：50w+。"""

    catalog = extract_recruitment_catalog(source)
    shared = extract_recruitment_shared_details(source)

    assert catalog["job_titles"] == [
        "硬件开发类", "软件算法类", "机械能源类", "仿真类", "AI类", "测试类", "产品类",
        "研发支持类", "应用开发类", "销服支持类", "供应链类", "综合职能类等",
    ]
    assert catalog["major_requirements"] == ["电子类、机械类、软件类等专业"]
    assert shared["locations"] == ["杭州", "苏州", "成都", "上海", "南京"]
    assert shared["salary"]["description"] == "本科：13-25w；硕士：20-40w；博士：50w+"


def test_inline_single_job_title_is_extracted_without_following_fields():
    catalog = extract_recruitment_catalog("投递指南 职位：芯片与器件设计工程师 岗位意向：数字芯片设计")

    assert catalog["job_titles"] == ["芯片与器件设计工程师"]


def test_major_name_catalog_has_exact_names_without_swallowing_job_roles():
    assert len(_major_name_catalog()) == 1385
    assert is_major_like_title("逻辑学")
    assert is_major_like_title("学科教学（数学）")
    assert not is_major_like_title("软件工程师")
    assert not is_major_like_title("逻辑学研究员")
    assert not is_major_like_title("软件工程实习生")
