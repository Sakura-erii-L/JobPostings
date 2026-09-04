import sqlite3

from app import db
from app.model_provider import ModelResult
from app.processing import ingest_message, process_one_batch


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
