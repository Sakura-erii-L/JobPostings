import json

from app import db
from app.catalog import apply_model_item, normalize_company_tags
from app.company_research import ensure_company_research_jobs, persist_company_research
from app.maintenance import repair_timeline_events
from app.processing import ingest_message


def configure_test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()


def test_model_company_tags_keep_taxonomy_labels_and_allow_supported_attributes():
    tags = normalize_company_tags(
        [
            {"category": "company_type", "code": "state_owned", "label": "模型自定义名称"},
            {"category": "industry", "code": "military_defense", "label": "任意行业"},
            {"category": "attribute", "code": "new_energy", "label": "新能源"},
            {"category": "unknown", "code": "ignored", "label": "忽略"},
        ]
    )

    assert {tag["code"]: tag["label"] for tag in tags} == {
        "state_owned": "国有企业",
        "military_defense": "军工/国防",
        "new_energy": "新能源",
    }


def test_company_names_stay_independent_but_same_company_jobs_are_merged(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    apply_model_item(
        {
            "is_recruitment": True,
            "company": {
                "display_name": "合并科技",
                "legal_name": "合并科技有限公司",
                "aliases": ["合并招聘"],
                "company_nature": "民营企业",
                "industry_codes": ["electronics_semiconductor"],
            },
            "batch": {"name": "2027 校招", "recruitment_type": "campus"},
            "jobs": [{
                "title": "嵌入式工程师",
                "recruitment_type": "campus",
                "employment_type": "unknown",
                "locations": ["南京"],
                "responsibilities": "负责底层开发",
            }],
        },
        None,
        "2026-09-04T00:00:00+00:00",
    )
    apply_model_item(
        {
            "is_recruitment": True,
            "company": {
                "display_name": "合并科技有限公司",
                "aliases": ["合并科技招聘品牌"],
                "industry_codes": ["electronics_semiconductor"],
                "tags": [{"category": "attribute", "code": "r_and_d_oriented", "label": "研发导向"}],
            },
            "batch": {"name": "2027 校招", "recruitment_type": "campus"},
            "jobs": [{
                "title": "嵌入式 工程师",
                "recruitment_type": "campus",
                "employment_type": "full_time",
                "locations": ["上海"],
                "responsibilities": "参与芯片驱动开发",
            }],
        },
        None,
        "2026-09-04T01:00:00+00:00",
    )

    apply_model_item(
        {
            "is_recruitment": True,
            "company": {"display_name": "合并科技", "industry_codes": ["electronics_semiconductor"]},
            "batch": {"name": "2027 春招", "recruitment_type": "campus"},
            "jobs": [{
                "title": "嵌入式 工程师",
                "recruitment_type": "campus",
                "employment_type": "full_time",
                "locations": ["上海"],
                "responsibilities": "参与芯片驱动开发",
            }],
        },
        None,
        "2026-09-04T02:00:00+00:00",
    )

    companies = db.all_rows("SELECT * FROM companies ORDER BY display_name")
    assert [company["display_name"] for company in companies] == ["合并科技", "合并科技有限公司"]
    first = next(company for company in companies if company["display_name"] == "合并科技")
    second = next(company for company in companies if company["display_name"] == "合并科技有限公司")
    assert "合并科技招聘品牌" not in json.loads(first["aliases_json"])
    assert "合并科技招聘品牌" in json.loads(second["aliases_json"])
    tags = json.loads(first["company_tags_json"])
    assert {tag["code"] for tag in tags} >= {"private", "electronics_semiconductor"}
    first_jobs = db.all_rows("SELECT * FROM jobs WHERE company_id=?", (first["id"],))
    second_jobs = db.all_rows("SELECT * FROM jobs WHERE company_id=?", (second["id"],))
    assert len(first_jobs) == 1
    assert len(second_jobs) == 1
    assert set(json.loads(first_jobs[0]["locations_json"])) == {"南京", "上海"}
    assert "底层开发" in first_jobs[0]["responsibilities"]
    assert "芯片驱动开发" in first_jobs[0]["responsibilities"]


def test_public_research_persists_summary_tags_and_original_sources(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    apply_model_item(
        {"is_recruitment": True, "company": {"display_name": "公开资料科技"}, "jobs": []},
        None,
        "2026-09-04T00:00:00+00:00",
    )
    company_id = db.one("SELECT id FROM companies")["id"]
    result = persist_company_research(company_id, {
        "status": "complete",
        "reason": "已核对公开来源",
        "summary": "公开资料科技是一家提供电子设备的企业。",
        "company_type": "private",
        "industry_codes": ["electronics_semiconductor"],
        "tags": [{"category": "attribute", "code": "technology_company", "label": "技术型企业"}],
        "facts": [{"fact": "主营电子设备", "source_title": "官网", "source_url": "https://example.com/about"}],
        "negative_findings": [{
            "title": "公开处罚记录",
            "summary": "公开来源记录了一项处罚信息。",
            "source_title": "公开信息",
            "source_url": "https://example.com/news",
            "resolved_url": "https://example.com/news?redirect=1",
            "published_at": "2026-08-01",
            "severity": "medium",
        }],
        "sources_checked": [{"title": "官网", "url": "https://example.com/about", "resolved_url": "", "excerpt": ""}],
    })

    company = db.one("SELECT * FROM companies WHERE id=?", (company_id,))
    finding = db.one("SELECT * FROM company_public_findings WHERE company_id=?", (company_id,))
    assert result["research_status"] == "public_web"
    assert company["summary"] == "公开资料科技是一家提供电子设备的企业。"
    assert {tag["code"] for tag in json.loads(company["company_tags_json"])} >= {"private", "electronics_semiconductor", "technology_company"}
    assert finding["source_url"] == "https://example.com/news"
    assert finding["resolved_url"] == "https://example.com/news?redirect=1"
    assert db.one("SELECT source_type FROM evidences WHERE source_type='public_negative_news'")


def test_force_company_research_queues_a_new_job_after_previous_completion(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    apply_model_item({"is_recruitment": True, "company": {"display_name": "重检科技"}, "jobs": []}, None, "2026-09-04T00:00:00+00:00")
    company_id = db.one("SELECT id FROM companies")["id"]
    with db.connect() as connection:
        connection.execute("UPDATE processing_jobs SET status='succeeded',stage='done' WHERE kind='research_company' AND company_id=?", (company_id,))

    normal = ensure_company_research_jobs(company_ids=[company_id])
    forced = ensure_company_research_jobs(force=True, company_ids=[company_id])
    assert normal["skipped_existing"] == 1
    assert forced["queued"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM processing_jobs WHERE kind='research_company' AND company_id=?", (company_id,))["count"] == 2


def test_company_research_recovers_legacy_unknown_kind_failure(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    apply_model_item({"is_recruitment": True, "company": {"display_name": "旧任务科技"}, "jobs": []}, None, "2026-09-04T00:00:00+00:00")
    company_id = db.one("SELECT id FROM companies")["id"]
    with db.connect() as connection:
        connection.execute(
            "UPDATE processing_jobs SET status='needs_review',stage='failed',error=? WHERE kind='research_company' AND company_id=?",
            ("Unknown processing job kind: research_company", company_id),
        )

    result = ensure_company_research_jobs(company_ids=[company_id])
    job = db.one("SELECT status,stage,error,attempts FROM processing_jobs WHERE kind='research_company' AND company_id=?", (company_id,))
    assert result["queued"] == 1
    assert dict(job) == {"status": "pending", "stage": "research_queued", "error": None, "attempts": 0}


def test_legacy_implausible_timeline_date_is_recovered_from_version_source(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    apply_model_item({"is_recruitment": True, "company": {"display_name": "时间修复科技"}, "jobs": []}, None, "2026-09-04T00:00:00+00:00")
    company_id = db.one("SELECT id FROM companies")["id"]
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO recruitment_events(id,company_id,title,event_type,start_at,end_at,timezone,format,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("event-legacy", company_id, "校园宣讲会", "宣讲会", "2001/09/04 19:00", None, "Asia/Shanghai", "offline", "upcoming", "2026-09-04T00:00:00+00:00", "2026-09-04T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO recruitment_event_versions(id,event_id,payload_json,observed_at,is_current) VALUES(?,?,?,?,1)",
            ("version-legacy", "event-legacy", json.dumps({"start_at": "2001/09/04 19:00", "source": "时间：2026年9月17日"}, ensure_ascii=False), "2026-09-04T01:00:00+00:00"),
        )

    result = repair_timeline_events()
    event = db.one("SELECT start_at FROM recruitment_events WHERE id='event-legacy'")
    assert result["recovered"] == 1
    assert event["start_at"] == "2026-09-16T16:00:00+00:00"


def test_midnight_event_is_repaired_from_relative_source_time(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    raw_id = ingest_message({"id": "relative-event", "type": "普通文本", "text": "招聘说明会：明日19点线上举行"}, "manual", None)
    assert raw_id
    apply_model_item(
        {
            "is_recruitment": True,
            "company": {"display_name": "相对时间科技"},
            "events": [{"title": "相对时间科技宣讲会", "event_type": "presentation", "start_at": "2026-09-04T00:00:00+00:00", "timezone": "Asia/Shanghai"}],
            "jobs": [],
        },
        raw_id,
        "2026-09-04T00:00:00+00:00",
    )

    result = repair_timeline_events()
    event = db.one("SELECT start_at FROM recruitment_events")
    assert result["recovered"] == 1
    assert event["start_at"] == "2026-09-05T11:00:00+00:00"
