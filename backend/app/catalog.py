from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .db import connect, one, utc_now
from .parsers import extract_recruitment_catalog, is_link_message, is_major_like_title, is_wechat_public_url, normalize_event_datetime, recover_original_source_url


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


def source_type_for_url(url: str) -> str:
    return "wechat_official_account" if is_wechat_public_url(url) else "public_web"
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


NON_JOB_TITLE_PATTERNS = (
    r"网申|报名|投递|网址|链接|二维码",
    r"简历(?:筛选|匹配)|资格(?:初审|审查)|初审",
    r"测评|笔试|面试|体检|录用|入职|签约|公示|审核",
    r"招聘流程|招聘行程|校招行程|活动(?:时间|安排|对象|形式)|宣讲会|招聘会|参访|大咖分享",
    r"安家费|年收入|薪资|工资|福利|津贴|补贴|奖金|事业编制|住房|公寓",
    r"博士研究生|硕士研究生|博士|硕士|本科|毕业|应届生|面向对象|活动对象",
    r"具体岗位(?:见|以)|岗位(?:列表|汇总|职责|要求)",
    r"^(?:[^岗位]{1,30})(?:类|专业|类别|方向)$",
)


def is_non_job_title(value: Any) -> bool:
    """Reject process, eligibility, benefits, event and URL text as job titles."""
    title = re.sub(r"\s+", "", str(value or "").strip())
    if not title:
        return True
    if len(title) > 120 or re.fullmatch(r"https?://\S+|[\w.-]+\.(?:com|cn|org|net)", title, re.IGNORECASE):
        return True
    if re.search(r"(?:20\d{2}年)?\d{1,2}月\d{1,2}日|20\d{2}届|\d{1,2}月(?:底|初)", title):
        return True
    return any(re.search(pattern, title, re.IGNORECASE) for pattern in NON_JOB_TITLE_PATTERNS)


def is_location_like_name(value: Any) -> bool:
    """Reject common venue/campus labels from becoming company names."""
    name = re.sub(r"\s+", "", str(value or "").strip())
    if not name:
        return False
    if re.fullmatch(r"[A-Z][0-9]{2,4}", name, re.IGNORECASE):
        return True
    if any(marker in name for marker in ("教学楼", "会议室", "科技会堂", "报告厅", "体育馆", "校区", "举办场地", "活动场地", "就业创业指导服务中心", "就业指导中心")):
        return not any(marker in name for marker in ("有限公司", "集团", "研究所", "大学", "学院", "银行", "医院"))
    return False


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


def normalize_employment_type(value: Any) -> str:
    text = str(value or "unknown").strip().lower().replace("-", "_").replace(" ", "")
    aliases = {
        "全职": "full_time", "全日制": "full_time", "正式": "full_time", "正式员工": "full_time",
        "实习": "internship", "实习生": "internship", "intern": "internship",
        "兼职": "part_time", "非全日制": "part_time", "劳务": "labor",
    }
    return aliases.get(text, text or "unknown")


def is_aggregate_job_title(value: Any) -> bool:
    title = str(value or "").strip()
    return any(marker in title for marker in ("招聘岗位", "招聘职位", "具体岗位", "岗位列表", "岗位汇总", "研发与技术岗位", "专业需求"))


