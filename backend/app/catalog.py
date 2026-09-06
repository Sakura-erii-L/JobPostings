from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import connect, one, utc_now
from .parsers import is_date_only_event_datetime, is_link_message, is_wechat_public_url, normalize_event_datetime, recover_original_source_url


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


def normalize_text_value(value: Any, *, preserve_newlines: bool = False) -> str | None:
    """Apply mechanical Unicode/whitespace cleanup without semantic inference."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if preserve_newlines:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        text = "\n".join(lines)
    else:
        text = re.sub(r"[ \t]+", " ", text).strip()
    return text or None


def normalize_url_value(value: Any) -> str | None:
    """URLs are only trimmed; their protocol, path and query stay untouched."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_url_or_text_list(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else ([value] if value is not None else [])
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        raw = str(item) if item is not None else ""
        cleaned = normalize_url_value(item) if re.match(r"https?://", raw.strip(), re.IGNORECASE) else normalize_text_value(item)
        if cleaned is None or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def normalize_text_list(value: Any, *, preserve_newlines: bool = False) -> list[str]:
    """Clean a string list, removing empty values and preserving first order."""
    values = value if isinstance(value, (list, tuple)) else ([value] if value is not None else [])
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        cleaned = normalize_text_value(item, preserve_newlines=preserve_newlines)
        if cleaned is None or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _normalize_int_list(value: Any) -> list[int]:
    values = value if isinstance(value, (list, tuple)) else ([value] if value is not None else [])
    result: list[int] = []
    seen: set[int] = set()
    for item in values:
        if isinstance(item, bool):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number not in seen:
            seen.add(number)
            result.append(number)
    return result


def _normalize_salary_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        cleaned_key = normalize_text_value(key)
        if cleaned_key is None:
            continue
        cleaned_value = normalize_text_value(item, preserve_newlines=True)
        if cleaned_value is not None:
            result[cleaned_key] = cleaned_value
    return result or None


def normalize_company_payload(value: Any) -> dict[str, Any]:
    """Mechanically clean model company fields; matching uses normalize_name separately."""
    source = value if isinstance(value, dict) else {}
    result = dict(source)
    scalar_fields = {
        "display_name", "legal_name", "company_nature", "primary_industry", "headquarters",
        "founded_at", "company_size",
    }
    list_fields = {
        "aliases", "secondary_industries", "industry_codes", "businesses", "highlights",
        "official_channels", "major_requirements",
    }
    for field in scalar_fields:
        result[field] = normalize_text_value(source.get(field), preserve_newlines=field in {"headquarters", "company_size"})
    result["website"] = normalize_url_value(source.get("website"))
    for field in list_fields:
        result[field] = normalize_url_or_text_list(source.get(field)) if field == "official_channels" else normalize_text_list(source.get(field))
    if isinstance(source.get("tags"), list):
        tags: list[dict[str, Any]] = []
        for tag in source["tags"]:
            if not isinstance(tag, dict):
                continue
            cleaned = dict(tag)
            for field in ("category", "code", "label"):
                cleaned[field] = normalize_text_value(tag.get(field))
            if not cleaned.get("category") or not cleaned.get("code") or not cleaned.get("label"):
                continue
            tags.append(cleaned)
        result["tags"] = tags
    if isinstance(source.get("relationship"), dict):
        relationship = dict(source["relationship"])
        relationship["type"] = normalize_text_value(relationship.get("type"))
        relationship["related_company_name"] = normalize_text_value(relationship.get("related_company_name"))
        result["relationship"] = relationship
    return result


