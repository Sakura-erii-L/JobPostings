from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from .config import config
from .db import connect, utc_now
from .parsers import parse_message_time, recover_original_source_url


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
    return result