def _raw_source_text(connection: Any, raw_message_id: str | None) -> str:
    if not raw_message_id:
        return ""
    raw = connection.execute("SELECT text_content,metadata_json FROM raw_messages WHERE id=?", (raw_message_id,)).fetchone()
    texts: list[str] = []
    if raw:
        try:
            metadata = json.loads(raw["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if metadata.get("_original_text_content"):
            texts.append(str(metadata["_original_text_content"]))
        for key in ("shared_title", "title"):
            if metadata.get(key):
                texts.append(str(metadata[key]))
        content_data = metadata.get("contentData")
        if isinstance(content_data, dict) and content_data.get("title"):
            texts.append(str(content_data["title"]))
        texts.append(str(raw["text_content"] or ""))
    texts.extend(
        str(row["ocr_text"] or "")
        for row in connection.execute("SELECT ocr_text FROM artifacts WHERE raw_message_id=?", (raw_message_id,)).fetchall()
        if row["ocr_text"]
    )
    return "\n".join(text for text in texts if text).strip()


def _split_job_title_parts(value: Any) -> list[str]:
    title = str(value or "").strip()
    if not title:
        return []
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for character in title:
        if character in "（(【[":
            depth += 1
        elif character in "）)】]" and depth:
            depth -= 1
        if character in "、；;•·，" and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
        else:
            current.append(character)
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts


def _major_requirements_from_jobs(job_values: Any) -> list[str]:
    requirements: list[str] = []
    for value in job_values or []:
        if not isinstance(value, dict):
            continue
        for major in value.get("majors") or []:
            text = str(major or "").strip()
            if text:
                requirements.append(text)
        for title in _split_job_title_parts(value.get("title")):
            if is_major_like_title(title):
                requirements.append(title.rstrip("。；;"))
    return list(dict.fromkeys(requirements))


def _prepare_job_items(job_values: Any, source_catalog: dict[str, list[str]]) -> list[dict[str, Any]]:
    model_jobs: list[dict[str, Any]] = []
    for value in job_values or []:
        if not isinstance(value, dict) or is_non_job_title(value.get("title")):
            continue
        for part in _split_job_title_parts(value.get("title")):
            if is_non_job_title(part) or is_major_like_title(part):
                continue
            model_jobs.append({**dict(value), "title": part, "majors": []})
    expanded: list[dict[str, Any]] = []
    expanded.extend(model_jobs)

    source_titles = [
        title for title in source_catalog.get("job_titles") or []
        if not is_non_job_title(title) and not is_major_like_title(title)
    ]
    if len(source_titles) < 2:
        return expanded
    specific = [job for job in expanded if not is_aggregate_job_title(job.get("title"))]
    template = next((job for job in specific), None)
    by_title = {normalize_title(str(job.get("title") or "")): job for job in specific}
    result: list[dict[str, Any]] = []
    for title in source_titles:
        matched = by_title.get(normalize_title(title))
        if matched:
            result.append({**matched, "title": title})
            continue
        if template:
            result.append({**template, "title": title})
        else:
            result.append({"title": title, "recruitment_type": "unknown", "employment_type": "unknown"})
    for job in specific:
        if normalize_title(str(job.get("title") or "")) not in {normalize_title(title) for title in source_titles}:
            result.append(job)
    return result


def _event_title_company_candidate(title: Any) -> str:
    text = str(title or "").strip()
    if not text:
        return ""
    candidate = re.split(
        r"(?:20\d{2}(?:届|年)?|20\d{2})?(?:秋季|春季|暑期|寒假)?"
        r"(?:校园|空中|线上|线下)?(?:招聘会|招聘|宣讲会|宣讲|说明会|说明|双选会|双选|开放日|网申|笔试|面试|全国统考)",
        text,
        maxsplit=1,
    )[0].strip(" ：:—-·")
    if not candidate or re.search(r"(?:^|\D)\d{1,2}月\d{1,2}[日号]?", candidate) or "汇总" in candidate:
        return ""
    return candidate


def is_aggregate_event_title(value: Any) -> bool:
    title = str(value or "").strip()
    return not _event_title_company_candidate(title) and bool(re.search(r"汇总|安排|日程|一览|活动信息", title))


def event_company_for_title(connection: Any, fallback_company_id: str | None, event: dict[str, Any]) -> str | None:
    venue_values = {
        normalize_name(str(event.get(key) or ""))
        for key in ("city", "campus", "location")
        if event.get(key)
    }
    candidates = [
        _event_title_company_candidate(event.get("title")),
        str(event.get("company_name") or "").strip(),
    ]
    for candidate in candidates:
        if not candidate or is_location_like_name(candidate):
            continue
        normalized = normalize_name(candidate)
        if not normalized or normalized in venue_values:
            continue
        for row in connection.execute("SELECT id,display_name,legal_name,aliases_json FROM companies").fetchall():
            names = [row["display_name"], row["legal_name"]]
            try:
                names.extend(json.loads(row["aliases_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                pass
            if normalized in {normalize_name(str(name)) for name in names if name}:
                return row["id"]
        if any(marker in candidate for marker in ("有限公司", "股份有限公司", "集团", "研究所", "大学", "学院", "银行", "医院", "中心")):
            return _company_for(connection, {"display_name": candidate, "industry_codes": []})
    return fallback_company_id


def _company_for(connection, company_data: dict[str, Any]) -> str:
    display_name = str(company_data.get("display_name") or "").strip()
    legal_name = str(company_data.get("legal_name") or "").strip() or None
    aliases = company_data.get("aliases") or []
    if not display_name:
        display_name = legal_name or f"未识别企业-{uuid4().hex[:8]}"
    existing = None
    matched_company_id = str(company_data.get("matched_company_id") or "").strip()
    if matched_company_id:
        existing = connection.execute("SELECT * FROM companies WHERE id=?", (matched_company_id,)).fetchone()
        if existing and normalize_name(str(existing["display_name"] or "")) != normalize_name(display_name):
            existing = None
    now = utc_now()
    if not existing:
        normalized_display_name = normalize_name(display_name)
        for row in connection.execute("SELECT * FROM companies").fetchall():
            if normalize_name(str(row["display_name"] or "")) == normalized_display_name:
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
        merged_major_requirements = _merge_list(existing["major_requirements_json"], company_data.get("major_requirements"))
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
               businesses_json=?,highlights_json=?,official_channels_json=?,major_requirements_json=?,company_tags_json=?,updated_at=? WHERE id=?""",
            (legal_name, json_text(merged_aliases, []), primary, primary, json_text(secondary, []),
             company_data.get("website") or None, company_data.get("company_nature") or None,
             company_data.get("founded_at") or None, company_data.get("company_size") or None,
             company_data.get("headquarters") or None, json_text(merged_businesses, []),
             json_text(merged_highlights, []), json_text(merged_channels, []), json_text(merged_major_requirements, []),
             json_text(merged_tags, []), now, company_id),
        )
        apply_company_overrides(connection, company_id, company_overrides(existing["manual_overrides_json"]), now)
        return company_id
    company_id = str(uuid4())
    tags = normalize_company_tags(company_data.get("tags"), company_data.get("company_nature"), industries)
    connection.execute(
        """INSERT INTO companies(id,display_name,legal_name,aliases_json,primary_industry,secondary_industries_json,
           website,company_nature,founded_at,company_size,headquarters,businesses_json,highlights_json,official_channels_json,major_requirements_json,company_tags_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (company_id, display_name, legal_name, json_text(aliases, []), primary, json_text(industries[1:], []),
         company_data.get("website") or None, company_data.get("company_nature") or None,
         company_data.get("founded_at") or None, company_data.get("company_size") or None,
         company_data.get("headquarters") or None, json_text(company_data.get("businesses"), []),
         json_text(company_data.get("highlights"), []), json_text(company_data.get("official_channels"), []),
         json_text(company_data.get("major_requirements"), []), json_text(tags, []), now, now),
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


def _job_identity_matches(row: Any, normalized_title: str, recruitment_type: str, employment_type: str, department: Any) -> bool:
    if row["normalized_title"] != normalized_title or row["recruitment_type"] != recruitment_type:
        return False
    known_department = normalize_title(str(row["department"] or ""))
    current_department = normalize_title(str(department or ""))
    return not known_department or not current_department or known_department == current_department


def _source_deadline(value: Any, source_text: str, observed_at: str) -> str | None:
    """Keep a deadline only when the source explicitly presents a deadline context."""
    raw = str(value or "").strip()
    if not raw or not source_text:
        return raw or None
    deadline_context = re.compile(
        r"(?:网申|报名|申请|投递|简历).{0,30}(?:截止|截至|最后|止于)|"
        r"(?:截止|截至|最后|止于).{0,30}(?:网申|报名|申请|投递|简历)|"
        r"(?:网申|报名|申请|投递).{0,20}(?:时间|日期)",
        re.IGNORECASE | re.DOTALL,
    )
    contexts = list(deadline_context.finditer(source_text))
    if not contexts:
        return None
    normalized = normalize_event_datetime(raw, "Asia/Shanghai", observed_at)
    if not normalized:
        return None
    local_date = datetime.fromisoformat(normalized).astimezone(timezone(timedelta(hours=8)))
    date_patterns = (
        rf"{local_date.year}\s*(?:年|[-/.])\s*{local_date.month}\s*(?:月|[-/.])\s*{local_date.day}",
        rf"{local_date.month}\s*月\s*{local_date.day}\s*(?:日|号)?",
        rf"{local_date.month:02d}[-/.]{local_date.day:02d}",
    )
    for context in contexts:
        window = source_text[max(0, context.start() - 30) : context.end() + 100]
        if any(re.search(pattern, window) for pattern in date_patterns):
            return local_date.date().isoformat()
    return None


def _make_job(connection, company_id: str, batch_id: str | None, job_data: dict[str, Any], observed_at: str, raw_message_id: str | None) -> str:
    title = str(job_data.get("title") or "未命名岗位").strip()
    normalized = normalize_title(title)
    recruitment_type = str(job_data.get("recruitment_type") or "unknown")
    if recruitment_type not in RECRUITMENT_TYPES:
        recruitment_type = "unknown"
    employment_type = normalize_employment_type(job_data.get("employment_type"))
    locations = job_data.get("locations") or []
    row = next(
        (
            candidate
            for candidate in connection.execute(
                "SELECT * FROM jobs WHERE company_id=? AND normalized_title=? AND recruitment_type=? ORDER BY created_at,id",
                (company_id, normalized, recruitment_type),
            ).fetchall()
            if _job_identity_matches(candidate, normalized, recruitment_type, employment_type, job_data.get("department"))
        ),
        None,
    )
    now = utc_now()
    source_text = _raw_source_text(connection, raw_message_id)
    explicit_deadline = _source_deadline(
        job_data.get("deadline") or job_data.get("explicit_deadline"),
        source_text,
        observed_at,
    )
    payload = {**job_data, "deadline": explicit_deadline or ""}
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
        existing_employment_type = normalize_employment_type(row["employment_type"])
        merged_employment_type = employment_type if existing_employment_type == "unknown" else existing_employment_type
        if employment_type not in {"unknown", merged_employment_type}:
            merged_employment_type = "unknown"
        connection.execute(
            """UPDATE jobs SET batch_id=COALESCE(?,batch_id),department=COALESCE(?,department),employment_type=?,locations_json=?,
               headcount=COALESCE(?,headcount),education_json=?,majors_json=?,experience_requirement=COALESCE(?,experience_requirement),
               salary_json=CASE WHEN ?='{}' THEN salary_json ELSE ? END,responsibilities=COALESCE(?,responsibilities),
               requirements=COALESCE(?,requirements),benefits_json=?,application_methods_json=?,contacts_json=?,
               last_effective_posted_at=?,explicit_deadline=COALESCE(?,explicit_deadline),status=?,updated_at=? WHERE id=?""",
            (batch_id, job_data.get("department") or None, merged_employment_type, json_text(merged_locations, []), job_data.get("headcount") or None,
             json_text(merged_education, []), json_text(merged_majors, []), _merge_text(row["experience_requirement"], job_data.get("experience_requirement")),
             json_text(job_data.get("salary"), {}), json_text(job_data.get("salary"), {}), _merge_text(row["responsibilities"], job_data.get("responsibilities")),
             _merge_text(row["requirements"], job_data.get("requirements")), json_text(merged_benefits, []), json_text(merged_methods, []),
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
                source_type = source_type_for_url(recover_original_source_url(raw_metadata.get("source_url") or raw_metadata.get("url")) or "")
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
        raw_row = connection.execute("SELECT connector_id,message_type,metadata_json FROM raw_messages WHERE id=?", (raw_message_id,)).fetchone() if raw_message_id else None
        source_catalog = extract_recruitment_catalog(_raw_source_text(connection, raw_message_id))
        company = dict(item.get("company") or {})
        company["major_requirements"] = list(dict.fromkeys([
            *(company.get("major_requirements") or []),
            *(source_catalog.get("major_requirements") or []),
            *_major_requirements_from_jobs(item.get("jobs") or []),
        ]))
        job_items = _prepare_job_items(item.get("jobs") or [], source_catalog)
        matched_company_id = str(company.get("matched_company_id") or "").strip()
        company_name = str(company.get("display_name") or company.get("legal_name") or "").strip()
        if not matched_company_id and is_location_like_name(company_name):
            company["display_name"] = ""
            company["legal_name"] = ""
            company_name = ""
        company_id = _company_for(connection, company) if matched_company_id or company_name else None
        if company_id is None:
            for candidate_event in item.get("events") or []:
                company_id = event_company_for_title(connection, None, candidate_event)
                if company_id:
                    break
        if company_id is None:
            # Do not create a synthetic company for an event-only summary or a
            # model result whose company name is actually a venue/campus.
            return []
        _record_company_relationship(connection, company_id, company.get("relationship") or {})
        metadata: dict[str, Any] = {}
        if raw_row:
            try:
                metadata = json.loads(raw_row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
        source_type = "manual_import" if raw_row and raw_row["connector_id"] == "manual" else "wechat_group"
        if raw_row and is_link_message(raw_row["message_type"], metadata):
            source_type = source_type_for_url(recover_original_source_url(metadata.get("source_url") or metadata.get("url")) or "")
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
        job_ids = [_make_job(connection, company_id, batch_id, job, observed, raw_message_id) for job in job_items]
        title_to_job = {normalize_title(str(job.get("title") or "")): job_id for job, job_id in zip(job_items, job_ids)}
        event_company_ids: list[str] = []
        for event in item.get("events") or []:
            event_company_id = event_company_for_title(connection, None, event)
            if not event_company_id and not is_aggregate_event_title(event.get("title")):
                event_company_id = company_id
            if not event_company_id:
                continue
            event_company_ids.append(event_company_id)
            event_batch_id = batch_id if event_company_id == company_id else _batch_for(connection, event_company_id, item.get("batch") or {}, recruitment_type)
            event_evidence_id = evidence_id
            if event_company_id != company_id:
                event_evidence_id = str(uuid4())
                connection.execute(
                    "INSERT INTO evidences(id,company_id,raw_message_id,artifact_id,source_url,source_type,excerpt,observed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (event_evidence_id, event_company_id, raw_message_id if raw_row else None, artifact_id, source_url, source_type, json.dumps(item, ensure_ascii=False), observed),
                )
            _merge_event(connection, event_company_id, event_batch_id, event, title_to_job if event_company_id == company_id else {}, event_evidence_id, observed)
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
        for event_company_id in set(event_company_ids):
            if event_company_id != company_id:
                queue_company_research_in_connection(connection, event_company_id)
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
    start_value = event.get("start_at") or event.get("date")
    end_value = event.get("end_at") or event.get("end_date")
    start_at = normalize_event_datetime(start_value, timezone_name, observed_at)
    end_at = normalize_event_datetime(end_value, timezone_name, observed_at)
    normalized_event = {**event, "start_at": start_at or "", "end_at": end_at or "", "timezone": timezone_name}
    location = str(event.get("location") or "").strip()
    title = str(event.get("title") or event_type).strip()
    existing = connection.execute(
        "SELECT * FROM recruitment_events WHERE company_id=? AND COALESCE(batch_id,'')=COALESCE(?,'') AND event_type=? AND lower(title)=lower(?) AND COALESCE(start_at,'')=COALESCE(?,'') AND COALESCE(location,'')=COALESCE(?,'') LIMIT 1",
        (company_id, batch_id, event_type, title, start_at, location),
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
            (event_id, company_id, batch_id, title, event_type, start_at, end_at,
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
    employment_type = normalize_employment_type(keep["employment_type"])
    duplicate_employment_type = normalize_employment_type(duplicate["employment_type"])
    if employment_type == "unknown":
        employment_type = duplicate_employment_type
    elif duplicate_employment_type not in {"unknown", employment_type}:
        employment_type = "unknown"
    updated_at = max(str(keep["updated_at"] or ""), str(duplicate["updated_at"] or "")) or utc_now()
    connection.execute(
        """UPDATE jobs SET department=?,employment_type=?,locations_json=?,headcount=?,education_json=?,majors_json=?,experience_requirement=?,
           salary_json=?,responsibilities=?,requirements=?,benefits_json=?,application_methods_json=?,contacts_json=?,
           explicit_deadline=?,effective_posted_at=?,last_effective_posted_at=?,status=?,industry_codes_json=?,
           job_function_codes_json=?,confidence=?,updated_at=? WHERE id=?""",
        (
            _merge_text(keep["department"], duplicate["department"]), employment_type, merged_lists["locations_json"],
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
            if _job_identity_matches(
                row,
                first["normalized_title"],
                first["recruitment_type"],
                normalize_employment_type(first["employment_type"]),
                first["department"],
            ):
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
