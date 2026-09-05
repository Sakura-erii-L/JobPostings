import json
import tempfile
from pathlib import Path

from app import db
from app.catalog import apply_model_item, normalize_name, normalize_title, refresh_expiration
from app.processing import ingest_message


def test_normalization():
    assert normalize_name("  星河（科技）有限公司 ") == "星河科技有限公司"
    assert normalize_title("嵌入式 工程师") == "嵌入式工程师"


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
    assert db.one("SELECT source_url FROM company_claims WHERE company_id=(SELECT company_id FROM evidences WHERE raw_message_id=? LIMIT 1)", (raw_id,))["source_url"] == original_url


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
                {"title": "软件工程师", "majors": ["计算机科学与技术"]},
            ],
        },
        None,
        "2026-09-04T00:00:00+00:00",
    )
    assert len(ids) == 1
    assert [row["canonical_title"] for row in db.all_rows("SELECT canonical_title FROM jobs")] == ["软件工程师"]
    company = db.one("SELECT major_requirements_json FROM companies WHERE display_name=?", ("字段隔离科技",))
    assert json.loads(company["major_requirements_json"]) == ["计算机科学与技术"]
    assert json.loads(db.one("SELECT majors_json FROM jobs")["majors_json"]) == []


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
