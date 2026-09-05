import json
import sqlite3
from pathlib import Path

import pytest

from app import db
from app.maintenance import repair_raw_message_times, repair_source_urls, repair_tracememo_file_attachments, reset_recruitment_data
from app.model_provider import ModelResult, classify_messages
from app.catalog import apply_model_item
from app.processing import _claim_one, _codex_extract, _extract_source_text, _fail, import_file, ingest_message, log_processing, process_one_batch
import app.tracememo as tracememo


def _classification_payload(company_name: str | None, jobs: list[dict] | None = None, *, is_recruitment: bool = True) -> dict:
    if not is_recruitment:
        return {"is_recruitment": False, "decision_reason": "正文不是招聘信息", "companies": []}
    company = {"display_name": company_name or "", "legal_name": None, "industry_codes": ["ai_data"]}
    return {
        "is_recruitment": True,
        "decision_reason": "正文明确包含招聘信息",
        "companies": [{
            "company": company,
            "recruitment": {
                "batch": {"name": "2026 春招", "recruitment_type": "campus"},
                "shared_details": {"locations": [], "salary": None},
                "jobs": jobs or [],
                "events": [],
            },
        }],
    }


def _run_classification(tmp_path, monkeypatch, payload: dict, text: str = "招聘信息") -> tuple[dict, str]:
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    raw_id = ingest_message({"id": f"result-{len(text)}", "type": "text", "text": text}, "manual", None)
    assert raw_id

    def fake_classify(messages, job_id=""):
        return ModelResult(payload=payload, input_tokens=1, output_tokens=1, estimated=True, provider="fake", model="fake")

    monkeypatch.setattr("app.processing.classify_messages", fake_classify)
    result = process_one_batch()
    assert result and result["processed"] == 1
    return result["results"][0], raw_id


def test_init_db_migrates_legacy_processing_jobs_before_index(tmp_path, monkeypatch):
    database = tmp_path / "data" / "jobpostings.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE processing_jobs (
               id TEXT PRIMARY KEY, kind TEXT NOT NULL, raw_message_id TEXT, status TEXT NOT NULL,
               attempts INTEGER NOT NULL DEFAULT 0, lease_until TEXT, error TEXT,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", database)
    db.init_db()
    columns = {row["name"] for row in db.all_rows("PRAGMA table_info(processing_jobs)")}
    assert "next_attempt_at" in columns
    assert db.one("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_processing_jobs_ready'")


