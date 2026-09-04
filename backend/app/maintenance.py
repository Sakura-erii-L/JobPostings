from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from .config import config
from .db import connect, utc_now


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
