import json
from datetime import datetime, timedelta, timezone

import pytest

from app import catalog, db
from app.catalog import CompanyManagementConflict, apply_model_item, company_management_impact, delete_company_records, merge_company_records, queue_company_management, recruitment_event_state
from app.main import app, recruitment_events
from app.model_provider import ModelResult
from app.processing import process_one_job


def configure_test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    db.init_db()


def _company_payload(name: str, legal_name: str, *, summary: str, website: str, event_title: str) -> dict:
    return {
        "is_recruitment": True,
        "companies": [{
            "company": {
                "display_name": name,
                "legal_name": legal_name,
                "aliases": [f"{name}别名"],
                "summary": summary,
                "website": website,
                "company_nature": f"{name}性质",
                "industry_codes": ["ai_data"],
            },
            "recruitment": {
                "batch": {"name": "2026校园招聘", "year": 2026, "season": "秋季", "recruitment_type": "campus"},
                "shared_details": {"locations": ["南京"], "salary": {"min": 10}},
                "jobs": [{"title": "算法工程师", "recruitment_type": "campus", "employment_type": "full_time", "department": "研发"}],
                "events": [{
                    "title": event_title,
                    "event_type": "presentation",
                    "start_at": "2026-09-20T10:00:00+00:00",
                    "end_at": "2026-09-20T11:00:00+00:00",
                    "timezone": "Asia/Shanghai",
                }],
            },
        }],
    }