def _normalize_job_payload(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = dict(source)
    for field in ("title", "department", "recruitment_type", "employment_type", "headcount", "experience_requirement", "deadline"):
        result[field] = normalize_text_value(source.get(field), preserve_newlines=field in {"experience_requirement", "headcount"})
    for field in ("locations", "education_requirements", "major_requirements", "benefits", "application_methods", "contacts", "industry_codes", "job_function_codes"):
        result[field] = normalize_text_list(source.get(field))
    for field in ("responsibilities", "requirements"):
        raw = source.get(field)
        if isinstance(raw, list):
            result[field] = normalize_text_list(raw, preserve_newlines=True)
        else:
            result[field] = normalize_text_value(raw, preserve_newlines=True)
    result["education"] = list(result.get("education_requirements") or normalize_text_list(source.get("education")))
    result["majors"] = list(result.get("major_requirements") or normalize_text_list(source.get("majors")))
    result["salary"] = _normalize_salary_payload(source.get("salary"))
    return result


def _normalize_event_payload(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = dict(source)
    for field in ("title", "event_type", "timezone", "format", "city", "campus", "location", "application_url", "audience"):
        result[field] = normalize_url_value(source.get(field)) if field == "application_url" else normalize_text_value(source.get(field))
    for field in ("start_at", "end_at", "date", "end_date"):
        result[field] = normalize_text_value(source.get(field))
    result["notes"] = normalize_text_value(source.get("notes"), preserve_newlines=True)
    result["job_titles"] = normalize_text_list(source.get("job_titles"))
    return result


def _append_date_only_event_note(value: Any) -> str:
    note = normalize_text_value(value, preserve_newlines=True) or ""
    if "未发具体时间" in note:
        return note
    return f"{note}\n未发具体时间".strip()


def normalize_recruitment_payload(value: Any) -> dict[str, Any]:
    """Clean only known structured fields before validation/persistence."""
    source = value if isinstance(value, dict) else {}
    result = dict(source)
    result["is_recruitment"] = source.get("is_recruitment")
    result["decision_reason"] = normalize_text_value(source.get("decision_reason"), preserve_newlines=True) or ""
    companies: list[dict[str, Any]] = []
    for entry in source.get("companies") if isinstance(source.get("companies"), list) else []:
        if not isinstance(entry, dict):
            continue
        company_entry = dict(entry)
        company_entry["company"] = normalize_company_payload(entry.get("company"))
        recruitment = dict(entry.get("recruitment") or {}) if isinstance(entry.get("recruitment"), dict) else {}
        batch = dict(recruitment.get("batch") or {}) if isinstance(recruitment.get("batch"), dict) else {}
        batch["name"] = normalize_text_value(batch.get("name"))
        batch["season"] = normalize_text_value(batch.get("season"))
        batch["recruitment_type"] = normalize_text_value(batch.get("recruitment_type"))
        if batch.get("year") is not None and not isinstance(batch.get("year"), bool):
            try:
                batch["year"] = int(batch["year"])
            except (TypeError, ValueError):
                batch["year"] = None
        else:
            batch["year"] = None
        shared = dict(recruitment.get("shared_details") or {}) if isinstance(recruitment.get("shared_details"), dict) else {}
        shared["locations"] = normalize_text_list(shared.get("locations"))
        shared["salary"] = _normalize_salary_payload(shared.get("salary"))
        shared["target_graduation_years"] = _normalize_int_list(shared.get("target_graduation_years"))
        for field in ("education_requirements", "major_requirements", "process", "benefits"):
            shared[field] = normalize_text_list(shared.get(field), preserve_newlines=field in {"process", "benefits"})
        shared["application_url"] = normalize_url_value(shared.get("application_url"))
        shared["deadline"] = normalize_text_value(shared.get("deadline"))
        recruitment["batch"] = batch
        recruitment["shared_details"] = shared
        recruitment["jobs"] = [_normalize_job_payload(job) for job in recruitment.get("jobs") or [] if isinstance(job, dict)]
        recruitment["events"] = [_normalize_event_payload(event) for event in recruitment.get("events") or [] if isinstance(event, dict)]
        company_entry["recruitment"] = recruitment
        companies.append(company_entry)
    result["companies"] = companies
    return result


def _parse_reliable_datetime(value: Any) -> datetime | None:
    """Parse only complete ISO date/datetime values for status comparisons."""
    text = normalize_text_value(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_event_status_datetime(value: Any, timezone_name: Any = "Asia/Shanghai") -> datetime | None:
    """Parse an event timestamp for display without trusting the stored status."""
    text = normalize_text_value(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(str(timezone_name or "Asia/Shanghai")))
        except Exception:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc)


def recruitment_event_state(event: Any, now: datetime | None = None) -> str:
    """Return the single display state used by every recruitment timeline view."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    timezone_name = event.get("timezone") if isinstance(event, dict) else None
    start_at = _parse_event_status_datetime(event.get("start_at"), timezone_name) if isinstance(event, dict) else None
    end_at = _parse_event_status_datetime(event.get("end_at"), timezone_name) if isinstance(event, dict) else None
    if start_at is None:
        return "uncertain"
    if end_at is not None and start_at <= current <= end_at:
        return "ongoing"
    if current < start_at:
        return "upcoming"
    return "historical"


def recruitment_event_sort_key(event: Any, now: datetime | None = None) -> tuple[int, float, float, str]:
    """Sort events as ongoing, upcoming, historical, then time-uncertain."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    timezone_name = event.get("timezone") if isinstance(event, dict) else None
    start_at = _parse_event_status_datetime(event.get("start_at"), timezone_name) if isinstance(event, dict) else None
    end_at = _parse_event_status_datetime(event.get("end_at"), timezone_name) if isinstance(event, dict) else None
    state = recruitment_event_state(event, current)
    start_timestamp = start_at.timestamp() if start_at is not None else float("inf")
    end_timestamp = end_at.timestamp() if end_at is not None else float("inf")
    event_id = str(event.get("id") or "") if isinstance(event, dict) else ""
    if state == "ongoing":
        return (0, 0 if end_at is not None else 1, end_timestamp, event_id)
    if state == "upcoming":
        return (1, start_timestamp, 0.0, event_id)
    if state == "historical":
        return (2, -(end_timestamp if end_at is not None else start_timestamp), 0.0, event_id)
    return (3, float("inf"), 0.0, event_id)


def _deadline_is_expired(value: Any) -> bool:
    text = normalize_text_value(value)
    if not text:
        return False

    try:
        deadline_date = date.fromisoformat(text)
        return deadline_date < datetime.now(timezone.utc).date()
    except ValueError:
        pass

    parsed = _parse_reliable_datetime(text)
    if parsed is None:
        return False

    return parsed < datetime.now(timezone.utc)


def _choose_temporal_text(values: list[Any], *, latest: bool) -> str | None:
    candidates = [normalize_text_value(value) for value in values]
    candidates = [value for value in candidates if value]
    if not candidates:
        return None
    parsed = [(value, _parse_reliable_datetime(value)) for value in candidates]
    reliable = [(value, moment) for value, moment in parsed if moment is not None]
    if not reliable:
        return candidates[0]
    chosen = (max if latest else min)(reliable, key=lambda pair: pair[1])
    return chosen[0]


NON_JOB_TITLE_PATTERNS = (
    r"网申|报名|投递|网址|链接|二维码",
    r"简历(?:筛选|匹配)|资格(?:初审|审查)|初审",
    r"测评|笔试|面试|体检|录用|入职|签约|公示|审核",
    r"招聘流程|招聘行程|校招行程|活动(?:时间|安排|对象|形式)|宣讲会|招聘会|参访|大咖分享",
    r"安家费|年收入|薪资|工资|福利|津贴|补贴|奖金|事业编制|住房|公寓",
    r"博士研究生|硕士研究生|博士|硕士|本科|毕业|应届生|面向对象|活动对象",
    r"具体岗位(?:见|以)|(?:岗位|职位)(?:列表|汇总|类别|职责|要求)",
    r"工作地点|岗位地点|办公地点|工作城市|招聘地点",
    r"^(?:[^岗位]{1,30})(?:类|专业|类别|方向)$",
)

# 纯地点是 OCR/模型最容易误报成岗位标题的一类值。这里只拦截“地点本身”
# 或明显的地点串，不会影响“北京研发工程师”这类真实岗位名称。
LOCATION_TITLE_NAMES = frozenset({
    "北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "苏州", "无锡",
    "常州", "合肥", "武汉", "西安", "成都", "长沙", "郑州", "济南", "青岛", "厦门",
    "福州", "南昌", "昆明", "贵阳", "太原", "石家庄", "沈阳", "大连", "长春", "哈尔滨",
    "海口", "乌鲁木齐", "兰州", "银川", "呼和浩特", "南宁", "珠海", "东莞", "佛山",
    "宁波", "嘉兴", "绍兴", "岳阳", "日本", "海外", "国内", "全国",
})


def is_location_like_title(value: Any) -> bool:
    """Return whether a candidate title is a venue/location label, not a job role."""
    title = re.sub(r"\s+", "", str(value or "").strip("。；;，, "))
    if not title:
        return True
    if re.search(r"(?:工作地点|岗位地点|办公地点|工作城市|招聘地点)", title):
        return True
    if any(title.startswith(location) for location in LOCATION_TITLE_NAMES) and re.search(r"(?:注[:：]|说明|部分(?:非|不)|全球派遣)", title):
        return True
    # Location-only lists can arrive as one model title when OCR removed line breaks.
    parts = [part for part in re.split(r"[、/／,，;；|｜]+", title) if part]
    return bool(parts) and all(part in LOCATION_TITLE_NAMES for part in parts)


def is_non_job_title(value: Any) -> bool:
    """Reject process, eligibility, benefits, event and URL text as job titles."""
    title = re.sub(r"\s+", "", str(value or "").strip())
    if not title:
        return True
    if len(title) > 120 or re.fullmatch(r"https?://\S+|[\w.-]+\.(?:com|cn|org|net)", title, re.IGNORECASE):
        return True
    if re.search(r"(?:20\d{2}年)?\d{1,2}月\d{1,2}日|20\d{2}届|\d{1,2}月(?:底|初)", title):
        return True
    return is_location_like_title(title) or any(re.search(pattern, title, re.IGNORECASE) for pattern in NON_JOB_TITLE_PATTERNS)


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
    seen_attribute_labels: set[str] = set()

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
            label = normalize_text_value(label or code) or ""
            label = label[:80]
            if not code or not label:
                return
            resolved_label = label
        else:
            return
        key = (category, code)
        if category == "attribute":
            label_key = re.sub(r"\s+", " ", label or resolved_label).strip().casefold()
            if label_key in seen_attribute_labels:
                return
            seen_attribute_labels.add(label_key)
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
    artifact_rows = [
        dict(row)
        for row in connection.execute(
            "SELECT id,filename,ocr_text,created_at FROM artifacts WHERE raw_message_id=?",
            (raw_message_id,),
        ).fetchall()
        if row["ocr_text"]
    ]

    def artifact_order(row: dict[str, Any]) -> tuple[int, int, str, str]:
        match = re.search(r"(?:linked-image|input)-(\d+)", str(row.get("filename") or ""), re.IGNORECASE)
        if match:
            return (0, int(match.group(1)), str(row.get("created_at") or ""), str(row.get("id") or ""))
        return (1, 0, str(row.get("created_at") or ""), str(row.get("id") or ""))

    texts.extend(str(row["ocr_text"] or "") for row in sorted(artifact_rows, key=artifact_order))
    return "\n".join(text for text in texts if text).strip()


def _prepare_job_items(job_values: Any) -> list[dict[str, Any]]:
    """Normalize only the structure returned by Codex."""
    prepared: list[dict[str, Any]] = []
    for value in job_values if isinstance(job_values, list) else []:
        if not isinstance(value, dict):
            continue
        job = _normalize_job_payload(value)
        title = job.get("title")
        if not title:
            continue
        job["title"] = title
        job["locations"] = normalize_text_list(job.get("locations"))
        job["education"] = normalize_text_list(job.get("education") or job.get("education_requirements"))
        job["majors"] = normalize_text_list(job.get("majors") or job.get("major_requirements"))
        for key in ("responsibilities", "requirements"):
            values = job.get(key) or []
            if isinstance(values, list):
                job[key] = "\n".join(normalize_text_list(values, preserve_newlines=True)) or None
            else:
                job[key] = normalize_text_value(values, preserve_newlines=True)
        job["benefits"] = normalize_text_list(job.get("benefits"), preserve_newlines=True)
        job["application_methods"] = normalize_text_list(job.get("application_methods"))
        job["contacts"] = normalize_text_list(job.get("contacts"), preserve_newlines=True)
        job["salary"] = _normalize_salary_payload(job.get("salary"))
        prepared.append(job)
    return prepared


def _store_recruitment_shared_details(
    connection: Any,
    company_id: str,
    batch_id: str | None,
    evidence_id: str | None,
    raw_message_id: str | None,
    details: dict[str, Any],
    observed_at: str,
) -> str | None:
    normalized = {
        "locations": normalize_text_list(details.get("locations")),
        "salary": _normalize_salary_payload(details.get("salary")),
        "target_graduation_years": _normalize_int_list(details.get("target_graduation_years")),
        "education_requirements": normalize_text_list(details.get("education_requirements")),
        "major_requirements": normalize_text_list(details.get("major_requirements")),
        "application_url": normalize_url_value(details.get("application_url")),
        "deadline": normalize_text_value(details.get("deadline")),
        "process": normalize_text_list(details.get("process"), preserve_newlines=True),
        "benefits": normalize_text_list(details.get("benefits"), preserve_newlines=True),
    }
    if not any((normalized["locations"], normalized["salary"], normalized["target_graduation_years"], normalized["education_requirements"], normalized["major_requirements"], normalized["application_url"], normalized["deadline"], normalized["process"], normalized["benefits"])):
        return None
    existing = connection.execute(
        """SELECT * FROM recruitment_shared_details
           WHERE company_id=? AND COALESCE(batch_id,'')=COALESCE(?,'') AND COALESCE(raw_message_id,'')=COALESCE(?,'')""",
        (company_id, batch_id, raw_message_id),
    ).fetchone()
    now = utc_now()
    if existing:
        def existing_json_list(column: str) -> list[Any]:
            try:
                value = json.loads(existing[column] or "[]")
            except (TypeError, json.JSONDecodeError):
                value = []
            return value if isinstance(value, list) else []

        merged_locations = list(dict.fromkeys([*existing_json_list("locations_json"), *normalized["locations"]]))
        merged_target_years = list(dict.fromkeys([*existing_json_list("target_graduation_years_json"), *normalized["target_graduation_years"]]))
        merged_education = list(dict.fromkeys([*existing_json_list("education_requirements_json"), *normalized["education_requirements"]]))
        merged_majors = list(dict.fromkeys([*existing_json_list("major_requirements_json"), *normalized["major_requirements"]]))
        merged_process = list(dict.fromkeys([*existing_json_list("process_json"), *normalized["process"]]))
        merged_benefits = list(dict.fromkeys([*existing_json_list("benefits_json"), *normalized["benefits"]]))
        try:
            old_salary = json.loads(existing["salary_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            old_salary = {}
        old_salary = old_salary if isinstance(old_salary, dict) else {}
        merged_salary = {**old_salary, **(normalized["salary"] or {})}
        connection.execute(
            """UPDATE recruitment_shared_details SET evidence_id=COALESCE(?,evidence_id),locations_json=?,salary_json=?
               ,target_graduation_years_json=?,education_requirements_json=?,major_requirements_json=?,application_url=?,deadline=?
               ,process_json=?,benefits_json=?,observed_at=?,updated_at=? WHERE id=?""",
            (evidence_id, json_text(merged_locations, []), json_text(merged_salary, {}),
             json_text(merged_target_years, []), json_text(merged_education, []),
             json_text(merged_majors, []), normalized["application_url"] or existing["application_url"],
             normalized["deadline"] or existing["deadline"], json_text(merged_process, []),
             json_text(merged_benefits, []), observed_at, now, existing["id"]),
        )
        return existing["id"]
    detail_id = str(uuid4())
    connection.execute(
        """INSERT INTO recruitment_shared_details(
               id,company_id,batch_id,evidence_id,raw_message_id,locations_json,salary_json,target_graduation_years_json,
               education_requirements_json,major_requirements_json,application_url,deadline,process_json,benefits_json,
               observed_at,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (detail_id, company_id, batch_id, evidence_id, raw_message_id, json_text(normalized["locations"], []),
         json_text(normalized["salary"], {}), json_text(normalized["target_graduation_years"], []),
         json_text(normalized["education_requirements"], []), json_text(normalized["major_requirements"], []),
         normalized["application_url"], normalized["deadline"], json_text(normalized["process"], []),
         json_text(normalized["benefits"], []), observed_at, now, now),
    )
    return detail_id


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


class CompanyIdentityConflict(RuntimeError):
    """Raised when an incoming company cannot be safely bound to one record."""


def _company_names(row: Any) -> set[str]:
    names = [row["display_name"], row["legal_name"]]
    try:
        names.extend(json.loads(row["aliases_json"] or "[]"))
    except (TypeError, json.JSONDecodeError):
        pass
    return {normalize_name(normalize_text_value(value) or "") for value in names if normalize_text_value(value)}


def _company_for(connection, company_data: dict[str, Any]) -> str:
    company_data = normalize_company_payload(company_data)
    display_name = company_data.get("display_name") or ""
    legal_name = company_data.get("legal_name") or None
    aliases = company_data.get("aliases") or []
    if not display_name:
        display_name = legal_name or f"未识别企业-{uuid4().hex[:8]}"
    incoming_names = {
        normalize_name(normalize_text_value(value) or "")
        for value in [display_name, legal_name, *aliases]
        if normalize_text_value(value)
    }
    matched_company_id = str(company_data.get("matched_company_id") or "").strip()
    rows = connection.execute("SELECT * FROM companies ORDER BY created_at,id").fetchall()
    candidates = [row for row in rows if incoming_names.intersection(_company_names(row))]
    if matched_company_id:
        explicit = next((row for row in rows if row["id"] == matched_company_id), None)
        if explicit and explicit not in candidates and incoming_names.intersection(_company_names(explicit)):
            candidates.append(explicit)
    if len(candidates) > 1:
        raise CompanyIdentityConflict(
            f"企业名称与多个现有企业匹配，需要人工审核：{', '.join(str(row['id']) for row in candidates)}"
        )
    existing = candidates[0] if candidates else None
    if existing and legal_name:
        existing_legal = normalize_name(normalize_text_value(existing["legal_name"]) or "")
        incoming_legal = normalize_name(legal_name)
        if existing_legal and existing_legal != incoming_legal:
            raise CompanyIdentityConflict(
                f"企业法定名称冲突，需要人工审核：{existing['legal_name']} / {legal_name}"
            )
    now = utc_now()
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
    name = normalize_text_value(batch.get("name")) or "未命名批次"
    year = batch.get("year")
    if isinstance(year, bool):
        year = None
    elif year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None
    season = normalize_text_value(batch.get("season"))
    existing = connection.execute(
        """SELECT id FROM recruitment_batches
           WHERE company_id=? AND name=? AND recruitment_type=? AND year IS ? AND season IS ?""",
        (company_id, name, recruitment_type, year, season),
    ).fetchone()
    if existing:
        return existing["id"]
    batch_id = str(uuid4())
    now = utc_now()
    connection.execute(
        "INSERT INTO recruitment_batches(id,company_id,name,year,season,recruitment_type,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (batch_id, company_id, name, year, season, recruitment_type, float(batch.get("confidence", 0)), now, now),
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
    if row["normalized_title"] != normalized_title:
        return False

    if row["recruitment_type"] != recruitment_type:
        return False

    known_department = normalize_title(str(row["department"] or ""))
    current_department = normalize_title(str(department or ""))

    if known_department and current_department and known_department != current_department:
        return False

    known_employment = normalize_employment_type(row["employment_type"])
    current_employment = normalize_employment_type(employment_type)

    if (
        known_employment != "unknown"
        and current_employment != "unknown"
        and known_employment != current_employment
    ):
        return False

    return True


def _make_job(connection, company_id: str, batch_id: str | None, job_data: dict[str, Any], observed_at: str, raw_message_id: str | None) -> str:
    job_data = _normalize_job_payload(job_data)
    title = job_data.get("title") or "未命名岗位"
    normalized = normalize_title(title)
    recruitment_type = str(job_data.get("recruitment_type") or "unknown")
    if recruitment_type not in RECRUITMENT_TYPES:
        recruitment_type = "unknown"
    employment_type = normalize_employment_type(job_data.get("employment_type"))
    locations = normalize_text_list(job_data.get("locations"))
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
    explicit_deadline = normalize_text_value(job_data.get("deadline") or job_data.get("explicit_deadline"))
    payload = {**job_data, "deadline": explicit_deadline or ""}
    content_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if row:
        job_id = row["id"]
        last = observed_at or now
        status = row["status"]
        if explicit_deadline:
            status = "expired" if _deadline_is_expired(explicit_deadline) else "active"
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
        status = "expired" if _deadline_is_expired(explicit_deadline) else "active"
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


def _queue_event_semantic_dedup(connection: Any, company_id: str, parent_job_id: str | None, ready_at: str) -> None:
    event_count = connection.execute(
        "SELECT COUNT(*) AS count FROM recruitment_events WHERE company_id=?",
        (company_id,),
    ).fetchone()["count"]
    if int(event_count or 0) < 2:
        return
    pending = connection.execute(
        """SELECT id FROM processing_jobs
           WHERE kind='deduplicate_events' AND company_id=? AND status='pending' AND cancel_requested=0
           ORDER BY created_at DESC,id DESC LIMIT 1""",
        (company_id,),
    ).fetchone()
    if pending:
        connection.execute(
            "UPDATE processing_jobs SET parent_job_id=COALESCE(?,parent_job_id),next_attempt_at=?,updated_at=? WHERE id=?",
            (parent_job_id, ready_at, utc_now(), pending["id"]),
        )
        return
    connection.execute(
        """INSERT INTO processing_jobs(
           id,kind,company_id,parent_job_id,status,stage,next_attempt_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (str(uuid4()), "deduplicate_events", company_id, parent_job_id, "pending", "waiting_for_sources", ready_at, utc_now(), utc_now()),
    )


def apply_model_item(item: dict[str, Any], raw_message_id: str | None, observed_at: str | None) -> dict[str, Any]:
    persistence: dict[str, Any] = {
        "company_ids": [],
        "job_ids": [],
        "company_names": [],
        "created_company_count": 0,
        "updated_company_count": 0,
        "invalid_company_entries": [],
        "invalid_company_count": 0,
    }
    if not item.get("is_recruitment"):
        return persistence
    # Mechanical compatibility for explicitly supplied legacy callers.  This
    # does not inspect source text or classify any value; normal model output
    # always arrives through the strict ``companies`` schema.
    if "companies" not in item and isinstance(item.get("company"), dict):
        shared = item.get("shared_job_info") if isinstance(item.get("shared_job_info"), dict) else {}
        item = {
            "is_recruitment": True,
            "companies": [{
                "company": dict(item.get("company") or {}),
                "recruitment": {
                    "batch": dict(item.get("batch") or {}),
                    "shared_details": {
                        "locations": list(shared.get("locations") or []),
                        "salary": shared.get("salary"),
                        "target_graduation_years": [], "education_requirements": [],
                        "major_requirements": [], "application_url": None, "deadline": None,
                        "process": [], "benefits": [],
                    },
                    "jobs": list(item.get("jobs") or []),
                    "events": list(item.get("events") or []),
                },
            }],
        }
    item = normalize_recruitment_payload(item)
    observed = observed_at or utc_now()
    company_items = item.get("companies") if isinstance(item.get("companies"), list) else []
    if not company_items:
        return persistence
    all_job_ids: list[str] = []
    persisted_company_ids: list[str] = []
    created_company_ids: set[str] = set()
    updated_company_ids: set[str] = set()
    with connect() as connection:
        raw_row = connection.execute("SELECT connector_id,message_type,metadata_json FROM raw_messages WHERE id=?", (raw_message_id,)).fetchone() if raw_message_id else None
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
        parent_job = connection.execute(
            "SELECT id FROM processing_jobs WHERE kind='classify' AND raw_message_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (raw_message_id,),
        ).fetchone() if raw_message_id else None
        parent_job_id = parent_job["id"] if parent_job else None
        initial_company_ids = {row["id"] for row in connection.execute("SELECT id FROM companies").fetchall()}
        for index, company_entry in enumerate(company_items):
            if not isinstance(company_entry, dict):
                persistence["invalid_company_entries"].append({"index": index, "reason": "company entry is not an object"})
                continue
            raw_company = company_entry.get("company")
            if not isinstance(raw_company, dict):
                persistence["invalid_company_entries"].append({"index": index, "reason": "company is not an object"})
                continue
            company = normalize_company_payload(raw_company)
            raw_recruitment = company_entry.get("recruitment")
            if raw_recruitment is not None and not isinstance(raw_recruitment, dict):
                persistence["invalid_company_entries"].append({"index": index, "reason": "recruitment is not an object"})
                continue
            recruitment = dict(raw_recruitment or {})
            company_name = company.get("display_name") or company.get("legal_name") or ""
            if not company_name:
                persistence["invalid_company_entries"].append({"index": index, "reason": "display_name and legal_name are empty"})
                continue
            industry_codes = list(dict.fromkeys([
                value for value in [*(company.get("industry_codes") or []), company.get("primary_industry"), *(company.get("secondary_industries") or [])]
                if isinstance(value, str) and value.strip()
            ]))
            company["industry_codes"] = industry_codes
            try:
                company_id = _company_for(connection, company)
            except CompanyIdentityConflict as exc:
                persistence["invalid_company_entries"].append({"index": index, "reason": "identity_conflict", "detail": str(exc)})
                continue
            if company_id in initial_company_ids:
                updated_company_ids.add(company_id)
            else:
                created_company_ids.add(company_id)
            persisted_company_ids.append(company_id)
            _record_company_relationship(connection, company_id, company.get("relationship") or {})
            evidence_id = str(uuid4())
            connection.execute(
                "INSERT INTO evidences(id,company_id,raw_message_id,artifact_id,source_url,source_type,excerpt,observed_at) VALUES(?,?,?,?,?,?,?,?)",
                (evidence_id, company_id, raw_message_id if raw_row else None, artifact_id, source_url, source_type, json.dumps(company_entry, ensure_ascii=False), observed),
            )
            for field_name, value in company.items():
                if field_name in {"industry_codes", "relationship"} or value in (None, "", []):
                    continue
                connection.execute(
                    "INSERT INTO company_claims(id,company_id,field_name,field_value,source_url,source_type,retrieved_at,confidence,is_current) VALUES(?,?,?,?,?,?,?,?,1)",
                    (str(uuid4()), company_id, field_name, json.dumps(value, ensure_ascii=False), source_url, source_type, observed, 1.0),
                )
            batch = recruitment.get("batch") if isinstance(recruitment.get("batch"), dict) else {}
            recruitment_type = str(batch.get("recruitment_type") or "unknown")
            if recruitment_type not in RECRUITMENT_TYPES:
                recruitment_type = "unknown"
            batch_id = _batch_for(connection, company_id, batch, recruitment_type)
            shared = recruitment.get("shared_details") if isinstance(recruitment.get("shared_details"), dict) else {}
            _store_recruitment_shared_details(connection, company_id, batch_id, evidence_id, raw_message_id, shared, observed)
            job_items = _prepare_job_items(recruitment.get("jobs") or [])
            job_ids = [_make_job(connection, company_id, batch_id, job, observed, raw_message_id) for job in job_items]
            all_job_ids.extend(job_ids)
            title_to_job = {normalize_title(str(job.get("title") or "")): job_id for job, job_id in zip(job_items, job_ids)}
            for event in recruitment.get("events") or []:
                if isinstance(event, dict):
                    _merge_event(connection, company_id, batch_id, event, title_to_job, evidence_id, observed)
        ready_at = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="seconds")
        for company_id in dict.fromkeys(persisted_company_ids):
            pending = connection.execute(
                "SELECT id FROM processing_jobs WHERE kind='consolidate_company' AND company_id=? AND status='pending' LIMIT 1",
                (company_id,),
            ).fetchone()
            if pending:
                connection.execute(
                    "UPDATE processing_jobs SET parent_job_id=COALESCE(?,parent_job_id),next_attempt_at=?,updated_at=? WHERE id=?",
                    (parent_job_id, ready_at, utc_now(), pending["id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO processing_jobs(id,kind,company_id,parent_job_id,status,stage,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid4()), "consolidate_company", company_id, parent_job_id, "pending", "waiting_for_sources", ready_at, utc_now(), utc_now()),
                )
            from .company_research import queue_company_research_in_connection
            queue_company_research_in_connection(connection, company_id, parent_job_id=parent_job_id)
            deduplicate_company_jobs(connection, company_id)
            _queue_event_semantic_dedup(connection, company_id, parent_job_id, ready_at)
    persistence["company_ids"] = list(dict.fromkeys(persisted_company_ids))
    persistence["job_ids"] = list(dict.fromkeys(all_job_ids))
    persistence["created_company_count"] = len(created_company_ids)
    persistence["updated_company_count"] = len(updated_company_ids)
    persistence["company_names"] = list(dict.fromkeys(
        normalize_text_value((entry.get("company") or {}).get("display_name") or (entry.get("company") or {}).get("legal_name")) or ""
        for entry in company_items
        if isinstance(entry, dict) and isinstance(entry.get("company"), dict)
        and normalize_text_value((entry.get("company") or {}).get("display_name") or (entry.get("company") or {}).get("legal_name"))
    ))
    persistence["invalid_company_count"] = len(persistence["invalid_company_entries"])
    return persistence


def _merge_event(
    connection: Any,
    company_id: str,
    batch_id: str | None,
    event: dict[str, Any],
    title_to_job: dict[str, str],
    evidence_id: str,
    observed_at: str,
) -> str:
    event = _normalize_event_payload(event)
    event_type = event.get("event_type") or "other"
    timezone_name = event.get("timezone") or "Asia/Shanghai"
    start_value = event.get("start_at") or event.get("date")
    end_value = event.get("end_at") or event.get("end_date")
    date_only_start = bool(start_value and is_date_only_event_datetime(start_value))
    start_at = normalize_event_datetime(start_value, timezone_name, observed_at, date_only_default_hour=14)
    end_at = normalize_event_datetime(end_value, timezone_name, observed_at)
    if date_only_start and start_at:
        event["notes"] = _append_date_only_event_note(event.get("notes"))
    normalized_event = {**event, "start_at": start_at or "", "end_at": end_at or "", "timezone": timezone_name}
    location = event.get("location") or ""
    title = event.get("title") or event_type
    identity_match = _find_matching_recruitment_event(connection, company_id, event_type, title, event, None, None)
    existing = _find_matching_recruitment_event(connection, company_id, event_type, title, event, start_at, end_at)
    time_conflict = bool(identity_match and existing is None and not _event_times_compatible(identity_match, start_at, end_at))
    job_ids = [title_to_job[normalize_title(str(title))] for title in event.get("job_titles") or [] if normalize_title(str(title)) in title_to_job]
    now = utc_now()
    if existing:
        event_id = existing["id"]
        merged_jobs = _merge_list(existing["job_ids_json"], job_ids)
        notes = event.get("notes") or None
        if date_only_start and notes and existing["notes"] and notes not in existing["notes"]:
            notes = f"{existing['notes']}\n{notes}"
        connection.execute(
            """UPDATE recruitment_events SET batch_id=COALESCE(batch_id,?),start_at=COALESCE(start_at,?),end_at=COALESCE(?,end_at),city=COALESCE(?,city),campus=COALESCE(?,campus),location=COALESCE(?,location),
               application_url=COALESCE(?,application_url),audience=COALESCE(?,audience),notes=COALESCE(?,notes),
               job_ids_json=?,updated_at=? WHERE id=?""",
            (batch_id, start_at, end_at, event.get("city") or None, event.get("campus") or None, event.get("location") or None,
             event.get("application_url") or None, event.get("audience") or None, notes,
             json_text(merged_jobs, []), now, event_id),
        )
    else:
        event_id = str(uuid4())
        status = recruitment_event_state(
            {"start_at": start_at, "end_at": end_at, "timezone": timezone_name},
            _parse_reliable_datetime(now),
        )
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
    if time_conflict and identity_match:
        _record_event_time_conflict(connection, identity_match, event_id, normalized_event)
    return event_id


def _event_identity_text(value: Any, *, title: bool = False) -> str:
    cleaned = normalize_text_value(value) or ""
    return normalize_title(cleaned) if title else cleaned.casefold()


def _event_field_value(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value.get(field)
    try:
        return value[field]
    except (IndexError, KeyError, TypeError):
        return None


def _event_location_identity(value: Any) -> tuple[str, str, str]:
    return tuple(_event_identity_text(_event_field_value(value, field)) for field in ("city", "campus", "location"))


def _event_location_compatible(left: Any, right: Any) -> bool:
    return all(
        not left_value or not right_value or left_value == right_value
        for left_value, right_value in zip(_event_location_identity(left), _event_location_identity(right))
    )


def _event_identity_matches(left: Any, right: Any) -> bool:
    left_company = _event_identity_text(_event_field_value(left, "company_id"))
    right_company = _event_identity_text(_event_field_value(right, "company_id"))
    if left_company and right_company and left_company != right_company:
        return False
    if _event_identity_text(_event_field_value(left, "event_type")) != _event_identity_text(_event_field_value(right, "event_type")):
        return False
    if _event_identity_text(_event_field_value(left, "title"), title=True) != _event_identity_text(_event_field_value(right, "title"), title=True):
        return False
    return _event_location_compatible(left, right)


def _event_times_compatible(existing: Any, start_at: str | None, end_at: str | None) -> bool:
    if existing["start_at"] and start_at and normalize_text_value(existing["start_at"]) != normalize_text_value(start_at):
        return False
    if existing["end_at"] and end_at and normalize_text_value(existing["end_at"]) != normalize_text_value(end_at):
        return False
    return True


def _find_matching_recruitment_event(
    connection: Any,
    company_id: str,
    event_type: str,
    title: str,
    event: dict[str, Any],
    start_at: str | None,
    end_at: str | None,
) -> Any | None:
    identity_probe = {"company_id": company_id, **event, "event_type": event_type, "title": title}
    for row in connection.execute("SELECT * FROM recruitment_events WHERE company_id=? ORDER BY created_at,id", (company_id,)).fetchall():
        if not _event_identity_matches(row, identity_probe):
            continue
        if _event_times_compatible(row, start_at, end_at):
            return row
    return None


def _event_row_payload(row: Any) -> dict[str, Any]:
    return {
        "title": row["title"],
        "event_type": row["event_type"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "timezone": row["timezone"],
        "format": row["format"],
        "city": row["city"],
        "campus": row["campus"],
        "location": row["location"],
        "application_url": row["application_url"],
        "audience": row["audience"],
        "notes": row["notes"],
        "job_titles": [],
    }


def _record_event_time_conflict(connection: Any, existing: Any, incoming_event_id: str, incoming: dict[str, Any]) -> None:
    existing_review = connection.execute(
        "SELECT id FROM review_items WHERE kind='event_time_conflict' AND entity_type='recruitment_event' AND entity_id=? AND status='open' LIMIT 1",
        (incoming_event_id,),
    ).fetchone()
    if existing_review:
        return
    payload = {
        "event_id": incoming_event_id,
        "existing_event_id": existing["id"],
        "reason": "双方都有明确但不同的活动时间，保留两条 event 等待人工审核",
        "existing": _event_row_payload(existing),
        "incoming": incoming,
    }
    connection.execute(
        "INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
        (str(uuid4()), "event_time_conflict", "recruitment_event", incoming_event_id, json.dumps(payload, ensure_ascii=False), utc_now()),
    )


def _merge_event_records(connection: Any, keep: Any, duplicate: Any) -> None:
    try:
        keep_jobs = json.loads(keep["job_ids_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        keep_jobs = []
    try:
        duplicate_jobs = json.loads(duplicate["job_ids_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        duplicate_jobs = []
    merged_jobs = list(dict.fromkeys([*keep_jobs, *duplicate_jobs]))
    now = utc_now()
    status = recruitment_event_state(
        {"start_at": keep["start_at"] or duplicate["start_at"], "end_at": keep["end_at"] or duplicate["end_at"], "timezone": keep["timezone"] or duplicate["timezone"]},
        _parse_reliable_datetime(now),
    )
    connection.execute(
        """UPDATE recruitment_events SET batch_id=COALESCE(batch_id,?),start_at=COALESCE(start_at,?),end_at=COALESCE(end_at,?),
           timezone=COALESCE(NULLIF(timezone,''),?),format=COALESCE(NULLIF(format,''),?),city=COALESCE(city,?),campus=COALESCE(campus,?),location=COALESCE(location,?),application_url=COALESCE(application_url,?),
           audience=COALESCE(audience,?),notes=COALESCE(notes,?),job_ids_json=?,status=?,updated_at=? WHERE id=?""",
        (duplicate["batch_id"], duplicate["start_at"], duplicate["end_at"], duplicate["timezone"], duplicate["format"], duplicate["city"], duplicate["campus"], duplicate["location"],
         duplicate["application_url"], duplicate["audience"], duplicate["notes"], json_text(merged_jobs, []), status, now, keep["id"]),
    )
    for version in connection.execute("SELECT id,payload_json,observed_at,is_current FROM recruitment_event_versions WHERE event_id=?", (duplicate["id"],)).fetchall():
        same = connection.execute(
            "SELECT id FROM recruitment_event_versions WHERE event_id=? AND payload_json=? LIMIT 1",
            (keep["id"], version["payload_json"]),
        ).fetchone()
        if same:
            connection.execute("DELETE FROM recruitment_event_versions WHERE id=?", (version["id"],))
        else:
            connection.execute("UPDATE recruitment_event_versions SET event_id=? WHERE id=?", (keep["id"], version["id"]))
    for evidence in connection.execute("SELECT evidence_id FROM recruitment_event_evidences WHERE event_id=?", (duplicate["id"],)).fetchall():
        connection.execute("INSERT OR IGNORE INTO recruitment_event_evidences(event_id,evidence_id) VALUES(?,?)", (keep["id"], evidence["evidence_id"]))
    connection.execute("DELETE FROM recruitment_event_evidences WHERE event_id=?", (duplicate["id"],))
    connection.execute("UPDATE recruitment_event_versions SET is_current=0 WHERE event_id=?", (keep["id"],))
    latest = connection.execute(
        "SELECT id FROM recruitment_event_versions WHERE event_id=? ORDER BY observed_at DESC,id DESC LIMIT 1",
        (keep["id"],),
    ).fetchone()
    if latest:
        connection.execute("UPDATE recruitment_event_versions SET is_current=1 WHERE id=?", (latest["id"],))
    connection.execute("DELETE FROM recruitment_events WHERE id=?", (duplicate["id"],))


def deduplicate_company_events(connection: Any, company_id: str) -> int:
    """Merge only mechanically identical event identities with compatible times."""
    rows = [dict(row) for row in connection.execute("SELECT * FROM recruitment_events WHERE company_id=? ORDER BY created_at,id", (company_id,)).fetchall()]
    removed = 0
    kept: list[dict[str, Any]] = []
    for row in rows:
        identity_matches = [candidate for candidate in kept if _event_identity_matches(candidate, row)]
        match = next(
            (candidate for candidate in identity_matches if _event_times_compatible(candidate, row["start_at"], row["end_at"])),
            None,
        )
        if match is None and not identity_matches:
            kept.append(row)
            continue
        if match is None:
            current = connection.execute("SELECT * FROM recruitment_events WHERE id=?", (identity_matches[0]["id"],)).fetchone()
            if current:
                _record_event_time_conflict(connection, current, row["id"], _event_row_payload(row))
            kept.append(row)
            continue
        current = connection.execute("SELECT * FROM recruitment_events WHERE id=?", (match["id"],)).fetchone()
        duplicate = connection.execute("SELECT * FROM recruitment_events WHERE id=?", (row["id"],)).fetchone()
        if current and duplicate:
            _merge_event_records(connection, current, duplicate)
            refreshed = connection.execute("SELECT * FROM recruitment_events WHERE id=?", (match["id"],)).fetchone()
            if refreshed:
                kept[kept.index(match)] = dict(refreshed)
            removed += 1
    return removed


def deduplicate_recruitment_events(connection: Any, company_id: str | None = None) -> int:
    """Deduplicate event records using the same identity helper as normal ingestion."""
    if company_id:
        return deduplicate_company_events(connection, company_id)
    company_ids = [row["company_id"] for row in connection.execute("SELECT DISTINCT company_id FROM recruitment_events ORDER BY company_id").fetchall()]
    return sum(deduplicate_company_events(connection, value) for value in company_ids)


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
    latest_deadline = _choose_temporal_text([keep["explicit_deadline"], duplicate["explicit_deadline"]], latest=True)
    first_posted = _choose_temporal_text([keep["effective_posted_at"], duplicate["effective_posted_at"]], latest=False)
    last_posted = _choose_temporal_text([keep["last_effective_posted_at"], duplicate["last_effective_posted_at"]], latest=True)
    employment_type = normalize_employment_type(keep["employment_type"])
    duplicate_employment_type = normalize_employment_type(duplicate["employment_type"])
    if employment_type == "unknown":
        employment_type = duplicate_employment_type
    elif duplicate_employment_type not in {"unknown", employment_type}:
        employment_type = "unknown"
    updated_at = _choose_temporal_text([keep["updated_at"], duplicate["updated_at"]], latest=True) or utc_now()
    connection.execute(
        """UPDATE jobs SET department=?,employment_type=?,locations_json=?,headcount=?,education_json=?,majors_json=?,experience_requirement=?,
           batch_id=COALESCE(batch_id,?),
           salary_json=?,responsibilities=?,requirements=?,benefits_json=?,application_methods_json=?,contacts_json=?,
           explicit_deadline=?,effective_posted_at=?,last_effective_posted_at=?,status=?,industry_codes_json=?,
           job_function_codes_json=?,confidence=?,updated_at=? WHERE id=?""",
        (
            _merge_text(keep["department"], duplicate["department"]), employment_type, merged_lists["locations_json"],
            _merge_text(keep["headcount"], duplicate["headcount"]), merged_lists["education_json"],
            merged_lists["majors_json"], _merge_text(keep["experience_requirement"], duplicate["experience_requirement"]),
            keep["batch_id"] or duplicate["batch_id"],
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
                (state, int(bool(row["favorite"] or existing["favorite"])), _choose_temporal_text([row["updated_at"], existing["updated_at"]], latest=True) or utc_now(), row["user_id"], keep["id"]),
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


def _apply_semantic_job_payload(connection: Any, job_id: str, payload: dict[str, Any]) -> None:
    job = _normalize_job_payload(payload)
    current = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    title = job.get("title") or "未命名岗位"
    recruitment_type = str(job.get("recruitment_type") or "unknown")
    if recruitment_type not in RECRUITMENT_TYPES:
        recruitment_type = "unknown"
    employment_type = normalize_employment_type(job.get("employment_type"))
    explicit_deadline = normalize_text_value(job.get("deadline") or job.get("explicit_deadline"))
    responsibilities = job.get("responsibilities")
    if isinstance(responsibilities, list):
        responsibilities = "\n".join(normalize_text_list(responsibilities, preserve_newlines=True)) or None
    requirements = job.get("requirements")
    if isinstance(requirements, list):
        requirements = "\n".join(normalize_text_list(requirements, preserve_newlines=True)) or None
    connection.execute(
        """UPDATE jobs SET canonical_title=?,normalized_title=?,department=?,locations_json=?,recruitment_type=?,employment_type=?,
           headcount=?,education_json=?,majors_json=?,experience_requirement=?,salary_json=?,responsibilities=?,requirements=?,
           benefits_json=?,application_methods_json=?,contacts_json=?,explicit_deadline=?,status=?,updated_at=? WHERE id=?""",
        (
            title, normalize_title(title), job.get("department"), json_text(normalize_text_list(job.get("locations")), []),
            recruitment_type, employment_type, job.get("headcount"), json_text(normalize_text_list(job.get("education")), []),
            json_text(normalize_text_list(job.get("majors")), []), job.get("experience_requirement"), json_text(job.get("salary"), {}),
            responsibilities, requirements, json_text(normalize_text_list(job.get("benefits"), preserve_newlines=True), []),
            json_text(normalize_text_list(job.get("application_methods")), []), json_text(normalize_text_list(job.get("contacts"), preserve_newlines=True), []),
            explicit_deadline,
            "expired" if explicit_deadline and _deadline_is_expired(explicit_deadline) else (current["status"] if current and not explicit_deadline else "active"),
            utc_now(), job_id,
        ),
    )
    encoded = json.dumps({**job, "deadline": explicit_deadline or ""}, ensure_ascii=False, sort_keys=True)
    content_hash = hashlib.sha256(encoded.encode()).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO job_versions(id,job_id,raw_json,content_hash,observed_at,is_current) VALUES(?,?,?,?,?,1)",
        (str(uuid4()), job_id, encoded, content_hash, utc_now()),
    )
    connection.execute("UPDATE job_versions SET is_current=CASE WHEN content_hash=? THEN 1 ELSE 0 END WHERE job_id=?", (content_hash, job_id))
    connection.execute("DELETE FROM search_index WHERE entity_type='job' AND entity_id=?", (job_id,))
    connection.execute(
        "INSERT INTO search_index(entity_type,entity_id,title,body) VALUES('job',?,?,?)",
        (job_id, title, f"{title} {requirements or ''} {responsibilities or ''}".strip()),
    )


def merge_job_records_with_payload(job_ids: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    ids = list(dict.fromkeys(str(value).strip() for value in job_ids if str(value).strip()))
    if len(ids) < 2:
        raise CompanyManagementValidationError("岗位语义合并至少需要两个记录")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = _management_job_rows(connection, ids)
        if len({row["company_id"] for row in rows}) != 1:
            raise CompanyManagementConflict("岗位不属于同一企业，不能语义合并")
        keep = rows[0]
        for duplicate in rows[1:]:
            current_keep = connection.execute("SELECT * FROM jobs WHERE id=?", (keep["id"],)).fetchone()
            current_duplicate = connection.execute("SELECT * FROM jobs WHERE id=?", (duplicate["id"],)).fetchone()
            if current_keep and current_duplicate:
                _merge_job_into(connection, current_keep, current_duplicate)
                keep = connection.execute("SELECT * FROM jobs WHERE id=?", (keep["id"],)).fetchone()
        _apply_semantic_job_payload(connection, keep["id"], payload)
        return {"status": "merged", "primary_job_id": keep["id"], "merged_job_ids": ids[1:]}


def _apply_semantic_event_payload(connection: Any, event_id: str, payload: dict[str, Any]) -> None:
    event = _normalize_event_payload(payload)
    current = connection.execute("SELECT * FROM recruitment_events WHERE id=?", (event_id,)).fetchone()
    if not current:
        return
    timezone_name = event.get("timezone") or current["timezone"] or "Asia/Shanghai"
    start_at = normalize_event_datetime(event.get("start_at"), timezone_name, utc_now()) or current["start_at"]
    end_at = normalize_event_datetime(event.get("end_at"), timezone_name, utc_now()) or current["end_at"]
    if not _event_times_compatible(current, start_at, end_at):
        raise CompanyManagementConflict("活动明确时间冲突，不能语义合并")
    try:
        job_ids = json.loads(current["job_ids_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        job_ids = []
    normalized_job_titles = {normalize_title(str(value)) for value in event.get("job_titles") or [] if normalize_title(str(value))}
    if normalized_job_titles:
        for job in connection.execute("SELECT id,canonical_title FROM jobs WHERE company_id=?", (current["company_id"],)).fetchall():
            if normalize_title(str(job["canonical_title"] or "")) in normalized_job_titles:
                job_ids.append(job["id"])
    job_ids = list(dict.fromkeys(str(value) for value in job_ids if str(value).strip()))
    fields = {
        "title": event.get("title") or current["title"],
        "event_type": event.get("event_type") or current["event_type"],
        "start_at": start_at,
        "end_at": end_at,
        "timezone": timezone_name,
        "format": event.get("format") or current["format"],
        "city": event.get("city") or current["city"],
        "campus": event.get("campus") or current["campus"],
        "location": event.get("location") or current["location"],
        "application_url": event.get("application_url") or current["application_url"],
        "audience": event.get("audience") or current["audience"],
        "notes": event.get("notes") or current["notes"],
    }
    connection.execute(
        "UPDATE recruitment_events SET title=?,event_type=?,start_at=?,end_at=?,timezone=?,format=?,city=?,campus=?,location=?,application_url=?,audience=?,notes=?,job_ids_json=?,updated_at=? WHERE id=?",
        (*fields.values(), json_text(job_ids, []), utc_now(), event_id),
    )
    version_payload = {**event, "start_at": start_at or "", "end_at": end_at or "", "timezone": timezone_name}
    connection.execute(
        "INSERT INTO recruitment_event_versions(id,event_id,payload_json,observed_at,is_current) VALUES(?,?,?,?,1)",
        (str(uuid4()), event_id, json.dumps(version_payload, ensure_ascii=False), utc_now()),
    )
    connection.execute("UPDATE recruitment_event_versions SET is_current=0 WHERE event_id=? AND id NOT IN (SELECT id FROM recruitment_event_versions WHERE event_id=? ORDER BY observed_at DESC,id DESC LIMIT 1)", (event_id, event_id))


def merge_event_records_with_payload(
    event_ids: list[str],
    payload: dict[str, Any],
    *,
    semantic_identity: bool = False,
) -> dict[str, Any]:
    ids = list(dict.fromkeys(str(value).strip() for value in event_ids if str(value).strip()))
    if len(ids) < 2:
        raise CompanyManagementValidationError("活动语义合并至少需要两个记录")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = _management_event_rows(connection, ids)
        first = rows[0]
        if len({row["company_id"] for row in rows}) != 1 or (
            not semantic_identity and any(not _event_identity_matches(first, row) for row in rows[1:])
        ):
            raise CompanyManagementConflict("活动身份字段不一致，不能语义合并")
        keep = rows[0]
        for duplicate in rows[1:]:
            current_keep = connection.execute("SELECT * FROM recruitment_events WHERE id=?", (keep["id"],)).fetchone()
            current_duplicate = connection.execute("SELECT * FROM recruitment_events WHERE id=?", (duplicate["id"],)).fetchone()
            if not current_keep or not current_duplicate:
                continue
            if not _event_times_compatible(current_keep, current_duplicate["start_at"], current_duplicate["end_at"]):
                raise CompanyManagementConflict("活动明确时间冲突，不能语义合并")
            _merge_event_records(connection, current_keep, current_duplicate)
            keep = connection.execute("SELECT * FROM recruitment_events WHERE id=?", (keep["id"],)).fetchone()
        _apply_semantic_event_payload(connection, keep["id"], payload)
        return {"status": "merged", "primary_event_id": keep["id"], "merged_event_ids": ids[1:]}


class CompanyManagementValidationError(ValueError):
    """Raised when a manual company operation has invalid input."""


class CompanyManagementNotFound(CompanyManagementValidationError):
    """Raised when a selected company no longer exists."""


class CompanyManagementConflict(RuntimeError):
    """Raised when a selected company is being modified by a worker."""


_COMPANY_MANAGEMENT_SCALAR_FIELDS = (
    "display_name", "legal_name", "summary", "primary_industry", "website",
    "company_nature", "founded_at", "company_size", "headquarters",
    "last_consolidated_at", "public_researched_at", "verification_status",
)
_COMPANY_MANAGEMENT_ARRAY_FIELDS = (
    "aliases", "secondary_industries", "businesses", "highlights",
    "official_channels", "major_requirements", "tags",
)


def _management_placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _management_company_ids(company_ids: Any) -> list[str]:
    if not isinstance(company_ids, (list, tuple)):
        raise CompanyManagementValidationError("company_ids must be a list")
    result = list(dict.fromkeys(str(value).strip() for value in company_ids if str(value).strip()))
    if not result:
        raise CompanyManagementValidationError("至少选择一个企业")
    return result


def _management_company_rows(connection: Any, company_ids: list[str]) -> list[Any]:
    placeholders = _management_placeholders(company_ids)
    rows = connection.execute(
        f"SELECT * FROM companies WHERE id IN ({placeholders})",
        tuple(company_ids),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    missing = [company_id for company_id in company_ids if company_id not in by_id]
    if missing:
        raise CompanyManagementNotFound(f"企业不存在：{', '.join(missing)}")
    return [by_id[company_id] for company_id in company_ids]


def _management_job_rows(connection: Any, job_ids: list[str]) -> list[Any]:
    placeholders = _management_placeholders(job_ids)
    rows = connection.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", tuple(job_ids)).fetchall()
    by_id = {row["id"]: row for row in rows}
    missing = [job_id for job_id in job_ids if job_id not in by_id]
    if missing:
        raise CompanyManagementNotFound(f"岗位不存在：{', '.join(missing)}")
    return [by_id[job_id] for job_id in job_ids]


def _management_event_rows(connection: Any, event_ids: list[str]) -> list[Any]:
    placeholders = _management_placeholders(event_ids)
    rows = connection.execute(f"SELECT * FROM recruitment_events WHERE id IN ({placeholders})", tuple(event_ids)).fetchall()
    by_id = {row["id"]: row for row in rows}
    missing = [event_id for event_id in event_ids if event_id not in by_id]
    if missing:
        raise CompanyManagementNotFound(f"活动不存在：{', '.join(missing)}")
    return [by_id[event_id] for event_id in event_ids]


def _management_json_array(row: Any, field: str, overrides: dict[str, Any] | None = None) -> list[Any]:
    if overrides is not None and field in overrides:
        value = overrides[field]
    else:
        column = COMPANY_OVERRIDE_COLUMNS.get(field, f"{field}_json")
        value = row[column]
        if isinstance(value, str):
            try:
                value = json.loads(value or "[]")
            except json.JSONDecodeError:
                value = []
    return value if isinstance(value, list) else []


def _company_merge_content_candidates(rows: list[Any]) -> list[dict[str, Any]]:
    overrides = [company_overrides(row["manual_overrides_json"]) for row in rows]
    summaries = [
        _management_effective_scalar(row, "summary", company_overrides(row["manual_overrides_json"]))
        for row in rows
    ]
    summary_owner = next((index for index, value in enumerate(summaries) if _management_non_empty(value)), None)
    merged_arrays = {
        field: _management_stable_list([
            value
            for row, row_overrides in zip(rows, overrides)
            for value in _management_json_array(row, field, row_overrides)
        ])
        for field in ("businesses", "highlights", "major_requirements")
    }
    candidates: list[dict[str, Any]] = []
    for index, (row, row_overrides) in enumerate(zip(rows, overrides)):
        candidates.append({
            "company_id": row["id"],
            "role": "primary" if index == 0 else "supplementary",
            "display_name": row["display_name"],
            "content": {
                "summary": str(summaries[index] or "") if index == summary_owner else "",
                "businesses": _management_stable_list(_management_json_array(row, "businesses", row_overrides)),
                "highlights": _management_stable_list(_management_json_array(row, "highlights", row_overrides)),
                "major_requirements": _management_stable_list(_management_json_array(row, "major_requirements", row_overrides)),
            },
        })
    candidates.append({
        "deterministic_content": {
            "summary": str(summaries[summary_owner] or "") if summary_owner is not None else "",
            **merged_arrays,
        },
    })
    return candidates


def _polish_company_merge_content(rows: list[Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        from .model_provider import polish_company_merge_content

        result = polish_company_merge_content(_company_merge_content_candidates(rows), f"manual-merge-{uuid4()}")
        payload = dict(result.payload)
        if payload.get("status") != "complete":
            return None, {
                "status": "uncertain",
                "reason": str(payload.get("reason") or "Codex 无法安全整理合并内容"),
            }
        return payload, {
            "status": "applied",
            "processor": f"{result.provider}:{result.model}",
        }
    except Exception as exc:
        return None, {
            "status": "fallback",
            "reason": "Codex 内容整理不可用，已保留确定性合并结果",
            "error_type": type(exc).__name__,
        }


def _management_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _management_stable_list(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        try:
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except TypeError:
            key = repr(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _management_effective_scalar(row: Any, field: str, overrides: dict[str, Any]) -> Any:
    if field in overrides:
        return overrides[field]
    return row[COMPANY_OVERRIDE_COLUMNS.get(field, field)]


def _merge_company_profile(
    connection: Any,
    primary: Any,
    supplements: list[Any],
    now: str,
    content_polish: dict[str, Any] | None = None,
    version_decision: str = "manual_merge",
    version_reason: str = "管理员手动合并企业",
    version_processor: str = "admin:manual",
) -> None:
    primary_overrides = company_overrides(primary["manual_overrides_json"])
    supplement_overrides = [company_overrides(row["manual_overrides_json"]) for row in supplements]
    merged_overrides = dict(primary_overrides)
    merged_scalars: dict[str, Any] = {}
    for field in _COMPANY_MANAGEMENT_SCALAR_FIELDS:
        value = _management_effective_scalar(primary, field, primary_overrides)
        if not _management_non_empty(value):
            for row, overrides in zip(supplements, supplement_overrides):
                candidate = _management_effective_scalar(row, field, overrides)
                if _management_non_empty(candidate):
                    value = candidate
                    if field not in primary_overrides and field in overrides:
                        merged_overrides[field] = candidate
                    break
        merged_scalars[field] = value

    merged_arrays: dict[str, list[Any]] = {}
    for field in _COMPANY_MANAGEMENT_ARRAY_FIELDS:
        primary_values = _management_json_array(primary, field, primary_overrides)
        values = list(primary_values)
        for row, overrides in zip(supplements, supplement_overrides):
            values.extend(_management_json_array(row, field, overrides))
        if field == "aliases":
            for row, overrides in zip(supplements, supplement_overrides):
                values.extend([
                    row["display_name"],
                    row["legal_name"],
                    *_management_json_array(row, "aliases", overrides),
                ])
        merged_arrays[field] = _management_stable_list(values)
        if field in primary_overrides:
            merged_overrides[field] = merged_arrays[field]
        if not primary_values and field not in primary_overrides:
            for overrides in supplement_overrides:
                if field in overrides:
                    merged_overrides[field] = merged_arrays[field]
                    break

    if content_polish and content_polish.get("status") == "complete":
        if "summary" not in primary_overrides and not primary["summary_locked"]:
            polished_summary = normalize_text_value(content_polish.get("summary"), preserve_newlines=True)
            if polished_summary:
                merged_scalars["summary"] = polished_summary
        for field in ("businesses", "highlights", "major_requirements"):
            if field in primary_overrides:
                continue
            polished_values = content_polish.get(field)
            if not isinstance(polished_values, list):
                continue
            polished_values = normalize_text_list(polished_values, preserve_newlines=True)
            if polished_values or not merged_arrays[field]:
                merged_arrays[field] = _management_stable_list(polished_values)
        for field in _COMPANY_MANAGEMENT_ARRAY_FIELDS:
            if field in primary_overrides or field in merged_overrides:
                merged_overrides[field] = merged_arrays[field]

    updates: dict[str, Any] = {
        COMPANY_OVERRIDE_COLUMNS.get(field, field): merged_scalars[field]
        for field in _COMPANY_MANAGEMENT_SCALAR_FIELDS
    }
    updates.update({
        COMPANY_OVERRIDE_COLUMNS.get(field, f"{field}_json"): json_text(merged_arrays[field], [])
        for field in _COMPANY_MANAGEMENT_ARRAY_FIELDS
    })
    updates["manual_overrides_json"] = json.dumps(merged_overrides, ensure_ascii=False)
    updates["summary_locked"] = int(bool(primary["summary_locked"]) or "summary" in primary_overrides or "summary" in merged_overrides)
    updates["updated_at"] = now
    assignments = ",".join(f"{column}=?" for column in updates)
    connection.execute(
        f"UPDATE companies SET {assignments} WHERE id=?",
        (*updates.values(), primary["id"]),
    )
    profile = {
        **merged_scalars,
        **merged_arrays,
        "merged_company_ids": [row["id"] for row in supplements],
    }
    connection.execute(
        "INSERT INTO company_versions(id,company_id,profile_json,decision,reason,processor,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            str(uuid4()), primary["id"], json.dumps(profile, ensure_ascii=False),
            version_decision, version_reason, version_processor, now,
        ),
    )


def _apply_semantic_company_payload(connection: Any, primary: Any, payload: dict[str, Any], now: str) -> None:
    """Apply one Codex-merged company structure without overriding admin locks."""
    merged = normalize_company_payload(payload)
    overrides = company_overrides(primary["manual_overrides_json"])
    scalar_values = {
        "display_name": merged.get("display_name"),
        "legal_name": merged.get("legal_name"),
        "company_nature": merged.get("company_nature"),
        "headquarters": merged.get("headquarters"),
        "founded_at": merged.get("founded_at"),
        "company_size": merged.get("company_size"),
        "website": merged.get("website"),
    }
    updates: dict[str, Any] = {}
    for field, value in scalar_values.items():
        if field not in overrides and _management_non_empty(value):
            updates[COMPANY_OVERRIDE_COLUMNS[field]] = value
    industries = [value for value in [merged.get("primary_industry"), *(merged.get("secondary_industries") or []), *(merged.get("industry_codes") or [])] if value in INDUSTRIES]
    if "primary_industry" not in overrides and industries:
        updates["primary_industry"] = industries[0]
    if "secondary_industries" not in overrides and industries:
        updates["secondary_industries_json"] = json_text(list(dict.fromkeys(industries[1:])), [])
    for field in ("aliases", "businesses", "highlights", "official_channels"):
        if field not in overrides and isinstance(merged.get(field), list):
            updates[COMPANY_OVERRIDE_COLUMNS[field]] = json_text(_management_stable_list(merged[field]), [])
    if "tags" not in overrides:
        updates["company_tags_json"] = json_text(normalize_company_tags(merged.get("tags"), merged.get("company_nature"), industries), [])
    if not updates:
        return
    updates["updated_at"] = now
    assignments = ",".join(f"{column}=?" for column in updates)
    connection.execute(f"UPDATE companies SET {assignments} WHERE id=?", (*updates.values(), primary["id"]))
    apply_company_overrides(connection, primary["id"], overrides, now)


def _merge_batch_records(connection: Any, keep_id: str, duplicate_id: str, now: str) -> None:
    keep = connection.execute("SELECT confidence,updated_at FROM recruitment_batches WHERE id=?", (keep_id,)).fetchone()
    duplicate = connection.execute("SELECT confidence,updated_at FROM recruitment_batches WHERE id=?", (duplicate_id,)).fetchone()
    if not keep or not duplicate:
        return
    connection.execute("UPDATE jobs SET batch_id=? WHERE batch_id=?", (keep_id, duplicate_id))
    connection.execute("UPDATE recruitment_shared_details SET batch_id=? WHERE batch_id=?", (keep_id, duplicate_id))
    connection.execute("UPDATE recruitment_events SET batch_id=? WHERE batch_id=?", (keep_id, duplicate_id))
    updated_at = _choose_temporal_text([keep["updated_at"], duplicate["updated_at"], now], latest=True) or now
    connection.execute(
        "UPDATE recruitment_batches SET confidence=?,updated_at=? WHERE id=?",
        (max(float(keep["confidence"] or 0), float(duplicate["confidence"] or 0)), updated_at, keep_id),
    )
    connection.execute("DELETE FROM recruitment_batches WHERE id=?", (duplicate_id,))


def _deduplicate_company_batches(connection: Any, company_id: str, now: str) -> int:
    rows = connection.execute(
        "SELECT * FROM recruitment_batches WHERE company_id=? ORDER BY created_at,id",
        (company_id,),
    ).fetchall()
    kept: dict[tuple[Any, ...], str] = {}
    removed = 0
    for row in rows:
        identity = (row["name"], row["year"], row["season"], row["recruitment_type"])
        keep_id = kept.get(identity)
        if keep_id is None:
            kept[identity] = row["id"]
            continue
        _merge_batch_records(connection, keep_id, row["id"], now)
        removed += 1
    return removed


def _remap_company_relations(connection: Any, company_ids: list[str], primary_id: str) -> None:
    mapping = {company_id: primary_id for company_id in company_ids}
    placeholders = _management_placeholders(company_ids)
    rows = connection.execute(
        f"SELECT id,parent_company_id,child_company_id,relation_type FROM company_relations WHERE parent_company_id IN ({placeholders}) OR child_company_id IN ({placeholders}) ORDER BY id",
        tuple(company_ids) * 2,
    ).fetchall()
    for row in rows:
        parent_id = mapping.get(row["parent_company_id"], row["parent_company_id"])
        child_id = mapping.get(row["child_company_id"], row["child_company_id"])
        if parent_id == child_id:
            connection.execute("DELETE FROM company_relations WHERE id=?", (row["id"],))
            continue
        duplicate = connection.execute(
            "SELECT id FROM company_relations WHERE parent_company_id=? AND child_company_id=? AND relation_type=? AND id<>? LIMIT 1",
            (parent_id, child_id, row["relation_type"], row["id"]),
        ).fetchone()
        if duplicate:
            connection.execute("DELETE FROM company_relations WHERE id=?", (row["id"],))
        else:
            connection.execute(
                "UPDATE company_relations SET parent_company_id=?,child_company_id=? WHERE id=?",
                (parent_id, child_id, row["id"]),
            )


def _remap_company_merge_rules(connection: Any, company_ids: list[str], primary_id: str) -> None:
    mapping = {company_id: primary_id for company_id in company_ids}
    placeholders = _management_placeholders(company_ids)
    rows = connection.execute(
        f"SELECT id,left_company_id,right_company_id FROM company_merge_rules WHERE left_company_id IN ({placeholders}) OR right_company_id IN ({placeholders}) ORDER BY id",
        tuple(company_ids) * 2,
    ).fetchall()
    for row in rows:
        left_id = mapping.get(row["left_company_id"], row["left_company_id"])
        right_id = mapping.get(row["right_company_id"], row["right_company_id"])
        if left_id == right_id:
            connection.execute("DELETE FROM company_merge_rules WHERE id=?", (row["id"],))
            continue
        duplicate = connection.execute(
            "SELECT id FROM company_merge_rules WHERE left_company_id=? AND right_company_id=? AND id<>? LIMIT 1",
            (left_id, right_id, row["id"]),
        ).fetchone()
        if duplicate:
            connection.execute("DELETE FROM company_merge_rules WHERE id=?", (row["id"],))
        else:
            connection.execute(
                "UPDATE company_merge_rules SET left_company_id=?,right_company_id=? WHERE id=?",
                (left_id, right_id, row["id"]),
            )


def _remap_company_follows(connection: Any, supplement_ids: list[str], primary_id: str) -> None:
    placeholders = _management_placeholders(supplement_ids)
    rows = connection.execute(
        f"SELECT user_id,company_id,created_at FROM user_follows WHERE company_id IN ({placeholders}) ORDER BY company_id,user_id",
        tuple(supplement_ids),
    ).fetchall()
    for row in rows:
        connection.execute(
            "INSERT OR IGNORE INTO user_follows(user_id,company_id,created_at) VALUES(?,?,?)",
            (row["user_id"], primary_id, row["created_at"]),
        )
        connection.execute(
            "DELETE FROM user_follows WHERE user_id=? AND company_id=?",
            (row["user_id"], row["company_id"]),
        )


def _rebuild_company_search_index(connection: Any, primary_id: str, company_ids: list[str], job_ids: list[str]) -> None:
    company_placeholders = _management_placeholders(company_ids)
    connection.execute(
        f"DELETE FROM search_index WHERE entity_type='company' AND entity_id IN ({company_placeholders})",
        tuple(company_ids),
    )
    if job_ids:
        job_placeholders = _management_placeholders(job_ids)
        connection.execute(
            f"DELETE FROM search_index WHERE entity_type='job' AND entity_id IN ({job_placeholders})",
            tuple(job_ids),
        )
    company = connection.execute("SELECT display_name,summary FROM companies WHERE id=?", (primary_id,)).fetchone()
    if not company:
        return
    connection.execute(
        "INSERT INTO search_index(entity_type,entity_id,title,body) VALUES('company',?,?,?)",
        (primary_id, company["display_name"], f"{company['display_name']} {company['summary'] or ''}".strip()),
    )
    for job in connection.execute(
        "SELECT id,canonical_title,requirements,responsibilities FROM jobs WHERE company_id=?",
        (primary_id,),
    ).fetchall():
        connection.execute(
            "INSERT INTO search_index(entity_type,entity_id,title,body) VALUES('job',?,?,?)",
            (
                job["id"], job["canonical_title"],
                f"{job['canonical_title']} {job['requirements'] or ''} {job['responsibilities'] or ''}".strip(),
            ),
        )


def _management_running_jobs(connection: Any, company_ids: list[str]) -> list[dict[str, Any]]:
    placeholders = _management_placeholders(company_ids)
    return [
        dict(row)
        for row in connection.execute(
            f"SELECT id,kind,company_id FROM processing_jobs WHERE status IN ('running','timeout') AND company_id IN ({placeholders}) ORDER BY created_at,id",
            tuple(company_ids),
        ).fetchall()
    ]


def _ensure_company_management_unlocked(connection: Any, company_ids: list[str], exclude_processing_job_id: str | None = None) -> None:
    running = _management_running_jobs(connection, company_ids)
    if exclude_processing_job_id:
        running = [row for row in running if row["id"] != exclude_processing_job_id]
    if running:
        raise CompanyManagementConflict("参与操作的企业存在正在运行的后台任务，请等待任务结束后重试")


def _management_count_by_ids(connection: Any, table: str, column: str, company_ids: list[str]) -> int:
    if not company_ids:
        return 0
    placeholders = _management_placeholders(company_ids)
    row = connection.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE {column} IN ({placeholders})",
        tuple(company_ids),
    ).fetchone()
    return int(row["count"] if row else 0)


def _company_management_impact(connection: Any, company_ids: list[str], operation: str, rows: list[Any] | None = None) -> dict[str, Any]:
    if operation not in {"merge", "delete"}:
        raise CompanyManagementValidationError("operation must be merge or delete")
    if operation == "merge" and len(company_ids) < 2:
        raise CompanyManagementValidationError("合并至少需要选择两个企业")
    rows = rows or _management_company_rows(connection, company_ids)
    target_ids = company_ids[1:] if operation == "merge" else company_ids
    target_placeholders = _management_placeholders(target_ids)
    job_rows = connection.execute(
        f"SELECT id FROM jobs WHERE company_id IN ({target_placeholders})",
        tuple(target_ids),
    ).fetchall() if target_ids else []
    job_ids = [row["id"] for row in job_rows]
    evidence_conditions = [f"company_id IN ({target_placeholders})"]
    evidence_params: list[Any] = list(target_ids)
    if job_ids:
        job_placeholders = _management_placeholders(job_ids)
        evidence_conditions.append(f"job_id IN ({job_placeholders})")
        evidence_params.extend(job_ids)
    evidence_row = connection.execute(
        f"SELECT COUNT(*) AS count FROM evidences WHERE {' OR '.join(evidence_conditions)}",
        tuple(evidence_params),
    ).fetchone()
    index_conditions = [f"(entity_type='company' AND entity_id IN ({target_placeholders}))"]
    index_params: list[Any] = list(target_ids)
    if job_ids:
        job_placeholders = _management_placeholders(job_ids)
        index_conditions.append(f"(entity_type='job' AND entity_id IN ({job_placeholders}))")
        index_params.extend(job_ids)
    index_row = connection.execute(
        f"SELECT COUNT(*) AS count FROM search_index WHERE {' OR '.join(index_conditions)}",
        tuple(index_params),
    ).fetchone()
    relation_row = connection.execute(
        f"SELECT COUNT(*) AS count FROM company_relations WHERE parent_company_id IN ({target_placeholders}) OR child_company_id IN ({target_placeholders})",
        tuple(target_ids) * 2,
    ).fetchone()
    merge_rule_row = connection.execute(
        f"SELECT COUNT(*) AS count FROM company_merge_rules WHERE left_company_id IN ({target_placeholders}) OR right_company_id IN ({target_placeholders})",
        tuple(target_ids) * 2,
    ).fetchone()
    counts = {
        "companies": len(target_ids),
        "jobs": len(job_ids),
        "recruitment_batches": _management_count_by_ids(connection, "recruitment_batches", "company_id", target_ids),
        "recruitment_shared_details": _management_count_by_ids(connection, "recruitment_shared_details", "company_id", target_ids),
        "recruitment_events": _management_count_by_ids(connection, "recruitment_events", "company_id", target_ids),
        "evidences": int(evidence_row["count"] if evidence_row else 0),
        "company_versions": _management_count_by_ids(connection, "company_versions", "company_id", target_ids),
        "company_claims": _management_count_by_ids(connection, "company_claims", "company_id", target_ids),
        "company_public_findings": _management_count_by_ids(connection, "company_public_findings", "company_id", target_ids),
        "company_relations": int(relation_row["count"] if relation_row else 0),
        "company_merge_rules": int(merge_rule_row["count"] if merge_rule_row else 0),
        "user_follows": _management_count_by_ids(connection, "user_follows", "company_id", target_ids),
        "processing_jobs": _management_count_by_ids(connection, "processing_jobs", "company_id", target_ids),
        "review_items": 0,
        "search_index": int(index_row["count"] if index_row else 0),
    }
    review_row = connection.execute(
        f"SELECT COUNT(*) AS count FROM review_items WHERE entity_type='company' AND entity_id IN ({target_placeholders})",
        tuple(target_ids),
    ).fetchone()
    counts["review_items"] = int(review_row["count"] if review_row else 0)
    running = _management_running_jobs(connection, company_ids)
    result: dict[str, Any] = {
        "operation": operation,
        "primary_company": {"id": rows[0]["id"], "display_name": rows[0]["display_name"]} if operation == "merge" else None,
        "supplementary_companies": [
            {"id": row["id"], "display_name": row["display_name"]}
            for row in rows[1:]
        ] if operation == "merge" else [],
        "selected_companies": [
            {"id": row["id"], "display_name": row["display_name"]}
            for row in rows
        ],
        "counts": counts,
        "running_jobs": running,
        "blocked": bool(running),
    }
    return result


def company_management_impact(company_ids: Any, operation: str) -> dict[str, Any]:
    ids = _management_company_ids(company_ids)
    with connect() as connection:
        return _company_management_impact(connection, ids, operation)


def queue_company_management(company_ids: Any, operation: str) -> dict[str, Any]:
    ids = _management_company_ids(company_ids)
    if operation not in {"merge", "delete"}:
        raise CompanyManagementValidationError("operation must be merge or delete")
    if operation == "merge" and len(ids) < 2:
        raise CompanyManagementValidationError("合并至少需要选择两个企业")
    kind = "merge_company" if operation == "merge" else "delete_company"
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = _management_company_rows(connection, ids)
        _ensure_company_management_unlocked(connection, ids)
        payload = {
            "operation": operation,
            "company_ids": ids,
            "company_names": [row["display_name"] for row in rows],
        }
        if operation == "merge":
            payload["primary_company_id"] = rows[0]["id"]
            payload["primary_company_name"] = rows[0]["display_name"]
        active_jobs = connection.execute(
            "SELECT id,kind,payload_json FROM processing_jobs WHERE kind IN ('merge_company','delete_company') AND status IN ('pending','running','retry_wait') ORDER BY created_at,id"
        ).fetchall()
        selected_set = set(ids)
        for active_job in active_jobs:
            try:
                active_payload = json.loads(active_job["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            active_ids = [str(value) for value in active_payload.get("company_ids") or []]
            if not active_ids or selected_set.isdisjoint(active_ids):
                continue
            same_selection = active_job["kind"] == kind and (
                active_ids == ids if operation == "merge" else set(active_ids) == selected_set
            )
            if same_selection:
                return {
                    "status": "already_queued",
                    "queued": False,
                    "job_id": active_job["id"],
                    "kind": kind,
                    "operation": operation,
                    "company_ids": ids,
                }
            raise CompanyManagementConflict("选中的企业已有管理任务正在排队，请等待该任务完成")
        job_id = str(uuid4())
        now = utc_now()
        connection.execute(
            "INSERT INTO processing_jobs(id,kind,payload_json,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, kind, json.dumps(payload, ensure_ascii=False), "pending", f"{operation}_queued", now, now),
        )
        return {
            "status": "queued",
            "queued": True,
            "job_id": job_id,
            "kind": kind,
            "operation": operation,
            "company_ids": ids,
        }


def merge_company_records(
    company_ids: Any,
    *,
    exclude_processing_job_id: str | None = None,
    semantic_company: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ids = _management_company_ids(company_ids)
    if len(ids) < 2:
        raise CompanyManagementValidationError("合并至少需要选择两个企业")
    with connect() as snapshot_connection:
        snapshot_rows = [dict(row) for row in _management_company_rows(snapshot_connection, ids)]
        _ensure_company_management_unlocked(snapshot_connection, ids, exclude_processing_job_id)
    snapshot_updated_at = {row["id"]: row["updated_at"] for row in snapshot_rows}
    if semantic_company is None:
        content_polish, content_polish_result = _polish_company_merge_content(snapshot_rows)
    else:
        content_polish, content_polish_result = None, {"status": "semantic", "processor": "codex"}
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = _management_company_rows(connection, ids)
        if any(row["updated_at"] != snapshot_updated_at.get(row["id"]) for row in rows):
            raise CompanyManagementConflict("企业资料在 Codex 整理期间发生变化，请重试")
        _ensure_company_management_unlocked(connection, ids, exclude_processing_job_id)
        impact = _company_management_impact(connection, ids, "merge", rows)
        primary_id = ids[0]
        supplement_ids = ids[1:]
        all_job_ids = [
            row["id"]
            for row in connection.execute(
                f"SELECT id FROM jobs WHERE company_id IN ({_management_placeholders(ids)})",
                tuple(ids),
            ).fetchall()
        ]
        _merge_company_profile(
            connection,
            rows[0],
            rows[1:],
            now,
            content_polish,
            version_decision="semantic_merge" if semantic_company is not None else "manual_merge",
            version_reason="Codex 历史语义合并" if semantic_company is not None else "管理员手动合并企业",
            version_processor="codex:historical_entity_dedup" if semantic_company is not None else "admin:manual",
        )
        if semantic_company is not None:
            _apply_semantic_company_payload(connection, rows[0], semantic_company, now)
        supplement_placeholders = _management_placeholders(supplement_ids)
        connection.execute(
            f"UPDATE recruitment_batches SET company_id=? WHERE company_id IN ({supplement_placeholders})",
            (primary_id, *supplement_ids),
        )
        _deduplicate_company_batches(connection, primary_id, now)
        connection.execute(
            f"UPDATE recruitment_shared_details SET company_id=? WHERE company_id IN ({supplement_placeholders})",
            (primary_id, *supplement_ids),
        )
        connection.execute(
            f"UPDATE recruitment_events SET company_id=? WHERE company_id IN ({supplement_placeholders})",
            (primary_id, *supplement_ids),
        )
        connection.execute(
            f"UPDATE evidences SET company_id=? WHERE company_id IN ({supplement_placeholders})",
            (primary_id, *supplement_ids),
        )
        for table in ("company_versions", "company_claims"):
            connection.execute(
                f"UPDATE {table} SET company_id=? WHERE company_id IN ({supplement_placeholders})",
                (primary_id, *supplement_ids),
            )
        duplicate_findings = connection.execute(
            f"SELECT id,content_hash FROM company_public_findings WHERE company_id IN ({supplement_placeholders}) ORDER BY company_id,id",
            tuple(supplement_ids),
        ).fetchall()
        for finding in duplicate_findings:
            existing = connection.execute(
                "SELECT id FROM company_public_findings WHERE company_id=? AND content_hash=? LIMIT 1",
                (primary_id, finding["content_hash"]),
            ).fetchone()
            if existing:
                connection.execute("DELETE FROM company_public_findings WHERE id=?", (finding["id"],))
            else:
                connection.execute(
                    "UPDATE company_public_findings SET company_id=? WHERE id=?",
                    (primary_id, finding["id"]),
                )
        _remap_company_relations(connection, ids, primary_id)
        _remap_company_merge_rules(connection, ids, primary_id)
        _remap_company_follows(connection, supplement_ids, primary_id)
        connection.execute(
            f"UPDATE processing_jobs SET company_id=? WHERE company_id IN ({supplement_placeholders})",
            (primary_id, *supplement_ids),
        )
        connection.execute(
            f"UPDATE review_items SET entity_id=? WHERE entity_type='company' AND entity_id IN ({supplement_placeholders})",
            (primary_id, *supplement_ids),
        )
        connection.execute(
            f"UPDATE jobs SET company_id=? WHERE company_id IN ({supplement_placeholders})",
            (primary_id, *supplement_ids),
        )
        deduplicated_jobs = deduplicate_company_jobs(connection, primary_id)
        deduplicated_events = deduplicate_recruitment_events(connection, primary_id)
        connection.execute(
            f"DELETE FROM search_index WHERE entity_type='company' AND entity_id IN ({supplement_placeholders})",
            tuple(supplement_ids),
        )
        connection.execute(
            f"DELETE FROM companies WHERE id IN ({supplement_placeholders})",
            tuple(supplement_ids),
        )
        _rebuild_company_search_index(connection, primary_id, ids, all_job_ids)
        return {
            "status": "merged",
            "primary_company_id": primary_id,
            "merged_company_ids": supplement_ids,
            "counts": impact["counts"],
            "deduplicated_jobs": deduplicated_jobs,
            "deduplicated_events": deduplicated_events,
            "content_polish": content_polish_result,
        }


def delete_company_records(company_ids: Any, *, exclude_processing_job_id: str | None = None) -> dict[str, Any]:
    ids = _management_company_ids(company_ids)
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = _management_company_rows(connection, ids)
        _ensure_company_management_unlocked(connection, ids, exclude_processing_job_id)
        impact = _company_management_impact(connection, ids, "delete", rows)
        company_placeholders = _management_placeholders(ids)
        job_ids = [
            row["id"]
            for row in connection.execute(
                f"SELECT id FROM jobs WHERE company_id IN ({company_placeholders})",
                tuple(ids),
            ).fetchall()
        ]
        event_ids = [
            row["id"]
            for row in connection.execute(
                f"SELECT id FROM recruitment_events WHERE company_id IN ({company_placeholders})",
                tuple(ids),
            ).fetchall()
        ]
        processing_job_where = f"company_id IN ({company_placeholders})"
        processing_job_params: list[Any] = list(ids)
        if exclude_processing_job_id:
            processing_job_where += " AND id<>?"
            processing_job_params.append(exclude_processing_job_id)
        processing_job_rows = connection.execute(
            f"SELECT id,raw_message_id FROM processing_jobs WHERE {processing_job_where}",
            tuple(processing_job_params),
        ).fetchall()
        processing_job_ids = [row["id"] for row in processing_job_rows]
        processing_raw_message_ids = list(dict.fromkeys(
            row["raw_message_id"] for row in processing_job_rows if row["raw_message_id"]
        ))
        evidence_conditions = [f"company_id IN ({company_placeholders})"]
        evidence_params: list[Any] = list(ids)
        if job_ids:
            job_placeholders = _management_placeholders(job_ids)
            evidence_conditions.append(f"job_id IN ({job_placeholders})")
            evidence_params.extend(job_ids)
        evidence_ids = [
            row["id"]
            for row in connection.execute(
                f"SELECT id FROM evidences WHERE {' OR '.join(evidence_conditions)}",
                tuple(evidence_params),
            ).fetchall()
        ]
        if event_ids or evidence_ids:
            event_conditions: list[str] = []
            event_params: list[Any] = []
            if event_ids:
                event_placeholders = _management_placeholders(event_ids)
                event_conditions.append(f"event_id IN ({event_placeholders})")
                event_params.extend(event_ids)
            if evidence_ids:
                evidence_placeholders = _management_placeholders(evidence_ids)
                event_conditions.append(f"evidence_id IN ({evidence_placeholders})")
                event_params.extend(evidence_ids)
            connection.execute(
                f"DELETE FROM recruitment_event_evidences WHERE {' OR '.join(event_conditions)}",
                tuple(event_params),
            )
        if event_ids:
            event_placeholders = _management_placeholders(event_ids)
            connection.execute(
                f"DELETE FROM recruitment_event_versions WHERE event_id IN ({event_placeholders})",
                tuple(event_ids),
            )
            connection.execute(
                f"DELETE FROM recruitment_events WHERE id IN ({event_placeholders})",
                tuple(event_ids),
            )
        connection.execute(
            f"DELETE FROM recruitment_shared_details WHERE company_id IN ({company_placeholders})",
            tuple(ids),
        )
        if evidence_ids:
            evidence_placeholders = _management_placeholders(evidence_ids)
            connection.execute(
                f"DELETE FROM evidences WHERE id IN ({evidence_placeholders})",
                tuple(evidence_ids),
            )
        if job_ids:
            job_placeholders = _management_placeholders(job_ids)
            for table in ("job_versions", "user_job_states", "application_events", "user_notes", "job_tag_links"):
                column = "job_id"
                connection.execute(
                    f"DELETE FROM {table} WHERE {column} IN ({job_placeholders})",
                    tuple(job_ids),
                )
            connection.execute(
                f"DELETE FROM jobs WHERE id IN ({job_placeholders})",
                tuple(job_ids),
            )
        connection.execute(
            f"DELETE FROM recruitment_batches WHERE company_id IN ({company_placeholders})",
            tuple(ids),
        )
        for table in ("company_versions", "company_claims", "company_public_findings"):
            connection.execute(
                f"DELETE FROM {table} WHERE company_id IN ({company_placeholders})",
                tuple(ids),
            )
        connection.execute(
            f"DELETE FROM company_relations WHERE parent_company_id IN ({company_placeholders}) OR child_company_id IN ({company_placeholders})",
            tuple(ids) * 2,
        )
        connection.execute(
            f"DELETE FROM company_merge_rules WHERE left_company_id IN ({company_placeholders}) OR right_company_id IN ({company_placeholders})",
            tuple(ids) * 2,
        )
        connection.execute(
            f"DELETE FROM user_follows WHERE company_id IN ({company_placeholders})",
            tuple(ids),
        )
        connection.execute(
            f"DELETE FROM processing_jobs WHERE {processing_job_where}",
            tuple(processing_job_params),
        )
        review_conditions: list[str] = []
        review_params: list[Any] = []
        if processing_job_ids:
            processing_job_placeholders = _management_placeholders(processing_job_ids)
            review_conditions.append(f"entity_id IN ({processing_job_placeholders})")
            review_params.extend(processing_job_ids)
        if processing_raw_message_ids:
            raw_message_placeholders = _management_placeholders(processing_raw_message_ids)
            review_conditions.append(
                f"(entity_id IN ({raw_message_placeholders}) AND NOT EXISTS "
                f"(SELECT 1 FROM processing_jobs survivor WHERE survivor.raw_message_id=review_items.entity_id "
                f"AND (survivor.company_id IS NULL OR survivor.company_id NOT IN ({company_placeholders}))))"
            )
            review_params.extend(processing_raw_message_ids)
            review_params.extend(ids)
        if review_conditions:
            connection.execute(
                f"DELETE FROM review_items WHERE entity_type='processing_job' AND ({' OR '.join(review_conditions)})",
                tuple(review_params),
            )
        connection.execute(
            f"DELETE FROM review_items WHERE entity_type='company' AND entity_id IN ({company_placeholders})",
            tuple(ids),
        )
        if job_ids:
            job_placeholders = _management_placeholders(job_ids)
            connection.execute(
                f"DELETE FROM search_index WHERE entity_type='job' AND entity_id IN ({job_placeholders})",
                tuple(job_ids),
            )
        connection.execute(
            f"DELETE FROM search_index WHERE entity_type='company' AND entity_id IN ({company_placeholders})",
            tuple(ids),
        )
        connection.execute(
            f"DELETE FROM companies WHERE id IN ({company_placeholders})",
            tuple(ids),
        )
        return {
            "status": "deleted",
            "deleted_company_ids": ids,
            "counts": impact["counts"],
            "deleted_at": now,
        }


def refresh_expiration() -> int:
    days = 45
    row = one("SELECT value_json FROM system_settings WHERE key='possibly_expired_days'")
    if row:
        try:
            days = int(json.loads(row["value_json"]))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    updated_at = utc_now()
    updated = 0
    with connect() as connection:
        rows = connection.execute(
            "SELECT id,last_effective_posted_at FROM jobs WHERE status='active' AND explicit_deadline IS NULL"
        ).fetchall()
        for row in rows:
            posted_at = _parse_reliable_datetime(row["last_effective_posted_at"])
            if posted_at is None or posted_at >= cutoff:
                continue
            connection.execute("UPDATE jobs SET status='possibly_expired',updated_at=? WHERE id=?", (updated_at, row["id"]))
            updated += 1
    return updated
