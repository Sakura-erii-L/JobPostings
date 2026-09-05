from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from uuid import uuid4

from .config import config
from .catalog import _batch_for, _job_identity_matches, _make_job, _raw_source_text, _store_recruitment_shared_details, deduplicate_company_jobs, event_company_for_title, is_aggregate_job_title, normalize_company_tags, normalize_employment_type, normalize_title
from .db import connect, utc_now
from .parsers import extract_event_datetime_candidates, extract_file, extract_recruitment_catalog, extract_recruitment_shared_details, is_file_message, is_major_like_title, is_major_requirement_heading, is_wechat_public_url, normalize_file_filename, normalize_event_datetime, parse_message_time, recover_original_source_url


# Imported recruitment content and its derived catalog are reset together so
# the next TraceMemo import starts from a consistent empty catalog.
_RECRUITMENT_TABLES = (
    "job_tag_links",
    "user_job_states",
    "application_events",
    "user_notes",
    "user_follows",
    "recruitment_event_evidences",
    "recruitment_event_versions",
    "recruitment_shared_details",
    "evidences",
    "job_versions",
    "jobs",
    "company_versions",
    "company_public_findings",
    "company_claims",
    "company_relations",
    "company_merge_rules",
    "recruitment_batches",
    "recruitment_events",
    "companies",
    "processing_logs",
    "review_items",
    "artifacts",
    "processing_jobs",
    "raw_messages",
    "sync_cursors",
    "tracememo_message_cache",
    "tracememo_cache_state",
    "ingest_runs",
    "llm_calls",
    "notifications",
)


