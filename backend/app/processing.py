from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .catalog import apply_model_item
from .db import connect, one, utc_now
from .model_provider import classify_messages, get_setting
from .parsers import extract_file, fetch_public_url, parse_message_payload, sha256_bytes


def message_hash(connector_id: str | None, group_id: str | None, message: dict[str, Any]) -> str:
    text, metadata = parse_message_payload(message)
    external_id = str(message.get("id") or message.get("messageId") or "")
    raw = "|".join([connector_id or "", group_id or "", external_id, text, json.dumps(metadata, ensure_ascii=False, sort_keys=True)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_artifact(raw_message_id: str, filename: str, data: bytes, mime_type: str | None = None, parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    digest = sha256_bytes(data)
    from .config import config

    target = config.blob_dir / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    parsed = parsed or extract_file(filename, data)
    with connect() as connection:
        artifact_id = str(uuid4())
        try:
            connection.execute(
                "INSERT INTO artifacts(id,raw_message_id,sha256,path,filename,mime_type,byte_size,ocr_text,qr_values_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, raw_message_id, digest, str(target), filename, mime_type, len(data), parsed.get("text", ""), json.dumps(parsed.get("qr_values", []), ensure_ascii=False), utc_now()),
            )
        except Exception:
            row = connection.execute("SELECT * FROM artifacts WHERE raw_message_id=? AND sha256=?", (raw_message_id, digest)).fetchone()
            artifact_id = row["id"] if row else artifact_id
    return {"id": artifact_id, "sha256": digest, "path": str(target), **parsed}


def attach_artifact(raw_message_id: str, filename: str, data: bytes, mime_type: str | None = None) -> dict[str, Any]:
    artifact = save_artifact(raw_message_id, filename, data, mime_type)
    with connect() as connection:
        raw = connection.execute("SELECT text_content,metadata_json FROM raw_messages WHERE id=?", (raw_message_id,)).fetchone()
        old_text = raw["text_content"] if raw else ""
        extracted = artifact.get("text", "")
        merged = "\n".join(value for value in (old_text, extracted) if value).strip()
        metadata = {}
        if raw:
            try:
                metadata = json.loads(raw["metadata_json"] or "{}")
            except json.JSONDecodeError:
                pass
        metadata["artifact_id"] = artifact["id"]
        connection.execute(
            "UPDATE raw_messages SET text_content=?,metadata_json=? WHERE id=?",
            (merged, json.dumps(metadata, ensure_ascii=False), raw_message_id),
        )
    return artifact


def ingest_message(message: dict[str, Any], connector_id: str | None, group_id: str | None) -> str | None:
    text, metadata = parse_message_payload(message)
    digest = message_hash(connector_id, group_id, message)
    message_type = str(message.get("type") or message.get("msgType") or "text").lower()
    external_id = str(message.get("id") or message.get("messageId") or "") or None
    sent_at = str(message.get("sent_at") or message.get("time") or message.get("timestamp") or utc_now())
    raw_id = str(uuid4())
    try:
        retention_days = max(1, int(get_setting("ordinary_retention_days", 30)))
    except (TypeError, ValueError):
        retention_days = 30
    retention = (datetime.now(timezone.utc) + timedelta(days=retention_days)).isoformat(timespec="seconds")
    with connect() as connection:
        existing = connection.execute("SELECT id FROM raw_messages WHERE content_hash=?", (digest,)).fetchone()
        if existing:
            return existing["id"]
        try:
            connection.execute(
                "INSERT INTO raw_messages(id,connector_id,source_group_id,external_message_id,sender,sent_at,message_type,text_content,metadata_json,content_hash,retention_until,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (raw_id, connector_id, group_id, external_id, str(message.get("sender") or message.get("talker") or ""), sent_at, message_type, text, json.dumps(metadata, ensure_ascii=False), digest, retention, utc_now()),
            )
            connection.execute(
                "INSERT INTO processing_jobs(id,kind,raw_message_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (str(uuid4()), "classify", raw_id, "pending", utc_now(), utc_now()),
            )
        except Exception:
            existing = connection.execute("SELECT id FROM raw_messages WHERE content_hash=?", (digest,)).fetchone()
            return existing["id"] if existing else None
    return raw_id


def process_one_batch(limit: int = 100) -> dict[str, Any] | None:
    with connect() as connection:
        rows = connection.execute(
            "SELECT p.id AS processing_id, r.* FROM processing_jobs p JOIN raw_messages r ON r.id=p.raw_message_id WHERE p.kind='classify' AND (p.status='pending' OR (p.status='running' AND p.lease_until<?)) ORDER BY p.created_at LIMIT ?",
            (utc_now(), limit),
        ).fetchall()
        if not rows:
            return None
        ids = [row["processing_id"] for row in rows]
        now = utc_now()
        for job_id in ids:
            connection.execute("UPDATE processing_jobs SET status='running', lease_until=?, attempts=attempts+1, updated_at=? WHERE id=?", ((datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), now, job_id))
    messages = []
    for row in rows:
        metadata = {}
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            pass
        messages.append({
            "id": row["id"],
            "sent_at": row["sent_at"],
            "message_type": row["message_type"],
            "text": row["text_content"] or "",
            "metadata": metadata,
        })
    try:
        result = classify_messages(messages)
        by_id = {item.get("message_id"): item for item in result.payload.get("items", [])}
        for row in rows:
            item = by_id.get(row["id"], {"message_id": row["id"], "is_recruitment": False})
            with connect() as connection:
                connection.execute("UPDATE raw_messages SET is_recruitment=? WHERE id=?", (1 if item.get("is_recruitment") else 0, row["id"]))
            job_ids = apply_model_item(item, row["id"], row["sent_at"])
            if job_ids:
                from .search import enrich_company

                company_row = one("SELECT company_id FROM jobs WHERE id=?", (job_ids[0],))
                if company_row:
                    connection_id = str(uuid4())
                    with connect() as queue_connection:
                        queue_connection.execute(
                            "INSERT INTO processing_jobs(id,kind,raw_message_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                            (connection_id, "enrich_company", row["id"], "pending", utc_now(), utc_now()),
                        )
            with connect() as connection:
                connection.execute("UPDATE processing_jobs SET status='succeeded', updated_at=?, error=NULL WHERE id=?", (utc_now(), row["processing_id"]))
                connection.execute(
                    "UPDATE review_items SET status='resolved',resolved_by='system',resolved_at=? WHERE kind='processing_failed' AND entity_id=? AND status='open'",
                    (utc_now(), row["id"]),
                )
        return {"processed": len(rows), "input_tokens": result.input_tokens, "output_tokens": result.output_tokens}
    except Exception as exc:
        error = str(exc)
        with connect() as connection:
            for row in rows:
                status = "paused_quota" if "budget" in error.lower() else "needs_review"
                now = utc_now()
                connection.execute("UPDATE processing_jobs SET status=?, error=?, updated_at=? WHERE id=?", (status, error, now, row["processing_id"]))
                existing_review = connection.execute(
                    "SELECT id FROM review_items WHERE kind='processing_failed' AND entity_id=? AND status='open' LIMIT 1",
                    (row["id"],),
                ).fetchone()
                payload = json.dumps({"error": error}, ensure_ascii=False)
                if existing_review:
                    connection.execute(
                        "UPDATE review_items SET payload_json=?,created_at=? WHERE id=?",
                        (payload, now, existing_review["id"]),
                    )
                else:
                    connection.execute("INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)", (str(uuid4()), "processing_failed", "raw_message", row["id"], payload, now))
        return {"processed": 0, "error": error}


def process_one_enrichment() -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT p.id AS processing_id, p.raw_message_id FROM processing_jobs p WHERE p.kind='enrich_company' AND (p.status='pending' OR (p.status='running' AND p.lease_until<?)) ORDER BY p.created_at LIMIT 1",
            (utc_now(),),
        ).fetchone()
        if not row:
            return None
        connection.execute("UPDATE processing_jobs SET status='running',lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?", ((datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), utc_now(), row["processing_id"]))
    try:
        company = one(
            "SELECT j.company_id FROM jobs j JOIN evidences e ON e.job_id=j.id WHERE e.raw_message_id=? ORDER BY j.updated_at DESC LIMIT 1",
            (row["raw_message_id"],),
        )
        if not company:
            raise RuntimeError("Company for enrichment task not found")
        from .search import enrich_company

        result = enrich_company(company["company_id"])
        with connect() as connection:
            connection.execute("UPDATE processing_jobs SET status='succeeded',error=NULL,updated_at=? WHERE id=?", (utc_now(), row["processing_id"]))
        return result
    except Exception as exc:
        with connect() as connection:
            connection.execute("UPDATE processing_jobs SET status='needs_review',error=?,updated_at=? WHERE id=?", (str(exc), utc_now(), row["processing_id"]))
        return {"status": "failed", "error": str(exc)}


def import_text(text: str, source_group_id: str | None = None, metadata: dict[str, Any] | None = None) -> str | None:
    message = {"type": "text", "text": text, **(metadata or {})}
    return ingest_message(message, "manual", source_group_id)


def import_url(url: str, source_group_id: str | None = None) -> str | None:
    parsed = fetch_public_url(url)
    return ingest_message({"type": "article", "text": parsed.get("text", ""), "url": parsed.get("url", url), "title": parsed.get("title", "")}, "manual", source_group_id)


def import_file(filename: str, data: bytes, mime_type: str | None = None, source_group_id: str | None = None) -> dict[str, Any]:
    parsed = extract_file(filename, data)
    raw_id = ingest_message(
        {"type": "file", "text": parsed.get("text", ""), "filename": filename, "mime_type": mime_type, "qr_values": parsed.get("qr_values", [])},
        "manual",
        source_group_id,
    )
    if not raw_id:
        raise RuntimeError("Unable to create import record")
    artifact = save_artifact(raw_id, filename, data, mime_type, parsed=parsed)
    with connect() as connection:
        connection.execute(
            "UPDATE raw_messages SET metadata_json=? WHERE id=?",
            (json.dumps({"filename": filename, "mime_type": mime_type, "artifact_id": artifact["id"], "qr_values": parsed.get("qr_values", [])}, ensure_ascii=False), raw_id),
        )
    return {"raw_message_id": raw_id, "artifact": artifact}
