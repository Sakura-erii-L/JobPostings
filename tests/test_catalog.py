import json
import tempfile
from pathlib import Path

from app import db
from app.catalog import apply_model_item, normalize_name, normalize_title, refresh_expiration


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

