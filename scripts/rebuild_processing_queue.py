from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4


PRESERVED_TABLES = ["raw_messages", "artifacts", "users", "system_settings", "connectors", "source_groups"]
DERIVED_TABLES = [
    "companies",
    "jobs",
    "evidences",
    "recruitment_events",
    "review_items",
    "llm_calls",
    "processing_jobs",
    "processing_logs",
]


def default_database() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "JobPostings" / "data" / "jobpostings.db"


def counts(connection: sqlite3.Connection, names: list[str]) -> dict[str, int | None]:
    existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {
        name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] if name in existing else None
        for name in names
    }


def backup_database(source_path: Path) -> Path:
    backup_dir = source_path.parent.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"pre-reprocess-{stamp}.db"
    source = sqlite3.connect(source_path, timeout=30)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
    finally:
        destination.close()
        source.close()
    return target


def rebuild(source_path: Path) -> dict[str, object]:
    backup_path = backup_database(source_path)

    import sys

    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root / "backend"))
    from app import db

    db.config.data_dir = source_path.parent.parent
    db.config.db_path = source_path
    db.init_db()

    connection = sqlite3.connect(source_path, timeout=30)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        before = counts(connection, PRESERVED_TABLES + DERIVED_TABLES)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM processing_jobs")
        connection.execute("DELETE FROM review_items")
        connection.execute("DELETE FROM companies")
        connection.execute("DELETE FROM search_index")
        connection.execute("DELETE FROM llm_calls")
        connection.execute("UPDATE raw_messages SET is_recruitment=NULL")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        raw_ids = [row[0] for row in connection.execute("SELECT id FROM raw_messages ORDER BY created_at,id")]
        connection.executemany(
            "INSERT INTO processing_jobs(id,kind,raw_message_id,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            [(str(uuid4()), "classify", raw_id, "pending", "queued", now, now) for raw_id in raw_ids],
        )
        connection.execute(
            "INSERT INTO queue_control(id,state,updated_at) VALUES(1,'paused',?) ON CONFLICT(id) DO UPDATE SET state='paused',updated_at=excluded.updated_at",
            (now,),
        )
        connection.commit()
        after = counts(connection, PRESERVED_TABLES + DERIVED_TABLES)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "database": str(source_path),
        "backup": str(backup_path),
        "backup_bytes": backup_path.stat().st_size,
        "integrity": integrity,
        "before": before,
        "after": after,
        "queue_state": "paused",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up JobPostings and rebuild its processing queue")
    parser.add_argument("--database", type=Path, default=default_database())
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    source_path = args.database
    if not str(source_path).startswith("\\\\?\\"):
        source_path = source_path.resolve()
    if not source_path.is_file():
        raise SystemExit(f"Database not found: {source_path}")
    if args.execute:
        print(json.dumps(rebuild(source_path), ensure_ascii=False, indent=2))
        return
    connection = sqlite3.connect(str(source_path))
    try:
        print(json.dumps({"database": str(source_path), "counts": counts(connection, PRESERVED_TABLES + DERIVED_TABLES)}, ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
