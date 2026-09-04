from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import config
from .db import connect
from .tracememo_cache import cache_stats, clear_cache


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _file_time(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return ""


def list_local_database_backups() -> list[dict[str, Any]]:
    backup_dir = config.data_dir / "backups"
    if not backup_dir.exists():
        return []
    current_path = config.db_path.resolve()
    result: list[dict[str, Any]] = []
    for path in backup_dir.iterdir():
        if path.suffix.lower() != ".db" or not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == current_path or resolved.parent != backup_dir.resolve():
            continue
        result.append({
            "name": path.name,
            "size": _file_size(path),
            "created_at": _file_time(path),
        })
    return sorted(result, key=lambda value: value["created_at"], reverse=True)


def delete_local_database_backup(filename: str) -> dict[str, Any]:
    if not filename or Path(filename).name != filename or Path(filename).suffix.lower() != ".db":
        raise ValueError("Invalid local backup filename")
    backup_dir = (config.data_dir / "backups").resolve()
    target = (backup_dir / filename).resolve()
    if target.parent != backup_dir or target == config.db_path.resolve():
        raise ValueError("Local backup path is not allowed")
    if not target.is_file():
        raise FileNotFoundError("Local backup not found")
    size = _file_size(target)
    target.unlink()
    return {"name": filename, "size": size}


def _database_stats() -> dict[str, Any]:
    paths = [config.db_path, Path(f"{config.db_path}-wal"), Path(f"{config.db_path}-shm")]
    return {
        "path": str(config.db_path),
        "size": sum(_file_size(path) for path in paths),
    }


def _chat_stats() -> dict[str, int]:
    with connect() as connection:
        raw_messages = connection.execute("SELECT COUNT(*) AS count FROM raw_messages").fetchone()["count"]
        artifacts = connection.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()["count"]
        artifact_bytes = connection.execute("SELECT COALESCE(SUM(byte_size),0) AS bytes FROM artifacts").fetchone()["bytes"]
    return {
        "messages": int(raw_messages),
        "artifacts": int(artifacts),
        "artifact_bytes": int(artifact_bytes),
    }


def storage_snapshot() -> dict[str, Any]:
    return {
        "database": _database_stats(),
        "backups": list_local_database_backups(),
        "tracememo_cache": cache_stats(),
        "chat_records": _chat_stats(),
    }


def clear_chat_records() -> dict[str, int | str]:
    from .codex_agent import cancel_codex_job
    import time

    with connect() as connection:
        raw_messages = connection.execute("SELECT COUNT(*) AS count FROM raw_messages").fetchone()["count"]
        artifacts = connection.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()["count"]
        jobs = connection.execute("SELECT COUNT(*) AS count FROM processing_jobs WHERE raw_message_id IS NOT NULL").fetchone()["count"]
        raw_ids = {row["id"] for row in connection.execute("SELECT id FROM raw_messages").fetchall()}
        processing_job_ids = {row["id"] for row in connection.execute("SELECT id FROM processing_jobs WHERE raw_message_id IS NOT NULL").fetchall()}
        review_ids: list[str] = []
        for row in connection.execute("SELECT id,entity_id,payload_json FROM review_items").fetchall():
            linked = row["entity_id"] in raw_ids or row["entity_id"] in processing_job_ids
            if not linked:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, dict):
                    snapshots = payload.get("original_messages") or ([payload.get("original_message")] if payload.get("original_message") else [])
                    linked = any(isinstance(item, dict) and item.get("id") in raw_ids for item in snapshots)
                    task = payload.get("processing_job")
                    linked = linked or (isinstance(task, dict) and task.get("id") in processing_job_ids)
            if linked:
                review_ids.append(row["id"])
        running_ids = [row["id"] for row in connection.execute("SELECT id FROM processing_jobs WHERE status='running'").fetchall()]
        previous_queue_state = connection.execute("SELECT state FROM queue_control WHERE id=1").fetchone()["state"]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute("UPDATE queue_control SET state='paused',updated_at=? WHERE id=1", (now,))
        connection.execute(
            """UPDATE processing_jobs SET status='canceled',stage='canceled',cancel_requested=1,
               lease_until=NULL,finished_at=?,updated_at=?
               WHERE status IN ('pending','running','needs_review','paused_quota','failed')""",
            (now, now),
        )
    for job_id in running_ids:
        cancel_codex_job(job_id)
    time.sleep(0.2)
    with connect() as connection:
        if review_ids:
            placeholders = ",".join("?" for _ in review_ids)
            connection.execute(f"DELETE FROM review_items WHERE id IN ({placeholders})", tuple(review_ids))
        connection.execute("DELETE FROM raw_messages")
    return {
        "messages": int(raw_messages),
        "artifacts": int(artifacts),
        "jobs": int(jobs),
        "review_items": len(review_ids),
        "queue_state": str(previous_queue_state),
    }


__all__ = [
    "clear_chat_records",
    "clear_cache",
    "delete_local_database_backup",
    "list_local_database_backups",
    "storage_snapshot",
]