def test_repair_tracememo_file_attachment_downloads_parses_and_requeues(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    now = db.utc_now()
    message = {
        "id": "local-file-1",
        "serverId": "server-file-1",
        "type": "file",
        "contentData": {"type": "share", "title": "history.txt"},
    }
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO connectors(id,kind,base_url,enabled,config_json,updated_at) VALUES(?,?,?,?,?,?)",
            ("connector-1", "tracememo", "http://127.0.0.1:6131/api/v1", 1, "{\"token\":\"\"}", now),
        )
        connection.execute(
            "INSERT INTO source_groups(id,connector_id,external_id,name,selected,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("group-1", "connector-1", "room-1", "招聘群", 1, 1, now, now),
        )
        connection.execute(
            """INSERT INTO raw_messages(
               id,connector_id,source_group_id,external_message_id,sender,sent_at,message_type,text_content,
               metadata_json,content_hash,is_recruitment,recognition_status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("raw-1", "connector-1", "group-1", "local-file-1", "", now, "file", "history.txt", json.dumps(message, ensure_ascii=False), "hash-1", 0, "succeeded", now),
        )
        connection.execute(
            "INSERT INTO processing_jobs(id,kind,raw_message_id,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("job-1", "classify", "raw-1", "succeeded", "completed", now, now),
        )
        connection.execute(
            """INSERT INTO tracememo_message_cache(
               id,connector_id,source_group_id,external_message_id,content_hash,source_time,message_json,
               first_fetched_at,last_fetched_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("cache-1", "connector-1", "group-1", "local-file-1", "cache-hash-1", now, json.dumps(message, ensure_ascii=False), now, now, now),
        )

    class FakeClient:
        def __init__(self, base_url, token):
            self.references = []

        def media(self, reference):
            self.references.append(reference)
            assert reference == "server-file-1"
            return "招聘岗位：历史测试工程师".encode("utf-8"), None

    monkeypatch.setattr(tracememo, "TraceMemoClient", FakeClient)
    monkeypatch.setattr("app.maintenance._create_safety_backup", lambda: "backup.db")

    result = repair_tracememo_file_attachments()

    assert result["status"] == "completed"
    assert result["media_attached"] == 1
    assert result["requeued"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM artifacts WHERE raw_message_id='raw-1'")["count"] == 1
    assert "历史测试工程师" in db.one("SELECT text_content FROM raw_messages WHERE id='raw-1'")["text_content"]
    assert db.one("SELECT status FROM processing_jobs WHERE id='job-1'")["status"] == "pending"


def test_init_db_migrates_legacy_raw_message_recognition_columns(tmp_path, monkeypatch):
    database = tmp_path / "data" / "jobpostings.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE raw_messages (
               id TEXT PRIMARY KEY, connector_id TEXT, source_group_id TEXT, external_message_id TEXT,
               sender TEXT, sent_at TEXT, message_type TEXT NOT NULL, text_content TEXT,
               metadata_json TEXT NOT NULL DEFAULT '{}', content_hash TEXT NOT NULL UNIQUE,
               is_recruitment INTEGER, retention_until TEXT, created_at TEXT NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO raw_messages(id,message_type,text_content,metadata_json,content_hash,is_recruitment,created_at) VALUES(?,?,?,?,?,?,?)",
            ("legacy-raw", "text", "旧消息", "{}", "legacy-hash", 0, db.utc_now()),
        )
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", database)
    db.init_db()
    columns = {row["name"] for row in db.all_rows("PRAGMA table_info(raw_messages)")}
    assert {"recognition_status", "recognized_at", "recognition_error"}.issubset(columns)
    assert db.one("SELECT recognition_status FROM raw_messages WHERE id='legacy-raw'")["recognition_status"] == "succeeded"


def test_message_processing_creates_catalog_and_consolidation_job(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    raw_id = ingest_message({"id": "m-1", "type": "text", "text": "测试科技招聘算法工程师，地点南京"}, "tracememo", "group-1")
    assert raw_id

    def fake_classify(messages):
        assert messages[0]["id"] == raw_id
        return ModelResult(
            payload={
                "is_recruitment": True,
                "decision_reason": "正文明确包含招聘岗位",
                "companies": [{
                    "company": {"display_name": "测试科技", "legal_name": None, "industry_codes": ["ai_data"]},
                    "recruitment": {
                        "batch": {"name": "2026 春招", "recruitment_type": "campus"},
                        "shared_details": {"locations": [], "salary": None},
                        "jobs": [{"title": "算法工程师", "locations": ["南京"], "recruitment_type": "campus", "employment_type": "full_time"}],
                        "events": [],
                    },
                }],
            },
            input_tokens=20,
            output_tokens=10,
            estimated=False,
            provider="fake",
            model="fake",
        )

    monkeypatch.setattr("app.processing.classify_messages", fake_classify)
    result = process_one_batch()
    assert result["processed"] == 1
    assert db.one("SELECT is_recruitment FROM raw_messages WHERE id=?", (raw_id,))["is_recruitment"] == 1
    assert db.one("SELECT display_name FROM companies")['display_name'] == "测试科技"
    assert db.one("SELECT canonical_title FROM jobs")['canonical_title'] == "算法工程师"
    assert db.one("SELECT source_type FROM evidences WHERE raw_message_id=?", (raw_id,))["source_type"] == "wechat_group"
    assert db.one("SELECT kind FROM processing_jobs WHERE kind='consolidate_company'")["kind"] == "consolidate_company"


def test_recruitment_without_companies_goes_to_review(tmp_path, monkeypatch):
    result, raw_id = _run_classification(tmp_path, monkeypatch, {
        "is_recruitment": True,
        "decision_reason": "招聘信息但企业未知",
        "companies": [],
    })
    assert result["status"] == "needs_review"
    job = db.one("SELECT status,stage,error,result_json FROM processing_jobs WHERE raw_message_id=?", (raw_id,))
    assert dict(job)["status"] == "needs_review"
    assert dict(job)["stage"] == "review"
    assert dict(job)["error"] == "模型判断为招聘信息，但未产生可持久化企业数据"
    assert db.one("SELECT COUNT(*) AS count FROM companies")["count"] == 0
    review = db.one("SELECT payload_json FROM review_items WHERE entity_type='processing_job'")
    payload = json.loads(review["payload_json"])
    assert payload["model_result"]["companies"] == []
    assert payload["original_message"]["id"] == raw_id
    assert payload["processing_logs"]


def test_recruitment_with_blank_company_name_goes_to_review(tmp_path, monkeypatch):
    result, raw_id = _run_classification(tmp_path, monkeypatch, _classification_payload(""))
    assert result["status"] == "needs_review"
    assert db.one("SELECT status FROM processing_jobs WHERE raw_message_id=?", (raw_id,))["status"] == "needs_review"
    assert db.one("SELECT COUNT(*) AS count FROM companies")["count"] == 0
    queue_result = json.loads(db.one("SELECT result_json FROM processing_jobs WHERE raw_message_id=?", (raw_id,))["result_json"])
    assert queue_result["invalid_company_count"] == 1
    assert queue_result["invalid_company_entries"] == [{"index": 0, "reason": "display_name and legal_name are empty"}]


def test_valid_company_without_jobs_succeeds(tmp_path, monkeypatch):
    result, raw_id = _run_classification(tmp_path, monkeypatch, _classification_payload("无岗位科技", []))
    assert result["status"] == "succeeded"
    assert result["company_count"] == 1
    assert result["job_count"] == 0
    stored = json.loads(db.one("SELECT result_json FROM processing_jobs WHERE raw_message_id=?", (raw_id,))["result_json"])
    assert stored["company_names"] == ["无岗位科技"]
    assert db.one("SELECT COUNT(*) AS count FROM companies")["count"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM jobs")["count"] == 0


def test_valid_company_and_jobs_report_counts(tmp_path, monkeypatch):
    jobs = [
        {"title": "算法工程师", "locations": ["南京"], "recruitment_type": "campus", "employment_type": "full_time"},
        {"title": "测试工程师", "locations": ["上海"], "recruitment_type": "campus", "employment_type": "full_time"},
    ]
    result, _ = _run_classification(tmp_path, monkeypatch, _classification_payload("双岗位科技", jobs))
    assert result["status"] == "succeeded"
    assert result["company_count"] == 1
    assert result["job_count"] == 2
    assert len(result["company_ids"]) == 1
    assert len(result["job_ids"]) == 2


def test_long_text_chunks_merge_anonymous_followup_without_needs_review(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    first = _classification_payload("长文本分块科技", [])
    second = _classification_payload("", [{"title": "后续算法工程师"}])
    payloads = [first, second]
    calls = []

    def fake_classify(messages, job_id=""):
        calls.append(messages[0]["text"])
        return ModelResult(payload=payloads[len(calls) - 1], input_tokens=1, output_tokens=1, estimated=True, provider="fake", model="fake")

    monkeypatch.setattr("app.processing.classify_messages", fake_classify)
    raw_id = ingest_message({"id": "long-source", "type": "text", "text": "企业介绍\n\n" + ("第一段内容 " * 7_000) + "\n\n岗位说明\n" + ("后续内容 " * 5_000)}, "manual", None)
    assert raw_id
    result = process_one_batch()
    assert result and result["results"][0]["status"] == "succeeded"
    assert len(calls) == 2
    assert db.one("SELECT COUNT(*) AS count FROM companies") ["count"] == 1
    assert db.one("SELECT canonical_title FROM jobs") ["canonical_title"] == "后续算法工程师"
    assert db.one("SELECT status FROM processing_jobs WHERE raw_message_id=? AND kind='classify'", (raw_id,))["status"] == "succeeded"


def test_existing_company_is_updated_not_created(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    seeded = apply_model_item(_classification_payload("已有企业科技", []), None, "2026-09-05T00:00:00+00:00")
    assert seeded["created_company_count"] == 1
    result, _ = _run_classification(tmp_path, monkeypatch, _classification_payload("已有企业科技", []), text="第二条招聘信息")
    assert result["status"] == "succeeded"
    assert result["created_company_count"] == 0
    assert result["updated_company_count"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM companies")["count"] == 1


def test_non_recruitment_succeeds_without_company(tmp_path, monkeypatch):
    result, raw_id = _run_classification(tmp_path, monkeypatch, _classification_payload(None, is_recruitment=False))
    assert result == {"status": "succeeded", "is_recruitment": False, "id": result["id"]}
    stored = json.loads(db.one("SELECT result_json FROM processing_jobs WHERE raw_message_id=?", (raw_id,))["result_json"])
    assert stored["is_recruitment"] is False
    assert stored["decision_reason"] == "正文不是招聘信息"
    assert db.one("SELECT COUNT(*) AS count FROM companies")["count"] == 0


def test_ingest_message_updates_external_id_and_requeues_changed_content(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    message = {"id": "m-1", "type": "text", "text": "旧招聘信息"}
    raw_id = ingest_message(message, "tracememo", "group-1")
    assert raw_id
    with db.connect() as connection:
        connection.execute("UPDATE processing_jobs SET status='canceled',stage='canceled' WHERE raw_message_id=?", (raw_id,))

    stats: dict[str, int] = {}
    updated_id = ingest_message({**message, "text": "新招聘信息"}, "tracememo", "group-1", stats)
    assert updated_id == raw_id
    assert stats == {"updated": 1}
    assert db.one("SELECT text_content FROM raw_messages WHERE id=?", (raw_id,))["text_content"] == "新招聘信息"
    assert dict(db.one("SELECT status,stage,cancel_requested FROM processing_jobs WHERE raw_message_id=?", (raw_id,))) == {
        "status": "pending",
        "stage": "queued",
        "cancel_requested": 0,
    }
    assert db.one("SELECT recognition_status FROM raw_messages WHERE id=?", (raw_id,))["recognition_status"] == "pending"

    duplicate_stats: dict[str, int] = {}
    assert ingest_message({**message, "text": "新招聘信息"}, "tracememo", "group-1", duplicate_stats) == raw_id
    assert duplicate_stats == {"duplicates": 1}


def test_recognized_message_is_not_requeued_when_fetched_again(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    message = {"id": "already-recognized", "type": "普通文本", "text": "已识别的招聘信息"}
    raw_id = ingest_message(message, "tracememo", "group-1")
    assert raw_id
    with db.connect() as connection:
        connection.execute(
            "UPDATE processing_jobs SET status='succeeded',stage='completed',finished_at=?,updated_at=? WHERE raw_message_id=?",
            (db.utc_now(), db.utc_now(), raw_id),
        )
        connection.execute("UPDATE raw_messages SET text_content=? WHERE id=?", ("已识别的招聘信息\nOCR 补充文字", raw_id))
        connection.execute(
            "UPDATE raw_messages SET is_recruitment=0,recognition_status='succeeded',recognized_at=? WHERE id=?",
            (db.utc_now(), raw_id),
        )

    stats: dict[str, int] = {}
    assert ingest_message({**message, "datetime": "2026/09/05 10:00:00"}, "tracememo", "group-1", stats) == raw_id
    assert stats == {"recognized_skipped": 1}
    assert db.one("SELECT COUNT(*) AS count FROM processing_jobs WHERE raw_message_id=?", (raw_id,))["count"] == 1
    assert db.one("SELECT recognition_status FROM raw_messages WHERE id=?", (raw_id,))["recognition_status"] == "succeeded"


def test_ingest_uses_trace_datetime_and_does_not_fallback_to_creation_time(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    dated_id = ingest_message({
        "id": "dated-1",
        "type": "普通文本",
        "text": "历史招聘信息",
        "datetime": "2026/8/31 11:16:30",
        "createTime": 1788146190,
    }, "tracememo", "group-1")
    unknown_id = ingest_message({
        "id": "unknown-1",
        "type": "普通文本",
        "text": "没有明确聊天时间",
        "createTime": 1788146190,
    }, "tracememo", "group-1")
    assert db.one("SELECT sent_at FROM raw_messages WHERE id=?", (dated_id,))["sent_at"] == "2026-08-31T03:16:30+00:00"
    assert db.one("SELECT sent_at FROM raw_messages WHERE id=?", (unknown_id,))["sent_at"] is None


def test_repair_raw_message_times_uses_stored_trace_datetime(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    raw_id = ingest_message({
        "id": "repair-1",
        "type": "普通文本",
        "text": "需要校正时间",
        "datetime": "2026/8/31 11:16:30",
        "createTime": 1788146190,
    }, "tracememo", "group-1")
    with db.connect() as connection:
        connection.execute("UPDATE raw_messages SET sent_at=? WHERE id=?", ("2026-09-04T12:00:00+00:00", raw_id))
    result = repair_raw_message_times()
    assert result == {"checked": 1, "updated": 1, "unknown": 0}
    assert db.one("SELECT sent_at FROM raw_messages WHERE id=?", (raw_id,))["sent_at"] == "2026-08-31T03:16:30+00:00"


def test_repair_source_urls_restores_original_links_and_keeps_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    original_url = "https://mp.weixin.qq.com/s/legacy"
    challenge_url = "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=temporary&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Flegacy"
    raw_id = ingest_message({"id": "legacy-link", "type": "article", "text": "公众号文章", "url": original_url}, "manual", None)
    assert raw_id
    with db.connect() as connection:
        connection.execute("UPDATE raw_messages SET metadata_json=? WHERE id=?", (json.dumps({"url": challenge_url}, ensure_ascii=False), raw_id))
        connection.execute(
            "INSERT INTO evidences(id,raw_message_id,source_url,source_type,excerpt,observed_at) VALUES(?,?,?,?,?,?)",
            ("legacy-evidence", raw_id, challenge_url, "public_web", "旧证据", db.utc_now()),
        )

    result = repair_source_urls()
    assert result["raw_messages_updated"] == 1
    assert result["evidences_updated"] == 1
    raw_metadata = json.loads(db.one("SELECT metadata_json FROM raw_messages WHERE id=?", (raw_id,))["metadata_json"])
    assert raw_metadata == {"url": original_url, "source_url": original_url, "resolved_url": challenge_url}
    assert db.one("SELECT source_url FROM evidences WHERE id='legacy-evidence'")["source_url"] == original_url
    assert db.one("SELECT source_type FROM evidences WHERE id='legacy-evidence'")["source_type"] == "wechat_official_account"


def test_system_messages_are_filtered_before_queueing(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    stats: dict[str, int] = {}
    assert ingest_message({"id": "system-1", "type": "公众号链接", "text": '"甲"邀请"乙"加入了群聊', "contentData": {"type": "share", "title": "招聘"}}, "tracememo", "group-1", stats) is None
    assert stats == {"filtered_system": 1}
    assert db.one("SELECT COUNT(*) AS count FROM raw_messages")["count"] == 1
    assert db.one("SELECT recognition_status,is_recruitment FROM raw_messages")["recognition_status"] == "filtered"
    assert db.one("SELECT COUNT(*) AS count FROM processing_jobs")["count"] == 0
    assert db.one("SELECT COUNT(*) AS count FROM processing_jobs")["count"] == 0


def test_link_page_images_are_ocr_and_qr_processed(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    raw_id = ingest_message({
        "id": "link-1",
        "type": "公众号链接",
        "text": "锦浪科技校招",
        "contentData": {"type": "share", "title": "锦浪科技校招", "url": "https://example.com/recruit"},
    }, "tracememo", "group-1")
    assert raw_id
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))

    monkeypatch.setattr("app.processing.fetch_public_url", lambda url: {
        "url": url,
        "title": "锦浪科技校招",
        "text": "网页正文包含招聘岗位和投递说明",
        "content_type": "text/html",
        "images": ["https://example.com/poster.png"],
    })

    class ImageResponse:
        headers = {"content-type": "image/png"}
        content = b"image-bytes"

    monkeypatch.setattr("app.processing.fetch_public_http", lambda url, timeout=30, max_bytes=10 * 1024 * 1024: ImageResponse())
    monkeypatch.setattr("app.processing.attach_artifact", lambda raw_id, filename, data, mime_type=None: {
        "id": "artifact-1",
        "text": "图片 OCR 出招聘工程师",
        "qr_values": ["https://apply.example.com"],
    })

    text, metadata = _extract_source_text(job, raw)
    assert "网页正文" in text
    assert "图片 OCR" in text
    assert metadata["linked_image_urls"] == ["https://example.com/poster.png"]
    assert metadata["qr_values"] == ["https://apply.example.com"]


def test_wechat_browser_rendered_images_are_processed_after_http_challenge(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    original_url = "https://mp.weixin.qq.com/s/browser-link"
    challenge_url = "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=temporary&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fbrowser-link"
    image_url = "https://mmbiz.qpic.cn/mmbiz_png/example/poster.png"
    raw_id = ingest_message({
        "id": "wechat-browser-1",
        "type": "公众号链接",
        "text": "公众号招聘",
        "contentData": {"type": "share", "title": "公众号招聘", "url": original_url},
    }, "tracememo", "group-1")
    assert raw_id
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    monkeypatch.setattr("app.processing.fetch_public_url", lambda url: {
        "url": challenge_url,
        "text": "",
        "content_type": "text/html",
        "images": [],
        "access_challenge": True,
        "access_error": "微信返回环境验证页面",
    })
    monkeypatch.setattr("app.processing.fetch_public_browser", lambda url: {
        "url": original_url,
        "title": "公众号招聘正文",
        "text": "",
        "content_type": "text/html; charset=UTF-8",
        "images": [image_url],
        "image_data": [{"url": image_url, "data": b"browser-image", "content_type": "image/png"}],
        "screenshot_data": b"browser-screenshot",
        "browser_rendered": True,
        "browser_image_count": 1,
        "browser_loaded_image_count": 1,
        "browser_downloaded_image_count": 0,
        "browser_article_text_chars": 0,
        "access_challenge": False,
    })
    artifact_calls: list[str] = []

    def fake_attach(raw_message_id, filename, data, mime_type=None):
        artifact_calls.append(filename)
        return {"id": f"artifact-{len(artifact_calls)}", "text": "图片 OCR 招聘正文", "qr_values": ["https://apply.example.com"]}

    monkeypatch.setattr("app.processing.attach_artifact", fake_attach)
    monkeypatch.setattr("app.processing.fetch_public_http", lambda *args, **kwargs: pytest.fail("公众号不应下载单张原图"))
    monkeypatch.setattr("app.processing._codex_extract", lambda *args, **kwargs: "图片 OCR 招聘正文")
    text, metadata = _extract_source_text(job, raw)
    assert "图片 OCR 招聘正文" in text
    assert metadata["browser_attempted"] is True
    assert metadata["browser_rendered"] is True
    assert metadata["browser_loaded_image_count"] == 1
    assert metadata["browser_downloaded_image_count"] == 0
    assert metadata["browser_screenshot_captured"] is True
    assert metadata["source_url"] == original_url
    assert metadata["resolved_url"] == challenge_url
    assert metadata["web_access_status"] == "ok"
    assert metadata["linked_image_urls"] == [image_url]
    assert metadata["qr_values"] == ["https://apply.example.com"]
    assert artifact_calls == ["webpage-screenshot.jpg"]


def test_browser_screenshot_is_used_when_image_ocr_has_no_text(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    original_url = "https://mp.weixin.qq.com/s/browser-screenshot"
    raw_id = ingest_message({
        "id": "wechat-browser-screenshot",
        "type": "公众号链接",
        "text": "图片招聘",
        "contentData": {"type": "share", "title": "图片招聘", "url": original_url},
    }, "tracememo", "group-1")
    assert raw_id
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    monkeypatch.setattr("app.processing.fetch_public_url", lambda url: {
        "url": url,
        "text": "",
        "content_type": "text/html",
        "images": [],
    })
    monkeypatch.setattr("app.processing.fetch_public_browser", lambda url: {
        "url": url,
        "title": "图片招聘",
        "text": "",
        "content_type": "text/html",
        "images": [],
        "image_data": [],
        "screenshot_data": b"browser-screenshot",
        "browser_rendered": True,
        "browser_image_count": 0,
        "browser_loaded_image_count": 0,
        "browser_downloaded_image_count": 0,
        "browser_article_text_chars": 0,
    })
    artifact_calls: list[str] = []

    def fake_attach(raw_message_id, filename, data, mime_type=None):
        artifact_calls.append(filename)
        return {"id": "screenshot-artifact", "text": "截图 OCR 招聘正文包含更多岗位信息", "qr_values": ["https://qr.example.com"]}

    monkeypatch.setattr("app.processing.attach_artifact", fake_attach)
    text, metadata = _extract_source_text(job, raw)
    assert text == "图片招聘\n截图 OCR 招聘正文包含更多岗位信息"
    assert metadata["browser_screenshot_ocr"] is True
    assert metadata["artifact_id"] == "screenshot-artifact"
    assert metadata["qr_values"] == ["https://qr.example.com"]
    assert artifact_calls == ["webpage-screenshot.jpg"]


def test_wechat_codex_ocr_uses_only_full_page_jpg_screenshot(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    url = "https://mp.weixin.qq.com/s/full-page-jpg"
    raw_id = ingest_message({
        "id": "wechat-full-page-jpg",
        "type": "公众号链接",
        "text": "公众号招聘",
        "contentData": {"type": "share", "title": "公众号招聘", "url": url},
    }, "tracememo", "group-1")
    assert raw_id
    screenshot = tmp_path / "screenshot-blob"
    poster = tmp_path / "poster-blob"
    screenshot.write_bytes(b"jpg-screenshot")
    poster.write_bytes(b"png-poster")
    now = db.utc_now()
    with db.connect() as connection:
        connection.executemany(
            "INSERT INTO artifacts(id,raw_message_id,sha256,path,filename,mime_type,byte_size,ocr_text,qr_values_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                ("poster-artifact", raw_id, "poster-sha", str(poster), "linked-image-1.png", "image/png", 10, "", "[]", now),
                ("screenshot-artifact", raw_id, "screenshot-sha", str(screenshot), "webpage-screenshot.jpg", "image/jpeg", 14, "", "[]", now),
            ],
        )
    captured: dict[str, object] = {}

    def fake_codex(task, payload, schema, *, job_id, image_paths=None, enable_web=False):
        captured["image_paths"] = list(image_paths or [])
        captured["payload"] = payload
        captured["enable_web"] = enable_web
        return {"text": "长截图 OCR 正文", "source_url": "", "notes": ""}

    monkeypatch.setattr("app.codex_agent.run_codex_json", fake_codex)
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    metadata = {"source_url": url, "url": url, "browser_rendered": True, "browser_screenshot_artifact_id": "screenshot-artifact"}

    assert _codex_extract(job, raw, metadata, "图片是主要来源内容", primary_ocr=True) == "长截图 OCR 正文"
    assert captured["image_paths"] == [str(screenshot)]
    assert captured["payload"]["image_count"] == 1
    assert captured["payload"]["image_order"] == ["webpage-screenshot.jpg"]
    assert captured["enable_web"] is False


def test_wechat_environment_challenge_uses_codex_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    original_url = "https://mp.weixin.qq.com/s/example"
    challenge_url = "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=temporary&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fexample"
    raw_id = ingest_message({
        "id": "wechat-link-1",
        "type": "公众号链接",
        "text": "锦浪科技校招",
        "contentData": {"type": "share", "title": "锦浪科技校招", "url": original_url},
    }, "tracememo", "group-1")
    assert raw_id
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    monkeypatch.setattr("app.processing.fetch_public_url", lambda url: {
        "url": challenge_url,
        "text": "",
        "content_type": "text/html",
        "images": [],
        "access_challenge": True,
        "access_error": "微信返回环境验证页面",
    })
    monkeypatch.setattr("app.processing.fetch_public_browser", lambda url: {
        "url": url,
        "text": "",
        "content_type": "text/html",
        "images": [],
        "image_data": [],
        "screenshot_data": b"challenge-screenshot",
        "browser_rendered": True,
    })
    captured: dict[str, object] = {}

    def fake_codex(job_value, raw_value, metadata, reason, *, primary_ocr=False):
        captured["reason"] = reason
        captured["metadata"] = metadata.copy()
        captured["primary_ocr"] = primary_ocr
        return "通过 Codex 获取的真实招聘正文"

    monkeypatch.setattr("app.processing._codex_extract", fake_codex)
    text, metadata = _extract_source_text(job, raw)
    assert "通过 Codex 获取的真实招聘正文" in text
    assert "当前环境异常" not in text
    assert metadata["web_access_status"] == "challenge"
    assert metadata["web_access_error"] == "微信返回环境验证页面"
    assert metadata["source_url"] == original_url
    assert metadata["url"] == original_url
    assert metadata["resolved_url"] == challenge_url
    stored_metadata = json.loads(db.one("SELECT metadata_json FROM raw_messages WHERE id=?", (raw_id,))["metadata_json"])
    assert stored_metadata["source_url"] == original_url
    assert stored_metadata["resolved_url"] == challenge_url
    assert captured["reason"] == "公众号页面返回微信环境验证页"


def test_plain_text_is_sent_directly_even_when_short(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    raw_id = ingest_message({"id": "text-1", "type": "普通文本", "text": "短招聘"}, "tracememo", "group-1")
    assert raw_id
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    monkeypatch.setattr("app.processing._codex_extract", lambda *args: (_ for _ in ()).throw(AssertionError("plain text must not use Codex fallback")))
    text, _ = _extract_source_text(job, raw)
    assert text == "短招聘"


def test_image_ocr_prefers_codex_over_local_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    imported = import_file("poster.png", b"image-bytes", "image/png")
    raw_id = imported["raw_message_id"]
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    calls: list[dict[str, object]] = []

    def fake_codex(job_value, raw_value, metadata, reason, *, primary_ocr=False):
        calls.append({"reason": reason, "primary_ocr": primary_ocr})
        metadata["codex_ocr"] = True
        return "Codex OCR 招聘正文"

    monkeypatch.setattr("app.processing._codex_extract", fake_codex)
    monkeypatch.setattr("app.processing._local_ocr_extract", lambda raw_value: (_ for _ in ()).throw(AssertionError("local OCR must wait for Codex")))
    text, metadata = _extract_source_text(job, raw)

    assert text == "Codex OCR 招聘正文"
    assert metadata["codex_ocr"] is True
    assert calls == [{"reason": "图片是主要来源内容", "primary_ocr": True}]


def test_image_ocr_local_fallback_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    imported = import_file("poster.png", b"image-bytes", "image/png")
    raw_id = imported["raw_message_id"]
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    monkeypatch.setattr("app.processing._codex_extract", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Codex unavailable")))
    monkeypatch.setattr("app.processing._local_ocr_extract", lambda raw_value: (_ for _ in ()).throw(AssertionError("local OCR fallback must be disabled by default")))

    with pytest.raises(RuntimeError, match="local OCR fallback is disabled"):
        _extract_source_text(job, raw)


def test_image_ocr_uses_local_fallback_only_after_codex_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    imported = import_file("poster.png", b"image-bytes", "image/png")
    raw_id = imported["raw_message_id"]
    with db.connect() as connection:
        connection.execute("UPDATE raw_messages SET text_content=? WHERE id=?", ("图片招聘说明", raw_id))
        connection.execute(
            "UPDATE system_settings SET value_json=? WHERE key='local_ocr_fallback_enabled'",
            ("true",),
        )
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    monkeypatch.setattr("app.processing._codex_extract", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Codex unavailable")))
    monkeypatch.setattr("app.processing._local_ocr_extract", lambda raw_value, metadata=None: ("RapidOCR 兜底文字", ["local test error"]))
    text, metadata = _extract_source_text(job, raw)

    assert text == "图片招聘说明\nRapidOCR 兜底文字"
    assert metadata["local_ocr_fallback"] is True
    assert metadata["local_ocr_errors"] == ["local test error"]


def test_timeout_failure_uses_existing_retry_mechanism(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    raw_id = ingest_message({"id": "timeout-1", "type": "普通文本", "text": "招聘信息"}, "manual", None)
    job = _claim_one()
    assert job and job["raw_message_id"] == raw_id

    result = _fail(job, TimeoutError("Local Codex timed out after 600 seconds"))

    assert result["status"] == "retry_wait"
    assert db.one("SELECT status,stage,error FROM processing_jobs WHERE id=?", (job["id"],))["status"] == "pending"
    assert db.one("SELECT stage,error FROM processing_jobs WHERE id=?", (job["id"],))["stage"] == "retry_wait"


def test_enrichment_jobs_are_limited_to_one_running_job_per_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    now = db.utc_now()
    with db.connect() as connection:
        connection.executemany(
            "INSERT INTO processing_jobs(id,kind,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            [
                ("consolidate-1", "consolidate_company", "pending", now, now),
                ("consolidate-2", "consolidate_company", "pending", now, now),
                ("research-1", "research_company", "pending", now, now),
                ("classify-1", "classify", "pending", now, now),
            ],
        )

    claimed = [_claim_one(prefer_enrichment=True) for _ in range(4)]

    assert [job["kind"] for job in claimed if job] == ["consolidate_company", "research_company", "classify"]
    with db.connect() as connection:
        connection.execute("UPDATE processing_jobs SET status='succeeded' WHERE id='consolidate-1'")
    assert _claim_one(prefer_enrichment=True)["kind"] == "consolidate_company"


def test_multiple_page_images_use_one_ordered_codex_ocr_and_retry_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    raw_id = ingest_message({"id": "multi-image", "type": "普通文本", "text": "多图招聘"}, "manual", None)
    assert raw_id
    image_two = tmp_path / "linked-image-2.png"
    image_one = tmp_path / "linked-image-1.png"
    image_two.write_bytes(b"second-image")
    image_one.write_bytes(b"first-image")
    with db.connect() as connection:
        now = db.utc_now()
        connection.execute("INSERT INTO artifacts(id,raw_message_id,sha256,path,filename,mime_type,byte_size,ocr_text,qr_values_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("image-2", raw_id, "sha-2", str(image_two), image_two.name, "image/png", 12, "", "[]", now))
        connection.execute("INSERT INTO artifacts(id,raw_message_id,sha256,path,filename,mime_type,byte_size,ocr_text,qr_values_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("image-1", raw_id, "sha-1", str(image_one), image_one.name, "image/png", 11, "", "[]", now))
    calls: list[list[str]] = []

    def fake_codex(task, payload, schema, *, job_id, image_paths=None, enable_web=False):
        calls.append([Path(path).name for path in image_paths or []])
        return {"text": "一次性 OCR 完整正文", "source_url": "", "notes": ""}

    monkeypatch.setattr("app.codex_agent.run_codex_json", fake_codex)
    job = dict(db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,)))
    raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    first_text, first_metadata = _extract_source_text(job, raw)
    refreshed_raw = dict(db.one("SELECT * FROM raw_messages WHERE id=?", (raw_id,)))
    second_text, second_metadata = _extract_source_text(job, refreshed_raw)

    assert calls == [["linked-image-1.png", "linked-image-2.png"]]
    assert first_text == second_text == "一次性 OCR 完整正文"
    assert first_metadata["codex_ocr_image_count"] == 2
    assert second_metadata["codex_ocr_complete"] is True


def test_failed_job_review_keeps_error_original_message_and_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    raw_id = ingest_message({"id": "failed-1", "type": "普通文本", "text": "完整的原始招聘正文"}, "tracememo", "group-1")
    assert raw_id
    job_row = db.one("SELECT * FROM processing_jobs WHERE raw_message_id=?", (raw_id,))
    assert job_row
    with db.connect() as connection:
        connection.execute("UPDATE processing_jobs SET status='running',attempts=3,stage='classifying' WHERE id=?", (job_row["id"],))
    log_processing(job_row["id"], "classifying", "识别开始")
    result = _fail(dict(db.one("SELECT * FROM processing_jobs WHERE id=?", (job_row["id"],))), RuntimeError("模型返回无效 JSON"))
    assert result["status"] == "needs_review"
    assert db.one("SELECT recognition_status,recognition_error FROM raw_messages WHERE id=?", (raw_id,))["recognition_status"] == "needs_review"
    assert db.one("SELECT recognition_error FROM raw_messages WHERE id=?", (raw_id,))["recognition_error"] == "模型返回无效 JSON"
    review = db.one("SELECT payload_json FROM review_items WHERE entity_type='processing_job'")
    assert review
    payload = json.loads(review["payload_json"])
    assert payload["error"] == {"type": "RuntimeError", "message": "模型返回无效 JSON"}
    assert payload["original_message"]["original_text_content"] == "完整的原始招聘正文"
    assert payload["processing_logs"][0]["message"] == "识别开始"


def test_classification_prompt_excludes_chat_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    captured: dict[str, list[dict[str, str]]] = {}

    def fake_engine(messages, task_type, schema, *, job_id):
        captured["messages"] = messages
        return ModelResult({"items": []}, 1, 1, True, "fake", "fake")

    monkeypatch.setattr("app.model_provider._call_processing_engine", fake_engine)
    classify_messages([{
        "id": "raw-id",
        "text": "招聘算法工程师",
        "sent_at": "2026-09-04T00:00:00+00:00",
        "message_type": "普通文本",
        "metadata": {"name": "群成员姓名", "senderId": "member-id", "sessionId": "group-id"},
    }])
    prompt = json.loads(captured["messages"][1]["content"])
    assert prompt["messages"] == [{"message_id": "item_1", "text": "招聘算法工程师", "source_datetime": "2026-09-04T00:00:00+00:00"}]
    assert "群成员姓名" not in captured["messages"][1]["content"]
    assert "member-id" not in captured["messages"][1]["content"]


def test_forced_reset_creates_backup_and_preserves_source_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    with db.connect() as connection:
        connection.execute("INSERT INTO connectors(id,kind,base_url,enabled,config_json,updated_at) VALUES(?,?,?,?,?,?)", ("trace", "tracememo", "http://trace", 1, "{}", db.utc_now()))
        connection.execute("INSERT INTO source_groups(id,connector_id,external_id,name,selected,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("group", "trace", "room", "招聘群", 1, 1, db.utc_now(), db.utc_now()))
    raw_id = ingest_message({"id": "reset-1", "type": "普通文本", "text": "旧招聘信息"}, "trace", "group")
    assert raw_id

    result = reset_recruitment_data()
    assert result["backup_path"]
    assert __import__("pathlib").Path(result["backup_path"]).exists()
    assert db.one("SELECT COUNT(*) AS count FROM raw_messages")["count"] == 0
    assert db.one("SELECT COUNT(*) AS count FROM processing_jobs")["count"] == 0
    assert db.one("SELECT id FROM connectors WHERE id='trace'")
    assert db.one("SELECT selected FROM source_groups WHERE id='group'")["selected"] == 1
    assert db.one("SELECT state FROM queue_control WHERE id=1")["state"] == "paused"