def test_manual_merge_migrates_history_and_deduplicates_jobs(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    monkeypatch.setattr(catalog, "_polish_company_merge_content", lambda rows: (None, {"status": "fallback", "reason": "test"}))
    primary_result = apply_model_item(
        _company_payload("主企业", "主企业有限公司", summary="主企业摘要", website="https://primary.example", event_title="主企业宣讲会"),
        None,
        "2026-09-06T00:00:00+00:00",
    )
    supplement_result = apply_model_item(
        _company_payload("补充企业", "补充企业有限公司", summary="补充企业摘要", website="https://supplement.example", event_title="补充企业宣讲会"),
        None,
        "2026-09-06T00:00:00+00:00",
    )
    primary_id, supplement_id = primary_result["company_ids"][0], supplement_result["company_ids"][0]
    primary_job = db.one("SELECT id FROM jobs WHERE company_id=?", (primary_id,))["id"]
    supplement_job = db.one("SELECT id FROM jobs WHERE company_id=?", (supplement_id,))["id"]
    with db.connect() as connection:
        connection.execute(
            "UPDATE companies SET summary=?,aliases_json=?,manual_overrides_json=?,summary_locked=1 WHERE id=?",
            ("主企业手工摘要", json.dumps(["主企业手工别名"], ensure_ascii=False), json.dumps({"summary": "主企业手工摘要", "aliases": ["主企业手工别名"]}, ensure_ascii=False), primary_id),
        )
        connection.execute("UPDATE companies SET summary=? WHERE id=?", ("补充企业摘要", supplement_id))
        connection.execute("INSERT INTO users(id,email,role,created_at) VALUES(?,?,?,?)", ("user-1", "user@example.com", "member", db.utc_now()))
        connection.execute("INSERT INTO user_follows(user_id,company_id,created_at) VALUES(?,?,?)", ("user-1", primary_id, db.utc_now()))
        connection.execute("INSERT INTO user_follows(user_id,company_id,created_at) VALUES(?,?,?)", ("user-1", supplement_id, db.utc_now()))
        connection.execute("INSERT INTO user_job_states(user_id,job_id,state,favorite,updated_at) VALUES(?,?,?,?,?)", ("user-1", primary_job, "interested", 0, db.utc_now()))
        connection.execute("INSERT INTO user_job_states(user_id,job_id,state,favorite,updated_at) VALUES(?,?,?,?,?)", ("user-1", supplement_job, "applied", 1, db.utc_now()))
        connection.execute("INSERT INTO user_tags(id,user_id,name,created_at) VALUES(?,?,?,?)", ("tag-1", "user-1", "重点", db.utc_now()))
        connection.execute("INSERT INTO job_tag_links(user_id,job_id,tag_id) VALUES(?,?,?)", ("user-1", supplement_job, "tag-1"))
        connection.execute("INSERT INTO company_relations(id,parent_company_id,child_company_id,relation_type,created_at) VALUES(?,?,?,?,?)", ("relation-1", primary_id, supplement_id, "brand_of", db.utc_now()))
        connection.execute("INSERT INTO company_merge_rules(id,left_company_id,right_company_id,action,created_at) VALUES(?,?,?,?,?)", ("rule-1", primary_id, supplement_id, "merge", db.utc_now()))
        connection.execute("INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)", ("review-1", "company_review", "company", supplement_id, "{}", db.utc_now()))

    impact = company_management_impact([primary_id, supplement_id], "merge")
    assert impact["primary_company"]["display_name"] == "主企业"
    assert impact["supplementary_companies"][0]["display_name"] == "补充企业"
    assert impact["counts"]["jobs"] == 1
    assert impact["counts"]["recruitment_batches"] == 1

    result = merge_company_records([primary_id, supplement_id])
    assert result["status"] == "merged"
    assert db.one("SELECT id FROM companies WHERE id=?", (primary_id,))
    assert db.one("SELECT id FROM companies WHERE id=?", (supplement_id,)) is None
    company = db.one("SELECT display_name,legal_name,summary,website,aliases_json FROM companies WHERE id=?", (primary_id,))
    assert dict(company)["display_name"] == "主企业"
    assert dict(company)["legal_name"] == "主企业有限公司"
    assert dict(company)["summary"] == "主企业手工摘要"
    assert dict(company)["website"] == "https://primary.example"
    assert set(json.loads(company["aliases_json"])) >= {"主企业手工别名", "补充企业", "补充企业有限公司", "补充企业别名"}
    overrides = json.loads(db.one("SELECT manual_overrides_json FROM companies WHERE id=?", (primary_id,))["manual_overrides_json"])
    assert set(overrides["aliases"]) >= {"主企业手工别名", "补充企业", "补充企业有限公司", "补充企业别名"}
    assert db.one("SELECT COUNT(*) AS count FROM jobs WHERE company_id=?", (primary_id,))["count"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM recruitment_batches WHERE company_id=?", (primary_id,))["count"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM recruitment_events WHERE company_id=?", (primary_id,))["count"] == 2
    assert db.one("SELECT COUNT(*) AS count FROM evidences WHERE company_id=?", (primary_id,))["count"] == 2
    assert dict(db.one("SELECT state,favorite FROM user_job_states WHERE user_id='user-1'")) == {"state": "applied", "favorite": 1}
    assert db.one("SELECT COUNT(*) AS count FROM user_follows WHERE user_id='user-1'") ["count"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM job_tag_links WHERE job_id IN (SELECT id FROM jobs WHERE company_id=?)", (primary_id,))["count"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM company_relations WHERE parent_company_id=? OR child_company_id=?", (primary_id, primary_id))["count"] == 0
    assert db.one("SELECT COUNT(*) AS count FROM company_merge_rules WHERE left_company_id=? OR right_company_id=?", (primary_id, primary_id))["count"] == 0
    assert db.one("SELECT COUNT(*) AS count FROM review_items WHERE entity_type='company' AND entity_id=?", (supplement_id,))["count"] == 0
    assert db.one("SELECT COUNT(*) AS count FROM review_items WHERE entity_type='company' AND entity_id=?", (primary_id,))["count"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM search_index WHERE entity_type='company' AND entity_id=?", (supplement_id,))["count"] == 0
    for table, column in (
        ("jobs", "company_id"), ("recruitment_batches", "company_id"), ("recruitment_shared_details", "company_id"),
        ("recruitment_events", "company_id"), ("company_versions", "company_id"), ("company_claims", "company_id"),
        ("company_public_findings", "company_id"), ("processing_jobs", "company_id"),
    ):
        assert db.one(f"SELECT COUNT(*) AS count FROM {table} WHERE {column}=?", (supplement_id,))["count"] == 0


def test_manual_merge_deduplicates_compatible_events_after_a_time_conflict(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    monkeypatch.setattr(catalog, "_polish_company_merge_content", lambda rows: (None, {"status": "fallback", "reason": "test"}))
    results = []
    for index, start_at in enumerate(("2026-09-20T10:00:00+00:00", "2026-09-21T10:00:00+00:00", "2026-09-21T10:00:00+00:00"), start=1):
        results.append(
            apply_model_item(
                {
                    "is_recruitment": True,
                    "companies": [{
                        "company": {"display_name": f"活动企业{index}", "legal_name": f"活动企业{index}有限公司"},
                        "recruitment": {
                            "batch": {"name": "2026校园招聘", "year": 2026, "season": "秋季", "recruitment_type": "campus"},
                            "shared_details": {"locations": [], "salary": None},
                            "jobs": [],
                            "events": [{
                                "title": "同一场宣讲会",
                                "event_type": "presentation",
                                "city": "南京",
                                "campus": "南京校区",
                                "location": "报告厅",
                                "start_at": start_at,
                                "timezone": "Asia/Shanghai",
                            }],
                        },
                    }],
                },
                None,
                "2026-09-06T00:00:00+00:00",
            )
        )
    company_ids = [result["company_ids"][0] for result in results]
    result = merge_company_records(company_ids)
    assert result["deduplicated_events"] == 1
    events = db.all_rows("SELECT id,start_at FROM recruitment_events WHERE company_id=? ORDER BY start_at", (company_ids[0],))
    assert [event["start_at"] for event in events] == ["2026-09-20T10:00:00+00:00", "2026-09-21T10:00:00+00:00"]
    assert db.one("SELECT COUNT(*) AS count FROM recruitment_event_evidences WHERE event_id=?", (events[1]["id"],))["count"] == 2
    assert db.one("SELECT COUNT(*) AS count FROM review_items WHERE kind='event_time_conflict'")["count"] == 1


def test_manual_merge_uses_codex_for_content_polish_without_overriding_manual_fields(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    primary_id = "company-primary"
    supplement_id = "company-supplement"
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO companies(id,display_name,legal_name,aliases_json,summary,businesses_json,highlights_json,major_requirements_json,manual_overrides_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (primary_id, "主企业", "主企业全称", "[\"主别名\"]", "主企业原始简介", "[\"主营业务\"]", "[\"主亮点\"]", "[\"计算机\"]", json.dumps({"summary": "主企业手动简介", "businesses": ["主企业手动业务"]}, ensure_ascii=False), db.utc_now(), db.utc_now()),
        )
        connection.execute(
            "INSERT INTO companies(id,display_name,legal_name,aliases_json,summary,businesses_json,highlights_json,major_requirements_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (supplement_id, "补充企业", "补充企业全称", "[]", "补充简介", "[\"补充业务\"]", "[\"补充亮点\"]", "[\"软件工程\"]", db.utc_now(), db.utc_now()),
        )
    captured: dict[str, object] = {}

    def fake_polish(candidates, job_id):
        captured["candidates"] = candidates
        captured["job_id"] = job_id
        return ModelResult(
            {
                "status": "complete",
                "reason": "去重并润色",
                "summary": "润色后的简介",
                "businesses": ["润色后的主营业务"],
                "highlights": ["润色后的亮点"],
                "major_requirements": ["软件工程"],
            },
            10,
            5,
            True,
            "local_codex",
            "gpt-5.6-luna",
        )

    monkeypatch.setattr("app.model_provider.polish_company_merge_content", fake_polish)
    result = merge_company_records([primary_id, supplement_id])

    assert captured["job_id"].startswith("manual-merge-")
    assert captured["candidates"][-1]["deterministic_content"]["summary"] == "主企业手动简介"
    assert result["content_polish"]["status"] == "applied"
    merged = db.one("SELECT summary,businesses_json,highlights_json,major_requirements_json,manual_overrides_json FROM companies WHERE id=?", (primary_id,))
    assert merged["summary"] == "主企业手动简介"
    assert json.loads(merged["businesses_json"]) == ["主企业手动业务", "补充业务"]
    assert json.loads(merged["highlights_json"]) == ["润色后的亮点"]
    assert json.loads(merged["major_requirements_json"]) == ["软件工程"]
    assert json.loads(merged["manual_overrides_json"])["businesses"] == ["主企业手动业务", "补充业务"]


def test_company_management_blocks_running_task_and_delete_cleans_references(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    result = apply_model_item(
        _company_payload("待删企业", "待删企业有限公司", summary="摘要", website="https://delete.example", event_title="待删宣讲会"),
        None,
        "2026-09-06T00:00:00+00:00",
    )
    company_id = result["company_ids"][0]
    with db.connect() as connection:
        job = connection.execute("SELECT id,raw_message_id FROM processing_jobs WHERE company_id=? LIMIT 1", (company_id,)).fetchone()
        connection.execute("INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)", ("processing-review", "processing_failed", "processing_job", job["id"], "{}", db.utc_now()))
        if job["raw_message_id"]:
            connection.execute("INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)", ("raw-review", "processing_failed", "processing_job", job["raw_message_id"], "{}", db.utc_now()))
        connection.execute("UPDATE processing_jobs SET status='running' WHERE id=?", (job["id"],))
    with pytest.raises(CompanyManagementConflict):
        delete_company_records([company_id])
    assert db.one("SELECT id FROM companies WHERE id=?", (company_id,))
    with db.connect() as connection:
        connection.execute("UPDATE processing_jobs SET status='pending' WHERE company_id=?", (company_id,))

    deleted = delete_company_records([company_id])
    assert deleted["status"] == "deleted"
    for table, column in (
        ("companies", "id"), ("jobs", "company_id"), ("recruitment_batches", "company_id"),
        ("recruitment_shared_details", "company_id"), ("recruitment_events", "company_id"),
        ("evidences", "company_id"), ("processing_jobs", "company_id"),
    ):
        assert db.one(f"SELECT COUNT(*) AS count FROM {table} WHERE {column}=?", (company_id,))["count"] == 0
    assert db.one("SELECT COUNT(*) AS count FROM search_index WHERE entity_id=?", (company_id,))["count"] == 0
    assert db.one("SELECT COUNT(*) AS count FROM review_items WHERE id IN ('processing-review','raw-review')")["count"] == 0


def test_company_management_queues_independent_operations_and_worker_executes_them(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    monkeypatch.setattr(catalog, "_polish_company_merge_content", lambda rows: (None, {"status": "fallback", "reason": "test"}))
    first = apply_model_item(_company_payload("队列主企业", "队列主企业有限公司", summary="主摘要", website="https://queue-primary.example", event_title="主宣讲会"), None, "2026-09-06T00:00:00+00:00")
    second = apply_model_item(_company_payload("队列补充企业", "队列补充企业有限公司", summary="补充摘要", website="https://queue-supplement.example", event_title="补充宣讲会"), None, "2026-09-06T00:00:00+00:00")
    third = apply_model_item(_company_payload("队列删除企业", "队列删除企业有限公司", summary="删除摘要", website="https://queue-delete.example", event_title="删除宣讲会"), None, "2026-09-06T00:00:00+00:00")
    primary_id, supplement_id = first["company_ids"][0], second["company_ids"][0]
    delete_id = third["company_ids"][0]

    queued_merge = queue_company_management([primary_id, supplement_id], "merge")
    queued_delete = queue_company_management([delete_id], "delete")
    assert queued_merge["status"] == "queued"
    assert queued_delete["status"] == "queued"
    assert db.one("SELECT status,stage FROM processing_jobs WHERE id=?", (queued_merge["job_id"],))["stage"] == "merge_queued"
    assert json.loads(db.one("SELECT payload_json FROM processing_jobs WHERE id=?", (queued_merge["job_id"],))["payload_json"])["company_names"] == ["队列主企业", "队列补充企业"]

    merged = process_one_job(prefer_enrichment=True)
    assert merged and merged["status"] == "succeeded"
    assert db.one("SELECT status FROM processing_jobs WHERE id=?", (queued_merge["job_id"],))["status"] == "succeeded"
    assert db.one("SELECT id FROM companies WHERE id=?", (supplement_id,)) is None

    deleted = process_one_job(prefer_enrichment=True)
    assert deleted and deleted["status"] == "succeeded"
    assert db.one("SELECT status FROM processing_jobs WHERE id=?", (queued_delete["job_id"],))["status"] == "succeeded"
    assert db.one("SELECT id FROM companies WHERE id=?", (delete_id,)) is None


def test_recruitment_event_state_and_api_sort_ignore_legacy_status(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert recruitment_event_state({"start_at": now.isoformat(), "end_at": now.isoformat(), "timezone": "UTC"}, now) == "ongoing"
    assert recruitment_event_state({"start_at": now.isoformat(), "timezone": "UTC"}, now) == "historical"
    assert recruitment_event_state({"start_at": None, "status": "upcoming", "timezone": "UTC"}, now) == "uncertain"
    with db.connect() as connection:
        connection.execute("INSERT INTO companies(id,display_name,created_at,updated_at) VALUES(?,?,?,?)", ("timeline-company", "时间轴企业", db.utc_now(), db.utc_now()))
        events = [
            ("ongoing", now - timedelta(hours=1), now + timedelta(hours=1), "upcoming"),
            ("upcoming", now + timedelta(hours=2), now + timedelta(hours=3), "historical"),
            ("historical", now - timedelta(hours=4), now - timedelta(hours=3), "ongoing"),
        ]
        for event_id, start_at, end_at, legacy_status in events:
            connection.execute(
                """INSERT INTO recruitment_events(id,company_id,title,event_type,start_at,end_at,timezone,format,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, "timeline-company", event_id, "presentation", start_at.isoformat(), end_at.isoformat(), "UTC", "online", legacy_status, db.utc_now(), db.utc_now()),
            )
    listed = recruitment_events(_={})
    assert [event["id"] for event in listed] == ["ongoing", "upcoming", "historical"]
    assert [event["status"] for event in listed] == ["ongoing", "upcoming", "historical"]


def test_company_management_api_enqueues_merge_and_delete_tasks(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    first = apply_model_item(_company_payload("接口主企业", "接口主企业有限公司", summary="主摘要", website="https://api-primary.example", event_title="主宣讲会"), None, "2026-09-06T00:00:00+00:00")
    second = apply_model_item(_company_payload("接口补充企业", "接口补充企业有限公司", summary="补充摘要", website="https://api-supplement.example", event_title="补充宣讲会"), None, "2026-09-06T00:00:00+00:00")
    third = apply_model_item(_company_payload("接口删除企业", "接口删除企业有限公司", summary="删除摘要", website="https://api-delete.example", event_title="删除宣讲会"), None, "2026-09-06T00:00:00+00:00")
    primary_id, supplement_id = first["company_ids"][0], second["company_ids"][0]
    delete_id = third["company_ids"][0]
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50122)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        merge_response = client.post("/api/v1/admin/companies/merge", json={"ids": [primary_id, supplement_id]})
        delete_response = client.request("DELETE", "/api/v1/admin/companies", json={"ids": [delete_id]})

        assert merge_response.status_code == 200
        assert merge_response.json()["status"] == "queued"
        assert delete_response.status_code == 200
        assert delete_response.json()["status"] == "queued"
        queue = client.get("/api/v1/admin/processing-queue").json()

    merge_item = next(item for item in queue["items"] if item["id"] == merge_response.json()["job_id"])
    delete_item = next(item for item in queue["items"] if item["id"] == delete_response.json()["job_id"])
    assert merge_item["kind"] == "merge_company"
    assert merge_item["task_payload"]["company_names"] == ["接口主企业", "接口补充企业"]
    assert delete_item["kind"] == "delete_company"
    assert delete_item["text_preview"] == "待删除企业：接口删除企业"


def test_processing_queue_returns_original_text_and_fallback(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50120)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        raw_id = client.post("/api/v1/imports/text", json={"text": "当前处理文本"}).json()["raw_message_id"]
        with db.connect() as connection:
            connection.execute("UPDATE raw_messages SET text_content=?,metadata_json=? WHERE id=?", ("OCR/处理后的文本", json.dumps({"_original_text_content": "原始聊天内容"}, ensure_ascii=False), raw_id))
        queue = client.get("/api/v1/admin/processing-queue").json()
        item = next(item for item in queue["items"] if item["raw_message_id"] == raw_id)
        assert item["original_text"] == "原始聊天内容"
        with db.connect() as connection:
            connection.execute("UPDATE raw_messages SET metadata_json=? WHERE id=?", ("{}", raw_id))
        queue = client.get("/api/v1/admin/processing-queue").json()
        item = next(item for item in queue["items"] if item["raw_message_id"] == raw_id)
        assert item["original_text"] == "OCR/处理后的文本"


def test_processing_queue_groups_followups_under_source_task_and_repairs_legacy_parent(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from app.processing import ingest_message

    raw_id = ingest_message({"id": "queue-source", "type": "普通文本", "text": "原始聊天招聘正文"}, "tracememo", "group-1")
    classify_id = db.one("SELECT id FROM processing_jobs WHERE kind='classify' AND raw_message_id=?", (raw_id,))["id"]
    seeded = apply_model_item(_company_payload("队列关联科技", "队列关联科技有限公司", summary="企业摘要", website="https://queue-linked.example", event_title="关联宣讲会"), raw_id, "2026-09-06T00:00:00+00:00")
    company_id = seeded["company_ids"][0]
    with db.connect() as connection:
        connection.execute("UPDATE raw_messages SET text_content=?,metadata_json=? WHERE id=?", ("处理后的提取正文", json.dumps({"_original_text_content": "原始聊天招聘正文"}, ensure_ascii=False), raw_id))
        connection.execute("UPDATE processing_jobs SET parent_job_id=NULL WHERE kind IN ('consolidate_company','research_company') AND company_id=?", (company_id,))
    db.init_db()

    with TestClient(app, client=("127.0.0.1", 50123)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        queue = client.get("/api/v1/admin/processing-queue").json()

    assert queue["total"] == 1
    assert queue["job_total"] == 3
    assert len(queue["items"]) == 1
    item = queue["items"][0]
    assert item["id"] == classify_id
    assert item["original_text"] == "原始聊天招聘正文"
    assert {subtask["kind"] for subtask in item["subtasks"]} == {"consolidate_company", "research_company"}
    assert all(subtask["parent_job_id"] == classify_id for subtask in item["subtasks"])


def test_company_management_endpoints_require_admin(tmp_path, monkeypatch):
    configure_test_db(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50121)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        assert client.post(
            "/api/v1/admin/invitations",
            json={"email": "member@example.com", "role": "member", "password": "MemberPass123!"},
        ).status_code == 200
        assert client.post("/api/v1/auth/logout").status_code == 200
        assert client.post("/api/v1/auth/login", json={"email": "member@example.com", "password": "MemberPass123!"}).status_code == 200
        assert client.post("/api/v1/admin/companies/impact", json={"operation": "merge", "ids": ["a", "b"]}).status_code == 403
        assert client.post("/api/v1/admin/companies/merge", json={"ids": ["a", "b"]}).status_code == 403
        assert client.request("DELETE", "/api/v1/admin/companies", json={"ids": ["a"]}).status_code == 403
