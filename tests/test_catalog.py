import json
import tempfile
from pathlib import Path

from app import db
from app.catalog import _make_job, _prepare_job_items, apply_model_item, is_location_like_title, normalize_name, normalize_title, refresh_expiration
from app.maintenance import migrate_major_jobs, repair_existing_catalog, requeue_missing_job_sources
from app.main import company_detail
from app.parsers import extract_recruitment_catalog
from app.processing import ingest_message


def test_normalization():
    assert normalize_name("  星河（科技）有限公司 ") == "星河科技有限公司"
    assert normalize_title("嵌入式 工程师") == "嵌入式工程师"


def test_location_labels_and_location_lists_are_not_jobs():
    assert is_location_like_title("北京")
    assert is_location_like_title("北京、南京、西安、上海")
    assert is_location_like_title("工作地点：深圳")

    source_catalog = extract_recruitment_catalog(
        "荣耀2027届全球校园招聘\n工作地点：深圳、北京、南京、西安、上海\n"
    )
    model_jobs = [
        {"title": "8大职位类别", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "研发", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "北京", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "研发 营销 服务 产品与设计 供应链 财经 流程IT与质量运营 战略管理 工作地点 深圳", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "上海 注：部分非研发岗位全球派遣", "recruitment_type": "campus", "employment_type": "full_time"},
    ]

    jobs = _prepare_job_items(model_jobs)
    assert [job["title"] for job in jobs] == [item["title"] for item in model_jobs]


