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
