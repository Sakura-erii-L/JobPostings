from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from .db import connect, one, utc_now
from .parsers import parse_message_time


def _message_hash(connector_id: str, source_group_id: str, message: dict[str, Any]) -> str:
    payload = json.dumps(
        {"connector_id": connector_id, "source_group_id": source_group_id, "message": message},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def has_cached_group(connector_id: str, source_group_id: str) -> bool:
    row = one(
        "SELECT 1 FROM tracememo_cache_state WHERE connector_id=? AND source_group_id=?",
        (connector_id, source_group_id),
    )
    return row is not None


def load_cached_messages(connector_id: str, source_group_id: str) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """SELECT message_json FROM tracememo_message_cache
               WHERE connector_id=? AND source_group_id=?
               ORDER BY CASE WHEN source_time IS NULL THEN 1 ELSE 0 END, source_time, id""",
            (connector_id, source_group_id),
        ).fetchall()
    messages: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = json.loads(row["message_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            messages.append(value)
    return messages


def store_messages(
    connector_id: str,
    source_group_id: str,
    messages: list[dict[str, Any]],
    start_at: datetime,
    end_at: datetime,
) -> dict[str, int]:
    fetched_at = utc_now()
    stored = 0
    updated = 0
    ignored = 0
    with connect() as connection:
        for message in messages:
            if not isinstance(message, dict):
                ignored += 1
                continue
            message_json = json.dumps(message, ensure_ascii=False, default=str)
            content_hash = _message_hash(connector_id, source_group_id, message)
            external_id = str(message.get("id") or message.get("messageId") or "") or None
            source_time = parse_message_time(message)
            existing = None
            if external_id:
                existing = connection.execute(
                    "SELECT id FROM tracememo_message_cache WHERE connector_id=? AND source_group_id=? AND external_message_id=?",
                    (connector_id, source_group_id, external_id),
                ).fetchone()
            if not existing:
                existing = connection.execute(
                    "SELECT id FROM tracememo_message_cache WHERE connector_id=? AND source_group_id=? AND content_hash=?",
                    (connector_id, source_group_id, content_hash),
                ).fetchone()
            if existing:
                connection.execute(
                    """UPDATE tracememo_message_cache
                       SET external_message_id=?,source_time=?,message_json=?,last_fetched_at=?,updated_at=?
                       WHERE id=?""",
                    (external_id, source_time, message_json, fetched_at, fetched_at, existing["id"]),
                )
                updated += 1
            else:
                connection.execute(
                    """INSERT INTO tracememo_message_cache(
                       id,connector_id,source_group_id,external_message_id,content_hash,source_time,
                       message_json,first_fetched_at,last_fetched_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid4()),
                        connector_id,
                        source_group_id,
                        external_id,
                        content_hash,
                        source_time,
                        message_json,
                        fetched_at,
                        fetched_at,
                        fetched_at,
                    ),
                )
                stored += 1
        message_count = connection.execute(
            "SELECT COUNT(*) AS count FROM tracememo_message_cache WHERE connector_id=? AND source_group_id=?",
            (connector_id, source_group_id),
        ).fetchone()["count"]
        state = connection.execute(
            "SELECT first_fetched_at FROM tracememo_cache_state WHERE connector_id=? AND source_group_id=?",
            (connector_id, source_group_id),
        ).fetchone()
        if state:
            connection.execute(
                """UPDATE tracememo_cache_state
                   SET last_fetched_at=?,last_start_at=?,last_end_at=?,message_count=?,updated_at=?
                   WHERE connector_id=? AND source_group_id=?""",
                (
                    fetched_at,
                    start_at.isoformat(),
                    end_at.isoformat(),
                    message_count,
                    fetched_at,
                    connector_id,
                    source_group_id,
                ),
            )
        else:
            connection.execute(
                """INSERT INTO tracememo_cache_state(
                   connector_id,source_group_id,first_fetched_at,last_fetched_at,last_start_at,
                   last_end_at,message_count,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    connector_id,
                    source_group_id,
                    fetched_at,
                    fetched_at,
                    start_at.isoformat(),
                    end_at.isoformat(),
                    message_count,
                    fetched_at,
                ),
            )
    return {"stored": stored, "updated": updated, "ignored": ignored, "message_count": int(message_count)}


def cache_stats() -> dict[str, int]:
    with connect() as connection:
        row = connection.execute(
            """SELECT COUNT(*) AS messages, COALESCE(SUM(LENGTH(message_json)),0) AS bytes,
                      COUNT(DISTINCT source_group_id) AS groups
               FROM tracememo_message_cache"""
        ).fetchone()
        states = connection.execute("SELECT COUNT(*) AS count FROM tracememo_cache_state").fetchone()["count"]
    return {
        "messages": int(row["messages"]),
        "bytes": int(row["bytes"]),
        "groups": max(int(row["groups"]), int(states)),
    }


def clear_cache() -> dict[str, int]:
    with connect() as connection:
        messages = connection.execute("SELECT COUNT(*) AS count FROM tracememo_message_cache").fetchone()["count"]
        groups = connection.execute("SELECT COUNT(*) AS count FROM tracememo_cache_state").fetchone()["count"]
        connection.execute("DELETE FROM tracememo_message_cache")
        connection.execute("DELETE FROM tracememo_cache_state")
        connection.execute("DELETE FROM sync_cursors")
    return {"messages": int(messages), "groups": int(groups)}
