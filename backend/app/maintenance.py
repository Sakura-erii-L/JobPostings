from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from .config import config
from .catalog import deduplicate_company_jobs, normalize_company_tags
from .db import connect, utc_now
from .parsers import extract_event_datetime_candidates, normalize_event_datetime, parse_message_time, recover_original_source_url


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

        evidence_rows = connection.execute("SELECT id,raw_message_id,source_url FROM evidences").fetchall()
        for row in evidence_rows:
            result["evidences_checked"] += 1
            original = raw_sources.get(row["raw_message_id"]) or recover_original_source_url(row["source_url"])
            if original and original != row["source_url"]:
                connection.execute("UPDATE evidences SET source_url=? WHERE id=?", (original, row["id"]))
                result["evidences_updated"] += 1

        claim_rows = connection.execute("SELECT id,source_url FROM company_claims").fetchall()
        for row in claim_rows:
            result["company_claims_checked"] += 1
            original = recover_original_source_url(row["source_url"])
            if original and original != row["source_url"]:
                connection.execute("UPDATE company_claims SET source_url=? WHERE id=?", (original, row["id"]))
                result["company_claims_updated"] += 1
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
                """SELECT COALESCE(r.text_content,'') AS raw_text,COALESCE(e.excerpt,'') AS excerpt
                   FROM recruitment_event_evidences ee JOIN evidences e ON e.id=ee.evidence_id
                   LEFT JOIN raw_messages r ON r.id=e.raw_message_id WHERE ee.event_id=?""",
                (event["id"],),
            ).fetchall()
            source_text = "\n".join(value for row in evidence_rows for value in (row["raw_text"], row["excerpt"]) if value)
            if version and version["payload_json"]:
                source_text = f"{source_text}\n{version['payload_json']}"
            candidates = extract_event_datetime_candidates(source_text, event["timezone"] or "Asia/Shanghai", reference_at)
            current_start = normalize_event_datetime(event["start_at"], event["timezone"] or "Asia/Shanghai", reference_at)
            current_end = normalize_event_datetime(event["end_at"], event["timezone"] or "Asia/Shanghai", reference_at)
            recovered = False
            if event["start_at"] and not current_start:
                current_start = _nearest_candidate(candidates, reference_at)
                recovered = bool(current_start)
            if event["end_at"] and not current_end:
                current_end = _nearest_candidate(candidates, reference_at, current_start)
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


def repair_existing_catalog() -> dict[str, Any]:
    """Run the one-time historical repair behind a recoverable database backup."""
    marker = "historical_catalog_repair_v2"
    with connect() as connection:
        if connection.execute("SELECT 1 FROM schema_meta WHERE key=?", (marker,)).fetchone():
            return {"status": "already_repaired", "backup_path": None, "timeline": {}, "jobs_removed": 0, "tags_updated": 0}
        counts = {
            "events": connection.execute("SELECT COUNT(*) AS count FROM recruitment_events").fetchone()["count"],
            "jobs": connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"],
            "companies": connection.execute("SELECT COUNT(*) AS count FROM companies").fetchone()["count"],
        }
    backup_path = _create_safety_backup() if any(counts.values()) else None
    timeline = repair_timeline_events()
    jobs_removed = deduplicate_all_jobs()
    tags_updated = backfill_company_tags()
    with connect() as connection:
        connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)", (marker, utc_now()))
    return {"status": "repaired", "backup_path": backup_path, "timeline": timeline, "jobs_removed": jobs_removed, "tags_updated": tags_updated}
