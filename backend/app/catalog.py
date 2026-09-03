from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .db import connect, one, utc_now


INDUSTRIES = {
    "internet_software", "ai_data", "electronics_semiconductor", "telecommunications",
    "manufacturing_automation", "automotive_transport_equipment", "energy_chemical_materials",
    "construction_real_estate", "finance", "consumer_retail_ecommerce", "healthcare_biopharma",
    "education_research", "media_culture_entertainment", "logistics_transportation",
    "professional_services", "government_public_nonprofit", "agriculture", "other",
}
JOB_FUNCTIONS = {
    "software_engineering", "hardware_engineering", "ai_data", "product_design", "testing_quality",
    "it_operations_security", "production_supply_chain", "sales_business_development",
    "marketing_content", "operations_customer_service", "finance_audit", "hr_admin_legal",
    "consulting_research", "healthcare", "education", "construction_engineering", "other",
}
RECRUITMENT_TYPES = {"campus", "social", "internship", "part_time", "labor", "unknown"}


def normalize_name(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[（）()【】\[\]，,。·•\s]", "", value)
    return value


def normalize_title(value: str) -> str:
    value = value.lower().strip()
    return re.sub(r"[（）()【】\[\]，,。·•\s]", "", value)


def json_text(value: Any, default: Any) -> str:
    return json.dumps(value if value is not None else default, ensure_ascii=False)


def _company_for(connection, company_data: dict[str, Any]) -> str:
    display_name = str(company_data.get("display_name") or "未识别企业").strip()
    legal_name = str(company_data.get("legal_name") or "").strip() or None
    aliases = company_data.get("aliases") or []
    candidates = [display_name, legal_name, *aliases]
    existing = None
    for candidate in candidates:
        if candidate:
            existing = connection.execute(
                "SELECT * FROM companies WHERE lower(display_name)=lower(?) OR lower(COALESCE(legal_name,''))=lower(?)",
                (candidate, candidate),
            ).fetchone()
            if existing:
                break
    now = utc_now()
    industries = [x for x in company_data.get("industry_codes", []) if x in INDUSTRIES]
    primary = industries[0] if industries else "other"
    if existing:
        company_id = existing["id"]
        old_aliases = json.loads(existing["aliases_json"])
        merged_aliases = list(dict.fromkeys(old_aliases + [x for x in aliases if x]))
        connection.execute(
            "UPDATE companies SET legal_name=COALESCE(?,legal_name), aliases_json=?, primary_industry=?, updated_at=? WHERE id=?",
            (legal_name, json_text(merged_aliases, []), primary, now, company_id),
        )
        return company_id
    company_id = str(uuid4())
    connection.execute(
        "INSERT INTO companies(id,display_name,legal_name,aliases_json,primary_industry,secondary_industries_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (company_id, display_name, legal_name, json_text(aliases, []), primary, json_text(industries[1:], []), now, now),
    )
    connection.execute("INSERT INTO search_index(entity_type,entity_id,title,body) VALUES('company',?,?,?)", (company_id, display_name, display_name))
    return company_id


def _batch_for(connection, company_id: str, batch: dict[str, Any], recruitment_type: str) -> str | None:
    name = str(batch.get("name") or "未命名批次").strip()
    year = batch.get("year")
    existing = connection.execute(
        "SELECT id FROM recruitment_batches WHERE company_id=? AND name=? AND recruitment_type=?",
        (company_id, name, recruitment_type),
    ).fetchone()
    if existing:
        return existing["id"]
    batch_id = str(uuid4())
    now = utc_now()
    connection.execute(
        "INSERT INTO recruitment_batches(id,company_id,name,year,season,recruitment_type,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (batch_id, company_id, name, year, batch.get("season"), recruitment_type, float(batch.get("confidence", 0)), now, now),
    )
    return batch_id


def _make_job(connection, company_id: str, batch_id: str | None, job_data: dict[str, Any], observed_at: str, raw_message_id: str | None) -> str:
    title = str(job_data.get("title") or "未命名岗位").strip()
    normalized = normalize_title(title)
    recruitment_type = str(job_data.get("recruitment_type") or "unknown")
    if recruitment_type not in RECRUITMENT_TYPES:
        recruitment_type = "unknown"
    employment_type = str(job_data.get("employment_type") or "unknown")
    locations = job_data.get("locations") or []
    row = connection.execute(
        "SELECT * FROM jobs WHERE company_id=? AND normalized_title=? AND recruitment_type=? AND employment_type=?",
        (company_id, normalized, recruitment_type, employment_type),
    ).fetchone()
    now = utc_now()
    explicit_deadline = job_data.get("deadline") or job_data.get("explicit_deadline")
    payload = dict(job_data)
    content_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if row:
        job_id = row["id"]
        last = observed_at or now
        status = row["status"]
        if explicit_deadline:
            try:
                status = "expired" if datetime.fromisoformat(str(explicit_deadline)).date() < datetime.now().date() else "active"
            except ValueError:
                pass
        connection.execute(
            "UPDATE jobs SET batch_id=COALESCE(?,batch_id), last_effective_posted_at=?, explicit_deadline=COALESCE(?,explicit_deadline), status=?, updated_at=? WHERE id=?",
            (batch_id, last, explicit_deadline, status, now, job_id),
        )
    else:
        job_id = str(uuid4())
        status = "active" if explicit_deadline is None or str(explicit_deadline) >= now[:10] else "expired"
        connection.execute(
            "INSERT INTO jobs(id,company_id,batch_id,canonical_title,normalized_title,department,locations_json,recruitment_type,employment_type,headcount,education_json,majors_json,experience_requirement,salary_json,responsibilities,requirements,benefits_json,application_methods_json,contacts_json,explicit_deadline,effective_posted_at,last_effective_posted_at,status,industry_codes_json,job_function_codes_json,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, company_id, batch_id, title, normalized, job_data.get("department"), json_text(locations, []), recruitment_type, employment_type, job_data.get("headcount"), json_text(job_data.get("education"), []), json_text(job_data.get("majors"), []), job_data.get("experience_requirement"), json_text(job_data.get("salary"), {}), job_data.get("responsibilities"), job_data.get("requirements"), json_text(job_data.get("benefits"), []), json_text(job_data.get("application_methods"), []), json_text(job_data.get("contacts"), []), explicit_deadline, observed_at, observed_at, status, json_text(job_data.get("industry_codes"), []), json_text([x for x in job_data.get("job_function_codes", []) if x in JOB_FUNCTIONS], []), float(job_data.get("confidence", 0)), now, now),
        )
        connection.execute("INSERT INTO search_index(entity_type,entity_id,title,body) VALUES('job',?,?,?)", (job_id, title, f"{title} {job_data.get('requirements','')} {job_data.get('responsibilities','')}"))
    version_id = str(uuid4())
    try:
        connection.execute(
            "INSERT INTO job_versions(id,job_id,raw_json,content_hash,observed_at,is_current) VALUES(?,?,?,?,?,1)",
            (version_id, job_id, json.dumps(payload, ensure_ascii=False), content_hash, observed_at),
        )
        connection.execute("UPDATE job_versions SET is_current=0 WHERE job_id=? AND id<>?", (job_id, version_id))
    except Exception:
        pass
    evidence_id = str(uuid4())
    evidence_raw_message_id = None
    if raw_message_id:
        raw_row = connection.execute("SELECT id FROM raw_messages WHERE id=?", (raw_message_id,)).fetchone()
        evidence_raw_message_id = raw_row["id"] if raw_row else None
    connection.execute(
        "INSERT INTO evidences(id,job_id,raw_message_id,source_type,excerpt,observed_at) VALUES(?,?,?,?,?,?)",
        (evidence_id, job_id, evidence_raw_message_id, "wechat_group", json.dumps(payload, ensure_ascii=False)[:4000], observed_at),
    )
    return job_id


def apply_model_item(item: dict[str, Any], raw_message_id: str | None, observed_at: str | None) -> list[str]:
    if not item.get("is_recruitment"):
        return []
    observed = observed_at or utc_now()
    with connect() as connection:
        company = item.get("company") or {}
        company_id = _company_for(connection, company)
        recruitment_type = str((item.get("batch") or {}).get("recruitment_type") or "unknown")
        if recruitment_type not in RECRUITMENT_TYPES:
            recruitment_type = "unknown"
        batch_id = _batch_for(connection, company_id, item.get("batch") or {}, recruitment_type)
        job_ids = [_make_job(connection, company_id, batch_id, job, observed, raw_message_id) for job in item.get("jobs") or []]
        confidence = float(item.get("confidence", 0))
        if confidence < 0.55:
            connection.execute(
                "INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid4()), "low_confidence_recruitment", "company", company_id, json.dumps(item, ensure_ascii=False), utc_now()),
            )
        return job_ids


def refresh_expiration() -> int:
    days = 45
    row = one("SELECT value_json FROM system_settings WHERE key='possibly_expired_days'")
    if row:
        try:
            days = int(json.loads(row["value_json"]))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status='possibly_expired', updated_at=? WHERE status='active' AND explicit_deadline IS NULL AND last_effective_posted_at<?",
            (utc_now(), cutoff),
        )
        return cursor.rowcount
