from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .db import connect, one, utc_now
from .parsers import is_link_message, normalize_event_datetime, recover_original_source_url


INDUSTRIES = {
    "internet_software", "ai_data", "electronics_semiconductor", "telecommunications",
    "manufacturing_automation", "automotive_transport_equipment", "energy_chemical_materials",
    "construction_real_estate", "finance", "consumer_retail_ecommerce", "healthcare_biopharma",
    "education_research", "media_culture_entertainment", "logistics_transportation",
    "professional_services", "government_public_nonprofit", "agriculture", "military_defense", "other",
}
INDUSTRY_LABELS = {
    "internet_software": "互联网/软件",
    "ai_data": "人工智能/数据",
    "electronics_semiconductor": "电子/半导体",
    "telecommunications": "通信",
    "manufacturing_automation": "制造/自动化",
    "automotive_transport_equipment": "汽车/交通装备",
    "energy_chemical_materials": "能源/化工/材料",
    "construction_real_estate": "建筑/房地产",
    "finance": "金融",
    "consumer_retail_ecommerce": "消费/零售/电商",
    "healthcare_biopharma": "医疗/生物医药",
    "education_research": "教育/科研",
    "media_culture_entertainment": "媒体/文化/娱乐",
    "logistics_transportation": "物流/交通运输",
    "professional_services": "专业服务",
    "government_public_nonprofit": "政府/事业单位/公益组织",
    "agriculture": "农业",
    "military_defense": "军工/国防",
    "other": "其他行业",
}
COMPANY_TYPE_CODES = {
    "private", "state_owned", "foreign_owned", "joint_venture", "public_company", "government", "unknown",
}
COMPANY_TYPE_LABELS = {
    "private": "民营企业",
    "state_owned": "国有企业",
    "foreign_owned": "外资/外企",
    "joint_venture": "合资企业",
    "public_company": "上市公司",
    "government": "政府/事业单位",
    "unknown": "企业类型待确认",
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


COMPANY_OVERRIDE_COLUMNS = {
    "display_name": "display_name",
    "legal_name": "legal_name",
    "aliases": "aliases_json",
    "summary": "summary",
    "primary_industry": "primary_industry",
    "secondary_industries": "secondary_industries_json",
    "website": "website",
    "company_nature": "company_nature",
    "founded_at": "founded_at",
    "company_size": "company_size",
    "headquarters": "headquarters",
    "businesses": "businesses_json",
    "highlights": "highlights_json",
    "official_channels": "official_channels_json",
    "tags": "company_tags_json",
}
COMPANY_OVERRIDE_LIST_FIELDS = {
    "aliases",
    "secondary_industries",
    "businesses",
    "highlights",
    "official_channels",
    "tags",
}


def _infer_company_type(company_nature: Any) -> str | None:
    text = str(company_nature or "").strip().lower()
    if not text:
        return None
    if any(value in text for value in ("国有", "央企", "国企", "state-owned", "state owned")):
        return "state_owned"
    if any(value in text for value in ("外资", "外商", "外企", "foreign", "multinational")):
        return "foreign_owned"
    if any(value in text for value in ("合资", "joint venture", "中外合资")):
        return "joint_venture"
    if any(value in text for value in ("上市", "公众公司", "public company")):
        return "public_company"
    if any(value in text for value in ("政府", "事业单位", "机关")):
        return "government"
    if any(value in text for value in ("民营", "私营", "私企", "private")):
        return "private"
    return None


def normalize_company_tags(value: Any, company_nature: Any = None, industries: list[str] | None = None, company_type: str | None = None) -> list[dict[str, str]]:
    """Keep model-generated company tags within the supported taxonomy."""
    candidates = value if isinstance(value, list) else []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(category: str, code: str, label: str | None = None) -> None:
        if category == "company_type":
            if code not in COMPANY_TYPE_CODES:
                return
            resolved_label = COMPANY_TYPE_LABELS[code]
        elif category == "industry":
            if code not in INDUSTRIES:
                return
            resolved_label = INDUSTRY_LABELS[code]
        elif category == "attribute":
            code = re.sub(r"\s+", "_", code)[:80]
            label = re.sub(r"[\r\n]+", " ", label or code).strip()[:80]
            if not code or not label:
                return
            resolved_label = label
        else:
            return
        key = (category, code)
        if key in seen:
            return
        seen.add(key)
        display_label = resolved_label if category != "attribute" else str(label or resolved_label)
        result.append({"category": category, "code": code, "label": display_label[:80]})

    for item in candidates:
        if not isinstance(item, dict):
            continue
        add(str(item.get("category") or "").strip(), str(item.get("code") or "").strip(), str(item.get("label") or "").strip())
    inferred_type = company_type if company_type in COMPANY_TYPE_CODES else _infer_company_type(company_nature)
    if inferred_type:
        add("company_type", inferred_type)
    for code in industries or []:
        if code in INDUSTRIES:
            add("industry", code)
    return result


def _stored_tags(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def apply_company_overrides(connection: Any, company_id: str, overrides: dict[str, Any], updated_at: str | None = None) -> None:
    changed_at = updated_at or utc_now()
    for field, column in COMPANY_OVERRIDE_COLUMNS.items():
        if field not in overrides:
            continue
        value = overrides[field]
        if field in COMPANY_OVERRIDE_LIST_FIELDS:
            value = json_text(value if isinstance(value, list) else [], [])
        connection.execute(f"UPDATE companies SET {column}=?,updated_at=? WHERE id=?", (value, changed_at, company_id))


def company_overrides(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
        merged_industries = list(dict.fromkeys([existing["primary_industry"], *json.loads(existing["secondary_industries_json"] or "[]"), *industries]))
        merged_tags = normalize_company_tags(
            [*_stored_tags(existing["company_tags_json"]), *(company_data.get("tags") or [])],
            company_data.get("company_nature") or existing["company_nature"],
            merged_industries,
        )
        connection.execute(
            """UPDATE companies SET legal_name=COALESCE(?,legal_name),aliases_json=?,
               primary_industry=CASE WHEN ?='other' THEN primary_industry ELSE ? END,
               secondary_industries_json=?,website=COALESCE(?,website),company_nature=COALESCE(?,company_nature),
               founded_at=COALESCE(?,founded_at),company_size=COALESCE(?,company_size),headquarters=COALESCE(?,headquarters),
               businesses_json=?,highlights_json=?,official_channels_json=?,company_tags_json=?,updated_at=? WHERE id=?""",
            (legal_name, json_text(merged_aliases, []), primary, primary, json_text(secondary, []),
             company_data.get("website") or None, company_data.get("company_nature") or None,
             company_data.get("founded_at") or None, company_data.get("company_size") or None,
             company_data.get("headquarters") or None, json_text(merged_businesses, []),
             json_text(merged_highlights, []), json_text(merged_channels, []), json_text(merged_tags, []), now, company_id),
        )
        apply_company_overrides(connection, company_id, company_overrides(existing["manual_overrides_json"]), now)
        return company_id
    company_id = str(uuid4())
    tags = normalize_company_tags(company_data.get("tags"), company_data.get("company_nature"), industries)
    connection.execute(
        """INSERT INTO companies(id,display_name,legal_name,aliases_json,primary_industry,secondary_industries_json,
           website,company_nature,founded_at,company_size,headquarters,businesses_json,highlights_json,official_channels_json,company_tags_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (company_id, display_name, legal_name, json_text(aliases, []), primary, json_text(industries[1:], []),
         company_data.get("website") or None, company_data.get("company_nature") or None,
         company_data.get("founded_at") or None, company_data.get("company_size") or None,
         company_data.get("headquarters") or None, json_text(company_data.get("businesses"), []),
         json_text(company_data.get("highlights"), []), json_text(company_data.get("official_channels"), []), json_text(tags, []), now, now),
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
            source_url = recover_original_source_url(raw_metadata.get("source_url") or raw_metadata.get("url"))
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
        source_url = recover_original_source_url(metadata.get("source_url") or metadata.get("url"))
        artifact_id = metadata.get("artifact_id")
        evidence_id = str(uuid4())
        connection.execute(
            "INSERT INTO evidences(id,company_id,raw_message_id,artifact_id,source_url,source_type,excerpt,observed_at) VALUES(?,?,?,?,?,?,?,?)",
            (evidence_id, company_id, raw_message_id if raw_row else None, artifact_id, source_url, source_type, json.dumps(item, ensure_ascii=False), observed),
        )
        for field_name, value in company.items():
            if field_name in {"matched_company_id", "relationship"} or value in (None, "", []):
                continue
            connection.execute(
                "INSERT INTO company_claims(id,company_id,field_name,field_value,source_url,source_type,retrieved_at,confidence,is_current) VALUES(?,?,?,?,?,?,?,?,1)",
                (str(uuid4()), company_id, field_name, json.dumps(value, ensure_ascii=False), source_url, source_type, observed, 1.0),
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
        from .company_research import queue_company_research_in_connection

        queue_company_research_in_connection(connection, company_id)
        deduplicate_company_jobs(connection, company_id)
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
    timezone_name = str(event.get("timezone") or "Asia/Shanghai")
    start_at = normalize_event_datetime(event.get("start_at"), timezone_name, observed_at)
    end_at = normalize_event_datetime(event.get("end_at"), timezone_name, observed_at)
    normalized_event = {**event, "start_at": start_at or "", "end_at": end_at or "", "timezone": timezone_name}
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
            (end_at, event.get("city") or None, event.get("campus") or None,
             event.get("application_url") or None, event.get("audience") or None, event.get("notes") or None,
             json_text(merged_jobs, []), now, event_id),
        )
    else:
        event_id = str(uuid4())
        status = "historical" if start_at and str(start_at) < now else "upcoming"
        connection.execute(
            """INSERT INTO recruitment_events(id,company_id,batch_id,title,event_type,start_at,end_at,timezone,format,city,campus,location,
               application_url,audience,notes,job_ids_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, company_id, batch_id, event.get("title") or event_type, event_type, start_at, end_at,
             timezone_name, event.get("format") or "unknown", event.get("city"), event.get("campus"),
             location or None, event.get("application_url"), event.get("audience"), event.get("notes"), json_text(job_ids, []), status, now, now),
        )
    connection.execute(
        "INSERT INTO recruitment_event_versions(id,event_id,payload_json,observed_at,is_current) VALUES(?,?,?,?,1)",
        (str(uuid4()), event_id, json.dumps(normalized_event, ensure_ascii=False), observed_at),
    )
    connection.execute("UPDATE recruitment_event_versions SET is_current=0 WHERE event_id=? AND id NOT IN (SELECT id FROM recruitment_event_versions WHERE event_id=? ORDER BY observed_at DESC LIMIT 1)", (event_id, event_id))
    connection.execute("INSERT OR IGNORE INTO recruitment_event_evidences(event_id,evidence_id) VALUES(?,?)", (event_id, evidence_id))
    return event_id


def _merge_text(old_value: Any, new_value: Any) -> str | None:
    old = str(old_value or "").strip()
    new = str(new_value or "").strip()
    if not old:
        return new or None
    if not new or new == old:
        return old
    if old in new:
        return new
    if new in old:
        return old
    return f"{old}\n{new}"


def _merge_json_object(old_value: Any, new_value: Any) -> str:
    try:
        old = json.loads(old_value or "{}") if isinstance(old_value, str) else (old_value or {})
    except json.JSONDecodeError:
        old = {}
    try:
        new = json.loads(new_value or "{}") if isinstance(new_value, str) else (new_value or {})
    except json.JSONDecodeError:
        new = {}
    if not isinstance(old, dict):
        old = {}
    if not isinstance(new, dict):
        new = {}
    merged = {**old}
    for key, value in new.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return json_text(merged, {})


def _job_status(old_status: str, new_status: str) -> str:
    priority = {
        "active": 5,
        "upcoming": 4,
        "possibly_expired": 3,
        "expired": 2,
        "unknown": 1,
    }
    return old_status if priority.get(old_status, 0) >= priority.get(new_status, 0) else new_status


def _merge_job_into(connection: Any, keep: Any, duplicate: Any) -> None:
    list_fields = {
        "locations_json", "education_json", "majors_json", "benefits_json",
        "application_methods_json", "contacts_json", "industry_codes_json", "job_function_codes_json",
    }
    merged_lists = {field: json_text(_merge_list(keep[field], json.loads(duplicate[field] or "[]")), []) for field in list_fields}
    explicit_deadlines = [value for value in (keep["explicit_deadline"], duplicate["explicit_deadline"]) if value]
    latest_deadline = max(explicit_deadlines) if explicit_deadlines else None
    posted_values = [value for value in (keep["effective_posted_at"], duplicate["effective_posted_at"]) if value]
    first_posted = min(posted_values) if posted_values else None
    last_values = [value for value in (keep["last_effective_posted_at"], duplicate["last_effective_posted_at"]) if value]
    last_posted = max(last_values) if last_values else None
    updated_at = max(str(keep["updated_at"] or ""), str(duplicate["updated_at"] or "")) or utc_now()
    connection.execute(
        """UPDATE jobs SET department=?,locations_json=?,headcount=?,education_json=?,majors_json=?,experience_requirement=?,
           salary_json=?,responsibilities=?,requirements=?,benefits_json=?,application_methods_json=?,contacts_json=?,
           explicit_deadline=?,effective_posted_at=?,last_effective_posted_at=?,status=?,industry_codes_json=?,
           job_function_codes_json=?,confidence=?,updated_at=? WHERE id=?""",
        (
            _merge_text(keep["department"], duplicate["department"]), merged_lists["locations_json"],
            _merge_text(keep["headcount"], duplicate["headcount"]), merged_lists["education_json"],
            merged_lists["majors_json"], _merge_text(keep["experience_requirement"], duplicate["experience_requirement"]),
            _merge_json_object(keep["salary_json"], duplicate["salary_json"]),
            _merge_text(keep["responsibilities"], duplicate["responsibilities"]),
            _merge_text(keep["requirements"], duplicate["requirements"]), merged_lists["benefits_json"],
            merged_lists["application_methods_json"], merged_lists["contacts_json"], latest_deadline,
            first_posted, last_posted, _job_status(str(keep["status"] or "unknown"), str(duplicate["status"] or "unknown")),
            merged_lists["industry_codes_json"], merged_lists["job_function_codes_json"],
            max(float(keep["confidence"] or 0), float(duplicate["confidence"] or 0)), updated_at, keep["id"],
        ),
    )

    for row in connection.execute("SELECT id,content_hash FROM job_versions WHERE job_id=?", (duplicate["id"],)).fetchall():
        same = connection.execute("SELECT id FROM job_versions WHERE job_id=? AND content_hash=?", (keep["id"], row["content_hash"])).fetchone()
        if same:
            connection.execute("DELETE FROM job_versions WHERE id=?", (row["id"],))
        else:
            connection.execute("UPDATE job_versions SET job_id=? WHERE id=?", (keep["id"], row["id"]))
    connection.execute("UPDATE evidences SET job_id=? WHERE job_id=?", (keep["id"], duplicate["id"]))
    connection.execute("UPDATE application_events SET job_id=? WHERE job_id=?", (keep["id"], duplicate["id"]))
    connection.execute("UPDATE user_notes SET job_id=? WHERE job_id=?", (keep["id"], duplicate["id"]))
    for row in connection.execute("SELECT user_id,state,favorite,updated_at FROM user_job_states WHERE job_id=?", (duplicate["id"],)).fetchall():
        existing = connection.execute("SELECT state,favorite,updated_at FROM user_job_states WHERE user_id=? AND job_id=?", (row["user_id"], keep["id"])).fetchone()
        if existing:
            state_priority = {"offer": 5, "interview": 4, "applied": 3, "interested": 2, "rejected": 1}
            state = row["state"] if state_priority.get(row["state"], 0) > state_priority.get(existing["state"], 0) else existing["state"]
            connection.execute(
                "UPDATE user_job_states SET state=?,favorite=?,updated_at=? WHERE user_id=? AND job_id=?",
                (state, int(bool(row["favorite"] or existing["favorite"])), max(row["updated_at"], existing["updated_at"]), row["user_id"], keep["id"]),
            )
            connection.execute("DELETE FROM user_job_states WHERE user_id=? AND job_id=?", (row["user_id"], duplicate["id"]))
        else:
            connection.execute("UPDATE user_job_states SET job_id=? WHERE user_id=? AND job_id=?", (keep["id"], row["user_id"], duplicate["id"]))
    connection.execute(
        """INSERT OR IGNORE INTO job_tag_links(user_id,job_id,tag_id)
           SELECT user_id,?,tag_id FROM job_tag_links WHERE job_id=?""",
        (keep["id"], duplicate["id"]),
    )
    connection.execute("DELETE FROM job_tag_links WHERE job_id=?", (duplicate["id"],))
    for event in connection.execute("SELECT id,job_ids_json FROM recruitment_events WHERE job_ids_json LIKE ?", (f"%{duplicate['id']}%",)).fetchall():
        try:
            job_ids = json.loads(event["job_ids_json"] or "[]")
        except json.JSONDecodeError:
            job_ids = []
        updated_ids = list(dict.fromkeys(keep["id"] if value == duplicate["id"] else value for value in job_ids))
        connection.execute("UPDATE recruitment_events SET job_ids_json=? WHERE id=?", (json_text(updated_ids, []), event["id"]))
    connection.execute("DELETE FROM search_index WHERE entity_type='job' AND entity_id=?", (duplicate["id"],))
    connection.execute("DELETE FROM jobs WHERE id=?", (duplicate["id"],))


def deduplicate_company_jobs(connection: Any, company_id: str) -> int:
    """Merge duplicate job records without dropping their user or evidence history."""
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM jobs WHERE company_id=? ORDER BY normalized_title,created_at,id", (company_id,)
    ).fetchall()]
    groups: list[list[dict[str, Any]]] = []
    for row in rows:
        matched = None
        for group in groups:
            first = group[0]
            if (first["normalized_title"], first["batch_id"], first["recruitment_type"]) != (row["normalized_title"], row["batch_id"], row["recruitment_type"]):
                continue
            known_types = {str(item["employment_type"] or "unknown") for item in group if item["employment_type"] != "unknown"}
            current_type = str(row["employment_type"] or "unknown")
            if current_type == "unknown" or not known_types or current_type in known_types:
                matched = group
                break
        if matched is None:
            matched = []
            groups.append(matched)
        matched.append(row)
    removed = 0
    for group in groups:
        if len(group) < 2:
            continue
        keep = group[0]
        for duplicate in group[1:]:
            if connection.execute("SELECT id FROM jobs WHERE id=?", (duplicate["id"],)).fetchone():
                _merge_job_into(connection, keep, duplicate)
                removed += 1
                refreshed = connection.execute("SELECT * FROM jobs WHERE id=?", (keep["id"],)).fetchone()
                if refreshed:
                    keep = dict(refreshed)
    return removed


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
