import json
import sqlite3

from app import db
from app.maintenance import repair_raw_message_times, reset_recruitment_data
from app.model_provider import ModelResult, classify_messages
from app.processing import _extract_source_text, _fail, ingest_message, log_processing, process_one_batch


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
                "items": [{
                    "message_id": raw_id,
                    "is_recruitment": True,
                    "confidence": 0.95,
                    "company": {"display_name": "测试科技", "industry_codes": ["ai_data"]},
                    "batch": {"name": "2026 春招", "recruitment_type": "campus"},
                    "jobs": [{"title": "算法工程师", "locations": ["南京"], "recruitment_type": "campus", "employment_type": "full_time"}],
                }]
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

    duplicate_stats: dict[str, int] = {}
    assert ingest_message({**message, "text": "新招聘信息"}, "tracememo", "group-1", duplicate_stats) == raw_id
    assert duplicate_stats == {"duplicates": 1}


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


def test_system_messages_are_filtered_before_queueing(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    stats: dict[str, int] = {}
    assert ingest_message({"id": "system-1", "type": "公众号链接", "text": '"甲"邀请"乙"加入了群聊', "contentData": {"type": "share", "title": "招聘"}}, "tracememo", "group-1", stats) is None
    assert stats == {"filtered_system": 1}
    assert db.one("SELECT COUNT(*) AS count FROM raw_messages")["count"] == 0
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
    assert prompt["messages"] == [{"message_id": "item_1", "text": "招聘算法工程师"}]
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
