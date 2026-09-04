from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from .catalog import (
    COMPANY_TYPE_LABELS,
    INDUSTRIES,
    deduplicate_company_jobs,
    normalize_company_tags,
)
from .db import all_rows, connect, one, utc_now
from .model_provider import get_setting, research_company_overview
from .parsers import recover_original_source_url, validate_public_url
from .search import search_company


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _safe_public_url(value: Any) -> str | None:
    candidate = recover_original_source_url(str(value or "").strip())
    if not candidate:
        return None
    try:
        validate_public_url(candidate)
    except (OSError, ValueError):
        return None
    return candidate


def _safe_resolved_url(value: Any) -> str | None:
    """Validate a redirect/final URL without rewriting it to its target."""
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        validate_public_url(candidate)
    except (OSError, ValueError):
        return None
    return candidate


def _source_hints(company: dict[str, Any]) -> list[dict[str, str]]:
    """Collect only public search-result metadata; page bodies stay local."""
    try:
        results = search_company(str(company.get("display_name") or ""))
    except Exception:
        return []
    hints: list[dict[str, str]] = []
    seen: set[str] = set()
    for result in results:
        url = _safe_public_url(result.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        hints.append({"title": str(result.get("title") or "")[:300], "url": url})
    return hints[:12]


def queue_company_research_in_connection(connection: Any, company_id: str, force: bool = False) -> str | None:
    existing = connection.execute(
        "SELECT id,status FROM processing_jobs WHERE kind='research_company' AND company_id=? ORDER BY created_at DESC LIMIT 1",
        (company_id,),
    ).fetchone()
    if existing and (not force or existing["status"] in {"pending", "running", "retry_wait"}):
        return existing["id"]
    job_id = str(uuid4())
    now = utc_now()
    connection.execute(
        "INSERT INTO processing_jobs(id,kind,company_id,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (job_id, "research_company", company_id, "pending", "research_queued", now, now),
    )
    return job_id


def ensure_company_research_jobs(force: bool = False, company_ids: list[str] | None = None) -> dict[str, int]:
    result = {"companies": 0, "queued": 0, "skipped_existing": 0, "skipped_active": 0}
    with connect() as connection:
        if company_ids:
            placeholders = ",".join("?" for _ in company_ids)
            companies = connection.execute(f"SELECT id FROM companies WHERE id IN ({placeholders})", tuple(company_ids)).fetchall()
        else:
            companies = connection.execute("SELECT id FROM companies ORDER BY updated_at DESC").fetchall()
        for company in companies:
            result["companies"] += 1
            jobs = connection.execute(
                "SELECT id,status,error FROM processing_jobs WHERE kind='research_company' AND company_id=? ORDER BY created_at DESC",
                (company["id"],),
            ).fetchall()
            stale = next(
                (
                    row for row in jobs
                    if row["status"] in {"pending", "needs_review", "failed"}
                    and str(row["error"] or "").startswith("Unknown processing job kind: research_company")
                ),
                None,
            )
            if stale and not force:
                now = utc_now()
                connection.execute(
                    """UPDATE processing_jobs SET status='pending',stage='research_queued',attempts=0,
                       lease_until=NULL,next_attempt_at=NULL,cancel_requested=0,error=NULL,finished_at=NULL,updated_at=? WHERE id=?""",
                    (now, stale["id"]),
                )
                result["queued"] += 1
                continue
            if any(row["status"] in {"pending", "running", "retry_wait"} for row in jobs):
                result["skipped_active"] += 1
                continue
            if jobs and not force:
                result["skipped_existing"] += 1
                continue
            queue_company_research_in_connection(connection, company["id"], force=force)
            result["queued"] += 1
    return result


def _finding_hash(title: str, summary: str, source_url: str) -> str:
    raw = "|".join((title.strip(), summary.strip(), source_url.strip()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def persist_company_research(company_id: str, payload: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
    company = one("SELECT * FROM companies WHERE id=?", (company_id,))
    if not company:
        raise RuntimeError("Company not found")
    now = utc_now()
    incoming_industries = [value for value in _json_list(payload.get("industry_codes")) if value in INDUSTRIES]
    old_industries = [company["primary_industry"], *_json_list(company["secondary_industries_json"])]
    tags = normalize_company_tags(
        [*_json_list(company["company_tags_json"]), *_json_list(payload.get("tags"))],
        payload.get("company_type") or company["company_nature"],
        list(dict.fromkeys([*old_industries, *incoming_industries])),
        str(payload.get("company_type") or ""),
    )
    primary_industry = incoming_industries[0] if incoming_industries else company["primary_industry"] or "other"
    secondary_industries = list(dict.fromkeys([
        value for value in [*old_industries, *incoming_industries]
        if value and value != primary_industry and value in INDUSTRIES
    ]))
    company_type = str(payload.get("company_type") or "")
    inferred_nature = COMPANY_TYPE_LABELS.get(company_type) if company_type in COMPANY_TYPE_LABELS else None
    summary = str(payload.get("summary") or "").strip()[:3000]
    status = "public_web" if str(payload.get("status") or "") == "complete" else "public_web_uncertain"
    source_rows = payload.get("sources_checked") if isinstance(payload.get("sources_checked"), list) else []
    first_source = next((_safe_public_url(item.get("url")) for item in source_rows if isinstance(item, dict) and _safe_public_url(item.get("url"))), None)
    public_findings: list[dict[str, Any]] = []
    with connect() as connection:
        locked = bool(company["summary_locked"])
        connection.execute(
            """UPDATE companies SET summary=CASE WHEN ? AND ?<>'' THEN ? ELSE summary END,
               primary_industry=?,secondary_industries_json=?,company_nature=COALESCE(NULLIF(company_nature,''),?),
               company_tags_json=?,public_researched_at=?,verification_status=?,updated_at=? WHERE id=?""",
            (
                int(not locked), summary, summary, primary_industry, json.dumps(secondary_industries, ensure_ascii=False),
                inferred_nature, json.dumps(tags, ensure_ascii=False), now, status, now, company_id,
            ),
        )
        if summary:
            connection.execute("UPDATE company_claims SET is_current=0 WHERE company_id=? AND field_name='summary'", (company_id,))
            connection.execute(
                "INSERT INTO company_claims(id,company_id,field_name,field_value,source_url,source_type,retrieved_at,confidence,is_current) VALUES(?,?,?,?,?,?,?,?,1)",
                (str(uuid4()), company_id, "summary", summary, first_source, "public_web", now, 0.8 if first_source else 0.45),
            )
        for fact in payload.get("facts") or []:
            if not isinstance(fact, dict) or not str(fact.get("fact") or "").strip():
                continue
            fact_url = _safe_public_url(fact.get("source_url"))
            connection.execute(
                "INSERT INTO company_claims(id,company_id,field_name,field_value,source_url,source_type,retrieved_at,confidence,is_current) VALUES(?,?,?,?,?,?,?,?,0)",
                (str(uuid4()), company_id, "public_fact", str(fact["fact"]).strip()[:2000], fact_url, "public_web", now, 0.7 if fact_url else 0.35),
            )
        for finding in payload.get("negative_findings") or []:
            if not isinstance(finding, dict):
                continue
            title = str(finding.get("title") or "").strip()[:500]
            finding_summary = str(finding.get("summary") or "").strip()[:4000]
            source_url = _safe_public_url(finding.get("source_url"))
            if not title or not finding_summary or not source_url:
                continue
            resolved_url = _safe_resolved_url(finding.get("resolved_url"))
            severity = str(finding.get("severity") or "unknown")
            if severity not in {"low", "medium", "high", "unknown"}:
                severity = "unknown"
            content_hash = _finding_hash(title, finding_summary, source_url)
            existing = connection.execute(
                "SELECT id FROM company_public_findings WHERE company_id=? AND content_hash=?",
                (company_id, content_hash),
            ).fetchone()
            values = (
                title, finding_summary, str(finding.get("source_title") or "").strip()[:500] or None,
                source_url, resolved_url, str(finding.get("published_at") or "").strip()[:100] or None,
                severity, now,
            )
            if existing:
                connection.execute(
                    "UPDATE company_public_findings SET title=?,summary=?,source_title=?,source_url=?,resolved_url=?,published_at=?,severity=?,retrieved_at=? WHERE id=?",
                    (*values, existing["id"]),
                )
                finding_id = existing["id"]
            else:
                finding_id = str(uuid4())
                connection.execute(
                    """INSERT INTO company_public_findings(id,company_id,finding_type,title,summary,source_title,source_url,
                       resolved_url,published_at,severity,content_hash,retrieved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (finding_id, company_id, "negative_news", *values[:6], values[6], content_hash, now),
                )
            evidence_excerpt = json.dumps({"finding_id": finding_id, "title": title, "summary": finding_summary, "source_title": finding.get("source_title") or "", "severity": severity}, ensure_ascii=False)
            if not connection.execute(
                "SELECT id FROM evidences WHERE company_id=? AND source_type='public_negative_news' AND source_url=? AND excerpt=?",
                (company_id, source_url, evidence_excerpt),
            ).fetchone():
                connection.execute(
                    "INSERT INTO evidences(id,company_id,source_url,source_type,excerpt,observed_at) VALUES(?,?,?,?,?,?)",
                    (str(uuid4()), company_id, source_url, "public_negative_news", evidence_excerpt, now),
                )
            public_findings.append({"id": finding_id, "title": title, "source_url": source_url, "severity": severity})
        connection.execute(
            "UPDATE search_index SET title=?,body=? WHERE entity_type='company' AND entity_id=?",
            (company["display_name"], f"{company[ 'display_name' ]} {summary} {' '.join(tag['label'] for tag in tags)}".strip(), company_id),
        )
        connection.execute(
            "INSERT INTO company_versions(id,company_id,profile_json,decision,reason,processor,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), company_id, json.dumps(payload, ensure_ascii=False), "public_research", str(payload.get("reason") or "")[:2000], "research_company", now),
        )
        deduplicate_company_jobs(connection, company_id)
    return {
        "status": "succeeded",
        "company_id": company_id,
        "job_id": job_id,
        "research_status": status,
        "summary_updated": bool(summary and not bool(company["summary_locked"])),
        "tags": tags,
        "negative_findings": len(public_findings),
    }


def execute_company_research(company_id: str, job_id: str, is_active: Any | None = None) -> dict[str, Any]:
    company_row = one("SELECT * FROM companies WHERE id=?", (company_id,))
    if not company_row:
        raise RuntimeError("Company not found")
    search_settings = get_setting("search", {}) or {}
    if not search_settings.get("enabled", True):
        return {"status": "disabled", "company_id": company_id, "job_id": job_id}
    company = dict(company_row)
    hints = _source_hints(company) if str(get_setting("processing_engine", "codex") or "codex") == "generic" else []
    model_result = research_company_overview(company, hints, job_id)
    if is_active is not None and not is_active():
        return {"status": "canceled", "company_id": company_id, "job_id": job_id}
    return persist_company_research(company_id, model_result.payload, job_id)