def _batch_from_evidence(connection: Any, company_id: str, excerpt: Any) -> str | None:
    try:
        item = json.loads(excerpt or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    batch = item.get("batch") if isinstance(item, dict) else None
    if not isinstance(batch, dict) or not any(batch.get(key) for key in ("name", "year", "season", "recruitment_type")):
        return None
    recruitment_type = str(batch.get("recruitment_type") or "unknown")
    return _batch_for(connection, company_id, batch, recruitment_type)


def _create_safety_backup() -> str:
    config.ensure_dirs()
    backup_dir = config.data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = backup_dir / f"pre-force-refetch-{stamp}.db"
    source = connect()
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    return str(target)


def reset_recruitment_data() -> dict[str, Any]:
    """Clear imported recruitment data after creating a recoverable backup."""
    from .codex_agent import cancel_codex_job

    backup_path = _create_safety_backup()
    now = utc_now()
    with connect() as connection:
        running_ids = [row["id"] for row in connection.execute("SELECT id FROM processing_jobs WHERE status='running'").fetchall()]
        connection.execute("UPDATE queue_control SET state='paused',updated_at=? WHERE id=1", (now,))
        connection.execute(
            """UPDATE processing_jobs
               SET status='canceled',stage='canceled',cancel_requested=1,lease_until=NULL,
                   finished_at=?,updated_at=?
               WHERE status IN ('pending','running','needs_review','paused_quota','failed')""",
            (now, now),
        )

    for job_id in running_ids:
        cancel_codex_job(job_id)

    # Let a terminated local process release its handles before deleting rows.
    # The status guard prevents late model results from being persisted.
    time.sleep(0.2)

    deleted: dict[str, int] = {}
    with connect() as connection:
        for table in _RECRUITMENT_TABLES:
            count = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            connection.execute(f"DELETE FROM {table}")
            deleted[table] = int(count)
        search_count = connection.execute("SELECT COUNT(*) AS count FROM search_index").fetchone()["count"]
        connection.execute("DELETE FROM search_index")
        deleted["search_index"] = int(search_count)
        connection.execute("UPDATE queue_control SET state='paused',updated_at=? WHERE id=1", (utc_now(),))
    return {"backup_path": backup_path, "deleted": deleted, "canceled_running": len(running_ids)}


def repair_raw_message_times() -> dict[str, int]:
    """Repair raw chat times from stored explicit source dates only."""
    result = {"checked": 0, "updated": 0, "unknown": 0}
    with connect() as connection:
        rows = connection.execute("SELECT id,sent_at,metadata_json FROM raw_messages").fetchall()
        for row in rows:
            result["checked"] += 1
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            source_time = parse_message_time(metadata) if isinstance(metadata, dict) else None
            if not source_time:
                result["unknown"] += 1
                continue
            if row["sent_at"] != source_time:
                connection.execute("UPDATE raw_messages SET sent_at=? WHERE id=?", (source_time, row["id"]))
                result["updated"] += 1
    return result


def repair_source_urls() -> dict[str, int]:
    """Restore original source URLs after a WeChat verification redirect."""
    result = {
        "raw_messages_checked": 0,
        "raw_messages_updated": 0,
        "evidences_checked": 0,
        "evidences_updated": 0,
        "company_claims_checked": 0,
        "company_claims_updated": 0,
        "source_types_updated": 0,
        "public_findings_checked": 0,
        "public_findings_updated": 0,
    }
    raw_sources: dict[str, str] = {}
    with connect() as connection:
        raw_rows = connection.execute("SELECT id,metadata_json FROM raw_messages").fetchall()
        for row in raw_rows:
            result["raw_messages_checked"] += 1
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                continue
            original = recover_original_source_url(metadata.get("source_url") or metadata.get("url"))
            if not original:
                continue
            raw_sources[row["id"]] = original
            changed = False
            if metadata.get("source_url") != original:
                metadata["source_url"] = original
                changed = True
            current_url = str(metadata.get("url") or "").strip()
            if current_url and current_url != original:
                if not metadata.get("resolved_url"):
                    metadata["resolved_url"] = current_url
                metadata["url"] = original
                changed = True
            if changed:
                connection.execute(
                    "UPDATE raw_messages SET metadata_json=? WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False), row["id"]),
                )
                result["raw_messages_updated"] += 1

        evidence_rows = connection.execute("SELECT id,raw_message_id,source_url,source_type FROM evidences").fetchall()
        for row in evidence_rows:
            result["evidences_checked"] += 1
            original = raw_sources.get(row["raw_message_id"]) or recover_original_source_url(row["source_url"])
            source_url = original or row["source_url"]
            updates: list[str] = []
            values: list[Any] = []
            if original and original != row["source_url"]:
                updates.append("source_url=?")
                values.append(original)
                result["evidences_updated"] += 1
            if row["source_type"] == "public_web" and is_wechat_public_url(source_url or ""):
                updates.append("source_type=?")
                values.append("wechat_official_account")
                result["source_types_updated"] += 1
            if updates:
                values.append(row["id"])
                connection.execute(f"UPDATE evidences SET {', '.join(updates)} WHERE id=?", tuple(values))

        claim_rows = connection.execute("SELECT id,source_url,source_type FROM company_claims").fetchall()
        for row in claim_rows:
            result["company_claims_checked"] += 1
            original = recover_original_source_url(row["source_url"])
            source_url = original or row["source_url"]
            updates = []
            values = []
            if original and original != row["source_url"]:
                updates.append("source_url=?")
                values.append(original)
                result["company_claims_updated"] += 1
            if row["source_type"] == "public_web" and is_wechat_public_url(source_url or ""):
                updates.append("source_type=?")
                values.append("wechat_official_account")
                result["source_types_updated"] += 1
            if updates:
                values.append(row["id"])
                connection.execute(f"UPDATE company_claims SET {', '.join(updates)} WHERE id=?", tuple(values))
        finding_rows = connection.execute("SELECT id,source_url FROM company_public_findings").fetchall()
        for row in finding_rows:
            result["public_findings_checked"] += 1
            original = recover_original_source_url(row["source_url"])
            if original and original != row["source_url"]:
                connection.execute("UPDATE company_public_findings SET source_url=? WHERE id=?", (original, row["id"]))
                result["public_findings_updated"] += 1
    return result


def _nearest_candidate(candidates: list[str], reference_at: str | None, minimum: str | None = None) -> str | None:
    if not candidates:
        return None
    try:
        reference = datetime.fromisoformat(reference_at) if reference_at else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        reference = datetime.now(timezone.utc)
    try:
        lower_bound = datetime.fromisoformat(minimum) if minimum else None
    except (TypeError, ValueError):
        lower_bound = None
    filtered = []
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if lower_bound is None or parsed >= lower_bound:
            filtered.append((abs((parsed - reference).total_seconds()), candidate))
    return min(filtered)[1] if filtered else None


def repair_timeline_events() -> dict[str, int]:
    """Repair implausible event dates from the original source text."""
    result = {"checked": 0, "updated": 0, "recovered": 0, "cleared": 0}
    with connect() as connection:
        events = connection.execute("SELECT * FROM recruitment_events").fetchall()
        for event in events:
            result["checked"] += 1
            version = connection.execute(
                "SELECT id,payload_json,observed_at FROM recruitment_event_versions WHERE event_id=? ORDER BY is_current DESC,observed_at DESC LIMIT 1",
                (event["id"],),
            ).fetchone()
            reference_at = version["observed_at"] if version else None
            evidence_rows = connection.execute(
                """SELECT r.id AS raw_message_id,COALESCE(r.text_content,'') AS raw_text,COALESCE(e.excerpt,'') AS excerpt
                   FROM recruitment_event_evidences ee JOIN evidences e ON e.id=ee.evidence_id
                   LEFT JOIN raw_messages r ON r.id=e.raw_message_id WHERE ee.event_id=?""",
                (event["id"],),
            ).fetchall()
            source_text = "\n".join(
                value
                for row in evidence_rows
                for value in (_raw_source_text(connection, row["raw_message_id"]) or row["raw_text"], row["excerpt"])
                if value
            )
            if version and version["payload_json"]:
                source_text = f"{source_text}\n{version['payload_json']}"
            candidates = extract_event_datetime_candidates(source_text, event["timezone"] or "Asia/Shanghai", reference_at)
            current_start = normalize_event_datetime(event["start_at"], event["timezone"] or "Asia/Shanghai", reference_at)
            current_end = normalize_event_datetime(event["end_at"], event["timezone"] or "Asia/Shanghai", reference_at)
            timed_candidates = []
            try:
                event_zone = ZoneInfo(event["timezone"] or "Asia/Shanghai")
            except Exception:
                event_zone = ZoneInfo("Asia/Shanghai")
            for candidate in candidates:
                try:
                    parsed_candidate = datetime.fromisoformat(candidate)
                except ValueError:
                    continue
                local_candidate = parsed_candidate.astimezone(event_zone)
                if local_candidate.hour or local_candidate.minute:
                    timed_candidates.append(candidate)
            recovered = False
            if event["start_at"] and (not current_start or (current_start.endswith("T00:00:00+00:00") and timed_candidates)):
                current_start = _nearest_candidate(timed_candidates or candidates, reference_at)
                recovered = bool(current_start)
            if event["end_at"] and not current_end:
                current_end = _nearest_candidate(timed_candidates or candidates, reference_at, current_start)
                recovered = recovered or bool(current_end)
            new_status = event["status"]
            if current_start:
                new_status = "historical" if current_start < utc_now() else "upcoming"
            changed = current_start != event["start_at"] or current_end != event["end_at"] or new_status != event["status"]
            if not changed:
                continue
            connection.execute(
                "UPDATE recruitment_events SET start_at=?,end_at=?,status=?,updated_at=? WHERE id=?",
                (current_start, current_end, new_status, utc_now(), event["id"]),
            )
            if (current_start and current_start != event["start_at"]) or (current_end and current_end != event["end_at"]):
                result["recovered"] += 1
            if (event["start_at"] and not current_start) or (event["end_at"] and not current_end):
                result["cleared"] += 1
            if version:
                try:
                    payload = json.loads(version["payload_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, dict):
                    payload["start_at"] = current_start or ""
                    payload["end_at"] = current_end or ""
                    payload["time_repair"] = "已根据来源正文校正；无法确认的异常日期已置为空"
                    connection.execute("UPDATE recruitment_event_versions SET payload_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), version["id"]))
            result["updated"] += 1
    return result


def repair_event_company_assignments() -> int:
    """Move events whose title contains an explicit, distinct company name."""
    updated = 0
    with connect() as connection:
        events = connection.execute("SELECT id,company_id,batch_id,title FROM recruitment_events").fetchall()
        for event in events:
            target_id = event_company_for_title(connection, event["company_id"], {"title": event["title"]})
            if not target_id or target_id == event["company_id"]:
                continue
            target_batch_id = None
            if event["batch_id"]:
                source_batch = connection.execute(
                    "SELECT name,year,season,recruitment_type,confidence FROM recruitment_batches WHERE id=?",
                    (event["batch_id"],),
                ).fetchone()
                if source_batch:
                    target_batch_id = _batch_for(connection, target_id, dict(source_batch), source_batch["recruitment_type"])
            connection.execute("UPDATE recruitment_events SET company_id=?,batch_id=?,updated_at=? WHERE id=?", (target_id, target_batch_id, utc_now(), event["id"]))
            evidence_rows = connection.execute(
                "SELECT e.* FROM recruitment_event_evidences ee JOIN evidences e ON e.id=ee.evidence_id WHERE ee.event_id=?",
                (event["id"],),
            ).fetchall()
            for evidence in evidence_rows:
                if evidence["company_id"] == target_id and evidence["job_id"] is None:
                    continue
                copied_id = str(uuid4())
                connection.execute(
                    "INSERT INTO evidences(id,company_id,job_id,raw_message_id,artifact_id,source_url,source_type,excerpt,location,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (copied_id, target_id, None, evidence["raw_message_id"], evidence["artifact_id"], evidence["source_url"], evidence["source_type"], evidence["excerpt"], evidence["location"], evidence["observed_at"]),
                )
                connection.execute("UPDATE recruitment_event_evidences SET evidence_id=? WHERE event_id=? AND evidence_id=?", (copied_id, event["id"], evidence["id"]))
            updated += 1
    return updated


def backfill_major_requirements() -> int:
    """Backfill company-level major requirements from stored source text and OCR."""
    updated = 0
    with connect() as connection:
        companies = connection.execute("SELECT id,major_requirements_json FROM companies").fetchall()
        for company in companies:
            try:
                existing = json.loads(company["major_requirements_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                existing = []
            if not isinstance(existing, list):
                existing = []
            existing = [str(value).strip() for value in existing if not is_major_requirement_heading(value)]
            raw_ids = connection.execute(
                """SELECT DISTINCT e.raw_message_id FROM evidences e
                   WHERE e.company_id=? OR e.job_id IN (SELECT id FROM jobs WHERE company_id=?)""",
                (company["id"], company["id"]),
            ).fetchall()
            discovered: list[str] = []
            for raw in raw_ids:
                catalog = extract_recruitment_catalog(_raw_source_text(connection, raw["raw_message_id"]))
                discovered.extend(catalog.get("major_requirements") or [])
            merged = list(dict.fromkeys([
                *existing,
                *(value for value in discovered if not is_major_requirement_heading(value)),
            ]))
            encoded = json.dumps(merged, ensure_ascii=False)
            if encoded != company["major_requirements_json"]:
                connection.execute("UPDATE companies SET major_requirements_json=?,updated_at=? WHERE id=?", (encoded, utc_now(), company["id"]))
                updated += 1
    return updated


def migrate_major_jobs() -> dict[str, int]:
    """Move legacy academic-major rows out of the active job catalog."""
    result = {"jobs_migrated": 0, "job_majors_cleared": 0, "major_requirements_updated": 0}
    with connect() as connection:
        companies = connection.execute("SELECT id,major_requirements_json FROM companies").fetchall()
        for company in companies:
            try:
                major_requirements = json.loads(company["major_requirements_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                major_requirements = []
            if not isinstance(major_requirements, list):
                major_requirements = []
            original_major_requirements = list(major_requirements)
            jobs = connection.execute(
                "SELECT id,canonical_title,majors_json FROM jobs WHERE company_id=? AND status<>'superseded'",
                (company["id"],),
            ).fetchall()
            for job in jobs:
                try:
                    job_majors = json.loads(job["majors_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    job_majors = []
                if isinstance(job_majors, list):
                    for major in job_majors:
                        value = str(major or "").strip()
                        if value and value not in major_requirements:
                            major_requirements.append(value)
                if is_major_like_title(job["canonical_title"]):
                    title = str(job["canonical_title"] or "").strip()
                    if title and title not in major_requirements:
                        major_requirements.append(title)
                    connection.execute("UPDATE jobs SET status='superseded',updated_at=? WHERE id=?", (utc_now(), job["id"]))
                    connection.execute("UPDATE evidences SET company_id=?,job_id=NULL WHERE job_id=?", (company["id"], job["id"]))
                    connection.execute("DELETE FROM search_index WHERE entity_type='job' AND entity_id=?", (job["id"],))
                    result["jobs_migrated"] += 1
                elif isinstance(job_majors, list) and job_majors:
                    connection.execute("UPDATE jobs SET majors_json='[]',updated_at=? WHERE id=?", (utc_now(), job["id"]))
                    result["job_majors_cleared"] += 1
            if major_requirements != original_major_requirements:
                connection.execute(
                    "UPDATE companies SET major_requirements_json=?,updated_at=? WHERE id=?",
                    (json.dumps(major_requirements, ensure_ascii=False), utc_now(), company["id"]),
                )
                result["major_requirements_updated"] += 1
    return result


def split_existing_job_lists() -> int:
    """Create missing jobs from explicit multi-job lists in existing sources."""
    created = 0
    with connect() as connection:
        company_ids = [row["id"] for row in connection.execute("SELECT id FROM companies").fetchall()]
        for company_id in company_ids:
            raw_rows = connection.execute(
                """SELECT e.id AS evidence_id,e.raw_message_id,e.excerpt,COALESCE(r.sent_at,e.observed_at) AS observed_at
                   FROM evidences e LEFT JOIN raw_messages r ON r.id=e.raw_message_id
                   WHERE e.company_id=? OR e.job_id IN (SELECT id FROM jobs WHERE company_id=?)""",
                (company_id, company_id),
            ).fetchall()
            processed_raw_ids: set[str] = set()
            for raw in raw_rows:
                if not raw["raw_message_id"] or raw["raw_message_id"] in processed_raw_ids:
                    continue
                processed_raw_ids.add(raw["raw_message_id"])
                catalog = extract_recruitment_catalog(_raw_source_text(connection, raw["raw_message_id"]))
                titles = catalog.get("job_titles") or []
                if not titles:
                    continue
                current_jobs = [dict(row) for row in connection.execute("SELECT * FROM jobs WHERE company_id=? AND status<>'superseded'", (company_id,)).fetchall()]
                template = next((job for job in current_jobs if is_aggregate_job_title(job["canonical_title"])), None)
                batch_id = _batch_from_evidence(connection, company_id, raw["excerpt"])
                for title in titles:
                    if any(normalize_title(job["canonical_title"]) == normalize_title(title) for job in current_jobs):
                        continue
                    job_data: dict[str, Any] = {
                        "title": title,
                        "recruitment_type": template["recruitment_type"] if template else "unknown",
                        "employment_type": template["employment_type"] if template else "unknown",
                    }
                    _make_job(connection, company_id, batch_id or (template["batch_id"] if template else None), job_data, raw["observed_at"] or utc_now(), raw["raw_message_id"])
                    current_jobs = [dict(row) for row in connection.execute("SELECT * FROM jobs WHERE company_id=? AND status<>'superseded'", (company_id,)).fetchall()]
                    created += 1
                if template:
                    connection.execute("UPDATE jobs SET status='superseded',updated_at=? WHERE id=?", (utc_now(), template["id"]))
                    connection.execute("UPDATE evidences SET company_id=?,job_id=NULL WHERE job_id=?", (company_id, template["id"]))
                    connection.execute("DELETE FROM search_index WHERE entity_type='job' AND entity_id=?", (template["id"],))
    return created


def backfill_recruitment_shared_details() -> int:
    """Backfill source/batch-level salary and locations from stored source text."""
    updated = 0
    with connect() as connection:
        rows = connection.execute(
            """SELECT e.id AS evidence_id,e.company_id,e.raw_message_id,e.excerpt,
                      COALESCE(r.sent_at,e.observed_at) AS observed_at
               FROM evidences e LEFT JOIN raw_messages r ON r.id=e.raw_message_id
               WHERE e.company_id IS NOT NULL AND e.raw_message_id IS NOT NULL
               ORDER BY e.observed_at,e.id"""
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["company_id"], row["raw_message_id"])
            if key in seen:
                continue
            seen.add(key)
            details = extract_recruitment_shared_details(_raw_source_text(connection, row["raw_message_id"]))
            if not details.get("locations") and not details.get("salary"):
                continue
            batch_id = _batch_from_evidence(connection, row["company_id"], row["excerpt"])
            detail_id = _store_recruitment_shared_details(
                connection,
                row["company_id"],
                batch_id,
                row["evidence_id"],
                row["raw_message_id"],
                details,
                row["observed_at"] or utc_now(),
            )
            if detail_id:
                updated += 1
    return updated


def requeue_missing_job_sources() -> int:
    """Retry successful recruitment sources that mention jobs but still produced none."""
    requeued = 0
    with connect() as connection:
        rows = connection.execute(
            """SELECT DISTINCT p.id AS processing_job_id,r.id AS raw_message_id,e.company_id
               FROM processing_jobs p
               JOIN raw_messages r ON r.id=p.raw_message_id
               JOIN evidences e ON e.raw_message_id=r.id AND e.company_id IS NOT NULL
               WHERE p.kind='classify' AND p.status='succeeded' AND r.is_recruitment=1
                 AND NOT EXISTS (
                     SELECT 1 FROM evidences job_evidence
                     JOIN jobs j ON j.id=job_evidence.job_id AND j.status<>'superseded'
                     WHERE job_evidence.raw_message_id=r.id
                 )"""
        ).fetchall()
        for row in rows:
            source_text = _raw_source_text(connection, row["raw_message_id"])
            if not re.search(r"岗位|职位|职务", source_text):
                continue
            connection.execute(
                """UPDATE processing_jobs SET status='pending',stage='queued',attempts=0,cancel_requested=0,
                   lease_until=NULL,next_attempt_at=NULL,processor=NULL,result_json=NULL,error=NULL,
                   started_at=NULL,finished_at=NULL,updated_at=? WHERE id=?""",
                (utc_now(), row["processing_job_id"]),
            )
            connection.execute(
                """UPDATE raw_messages SET recognition_status='pending',recognized_at=NULL,recognition_error=NULL
                   WHERE id=?""",
                (row["raw_message_id"],),
            )
            requeued += 1
    return requeued


def supersede_duplicate_jobs() -> int:
    """Hide duplicate jobs without deleting their historical records."""
    updated = 0
    with connect() as connection:
        for company in connection.execute("SELECT id FROM companies").fetchall():
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM jobs WHERE company_id=? AND status<>'superseded' ORDER BY normalized_title,created_at,id",
                (company["id"],),
            ).fetchall()]
            groups: list[list[dict[str, Any]]] = []
            for row in rows:
                group = next(
                    (
                        candidate for candidate in groups
                        if _job_identity_matches(
                            row,
                            candidate[0]["normalized_title"],
                            candidate[0]["recruitment_type"],
                            normalize_employment_type(candidate[0]["employment_type"]),
                            candidate[0]["department"],
                        )
                    ),
                    None,
                )
                if group is None:
                    group = []
                    groups.append(group)
                group.append(row)
            for group in groups:
                for duplicate in group[1:]:
                    has_user_state = any(
                        connection.execute(f"SELECT 1 FROM {table} WHERE job_id=? LIMIT 1", (duplicate["id"],)).fetchone()
                        for table in ("user_job_states", "application_events", "user_notes", "job_tag_links")
                    )
                    if has_user_state:
                        continue
                    connection.execute("UPDATE jobs SET status='superseded',updated_at=? WHERE id=?", (utc_now(), duplicate["id"]))
                    connection.execute("UPDATE evidences SET company_id=?,job_id=NULL WHERE job_id=?", (company["id"], duplicate["id"]))
                    connection.execute("DELETE FROM search_index WHERE entity_type='job' AND entity_id=?", (duplicate["id"],))
                    updated += 1
    return updated


def backfill_company_tags() -> int:
    updated = 0
    with connect() as connection:
        rows = connection.execute("SELECT id,company_nature,primary_industry,secondary_industries_json,company_tags_json FROM companies").fetchall()
        for row in rows:
            try:
                secondary = json.loads(row["secondary_industries_json"] or "[]")
                tags = json.loads(row["company_tags_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                secondary, tags = [], []
            normalized = normalize_company_tags(tags, row["company_nature"], [row["primary_industry"], *secondary])
            encoded = json.dumps(normalized, ensure_ascii=False)
            if encoded != row["company_tags_json"]:
                connection.execute("UPDATE companies SET company_tags_json=?,updated_at=? WHERE id=?", (encoded, utc_now(), row["id"]))
                updated += 1
    return updated


def deduplicate_all_jobs() -> int:
    removed = 0
    with connect() as connection:
        company_ids = [row["id"] for row in connection.execute("SELECT id FROM companies").fetchall()]
        for company_id in company_ids:
            removed += deduplicate_company_jobs(connection, company_id)
    return removed


def _merge_repaired_text(existing: Any, extracted: Any) -> str:
    old_text = str(existing or "").strip()
    new_text = str(extracted or "").strip()
    if not new_text or new_text in old_text:
        return old_text
    if old_text and old_text in new_text:
        return new_text
    return "\n".join(value for value in (old_text, new_text) if value).strip()


def repair_tracememo_file_attachments() -> dict[str, Any]:
    """Repair cached TraceMemo documents without marking unresolved rows complete."""
    from .processing import attach_artifact, requeue_message_for_processing
    from .security import SecretVault
    from .tracememo import TraceMemoClient, tracememo_filename, tracememo_inline_media, tracememo_local_media, tracememo_media_references

    result: dict[str, Any] = {
        "status": "no_candidates",
        "backup_path": None,
        "raw_checked": 0,
        "artifacts_reparsed": 0,
        "artifacts_updated": 0,
        "cached_files_checked": 0,
        "matched": 0,
        "media_attached": 0,
        "media_failed": 0,
        "unmatched": 0,
        "requeued": 0,
    }
    with connect() as connection:
        connector = connection.execute(
            "SELECT * FROM connectors WHERE kind='tracememo' AND enabled=1"
        ).fetchone()
        if not connector:
            result["status"] = "disabled"
            return result
        raw_rows = connection.execute(
            """SELECT r.*,
                      EXISTS(SELECT 1 FROM artifacts a WHERE a.raw_message_id=r.id) AS has_artifact
               FROM raw_messages r
               WHERE r.connector_id=?""",
            (connector["id"],),
        ).fetchall()
        raw_by_external: dict[tuple[str | None, str], list[Any]] = {}
        raw_by_metadata: dict[tuple[str | None, str], list[Any]] = {}
        raw_file_rows: list[Any] = []
        for raw in raw_rows:
            result["raw_checked"] += 1
            try:
                metadata = json.loads(raw["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not is_file_message(raw["message_type"], metadata):
                continue
            raw_file_rows.append(raw)
            group_key = raw["source_group_id"]
            if raw["external_message_id"]:
                raw_by_external.setdefault((group_key, str(raw["external_message_id"])), []).append(raw)
            if isinstance(metadata, dict):
                for key in ("id", "messageId", "message_id", "serverId", "server_id", "localId", "local_id"):
                    value = str(metadata.get(key) or "").strip()
                    if value:
                        raw_by_metadata.setdefault((group_key, value), []).append(raw)
        cache_rows = connection.execute(
            "SELECT source_group_id,external_message_id,message_json FROM tracememo_message_cache WHERE connector_id=?",
            (connector["id"],),
        ).fetchall()
    if not raw_file_rows and not cache_rows:
        return result
    local_artifact_candidates = [row for row in raw_file_rows if row["has_artifact"]]
    cached_file_rows: list[tuple[Any, dict[str, Any], Any | None]] = []
    cached_file_candidates: list[tuple[Any, dict[str, Any], Any]] = []
    for cache in cache_rows:
        try:
            message = json.loads(cache["message_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict):
            continue
        message_type = str(message.get("type") or message.get("msgType") or "text")
        if not is_file_message(message_type, message):
            continue
        references = tracememo_media_references(message)
        inline_media = tracememo_inline_media(message) or tracememo_local_media(message)
        raw_matches: list[Any] = []
        for value in [cache["external_message_id"], message.get("id"), message.get("messageId"), message.get("serverId")]:
            if value is None:
                continue
            raw_matches.extend(raw_by_external.get((cache["source_group_id"], str(value)), []))
            raw_matches.extend(raw_by_metadata.get((cache["source_group_id"], str(value)), []))
        unique_matches = {row["id"]: row for row in raw_matches}
        raw = next(iter(unique_matches.values()), None)
        cached_file_rows.append((cache, message, raw))
        if raw and not raw["has_artifact"] and (references or inline_media):
            cached_file_candidates.append((cache, message, raw))
    result["cached_files_checked"] = len(cached_file_rows)
    result["matched"] = sum(1 for _, _, raw in cached_file_rows if raw)
    result["unmatched"] = sum(1 for _, _, raw in cached_file_rows if not raw)
    if not local_artifact_candidates and not cached_file_candidates:
        return result
    result["status"] = "repairing"
    def ensure_backup() -> None:
        if not result["backup_path"]:
            result["backup_path"] = _create_safety_backup()

    requeue_ids: set[str] = set()
    for raw in local_artifact_candidates:
        with connect() as connection:
            artifacts = connection.execute(
                "SELECT * FROM artifacts WHERE raw_message_id=? ORDER BY created_at",
                (raw["id"],),
            ).fetchall()
            for artifact in artifacts:
                path = Path(str(artifact["path"] or ""))
                if not path.exists():
                    continue
                try:
                    data = path.read_bytes()
                    filename = normalize_file_filename(artifact["filename"] or "attachment.bin", data, artifact["mime_type"])
                    parsed = extract_file(filename, data, mime_type=artifact["mime_type"])
                except (OSError, ValueError) as exc:
                    result.setdefault("errors", []).append(f"{raw['id']}: {exc}")
                    continue
                result["artifacts_reparsed"] += 1
                mime_type = str(artifact["mime_type"] or "").split(";", 1)[0].strip() or None
                new_text = str(parsed.get("text") or "").strip()
                old_text = str(artifact["ocr_text"] or "").strip()
                raw_row = connection.execute("SELECT text_content FROM raw_messages WHERE id=?", (raw["id"],)).fetchone()
                merged_text = _merge_repaired_text(raw_row["text_content"] if raw_row else "", new_text)
                changed = (
                    artifact["filename"] != filename
                    or artifact["mime_type"] != mime_type
                    or old_text != new_text
                    or (raw_row and raw_row["text_content"] != merged_text)
                )
                if changed:
                    ensure_backup()
                    connection.execute(
                        "UPDATE artifacts SET filename=?,mime_type=?,ocr_text=?,qr_values_json=? WHERE id=?",
                        (filename, mime_type, new_text, json.dumps(parsed.get("qr_values", []), ensure_ascii=False), artifact["id"]),
                    )
                    connection.execute("UPDATE raw_messages SET text_content=? WHERE id=?", (merged_text, raw["id"]))
                    result["artifacts_updated"] += 1
                    requeue_ids.add(raw["id"])
    client = None
    if cached_file_candidates:
        settings = json.loads(connector["config_json"] or "{}")
        client = TraceMemoClient(connector["base_url"], SecretVault().decrypt(settings["token"]) if settings.get("token") else "")
    for cache, message, raw in cached_file_candidates:
        inline_media = tracememo_inline_media(message) or tracememo_local_media(message)
        inline_error: Exception | None = None
        if inline_media:
            try:
                ensure_backup()
                media, filename, mime_type = inline_media
                attach_artifact(raw["id"], filename or tracememo_filename(message), media, mime_type)
                result["media_attached"] += 1
                requeue_ids.add(raw["id"])
                continue
            except Exception as exc:
                inline_error = exc
        last_error: Exception | None = None
        for reference in tracememo_media_references(message):
            try:
                if client is None:
                    raise RuntimeError("TraceMemo client is not available")
                media, suggested_name = client.media(reference)
                filename = tracememo_filename(message, suggested_name)
                mime_type = message.get("mime_type") or message.get("mimeType")
                content_data = message.get("contentData") if isinstance(message.get("contentData"), dict) else {}
                mime_type = mime_type or content_data.get("mime_type") or content_data.get("mimeType")
                ensure_backup()
                attach_artifact(raw["id"], filename, media, mime_type)
                result["media_attached"] += 1
                requeue_ids.add(raw["id"])
                break
            except Exception as exc:
                last_error = exc
        else:
            result["media_failed"] += 1
            result.setdefault("errors", []).append(f"{raw['id']}: {last_error or inline_error}")
    for raw_id in sorted(requeue_ids):
        requeue_message_for_processing(raw_id)
    result["requeued"] = len(requeue_ids)
    result["status"] = "completed" if not result["media_failed"] else "partial"
    return result


def repair_existing_catalog() -> dict[str, Any]:
    """Run the one-time historical repair behind a recoverable database backup."""
    marker = "historical_catalog_repair_v6"
    with connect() as connection:
        if connection.execute("SELECT 1 FROM schema_meta WHERE key=?", (marker,)).fetchone():
            return {"status": "already_repaired", "backup_path": None, "timeline": {}, "events_reassigned": 0, "jobs_created": 0, "jobs_superseded": 0, "tags_updated": 0, "major_requirements_updated": 0, "major_jobs_migrated": 0, "job_majors_cleared": 0, "shared_details_updated": 0, "jobs_requeued": 0}
        has_previous_catalog_repair = bool(connection.execute("SELECT 1 FROM schema_meta WHERE key=?", ("historical_catalog_repair_v5",)).fetchone())
        has_major_job_migration = bool(connection.execute("SELECT 1 FROM schema_meta WHERE key=?", ("historical_catalog_repair_v4",)).fetchone())
        has_previous_repair = bool(connection.execute("SELECT 1 FROM schema_meta WHERE key=?", ("historical_catalog_repair_v3",)).fetchone())
        counts = {
            "events": connection.execute("SELECT COUNT(*) AS count FROM recruitment_events").fetchone()["count"],
            "jobs": connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"],
            "companies": connection.execute("SELECT COUNT(*) AS count FROM companies").fetchone()["count"],
        }
    backup_path = _create_safety_backup() if any(counts.values()) else None
    if has_previous_catalog_repair:
        jobs_created = split_existing_job_lists()
        shared_details_updated = backfill_recruitment_shared_details()
        major_requirements_updated = backfill_major_requirements()
        jobs_requeued = requeue_missing_job_sources()
        with connect() as connection:
            connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)", (marker, utc_now()))
        return {
            "status": "repaired", "backup_path": backup_path, "timeline": {}, "events_reassigned": 0,
            "jobs_created": jobs_created, "jobs_superseded": 0, "tags_updated": 0,
            "major_requirements_updated": major_requirements_updated, "major_jobs_migrated": 0,
            "job_majors_cleared": 0, "shared_details_updated": shared_details_updated, "jobs_requeued": jobs_requeued,
        }
    if has_major_job_migration:
        jobs_created = split_existing_job_lists()
        shared_details_updated = backfill_recruitment_shared_details()
        major_requirements_updated = backfill_major_requirements()
        jobs_requeued = requeue_missing_job_sources()
        with connect() as connection:
            connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)", (marker, utc_now()))
        return {
            "status": "repaired",
            "backup_path": backup_path,
            "timeline": {},
            "events_reassigned": 0,
            "jobs_created": jobs_created,
            "jobs_superseded": 0,
            "tags_updated": 0,
            "major_requirements_updated": major_requirements_updated,
            "major_jobs_migrated": 0,
            "job_majors_cleared": 0,
            "shared_details_updated": shared_details_updated,
            "jobs_requeued": jobs_requeued,
        }
    if has_previous_repair:
        jobs_created = split_existing_job_lists()
        shared_details_updated = backfill_recruitment_shared_details()
        major_migration = migrate_major_jobs()
        major_requirements_updated = backfill_major_requirements()
        jobs_requeued = requeue_missing_job_sources()
        with connect() as connection:
            connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)", (marker, utc_now()))
        return {
            "status": "repaired",
            "backup_path": backup_path,
            "timeline": {},
            "events_reassigned": 0,
            "jobs_created": jobs_created,
            "jobs_superseded": 0,
            "tags_updated": 0,
            "major_requirements_updated": major_requirements_updated + major_migration["major_requirements_updated"],
            "major_jobs_migrated": major_migration["jobs_migrated"],
            "job_majors_cleared": major_migration["job_majors_cleared"],
            "shared_details_updated": shared_details_updated,
            "jobs_requeued": jobs_requeued,
        }
    events_reassigned = repair_event_company_assignments()
    jobs_created = split_existing_job_lists()
    shared_details_updated = backfill_recruitment_shared_details()
    major_migration = migrate_major_jobs()
    major_requirements_updated = backfill_major_requirements()
    timeline = repair_timeline_events()
    jobs_superseded = supersede_duplicate_jobs()
    tags_updated = backfill_company_tags()
    jobs_requeued = requeue_missing_job_sources()
    with connect() as connection:
        connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)", (marker, utc_now()))
    return {"status": "repaired", "backup_path": backup_path, "timeline": timeline, "events_reassigned": events_reassigned, "jobs_created": jobs_created, "jobs_superseded": jobs_superseded, "tags_updated": tags_updated, "major_requirements_updated": major_requirements_updated + major_migration["major_requirements_updated"], "major_jobs_migrated": major_migration["jobs_migrated"], "job_majors_cleared": major_migration["job_majors_cleared"], "shared_details_updated": shared_details_updated, "jobs_requeued": jobs_requeued}
