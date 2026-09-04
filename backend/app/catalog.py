from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .db import connect, one, utc_now
from .parsers import is_link_message


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


def _merge_list(old_value: str | None, new_value: Any) -> list[Any]:
    try:
        old = json.loads(old_value or "[]")
    except json.JSONDecodeError:
        old = []
    incoming = new_value if isinstance(new_value, list) else ([] if new_value in (None, "") else [new_value])
    return list(dict.fromkeys([*old, *incoming]))


def _company_for(connection, company_data: dict[str, Any]) -> str:
    display_name = str(company_data.get("display_name") or "未识别企业").strip()
    legal_name = str(company_data.get("legal_name") or "").strip() or None
    aliases = company_data.get("aliases") or []
    candidates = [display_name, legal_name, *aliases]
    existing = None
    matched_company_id = str(company_data.get("matched_company_id") or "").strip()
    if matched_company_id:
        existing = connection.execute("SELECT * FROM companies WHERE id=?", (matched_company_id,)).fetchone()
    for candidate in candidates:
        if existing:
            break
        if candidate:
            existing = connection.execute(
                "SELECT * FROM companies WHERE lower(display_name)=lower(?) OR lower(COALESCE(legal_name,''))=lower(?)",
                (candidate, candidate),
            ).fetchone()
            if existing:
                break
    now = utc_now()
    if not existing:
        normalized_candidates = {normalize_name(str(value)) for value in candidates if value}
        for row in connection.execute("SELECT * FROM companies").fetchall():
            known_names = [row["display_name"], row["legal_name"], *json.loads(row["aliases_json"] or "[]")]
            if normalized_candidates.intersection(normalize_name(str(value)) for value in known_names if value):
                existing = row
                break
    industries = [x for x in company_data.get("industry_codes", []) if x in INDUSTRIES]
    primary = industries[0] if industries else "other"
    if existing:
        company_id = existing["id"]
        merged_aliases = _merge_list(existing["aliases_json"], [x for x in [display_name, legal_name, *aliases] if x and x not in {existing["display_name"], existing["legal_name"]}])
        merged_businesses = _merge_list(existing["businesses_json"], company_data.get("businesses"))
        merged_highlights = _merge_list(existing["highlights_json"], company_data.get("highlights"))
        merged_channels = _merge_list(existing["official_channels_json"], company_data.get("official_channels"))
        secondary = _merge_list(existing["secondary_industries_json"], industries[1:])
        connection.execute(
            """UPDATE companies SET legal_name=COALESCE(?,legal_name),aliases_json=?,
               primary_industry=CASE WHEN ?='other' THEN primary_industry ELSE ? END,
               secondary_industries_json=?,website=COALESCE(?,website),company_nature=COALESCE(?,company_nature),
               founded_at=COALESCE(?,founded_at),company_size=COALESCE(?,company_size),headquarters=COALESCE(?,headquarters),
               businesses_json=?,highlights_json=?,official_channels_json=?,updated_at=? WHERE id=?""",
            (legal_name, json_text(merged_aliases, []), primary, primary, json_text(secondary, []),
             company_data.get("website") or None, company_data.get("company_nature") or None,
             company_data.get("founded_at") or None, company_data.get("company_size") or None,
             company_data.get("headquarters") or None, json_text(merged_businesses, []),
             json_text(merged_highlights, []), json_text(merged_channels, []), now, company_id),
        )
        return company_id
    company_id = str(uuid4())
    connection.execute(
        """INSERT INTO companies(id,display_name,legal_name,aliases_json,primary_industry,secondary_industries_json,
           website,company_nature,founded_at,company_size,headquarters,businesses_json,highlights_json,official_channels_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (company_id, display_name, legal_name, json_text(aliases, []), primary, json_text(industries[1:], []),
         company_data.get("website") or None, company_data.get("company_nature") or None,
         company_data.get("founded_at") or None, company_data.get("company_size") or None,
         company_data.get("headquarters") or None, json_text(company_data.get("businesses"), []),
         json_text(company_data.get("highlights"), []), json_text(company_data.get("official_channels"), []), now, now),
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


def _record_company_relationship(connection: Any, company_id: str, relationship: dict[str, Any]) -> None:
    related_name = str(relationship.get("related_company_name") or "").strip()
    relation_type = str(relationship.get("type") or "").strip()
    if not related_name or not relation_type:
        return
    related = None
    normalized = normalize_name(related_name)
    for row in connection.execute("SELECT id,display_name,legal_name,aliases_json FROM companies").fetchall():
        names = [row["display_name"], row["legal_name"], *json.loads(row["aliases_json"] or "[]")]
        if normalized in {normalize_name(str(value)) for value in names if value}:
            related = row
            break
    if related:
        related_id = related["id"]
    else:
        related_id = _company_for(connection, {"display_name": related_name, "legal_name": "", "aliases": [], "industry_codes": []})
    if related_id == company_id:
        return
    if relation_type in {"subsidiary_of", "member_of", "brand_of"}:
        parent_id, child_id = related_id, company_id
    else:
        parent_id, child_id = company_id, related_id
    connection.execute(
        "INSERT OR IGNORE INTO company_relations(id,parent_company_id,child_company_id,relation_type,created_at) VALUES(?,?,?,?,?)",
        (str(uuid4()), parent_id, child_id, relation_type, utc_now()),
    )


def _make_job(connection, company_id: str, batch_id: str | None, job_data: dict[str, Any], observed_at: str, raw_message_id: str | None) -> str:
    title = str(job_data.get("title") or "未命名岗位").strip()
    normalized = normalize_title(title)
    recruitment_type = str(job_data.get("recruitment_type") or "unknown")
    if recruitment_type not in RECRUITMENT_TYPES:
        recruitment_type = "unknown"
    employment_type = str(job_data.get("employment_type") or "unknown")
    locations = job_data.get("locations") or []
    row = connection.execute(
        "SELECT * FROM jobs WHERE company_id=? AND normalized_title=? AND recruitment_type=? AND employment_type=? AND COALESCE(batch_id,'')=COALESCE(?,'') LIMIT 1",
        (company_id, normalized, recruitment_type, employment_type, batch_id),
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
        merged_locations = _merge_list(row["locations_json"], locations)
        merged_education = _merge_list(row["education_json"], job_data.get("education"))
        merged_majors = _merge_list(row["majors_json"], job_data.get("majors"))
        merged_benefits = _merge_list(row["benefits_json"], job_data.get("benefits"))
        merged_methods = _merge_list(row["application_methods_json"], job_data.get("application_methods"))
        merged_contacts = _merge_list(row["contacts_json"], job_data.get("contacts"))
        connection.execute(
            """UPDATE jobs SET batch_id=COALESCE(?,batch_id),department=COALESCE(?,department),locations_json=?,
               headcount=COALESCE(?,headcount),education_json=?,majors_json=?,experience_requirement=COALESCE(?,experience_requirement),
               salary_json=CASE WHEN ?='{}' THEN salary_json ELSE ? END,responsibilities=COALESCE(?,responsibilities),
               requirements=COALESCE(?,requirements),benefits_json=?,application_methods_json=?,contacts_json=?,
               last_effective_posted_at=?,explicit_deadline=COALESCE(?,explicit_deadline),status=?,updated_at=? WHERE id=?""",
            (batch_id, job_data.get("department") or None, json_text(merged_locations, []), job_data.get("headcount") or None,
             json_text(merged_education, []), json_text(merged_majors, []), job_data.get("experience_requirement") or None,
             json_text(job_data.get("salary"), {}), json_text(job_data.get("salary"), {}), job_data.get("responsibilities") or None,
             job_data.get("requirements") or None, json_text(merged_benefits, []), json_text(merged_methods, []),
             json_text(merged_contacts, []), last, explicit_deadline, status, now, job_id),
        )
    else:
        job_id = str(uuid4())
        status = "active" if explicit_deadline is None or str(explicit_deadline) >= now[:10] else "expired"
        connection.execute(
            "INSERT INTO jobs(id,company_id,batch_id,canonical_title,normalized_title,department,locations_json,recruitment_type,employment_type,headcount,education_json,majors_json,experience_requirement,salary_json,responsibilities,requirements,benefits_json,application_methods_json,contacts_json,explicit_deadline,effective_posted_at,last_effective_posted_at,status,industry_codes_json,job_function_codes_json,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, company_id, batch_id, title, normalized, job_data.get("department"), json_text(locations, []), recruitment_type, employment_type, job_data.get("headcount"), json_text(job_data.get("education"), []), json_text(job_data.get("majors"), []), job_data.get("experience_requirement"), json_text(job_data.get("salary"), {}), job_data.get("responsibilities"), job_data.get("requirements"), json_text(job_data.get("benefits"), []), json_text(job_data.get("application_methods"), []), json_text(job_data.get("contacts"), []), explicit_deadline, observed_at, observed_at, status, json_text(job_data.get("industry_codes"), []), json_text([x for x in job_data.get("job_function_codes", []) if x in JOB_FUNCTIONS], []), float(job_data.get("confidence", 0)), now, now),
        )
        connection.execute("INSERT INTO search_index(entity_type,entity_id,title,body) VALUES('job',?,?,?)", (job_id, title, f"{title} {job_data.get('requirements','')} {job_data.get('responsibilities','')}"))
        followers = connection.execute("SELECT user_id FROM user_follows WHERE company_id=?", (company_id,)).fetchall()
        for follower in followers:
            connection.execute("INSERT INTO notifications(id,user_id,kind,title,body,created_at) VALUES(?,?,?,?,?,?)", (str(uuid4()), follower["user_id"], "new_job", f"{title} 有新岗位", f"{title} 已加入招聘知识库。", now))
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
    source_type = "wechat_group"
    source_url = None
    if raw_message_id:
        raw_row = connection.execute("SELECT id,connector_id,message_type,metadata_json FROM raw_messages WHERE id=?", (raw_message_id,)).fetchone()
        evidence_raw_message_id = raw_row["id"] if raw_row else None
        if raw_row and raw_row["connector_id"] == "manual":
            source_type = "manual_import"
        if raw_row:
            try:
                raw_metadata = json.loads(raw_row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                raw_metadata = {}
            if is_link_message(raw_row["message_type"], raw_metadata):
                source_type = "public_web"
        if raw_row:
            try:
                source_url = json.loads(raw_row["metadata_json"] or "{}").get("url")
            except json.JSONDecodeError:
                source_url = None
    connection.execute(
        "INSERT INTO evidences(id,job_id,raw_message_id,source_url,source_type,excerpt,observed_at) VALUES(?,?,?,?,?,?,?)",
        (evidence_id, job_id, evidence_raw_message_id, source_url, source_type, json.dumps(payload, ensure_ascii=False), observed_at),
    )
    return job_id


def apply_model_item(item: dict[str, Any], raw_message_id: str | None, observed_at: str | None) -> list[str]:
    if not item.get("is_recruitment"):
        return []
    observed = observed_at or utc_now()
    with connect() as connection:
        company = item.get("company") or {}
        company_id = _company_for(connection, company)
        _record_company_relationship(connection, company_id, company.get("relationship") or {})
        raw_row = connection.execute("SELECT connector_id,message_type,metadata_json FROM raw_messages WHERE id=?", (raw_message_id,)).fetchone() if raw_message_id else None
        metadata: dict[str, Any] = {}
        if raw_row:
            try:
                metadata = json.loads(raw_row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
        source_type = "manual_import" if raw_row and raw_row["connector_id"] == "manual" else "wechat_group"
        if raw_row and is_link_message(raw_row["message_type"], metadata):
            source_type = "public_web"
        artifact_id = metadata.get("artifact_id")
        evidence_id = str(uuid4())
        connection.execute(
            "INSERT INTO evidences(id,company_id,raw_message_id,artifact_id,source_url,source_type,excerpt,observed_at) VALUES(?,?,?,?,?,?,?,?)",
            (evidence_id, company_id, raw_message_id if raw_row else None, artifact_id, metadata.get("url"), source_type, json.dumps(item, ensure_ascii=False), observed),
        )
        for field_name, value in company.items():
            if field_name in {"matched_company_id", "relationship"} or value in (None, "", []):
                continue
            connection.execute(
                "INSERT INTO company_claims(id,company_id,field_name,field_value,source_url,source_type,retrieved_at,confidence,is_current) VALUES(?,?,?,?,?,?,?,?,1)",
                (str(uuid4()), company_id, field_name, json.dumps(value, ensure_ascii=False), metadata.get("url"), source_type, observed, 1.0),
            )
        recruitment_type = str((item.get("batch") or {}).get("recruitment_type") or "unknown")
        if recruitment_type not in RECRUITMENT_TYPES:
            recruitment_type = "unknown"
        batch_id = _batch_for(connection, company_id, item.get("batch") or {}, recruitment_type)
        job_ids = [_make_job(connection, company_id, batch_id, job, observed, raw_message_id) for job in item.get("jobs") or []]
        title_to_job = {normalize_title(str(job.get("title") or "")): job_id for job, job_id in zip(item.get("jobs") or [], job_ids)}
        for event in item.get("events") or []:
            _merge_event(connection, company_id, batch_id, event, title_to_job, evidence_id, observed)
        ready_at = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="seconds")
        pending = connection.execute(
            "SELECT id FROM processing_jobs WHERE kind='consolidate_company' AND company_id=? AND status='pending' LIMIT 1",
            (company_id,),
        ).fetchone()
        if pending:
            connection.execute("UPDATE processing_jobs SET next_attempt_at=?,updated_at=? WHERE id=?", (ready_at, utc_now(), pending["id"]))
        else:
            connection.execute(
                "INSERT INTO processing_jobs(id,kind,company_id,status,stage,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid4()), "consolidate_company", company_id, "pending", "waiting_for_sources", ready_at, utc_now(), utc_now()),
            )
        return job_ids


def _merge_event(
    connection: Any,
    company_id: str,
    batch_id: str | None,
    event: dict[str, Any],
    title_to_job: dict[str, str],
    evidence_id: str,
    observed_at: str,
) -> str:
    event_type = str(event.get("event_type") or "other")
    start_at = event.get("start_at")
    location = str(event.get("location") or "").strip()
    existing = connection.execute(
        "SELECT * FROM recruitment_events WHERE company_id=? AND COALESCE(batch_id,'')=COALESCE(?,'') AND event_type=? AND COALESCE(start_at,'')=COALESCE(?,'') AND COALESCE(location,'')=COALESCE(?,'') LIMIT 1",
        (company_id, batch_id, event_type, start_at, location),
    ).fetchone()
    job_ids = [title_to_job[normalize_title(str(title))] for title in event.get("job_titles") or [] if normalize_title(str(title)) in title_to_job]
    now = utc_now()
    if existing:
        event_id = existing["id"]
        merged_jobs = _merge_list(existing["job_ids_json"], job_ids)
        connection.execute(
            """UPDATE recruitment_events SET end_at=COALESCE(?,end_at),city=COALESCE(?,city),campus=COALESCE(?,campus),
               application_url=COALESCE(?,application_url),audience=COALESCE(?,audience),notes=COALESCE(?,notes),
               job_ids_json=?,updated_at=? WHERE id=?""",
            (event.get("end_at") or None, event.get("city") or None, event.get("campus") or None,
             event.get("application_url") or None, event.get("audience") or None, event.get("notes") or None,
             json_text(merged_jobs, []), now, event_id),
        )
    else:
        event_id = str(uuid4())
        status = "historical" if start_at and str(start_at) < now else "upcoming"
        connection.execute(
            """INSERT INTO recruitment_events(id,company_id,batch_id,title,event_type,start_at,end_at,timezone,format,city,campus,location,
               application_url,audience,notes,job_ids_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, company_id, batch_id, event.get("title") or event_type, event_type, start_at, event.get("end_at"),
             event.get("timezone") or "Asia/Shanghai", event.get("format") or "unknown", event.get("city"), event.get("campus"),
             location or None, event.get("application_url"), event.get("audience"), event.get("notes"), json_text(job_ids, []), status, now, now),
        )
    connection.execute(
        "INSERT INTO recruitment_event_versions(id,event_id,payload_json,observed_at,is_current) VALUES(?,?,?,?,1)",
        (str(uuid4()), event_id, json.dumps(event, ensure_ascii=False), observed_at),
    )
    connection.execute("UPDATE recruitment_event_versions SET is_current=0 WHERE event_id=? AND id NOT IN (SELECT id FROM recruitment_event_versions WHERE event_id=? ORDER BY observed_at DESC LIMIT 1)", (event_id, event_id))
    connection.execute("INSERT OR IGNORE INTO recruitment_event_evidences(event_id,evidence_id) VALUES(?,?)", (event_id, evidence_id))
    return event_id


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