def test_company_and_job_are_created(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    ids = apply_model_item(
        {
            "is_recruitment": True,
            "confidence": 0.9,
            "company": {"display_name": "测试科技", "industry_codes": ["ai_data"]},
            "batch": {"name": "2026 春招", "recruitment_type": "campus"},
            "jobs": [{"title": "算法工程师", "recruitment_type": "campus", "employment_type": "full_time", "locations": ["南京"]}],
        },
        "message-1",
        "2026-09-03T00:00:00+00:00",
    )
    assert len(ids) == 1
    assert db.one("SELECT COUNT(*) AS n FROM companies")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 1


def test_catalog_evidence_uses_original_source_url(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    original_url = "https://mp.weixin.qq.com/s/catalog-link"
    challenge_url = "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=temporary&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fcatalog-link"
    raw_id = ingest_message({"id": "catalog-link", "type": "article", "text": "公众号文章", "url": original_url}, "manual", None)
    assert raw_id
    with db.connect() as connection:
        connection.execute("UPDATE raw_messages SET metadata_json=? WHERE id=?", (json.dumps({"url": challenge_url}, ensure_ascii=False), raw_id))

    apply_model_item(
        {
            "is_recruitment": True,
            "confidence": 0.9,
            "company": {"display_name": "来源测试科技", "industry_codes": ["ai_data"]},
            "batch": {"name": "2026 校招", "recruitment_type": "campus"},
            "jobs": [{"title": "测试工程师", "recruitment_type": "campus", "employment_type": "full_time"}],
        },
        raw_id,
        "2026-09-04T00:00:00+00:00",
    )
    assert db.one("SELECT source_url FROM evidences WHERE raw_message_id=?", (raw_id,))["source_url"] == original_url
    assert db.one("SELECT source_type FROM evidences WHERE raw_message_id=?", (raw_id,))["source_type"] == "wechat_official_account"
    assert db.one("SELECT source_url FROM company_claims WHERE company_id=(SELECT company_id FROM evidences WHERE raw_message_id=? LIMIT 1)", (raw_id,))["source_url"] == original_url
    assert db.one("SELECT source_type FROM company_claims WHERE company_id=(SELECT company_id FROM evidences WHERE raw_message_id=? LIMIT 1)", (raw_id,))["source_type"] == "wechat_official_account"


def test_shared_salary_and_locations_are_stored_separately_from_jobs(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    source = "测试科技招聘。【推荐岗位】：算法类/测试类；【工作地点】：南京/上海；【年薪范围】：20-40w。"
    raw_id = ingest_message({"id": "shared-details", "type": "普通文本", "text": source}, "manual", None)
    assert raw_id

    ids = apply_model_item(
        {
            "is_recruitment": True,
            "company": {"display_name": "测试科技"},
            "batch": {"name": "2027 校招", "recruitment_type": "campus"},
            "shared_job_info": {"locations": ["南京", "上海"], "salary": {"description": "20-40w"}},
            "jobs": [],
        },
        raw_id,
        "2026-09-05T00:00:00+00:00",
    )

    assert [row["canonical_title"] for row in db.all_rows("SELECT canonical_title FROM jobs ORDER BY canonical_title")] == ["测试类", "算法类"]
    assert all(json.loads(row["locations_json"]) == [] for row in db.all_rows("SELECT locations_json FROM jobs"))
    detail = db.one("SELECT locations_json,salary_json FROM recruitment_shared_details")
    assert json.loads(detail["locations_json"]) == ["南京", "上海"]
    assert json.loads(detail["salary_json"])["description"] == "20-40w"
    company_id = db.one("SELECT id FROM companies WHERE display_name=?", ("测试科技",))["id"]
    payload = company_detail(company_id, {})
    assert payload["recruitment_shared_details"][0]["locations"] == ["南京", "上海"]
    assert payload["recruitment_shared_details"][0]["salary"]["description"] == "20-40w"


def test_successful_source_with_job_clues_and_no_jobs_is_requeued(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    raw_id = ingest_message({"id": "missing-jobs", "type": "普通文本", "text": "测试科技校招，共有十大岗位，详情见招聘图片"}, "manual", None)
    assert raw_id
    apply_model_item(
        {"is_recruitment": True, "company": {"display_name": "测试科技"}, "jobs": []},
        raw_id,
        "2026-09-05T00:00:00+00:00",
    )
    with db.connect() as connection:
        connection.execute("UPDATE raw_messages SET is_recruitment=1,recognition_status='succeeded' WHERE id=?", (raw_id,))
        connection.execute("UPDATE processing_jobs SET status='succeeded',stage='completed' WHERE raw_message_id=? AND kind='classify'", (raw_id,))

    assert requeue_missing_job_sources() == 1
    assert db.one("SELECT status FROM processing_jobs WHERE raw_message_id=? AND kind='classify'", (raw_id,))["status"] == "pending"
    assert db.one("SELECT recognition_status FROM raw_messages WHERE id=?", (raw_id,))["recognition_status"] == "pending"


def test_explicit_job_and_major_sections_are_saved_separately(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    source = """四、招聘岗位
航空发动机总体设计（含混电、氢能等）、航空发动机部件设计、传动系统设计、发动机健康管理及数智化设计、发动机控制系统设计、机械系统设计（滑油、轴承、密封等）、结构强度设计及优化、发动机试验与测试、信息系统开发与运维、航空发动机装配工艺、发动机通用基础技术研究等。
五、需求专业
航空航天类：航空宇航推进理论与工程、航空宇航科学与技术、航空工程
能源动力类：动力工程、工程热物理、动力机械及工程
机械类：机械工程、机械设计制造及其自动化
六、报名方式
请在线报名
"""
    raw_id = ingest_message({"id": "hunan-source", "type": "普通文本", "text": source}, "manual", None)
    assert raw_id
    ids = apply_model_item(
        {
            "is_recruitment": True,
            "company": {"display_name": "中国航发湖南动力机械研究所", "industry_codes": ["military_defense"]},
            "batch": {"name": "2027 校招", "recruitment_type": "campus"},
            "jobs": [{"title": "航空发动机总体设计等研发与技术岗位", "recruitment_type": "campus", "employment_type": "full_time"}],
        },
        raw_id,
        "2026-09-04T00:00:00+00:00",
    )
    titles = [row["canonical_title"] for row in db.all_rows("SELECT canonical_title FROM jobs WHERE status<>'superseded'")]
    company = db.one("SELECT major_requirements_json FROM companies WHERE display_name=?", ("中国航发湖南动力机械研究所",))
    assert len(ids) == 11
    assert set(titles) == {
        "航空发动机总体设计（含混电、氢能等）", "航空发动机部件设计", "传动系统设计", "发动机健康管理及数智化设计",
        "发动机控制系统设计", "机械系统设计（滑油、轴承、密封等）", "结构强度设计及优化", "发动机试验与测试",
        "信息系统开发与运维", "航空发动机装配工艺", "发动机通用基础技术研究等",
    }
    assert json.loads(company["major_requirements_json"]) == [
        "航空航天类：航空宇航推进理论与工程、航空宇航科学与技术、航空工程",
        "能源动力类：动力工程、工程热物理、动力机械及工程",
        "机械类：机械工程、机械设计制造及其自动化",
    ]
    assert all(json.loads(row["majors_json"] or "[]") == [] for row in db.all_rows("SELECT majors_json FROM jobs"))


def test_process_benefits_and_eligibility_are_not_jobs(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    ids = apply_model_item(
        {
            "is_recruitment": True,
            "company": {"display_name": "字段隔离科技", "industry_codes": ["ai_data"]},
            "jobs": [
                {"title": "网申投递", "majors": ["计算机科学与技术"]},
                {"title": "简历筛选"},
                {"title": "安家费30-50万元"},
                {"title": "2027届博士"},
                {"title": "逻辑学"},
                {"title": "软件工程师", "majors": ["计算机科学与技术"]},
            ],
        },
        None,
        "2026-09-04T00:00:00+00:00",
    )
    assert len(ids) == 1
    assert [row["canonical_title"] for row in db.all_rows("SELECT canonical_title FROM jobs")] == ["软件工程师"]
    company = db.one("SELECT major_requirements_json FROM companies WHERE display_name=?", ("字段隔离科技",))
    assert json.loads(company["major_requirements_json"]) == ["计算机科学与技术", "逻辑学"]
    assert json.loads(db.one("SELECT majors_json FROM jobs")["majors_json"]) == []


def test_decorated_sections_after_job_demand_do_not_create_jobs():
    source = """汇川技术2027届秋季校园招聘
✅【岗位需求】
💫2027届国内外应届毕业生💫
电气类、自动化类、机械类、通信类、计算机类、工业管理类等类别专业
📍【工作地点】
苏州、西安、南京、深圳、上海、岳阳、日本等海内外多个城市
👔【企业福利】
工作双休，14天超长春节假期；
食堂价格亲民，班车接送；
快来加入汇川，推进工业文明，共创美好生活！
"""
    source_catalog = extract_recruitment_catalog(source)

    assert _prepare_job_items([]) == []


def test_job_details_do_not_expand_valid_model_jobs():
    source = """【新凯来 XKL 2027届秋招内推】
1. 招聘岗位及要求
软件开发工程师
工作内容：
负责数据分析、仿真建模类产品后端软件交付的全生命周期开发，主导相关产品软件算法设计、功能开发。
软件测试工程师
岗位职责
1、测试和维护半导体设备平台软件。
算法技术工程师
需求背景：物理、数学、光学等理工科专业。
2. 关于新凯来 (XKL)
行业定位：半导体行业新锐。
"""
    model_jobs = [
        {"title": "软件开发工程师", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "软件测试工程师", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "算法技术工程师", "recruitment_type": "campus", "employment_type": "full_time"},
    ]

    source_catalog = extract_recruitment_catalog(source)
    titles = [job["title"] for job in _prepare_job_items(model_jobs)]

    assert titles == ["软件开发工程师", "软件测试工程师", "算法技术工程师"]


def test_major_titles_from_model_jobs_are_kept_out_of_job_catalog():
    source_catalog = extract_recruitment_catalog("岗位需求\n软件开发、软件工程、计算机科学与技术、逻辑学\n")
    model_jobs = [
        {"title": "软件开发", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "软件工程", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "计算机科学与技术", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "逻辑学", "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "逻辑学研究员", "recruitment_type": "campus", "employment_type": "full_time"},
    ]

    jobs = _prepare_job_items(model_jobs)

    assert [job["title"] for job in jobs] == [item["title"] for item in model_jobs]


def test_migrate_legacy_major_jobs_to_company_requirements(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    apply_model_item(
        {
            "is_recruitment": True,
            "company": {"display_name": "历史目录科技", "industry_codes": ["ai_data"]},
            "jobs": [{"title": "软件开发", "recruitment_type": "campus", "employment_type": "full_time"}],
        },
        None,
        "2026-09-05T00:00:00+00:00",
    )
    company_id = db.one("SELECT id FROM companies WHERE display_name=?", ("历史目录科技",))["id"]
    with db.connect() as connection:
        _make_job(
            connection,
            company_id,
            None,
            {"title": "软件开发", "majors": ["计算机科学与技术"], "recruitment_type": "campus", "employment_type": "full_time"},
            "2026-09-05T00:00:00+00:00",
            None,
        )
        _make_job(
            connection,
            company_id,
            None,
            {"title": "软件工程", "recruitment_type": "campus", "employment_type": "full_time"},
            "2026-09-05T00:00:00+00:00",
            None,
        )

    result = migrate_major_jobs()

    assert result == {"jobs_migrated": 1, "job_majors_cleared": 1, "major_requirements_updated": 1}
    assert [row["canonical_title"] for row in db.all_rows("SELECT canonical_title FROM jobs WHERE status<>'superseded'")] == ["软件开发"]
    assert json.loads(db.one("SELECT majors_json FROM jobs WHERE canonical_title='软件开发'")["majors_json"]) == []
    company = db.one("SELECT major_requirements_json FROM companies WHERE id=?", (company_id,))
    assert json.loads(company["major_requirements_json"]) == ["计算机科学与技术", "软件工程"]


def test_repair_existing_catalog_upgrades_v3_marker(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    with db.connect() as connection:
        connection.execute("INSERT INTO schema_meta(key,value) VALUES(?,?)", ("historical_catalog_repair_v3", "done"))

    result = repair_existing_catalog()

    assert result["status"] == "repaired"
    assert result["jobs_created"] == 0
    assert result["major_jobs_migrated"] == 0
    assert db.one("SELECT value FROM schema_meta WHERE key=?", ("historical_catalog_repair_v6",))["value"]


def test_event_summary_does_not_create_venue_company_and_keeps_time(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    apply_model_item(
        {
            "is_recruitment": True,
            "company": {},
            "jobs": [],
            "events": [
                {"title": "9月4日招聘会宣讲会汇总", "event_type": "presentation", "city": "南京", "campus": "南京理工大学", "location": "第四教学楼A106"},
                {"title": "中国航发贵阳发动机设计研究所招聘宣讲会", "event_type": "presentation", "start_at": "09-04 09:00", "end_at": "09-04 10:20", "timezone": "Asia/Shanghai", "city": "南京", "campus": "南京理工大学", "location": "第四教学楼A106"},
            ],
        },
        None,
        "2026-09-03T15:58:47+00:00",
    )
    assert db.one("SELECT COUNT(*) AS n FROM companies")["n"] == 1
    assert db.one("SELECT display_name FROM companies")["display_name"] == "中国航发贵阳发动机设计研究所"
    event = db.one("SELECT start_at,end_at,location FROM recruitment_events")
    assert dict(event) == {"start_at": "2026-09-04T01:00:00+00:00", "end_at": "2026-09-04T02:20:00+00:00", "location": "第四教学楼A106"}


def test_deadline_requires_source_deadline_context(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    raw_id = ingest_message({"id": "deadline-source", "type": "普通文本", "text": "报名截止时间：9月30日\n招聘对象：2027年12月31日前毕业"}, "manual", None)
    apply_model_item(
        {"is_recruitment": True, "company": {"display_name": "截止测试科技"}, "jobs": [{"title": "算法工程师", "deadline": "9月30日"}]},
        raw_id,
        "2026-09-04T00:00:00+00:00",
    )
    assert db.one("SELECT explicit_deadline FROM jobs")["explicit_deadline"] == "2026-09-30"

    raw_id = ingest_message({"id": "eligibility-only", "type": "普通文本", "text": "招聘对象：2027年12月31日前毕业"}, "manual", None)
    apply_model_item(
        {"is_recruitment": True, "company": {"display_name": "资格测试科技"}, "jobs": [{"title": "测试工程师", "deadline": "2027-12-31"}]},
        raw_id,
        "2026-09-04T00:00:00+00:00",
    )
    assert db.one("SELECT explicit_deadline FROM jobs WHERE canonical_title=?", ("测试工程师",))["explicit_deadline"] is None


def test_event_title_company_overrides_context_company(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    apply_model_item({"is_recruitment": True, "company": {"display_name": "哈尔滨飞机工业集团有限责任公司"}, "jobs": []}, None, "2026-09-04T00:00:00+00:00")
    apply_model_item(
        {
            "is_recruitment": True,
            "company": {"display_name": "哈尔滨飞机工业集团有限责任公司"},
            "events": [{"title": "晓禾科技（武汉）有限公司空中宣讲会", "company_name": "哈尔滨飞机工业集团有限责任公司", "event_type": "presentation", "start_at": "2026-09-05T11:00:00+00:00", "timezone": "Asia/Shanghai"}],
            "jobs": [],
        },
        None,
        "2026-09-04T00:00:00+00:00",
    )
    event = db.one("SELECT company_id FROM recruitment_events")
    target = db.one("SELECT id FROM companies WHERE display_name=?", ("晓禾科技（武汉）有限公司",))
    assert target
    assert event["company_id"] == target["id"]


def test_duplicate_jobs_across_batches_are_hidden_but_departments_stay_separate(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "db_path", db_path)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()
    base = {"is_recruitment": True, "company": {"display_name": "去重测试科技"}, "jobs": [{"title": "软件工程师", "recruitment_type": "campus", "employment_type": "full_time", "department": "研发"}]}
    apply_model_item({**base, "batch": {"name": "2026 校招", "recruitment_type": "campus"}}, None, "2026-09-04T00:00:00+00:00")
    apply_model_item({**base, "batch": {"name": "2027 校招", "recruitment_type": "campus"}, "jobs": [{**base["jobs"][0], "employment_type": "part_time"}]}, None, "2026-09-05T00:00:00+00:00")
    apply_model_item({**base, "batch": {"name": "2028 校招", "recruitment_type": "campus"}, "jobs": [{**base["jobs"][0], "department": "测试"}]}, None, "2026-09-06T00:00:00+00:00")
    company_id = db.one("SELECT id FROM companies WHERE display_name=?", ("去重测试科技",))["id"]
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE company_id=? AND status<>'superseded'", (company_id,))["n"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE company_id=? AND status='superseded'", (company_id,))["n"] == 0
    assert db.one("SELECT employment_type FROM jobs WHERE company_id=? AND department='研发'", (company_id,))["employment_type"] == "unknown"
