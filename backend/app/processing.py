from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from urllib.parse import urlparse

from .browser import fetch_public_browser
from .catalog import INDUSTRIES, apply_company_overrides, apply_model_item, company_overrides, deduplicate_company_jobs, normalize_company_tags, normalize_recruitment_payload, normalize_text_value
from .company_research import execute_company_research
from .db import connect, one, utc_now
from .model_provider import RecruitmentPayloadValidationError, classify_messages, consolidate_company_profile, get_setting, validate_recruitment_payload
from .parsers import (
    extract_file,
    fetch_public_http,
    fetch_public_url,
    is_file_message,
    is_image_message,
    is_link_message,
    is_system_message,
    detect_image_suffix,
    is_text_message,
    normalize_file_filename,
    parse_message_time,
    parse_message_payload,
    recover_original_source_url,
    sha256_bytes,
)


def message_hash(connector_id: str | None, group_id: str | None, message: dict[str, Any]) -> str:
    text, metadata = parse_message_payload(message)
    external_id = str(message.get("id") or message.get("messageId") or "")
    raw = "|".join([connector_id or "", group_id or "", external_id, text, json.dumps(metadata, ensure_ascii=False, sort_keys=True)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_artifact(raw_message_id: str, filename: str, data: bytes, mime_type: str | None = None, parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    filename = normalize_file_filename(filename, data, mime_type)
    mime_type = str(mime_type or "").split(";", 1)[0].strip() or mimetypes.guess_type(filename)[0]
    digest = sha256_bytes(data)
    from .config import config

    target = config.blob_dir / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    parsed = parsed or extract_file(filename, data, mime_type=mime_type)
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
    return {"id": artifact_id, "sha256": digest, "path": str(target), "filename": filename, "mime_type": mime_type, **parsed}


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
        artifact_ids = list(dict.fromkeys([*(metadata.get("artifact_ids") or []), artifact["id"]]))
        metadata["artifact_ids"] = artifact_ids
        metadata["qr_values"] = list(dict.fromkeys([*(metadata.get("qr_values") or []), *(artifact.get("qr_values") or [])]))
        connection.execute(
            "UPDATE raw_messages SET text_content=?,metadata_json=? WHERE id=?",
            (merged, json.dumps(metadata, ensure_ascii=False), raw_message_id),
        )
    return artifact


def _increment_ingest_stat(stats: dict[str, int] | None, key: str) -> None:
    if stats is not None:
        stats[key] = stats.get(key, 0) + 1


def _queue_classification(connection: Any, raw_id: str) -> None:
    connection.execute(
        "UPDATE raw_messages SET recognition_status='pending',recognized_at=NULL,recognition_error=NULL WHERE id=? AND recognition_status NOT IN ('running')",
        (raw_id,),
    )
    job = connection.execute(
        "SELECT id,status FROM processing_jobs WHERE raw_message_id=? AND kind='classify' ORDER BY created_at DESC LIMIT 1",
        (raw_id,),
    ).fetchone()
    if not job:
        connection.execute(
            "INSERT INTO processing_jobs(id,kind,raw_message_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (str(uuid4()), "classify", raw_id, "pending", utc_now(), utc_now()),
        )
        return
    if job["status"] != "running":
        connection.execute(
            """UPDATE processing_jobs
               SET status='pending',stage='queued',attempts=0,cancel_requested=0,lease_until=NULL,
                   next_attempt_at=NULL,processor=NULL,result_json=NULL,error=NULL,started_at=NULL,
                   finished_at=NULL,updated_at=?
               WHERE id=?""",
            (utc_now(), job["id"]),
        )


def requeue_message_for_processing(raw_message_id: str) -> None:
    """Put a repaired raw message back into the classification queue."""
    with connect() as connection:
        _queue_classification(connection, raw_message_id)


def _set_recognition_status(raw_message_id: str | None, status: str, error: str | None = None) -> None:
    if not raw_message_id:
        return
    recognized_at = utc_now() if status in {"succeeded", "filtered"} else None
    with connect() as connection:
        connection.execute(
            """UPDATE raw_messages
               SET recognition_status=?,recognized_at=?,recognition_error=?
               WHERE id=?""",
            (status, recognized_at, error, raw_message_id),
        )


def ingest_message(message: dict[str, Any], connector_id: str | None, group_id: str | None, stats: dict[str, int] | None = None) -> str | None:
    message_type = str(message.get("type") or message.get("msgType") or "text").strip().lower()
    if is_file_message(message_type, message):
        message_type = "file"
    original_text = str(message.get("text") or message.get("content") or "")
    system_message = is_system_message(message_type, original_text, message)
    text, metadata = parse_message_payload(message)
    digest = message_hash(connector_id, group_id, message)
    metadata["_original_text_content"] = original_text
    metadata["_parsed_text_content"] = text
    external_id = str(message.get("id") or message.get("messageId") or "") or None
    sent_at = parse_message_time(message)
    raw_id = str(uuid4())
    try:
        retention_days = max(1, int(get_setting("ordinary_retention_days", 30)))
    except (TypeError, ValueError):
        retention_days = 30
    retention = (datetime.now(timezone.utc) + timedelta(days=retention_days)).isoformat(timespec="seconds")
    with connect() as connection:
        existing = connection.execute("SELECT id,sent_at,recognition_status FROM raw_messages WHERE content_hash=?", (digest,)).fetchone()
        if existing:
            if sent_at and existing["sent_at"] != sent_at:
                connection.execute("UPDATE raw_messages SET sent_at=? WHERE id=?", (sent_at, existing["id"]))
            if system_message:
                connection.execute(
                    "UPDATE raw_messages SET is_recruitment=0,recognition_status='filtered',recognized_at=COALESCE(recognized_at,?),recognition_error=NULL WHERE id=?",
                    (utc_now(), existing["id"]),
                )
                _increment_ingest_stat(stats, "filtered_system")
            elif existing["recognition_status"] == "canceled":
                _queue_classification(connection, existing["id"])
                _increment_ingest_stat(stats, "updated")
            elif existing["recognition_status"] in {"succeeded", "filtered"}:
                _increment_ingest_stat(stats, "recognized_skipped")
            else:
                _increment_ingest_stat(stats, "duplicates")
            return None if system_message else existing["id"]
        existing_external = None
        if connector_id and group_id and external_id:
            existing_external = connection.execute(
                "SELECT id,text_content,message_type,metadata_json,recognition_status,is_recruitment FROM raw_messages WHERE connector_id=? AND source_group_id=? AND external_message_id=?",
                (connector_id, group_id, external_id),
            ).fetchone()
        if existing_external:
            old_metadata: dict[str, Any] = {}
            try:
                loaded_metadata = json.loads(existing_external["metadata_json"] or "{}")
                if isinstance(loaded_metadata, dict):
                    old_metadata = loaded_metadata
            except (TypeError, json.JSONDecodeError):
                pass
            merged_metadata = {**old_metadata, **metadata}
            previous_source_text = old_metadata.get("_parsed_text_content", existing_external["text_content"] or "")
            same_content = (text or "") == str(previous_source_text or "") and message_type == existing_external["message_type"] and (
                str(metadata.get("source_url") or metadata.get("url") or "")
                == str(old_metadata.get("source_url") or old_metadata.get("url") or "")
            )
            if same_content:
                connection.execute(
                    """UPDATE raw_messages
                       SET sender=?,sent_at=?,message_type=?,metadata_json=?,content_hash=?,retention_until=?
                       WHERE id=?""",
                    (
                        str(message.get("sender") or message.get("talker") or ""),
                        sent_at,
                        message_type,
                        json.dumps(merged_metadata, ensure_ascii=False),
                        digest,
                        retention,
                        existing_external["id"],
                    ),
                )
                if system_message:
                    connection.execute(
                        "UPDATE raw_messages SET is_recruitment=0,recognition_status='filtered',recognized_at=COALESCE(recognized_at,?),recognition_error=NULL WHERE id=?",
                        (utc_now(), existing_external["id"]),
                    )
                    _increment_ingest_stat(stats, "filtered_system")
                elif existing_external["recognition_status"] == "canceled":
                    _queue_classification(connection, existing_external["id"])
                    _increment_ingest_stat(stats, "updated")
                elif existing_external["recognition_status"] in {"succeeded", "filtered"}:
                    _increment_ingest_stat(stats, "recognized_skipped")
                else:
                    _increment_ingest_stat(stats, "duplicates")
                return None if system_message else existing_external["id"]
            connection.execute(
                """UPDATE raw_messages
                   SET sender=?,sent_at=?,message_type=?,text_content=?,metadata_json=?,content_hash=?,
                       is_recruitment=?,recognition_status=?,recognized_at=?,recognition_error=NULL,retention_until=?
                   WHERE id=?""",
                (
                    str(message.get("sender") or message.get("talker") or ""),
                    sent_at,
                    message_type,
                    text or existing_external["text_content"] or "",
                    json.dumps(merged_metadata, ensure_ascii=False),
                    digest,
                    0 if system_message else None,
                    "filtered" if system_message else "pending",
                    utc_now() if system_message else None,
                    retention,
                    existing_external["id"],
                ),
            )
            if system_message:
                _increment_ingest_stat(stats, "filtered_system")
            else:
                _queue_classification(connection, existing_external["id"])
                _increment_ingest_stat(stats, "updated")
            return None if system_message else existing_external["id"]
        try:
            connection.execute(
                """INSERT INTO raw_messages(
                   id,connector_id,source_group_id,external_message_id,sender,sent_at,message_type,text_content,
                   metadata_json,content_hash,is_recruitment,recognition_status,recognized_at,recognition_error,
                   retention_until,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    raw_id,
                    connector_id,
                    group_id,
                    external_id,
                    str(message.get("sender") or message.get("talker") or ""),
                    sent_at,
                    message_type,
                    text,
                    json.dumps(metadata, ensure_ascii=False),
                    digest,
                    0 if system_message else None,
                    "filtered" if system_message else "pending",
                    utc_now() if system_message else None,
                    None,
                    retention,
                    utc_now(),
                ),
            )
            if system_message:
                _increment_ingest_stat(stats, "filtered_system")
            else:
                _queue_classification(connection, raw_id)
                _increment_ingest_stat(stats, "created")
        except sqlite3.IntegrityError:
            existing = connection.execute("SELECT id FROM raw_messages WHERE content_hash=?", (digest,)).fetchone()
            if existing:
                if system_message:
                    _increment_ingest_stat(stats, "filtered_system")
                    return None
                _increment_ingest_stat(stats, "duplicates")
                return existing["id"]
            raise
    return None if system_message else raw_id


def log_processing(job_id: str, stage: str, message: str, level: str = "info", details: dict[str, Any] | None = None) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO processing_logs(id,processing_job_id,stage,level,message,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), job_id, stage, level, message, json.dumps(details or {}, ensure_ascii=False), utc_now()),
        )


def queue_is_running() -> bool:
    row = one("SELECT state FROM queue_control WHERE id=1")
    return bool(row and row["state"] == "running")


def _claim_one(*, prefer_enrichment: bool = False) -> dict[str, Any] | None:
    now = utc_now()
    priority = (
        "CASE kind WHEN 'consolidate_company' THEN 0 WHEN 'research_company' THEN 1 ELSE 2 END"
        if prefer_enrichment
        else "CASE kind WHEN 'classify' THEN 0 ELSE 1 END"
    )
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            f"""SELECT * FROM processing_jobs
               WHERE status='pending' AND cancel_requested=0
                 AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                 AND (
                   kind NOT IN ('consolidate_company', 'research_company')
                   OR (SELECT COUNT(*) FROM processing_jobs active
                       WHERE active.kind=processing_jobs.kind AND active.status='running') < 1
                 )
               ORDER BY {priority}, created_at LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            return None
        changed = connection.execute(
            """UPDATE processing_jobs SET status='running',stage='starting',lease_until=?,attempts=attempts+1,
               started_at=COALESCE(started_at,?),updated_at=? WHERE id=? AND status='pending'""",
            ((datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="seconds"), now, now, row["id"]),
        ).rowcount
        if not changed:
            return None
        if row["kind"] == "classify" and row["raw_message_id"]:
            connection.execute(
                "UPDATE raw_messages SET recognition_status='running',recognized_at=NULL,recognition_error=NULL WHERE id=?",
                (row["raw_message_id"],),
            )
        return dict(connection.execute("SELECT * FROM processing_jobs WHERE id=?", (row["id"],)).fetchone())


def _still_active(job_id: str) -> bool:
    row = one("SELECT status,cancel_requested FROM processing_jobs WHERE id=?", (job_id,))
    return bool(row and row["status"] == "running" and not row["cancel_requested"])


def _stage(job_id: str, stage: str, message: str, processor: str | None = None) -> None:
    with connect() as connection:
        connection.execute(
            "UPDATE processing_jobs SET stage=?,processor=COALESCE(?,processor),updated_at=? WHERE id=?",
            (stage, processor, utc_now(), job_id),
        )
    log_processing(job_id, stage, message)


def _image_artifact_rows(raw_message_id: str) -> list[dict[str, Any]]:
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
    rows = [
        row for row in _artifact_rows(raw_message_id)
        if str(row["mime_type"] or "").startswith("image/") or Path(str(row["filename"] or "")).suffix.lower() in image_suffixes
    ]

    def page_order(row: dict[str, Any]) -> tuple[int, int, str, str]:
        match = re.search(r"(?:linked-image|input)-(\d+)", str(row.get("filename") or ""), re.IGNORECASE)
        if match:
            return (0, int(match.group(1)), str(row.get("created_at") or ""), str(row.get("id") or ""))
        return (1, 0, str(row.get("created_at") or ""), str(row.get("id") or ""))

    return sorted(rows, key=page_order)


def _codex_extract(
    job: dict[str, Any],
    raw: dict[str, Any],
    metadata: dict[str, Any],
    reason: str,
    *,
    primary_ocr: bool = False,
) -> str:
    from .codex_agent import run_codex_json

    artifacts = _image_artifact_rows(raw["id"])
    images = [row["path"] for row in artifacts if row["path"]]
    artifact_ids = [row["id"] for row in artifacts if row.get("path")]
    artifact = artifacts[-1] if artifacts else None
    payload = {
        "reason": reason,
        "source_type": raw["message_type"],
        "web_access_status": metadata.get("web_access_status"),
        "url": metadata.get("url"),
        "filename": artifact["filename"] if artifact else metadata.get("filename"),
        "image_count": len(images),
        "image_order": [row.get("filename") for row in artifacts if row.get("path")],
        "existing_text": raw.get("text_content") or "",
    }
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "source_url": {"type": "string"}, "notes": {"type": "string"}},
        "required": ["text", "source_url", "notes"],
        "additionalProperties": False,
    }
    stage = "codex_ocr" if primary_ocr else "codex_fallback"
    message = f"发现图片来源，优先使用 Codex 进行图片 OCR：{reason}" if primary_ocr else f"本地提取不足，使用 Codex 兜底：{reason}"
    _stage(job["id"], stage, message, "local_codex:gpt-5.6-luna")
    result = run_codex_json("source_text_extraction", payload, schema, job_id=job["id"], image_paths=images, enable_web=bool(metadata.get("url")))
    text = str(result.get("text") or "").strip()
    if not text:
        raise RuntimeError("Codex did not extract readable text")
    metadata["codex_ocr"] = bool(primary_ocr or images)
    metadata["ocr_engine"] = "codex" if images else metadata.get("ocr_engine", "")
    if images:
        metadata["codex_ocr_complete"] = True
        metadata["codex_ocr_artifact_ids"] = artifact_ids
        metadata["codex_ocr_image_count"] = len(images)
    if not primary_ocr:
        metadata["codex_fallback"] = True
    metadata["codex_notes"] = result.get("notes")
    with connect() as connection:
        connection.execute(
            "UPDATE raw_messages SET text_content=?,metadata_json=? WHERE id=?",
            (text, json.dumps(metadata, ensure_ascii=False), raw["id"]),
        )
    return text


def _artifact_rows(raw_message_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in _all_rows("SELECT * FROM artifacts WHERE raw_message_id=? ORDER BY created_at", (raw_message_id,))]


def _local_ocr_extract(raw: dict[str, Any]) -> tuple[str, list[str]]:
    texts: list[str] = []
    errors: list[str] = []
    for artifact in _image_artifact_rows(raw["id"]):
        existing_text = str(artifact.get("ocr_text") or "").strip()
        if existing_text:
            texts.append(existing_text)
            continue
        path = str(artifact.get("path") or "")
        if not path:
            continue
        try:
            filename = str(artifact.get("filename") or "image.png")
            if Path(filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
                filename = f"{Path(filename).stem or 'image'}.png"
            parsed = extract_file(
                filename,
                Path(path).read_bytes(),
                local_ocr=True,
            )
            extracted = str(parsed.get("text") or "").strip()
            if extracted:
                texts.append(extracted)
                with connect() as connection:
                    connection.execute("UPDATE artifacts SET ocr_text=? WHERE id=?", (extracted, artifact["id"]))
            ocr_error = str((parsed.get("metadata") or {}).get("ocr_error") or "").strip()
            if ocr_error:
                errors.append(ocr_error)
        except Exception as exc:
            errors.append(str(exc))
    return _merge_texts(*texts), errors


def _is_wechat_public_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "mp.weixin.qq.com" or host.endswith(".weixin.qq.com")


def _should_render_in_browser(url: str, parsed: dict[str, Any]) -> bool:
    if not _is_wechat_public_url(url):
        return False
    text = str(parsed.get("text") or "").strip()
    return bool(parsed.get("access_challenge")) or len(text) < 300


def _merge_browser_result(parsed: dict[str, Any], browser_result: dict[str, Any]) -> dict[str, Any]:
    merged = dict(parsed)
    if not browser_result.get("browser_rendered"):
        return merged
    for key in (
        "url",
        "title",
        "text",
        "content_type",
        "links",
        "images",
        "image_data",
        "screenshot_data",
        "browser_rendered",
        "browser_image_count",
        "browser_loaded_image_count",
        "browser_downloaded_image_count",
        "browser_article_text_chars",
    ):
        if key in browser_result:
            merged[key] = browser_result[key]
    browser_text = str(browser_result.get("text") or "").strip()
    browser_has_content = bool(browser_text) or bool(browser_result.get("images")) or bool(browser_result.get("image_data"))
    if parsed.get("access_challenge") and not browser_has_content:
        return merged
    if browser_result.get("access_challenge"):
        merged["access_challenge"] = True
        merged["access_error"] = browser_result.get("access_error") or "浏览器页面要求环境验证"
    else:
        merged.pop("access_challenge", None)
        merged.pop("access_error", None)
    return merged


def _all_rows(sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    with connect() as connection:
        return connection.execute(sql, params).fetchall()


def _web_image_filename(url: str, content_type: str, index: int, detected_suffix: str | None = None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        suffix = mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower()) or detected_suffix or ".bin"
    return f"linked-image-{index}{suffix}"


def _merge_texts(*values: Any) -> str:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return "\n".join(result)


def _raw_snapshot(raw_message_id: str | None) -> dict[str, Any] | None:
    if not raw_message_id:
        return None
    row = one("SELECT * FROM raw_messages WHERE id=?", (raw_message_id,))
    if not row:
        return None
    value = dict(row)
    try:
        metadata = json.loads(value.get("metadata_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {"_invalid_metadata_json": value.get("metadata_json") or ""}
    original_text = metadata.get("_original_text_content")
    return {
        "id": value.get("id"),
        "connector_id": value.get("connector_id"),
        "source_group_id": value.get("source_group_id"),
        "external_message_id": value.get("external_message_id"),
        "sender": value.get("sender"),
        "sent_at": value.get("sent_at"),
        "message_type": value.get("message_type"),
        "recognition_status": value.get("recognition_status"),
        "recognized_at": value.get("recognized_at"),
        "recognition_error": value.get("recognition_error"),
        "original_text_content": original_text if original_text is not None else value.get("text_content") or "",
        "current_text_content": value.get("text_content") or "",
        "metadata": metadata,
        "content_hash": value.get("content_hash"),
        "created_at": value.get("created_at"),
    }


def _job_snapshot(job_id: str) -> dict[str, Any] | None:
    row = one("SELECT * FROM processing_jobs WHERE id=?", (job_id,))
    return dict(row) if row else None


def _processing_log_snapshots(job_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in _all_rows("SELECT * FROM processing_logs WHERE processing_job_id=? ORDER BY created_at", (job_id,)):
        value = dict(row)
        try:
            value["details"] = json.loads(value.pop("details_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            value["details"] = {"_invalid_details_json": value.pop("details_json", "")}
        result.append(value)
    return result


def _review_context(job_id: str, raw_message_id: str | None = None, company_id: str | None = None) -> dict[str, Any]:
    job = _job_snapshot(job_id)
    raw = _raw_snapshot(raw_message_id or (job or {}).get("raw_message_id"))
    return {
        "job": job,
        "original_message": raw,
        "processing_logs": _processing_log_snapshots(job_id),
        "company_id": company_id or (job or {}).get("company_id"),
    }


def enrich_review_payload(payload: dict[str, Any], entity_type: str | None, entity_id: str | None) -> dict[str, Any]:
    """Add durable task/source context to both new and legacy review records."""
    result = dict(payload)
    if entity_type == "processing_job":
        job_id = str(result.get("job_id") or entity_id or "")
        if job_id and not _job_snapshot(job_id) and entity_id:
            linked_job = one("SELECT id FROM processing_jobs WHERE raw_message_id=? ORDER BY created_at DESC LIMIT 1", (entity_id,))
            job_id = linked_job["id"] if linked_job else job_id
        if job_id:
            context = _review_context(job_id)
            if context["job"]:
                result["job"] = context["job"]
                if context["job"].get("error") and not result.get("error"):
                    result["error"] = {"type": "processing_error", "message": context["job"]["error"]}
            if context["original_message"]:
                result["original_message"] = context["original_message"]
            result["processing_logs"] = context["processing_logs"]
        return result
    if entity_type == "company" and entity_id and not result.get("original_messages"):
        raw_ids = [row["raw_message_id"] for row in _all_rows(
            "SELECT DISTINCT raw_message_id FROM evidences WHERE company_id=? AND raw_message_id IS NOT NULL ORDER BY observed_at",
            (entity_id,),
        )]
        result["original_messages"] = [snapshot for raw_id in raw_ids if (snapshot := _raw_snapshot(raw_id))]
    return result


def _persist_raw_extraction(raw_message_id: str, text: str, metadata: dict[str, Any]) -> None:
    with connect() as connection:
        connection.execute(
            "UPDATE raw_messages SET text_content=?,metadata_json=? WHERE id=?",
            (text, json.dumps(metadata, ensure_ascii=False), raw_message_id),
        )


def _extract_link_images(job: dict[str, Any], raw: dict[str, Any], parsed: dict[str, Any], metadata: dict[str, Any]) -> tuple[list[str], str]:
    qr_values: list[str] = []
    ocr_texts: list[str] = []
    artifact_ids: list[str] = []
    image_urls = [str(value) for value in parsed.get("images") or [] if value]
    browser_image_data = [
        value
        for value in parsed.get("image_data") or []
        if isinstance(value, dict) and isinstance(value.get("data"), bytes) and value.get("data")
    ]
    processed_image_urls: set[str] = set()
    downloaded = 0
    direct_data = parsed.get("data")
    if isinstance(direct_data, bytes) and direct_data:
        try:
            artifact = attach_artifact(
                raw["id"],
                str(parsed.get("filename") or "web-image.bin"),
                direct_data,
                str(parsed.get("content_type") or "") or None,
            )
            artifact_ids.append(artifact["id"])
            qr_values.extend(artifact.get("qr_values") or [])
            ocr_texts.append(str(artifact.get("text") or ""))
            downloaded += 1
        except Exception as exc:
            log_processing(job["id"], "extracting", "网页本身为图片，但图片提取失败", "warning", {"error": str(exc)})
    for index, image in enumerate(browser_image_data[:24], start=1):
        image_url = str(image.get("url") or "")
        try:
            artifact = attach_artifact(
                raw["id"],
                _web_image_filename(image_url, str(image.get("content_type") or ""), index, detect_image_suffix(image["data"])),
                image["data"],
                str(image.get("content_type") or "") or None,
            )
            artifact_ids.append(artifact["id"])
            qr_values.extend(artifact.get("qr_values") or [])
            ocr_texts.append(str(artifact.get("text") or ""))
            processed_image_urls.add(image_url)
            downloaded += 1
        except Exception as exc:
            log_processing(job["id"], "extracting", "浏览器图片提取失败", "warning", {"url": image_url, "error": str(exc)})
    image_urls = list(dict.fromkeys([*image_urls, *(str(image.get("url") or "") for image in browser_image_data if image.get("url"))]))
    for index, image_url in enumerate(image_urls[:24], start=1):
        if image_url in processed_image_urls:
            continue
        try:
            response = fetch_public_http(image_url, timeout=30, max_bytes=10 * 1024 * 1024)
            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            detected_suffix = detect_image_suffix(response.content)
            if not media_type.startswith("image/") and not detected_suffix:
                continue
            artifact = attach_artifact(
                raw["id"],
                _web_image_filename(image_url, content_type, index, detected_suffix),
                response.content,
                content_type or None,
            )
            artifact_ids.append(artifact["id"])
            qr_values.extend(artifact.get("qr_values") or [])
            ocr_texts.append(str(artifact.get("text") or ""))
            downloaded += 1
        except Exception as exc:
            log_processing(job["id"], "extracting", "网页图片提取失败", "warning", {"url": image_url, "error": str(exc)})
    if image_urls:
        metadata["linked_image_urls"] = image_urls[:24]
    screenshot_data = parsed.get("screenshot_data")
    if isinstance(screenshot_data, bytes) and screenshot_data and downloaded == 0:
        try:
            artifact = attach_artifact(raw["id"], "webpage-screenshot.png", screenshot_data, "image/png")
            artifact_ids.append(artifact["id"])
            qr_values.extend(artifact.get("qr_values") or [])
            ocr_texts.append(str(artifact.get("text") or ""))
            metadata["browser_screenshot_ocr"] = True
        except Exception as exc:
            log_processing(job["id"], "extracting", "网页截图 OCR 失败", "warning", {"error": str(exc)})
    if artifact_ids:
        metadata["artifact_id"] = artifact_ids[-1]
        metadata["artifact_ids"] = list(dict.fromkeys([*(metadata.get("artifact_ids") or []), *artifact_ids]))
    if qr_values:
        metadata["qr_values"] = list(dict.fromkeys([*(metadata.get("qr_values") or []), *qr_values]))
    if downloaded:
        metadata["linked_images_downloaded"] = downloaded
        log_processing(job["id"], "extracting", f"已下载 {downloaded} 个网页图片资源，等待 Codex OCR", details={"qr_values": list(dict.fromkeys(qr_values))})
    return list(dict.fromkeys(qr_values)), _merge_texts(*ocr_texts)


def _extract_source_text(job: dict[str, Any], raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        metadata = json.loads(raw.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    text = str(raw.get("text_content") or "").strip()
    _stage(job["id"], "extracting", "开始提取来源文字", "local_parser")
    source_url = recover_original_source_url(metadata.get("source_url") or metadata.get("url"))
    if source_url:
        metadata["source_url"] = source_url
        metadata["url"] = source_url
    url = source_url
    if is_link_message(raw["message_type"], metadata) and url:
        try:
            parsed = fetch_public_url(str(url))
            direct_resolved_url = str(parsed.get("url") or url)
            if _should_render_in_browser(str(url), parsed):
                metadata["browser_attempted"] = True
                _stage(job["id"], "extracting", "公众号普通抓取不足，启动浏览器渲染", "playwright")
                try:
                    browser_result = fetch_public_browser(str(url))
                    parsed = _merge_browser_result(parsed, browser_result)
                    if browser_result.get("browser_rendered"):
                        metadata.update({
                            "browser_rendered": True,
                            "browser_image_count": browser_result.get("browser_image_count", 0),
                            "browser_loaded_image_count": browser_result.get("browser_loaded_image_count", 0),
                            "browser_downloaded_image_count": browser_result.get("browser_downloaded_image_count", 0),
                            "browser_article_text_chars": browser_result.get("browser_article_text_chars", 0),
                            "browser_screenshot_captured": bool(browser_result.get("screenshot_data")),
                        })
                        log_processing(
                            job["id"],
                            "extracting",
                            "浏览器渲染完成，继续处理页面文字和图片",
                            details={
                                "images": browser_result.get("browser_image_count", 0),
                                "loaded_images": browser_result.get("browser_loaded_image_count", 0),
                                "downloaded_images": browser_result.get("browser_downloaded_image_count", 0),
                                "article_text_characters": browser_result.get("browser_article_text_chars", 0),
                            },
                        )
                except Exception as exc:
                    metadata["browser_error"] = str(exc)
                    log_processing(job["id"], "extracting", "浏览器渲染失败，保留普通抓取结果", "warning", {"error": str(exc)})
            access_challenge = bool(parsed.get("access_challenge"))
            browser_resolved_url = str(parsed.get("url") or url)
            resolved_url = direct_resolved_url if direct_resolved_url != url else browser_resolved_url
            metadata.update({
                "source_url": url,
                "url": url,
                "resolved_url": resolved_url,
                "title": parsed.get("title", ""),
                "web_content_type": parsed.get("content_type", ""),
                "backend_fetched": not access_challenge,
                "web_access_status": "challenge" if access_challenge else "ok",
            })
            if access_challenge:
                metadata["web_access_error"] = parsed.get("access_error") or "网页要求环境验证"
                log_processing(
                    job["id"],
                    "extracting",
                    "公众号页面要求环境验证，转交 Codex 兜底访问",
                    "warning",
                    {"url": metadata.get("url"), "error": metadata["web_access_error"]},
                )
            else:
                text = _merge_texts(text, parsed.get("text"))
            _, image_text = _extract_link_images(job, raw, parsed, metadata)
            text = _merge_texts(text, image_text)
            _persist_raw_extraction(raw["id"], text, metadata)
            raw["text_content"] = text
            raw["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
        except Exception as exc:
            log_processing(job["id"], "extracting", "后端网页提取失败", "warning", {"error": str(exc)})
    image_artifacts = _image_artifact_rows(raw["id"])
    if image_artifacts:
        current_artifact_ids = [row["id"] for row in image_artifacts if row.get("path")]
        cached_artifact_ids = metadata.get("codex_ocr_artifact_ids") or []
        if text and metadata.get("codex_ocr_complete") and cached_artifact_ids == current_artifact_ids:
            log_processing(job["id"], "codex_ocr_cached", f"复用同页 {len(current_artifact_ids)} 张图片的一次性 OCR 结果")
        else:
            try:
                reason = "公众号页面返回微信环境验证页" if metadata.get("web_access_status") == "challenge" else "图片是主要来源内容"
                text = _codex_extract(job, raw, metadata, reason, primary_ocr=True)
                raw["text_content"] = text
                raw["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
            except Exception as exc:
                metadata["codex_ocr_error"] = str(exc)
                if not bool(get_setting("local_ocr_fallback_enabled", False)):
                    raise RuntimeError("Codex OCR failed and local OCR fallback is disabled") from exc
                _stage(job["id"], "local_ocr_fallback", "Codex OCR 失败，改用本地 OCR 兜底", "rapidocr")
                log_processing(job["id"], "local_ocr_fallback", "Codex OCR 失败，使用本地 OCR 兜底", "warning", {"error": str(exc)})
                local_text, local_errors = _local_ocr_extract(raw)
                text = _merge_texts(text, local_text)
                metadata["local_ocr_fallback"] = True
                if local_errors:
                    metadata["local_ocr_errors"] = local_errors[:10]
                if not text or (metadata.get("web_access_status") == "challenge" and not local_text):
                    raise RuntimeError("Codex OCR failed and local OCR fallback returned no text") from exc
                _persist_raw_extraction(raw["id"], text, metadata)
                raw["text_content"] = text
                raw["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
    elif not is_text_message(raw["message_type"], metadata):
        if metadata.get("web_access_status") == "challenge":
            text = _codex_extract(job, raw, metadata, "公众号页面返回微信环境验证页")
        elif len(text) < 20:
            text = _codex_extract(job, raw, metadata, "正文为空、过短或本地解析失败")
    log_processing(job["id"], "extracting", "来源文字提取完成", details={"characters": len(text)})
    return text, metadata


def _finish(job_id: str, result: dict[str, Any]) -> None:
    if not _still_active(job_id):
        log_processing(job_id, "canceled", "任务已取消，忽略迟到结果", "warning")
        return
    with connect() as connection:
        job = connection.execute("SELECT kind,raw_message_id FROM processing_jobs WHERE id=?", (job_id,)).fetchone()
        finished_at = utc_now()
        connection.execute(
            "UPDATE processing_jobs SET status='succeeded',stage='completed',lease_until=NULL,error=NULL,result_json=?,finished_at=?,updated_at=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), finished_at, finished_at, job_id),
        )
        if job and job["kind"] == "classify" and job["raw_message_id"]:
            connection.execute(
                "UPDATE raw_messages SET recognition_status='succeeded',recognized_at=?,recognition_error=NULL WHERE id=?",
                (finished_at, job["raw_message_id"]),
            )
    log_processing(job_id, "completed", "任务处理完成", details=result)


def _mark_classification_needs_review(
    job: dict[str, Any],
    model_result: dict[str, Any],
    persistence_result: dict[str, Any],
    error: str = "模型判断为招聘信息，但未产生可持久化企业数据",
) -> dict[str, Any]:
    """Record a model/persistence mismatch without marking the task succeeded."""
    finished_at = utc_now()
    queue_result = {
        "is_recruitment": model_result.get("is_recruitment") is True,
        "company_count": len(persistence_result.get("company_ids") or []),
        "company_ids": persistence_result.get("company_ids") or [],
        "company_names": persistence_result.get("company_names") or [],
        "job_count": len(persistence_result.get("job_ids") or []),
        "job_ids": persistence_result.get("job_ids") or [],
        "created_company_count": persistence_result.get("created_company_count", 0),
        "updated_company_count": persistence_result.get("updated_company_count", 0),
        "invalid_company_count": persistence_result.get("invalid_company_count", 0),
        "error": error,
    }
    if persistence_result.get("invalid_company_entries"):
        queue_result["invalid_company_entries"] = persistence_result["invalid_company_entries"]
    if persistence_result.get("validation_error"):
        queue_result["validation_error"] = persistence_result["validation_error"]
    with connect() as connection:
        connection.execute(
            """UPDATE processing_jobs
               SET status='needs_review',stage='review',lease_until=NULL,error=?,result_json=?,finished_at=?,updated_at=?
               WHERE id=?""",
            (error, json.dumps(queue_result, ensure_ascii=False), finished_at, finished_at, job["id"]),
        )
    _set_recognition_status(job.get("raw_message_id"), "needs_review", error)
    log_processing(
        job["id"],
        "review",
        error,
        "error",
        {"persistence_result": persistence_result},
    )
    review_payload = _review_context(job["id"], job.get("raw_message_id"), job.get("company_id"))
    review_payload.update({
        "error": {"type": "persistence_error", "message": error},
        "model_result": model_result,
        "persistence_result": persistence_result,
    })
    with connect() as connection:
        connection.execute(
            "INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (str(uuid4()), "processing_failed", "processing_job", job["id"], json.dumps(review_payload, ensure_ascii=False), finished_at),
        )
    return {
        "status": "needs_review",
        "is_recruitment": model_result.get("is_recruitment") is True,
        "error": error,
        "persistence": persistence_result,
        "id": job["id"],
    }


def _fail(job: dict[str, Any], exc: Exception) -> dict[str, Any]:
    error = str(exc)
    current = one("SELECT status,cancel_requested,attempts FROM processing_jobs WHERE id=?", (job["id"],))
    if not current or current["status"] == "canceled" or current["cancel_requested"]:
        _set_recognition_status(job.get("raw_message_id"), "canceled")
        return {"status": "canceled", "id": job["id"]}
    attempts = int(current["attempts"])
    delays = [10, 30, 90]
    retry_at: str | None = None
    with connect() as connection:
        if attempts < 3:
            retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delays[attempts - 1])).isoformat(timespec="seconds")
            connection.execute(
                "UPDATE processing_jobs SET status='pending',stage='retry_wait',next_attempt_at=?,lease_until=NULL,processor=NULL,result_json=NULL,started_at=NULL,finished_at=NULL,error=?,updated_at=? WHERE id=?",
                (retry_at, error, utc_now(), job["id"]),
            )
        else:
            status = "paused_quota" if "budget" in error.lower() else "needs_review"
            review_payload = _review_context(job["id"], job.get("raw_message_id"), job.get("company_id"))
            review_payload["error"] = {"type": type(exc).__name__, "message": error}
            connection.execute(
                "UPDATE processing_jobs SET status=?,stage='failed',lease_until=NULL,error=?,finished_at=?,updated_at=? WHERE id=?",
                (status, error, utc_now(), utc_now(), job["id"]),
            )
            entity_id = job.get("raw_message_id") or job.get("company_id") or job["id"]
            connection.execute(
                "INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid4()), "processing_failed", "processing_job", entity_id, json.dumps(review_payload, ensure_ascii=False), utc_now()),
            )
    if retry_at:
        _set_recognition_status(job.get("raw_message_id"), "pending", error)
        log_processing(job["id"], "retry_wait", f"处理失败，将自动进行第 {attempts + 1} 次尝试", "warning", {"error": error, "retry_at": retry_at})
        return {"status": "retry_wait", "id": job["id"], "error": error}
    _set_recognition_status(job.get("raw_message_id"), "needs_review", error)
    log_processing(job["id"], "failed", "自动重试已用尽，转入人工处理", "error", {"error": error})
    return {"status": status, "id": job["id"], "error": error}


def _split_text(text: str, limit: int = 50_000) -> list[str]:
    if len(text) <= limit:
        return [text]
    paragraph_parts = re.split(r"(\n\s*\n+)", text)
    units: list[str] = []
    for index in range(0, len(paragraph_parts), 2):
        paragraph = paragraph_parts[index]
        separator = paragraph_parts[index + 1] if index + 1 < len(paragraph_parts) else ""
        segment = paragraph + separator
        if not segment:
            continue
        if len(segment) <= limit:
            units.append(segment)
            continue
        lines = segment.splitlines(keepends=True)
        units.extend(lines if lines else [segment])
    chunks: list[str] = []
    current = ""
    for unit in units:
        if len(unit) > limit:
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(unit):
                end = min(len(unit), start + limit)
                chunks.append(unit[start:end])
                if end == len(unit):
                    break
                start = end
            continue
        if current and len(current) + len(unit) > limit:
            chunks.append(current)
            current = ""
        current += unit
    if current:
        chunks.append(current)
    return chunks or [text]


def _unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _merge_json_list(old_value: Any, new_value: Any) -> list[Any]:
    try:
        old = json.loads(old_value or "[]") if isinstance(old_value, str) else (old_value or [])
    except (TypeError, json.JSONDecodeError):
        old = []
    incoming = new_value if isinstance(new_value, list) else []
    values = [value for value in [*old, *incoming] if value not in (None, "")]
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _merge_extracted_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge chunk results without interpreting source text."""
    recruitment = [item for item in items if item.get("is_recruitment")]
    invalid_non_recruitment_entries = [
        entry
        for item in items
        if not item.get("is_recruitment")
        for entry in item.get("companies") or []
        if isinstance(entry, dict)
    ]
    if invalid_non_recruitment_entries:
        return {
            "is_recruitment": False,
            "decision_reason": "；".join(dict.fromkeys(
                str(item.get("decision_reason") or "").strip()
                for item in items
                if str(item.get("decision_reason") or "").strip()
            )),
            "companies": invalid_non_recruitment_entries,
        }
    if not recruitment:
        return {"is_recruitment": False, "decision_reason": "；".join(dict.fromkeys(
            str(item.get("decision_reason") or "").strip() for item in items if str(item.get("decision_reason") or "").strip()
        )), "companies": []}
    companies: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    anonymous_entries: list[dict[str, Any]] = []

    def merge_entry(target: dict[str, Any], entry: dict[str, Any]) -> None:
        company = entry.get("company") if isinstance(entry.get("company"), dict) else {}
        for field, value in company.items():
            if isinstance(value, list):
                target["company"][field] = list(dict.fromkeys([*(target["company"].get(field) or []), *value]))
            elif value not in (None, "", {}, []) and target["company"].get(field) in (None, "", {}, []):
                target["company"][field] = value
        incoming_recruitment = entry.get("recruitment") if isinstance(entry.get("recruitment"), dict) else {}
        for section in ("batch", "shared_details"):
            target_section = target["recruitment"].setdefault(section, {})
            for field, value in (incoming_recruitment.get(section) or {}).items():
                if isinstance(value, list):
                    target_section[field] = list(dict.fromkeys([*(target_section.get(field) or []), *value]))
                elif value not in (None, "", {}, []) and target_section.get(field) in (None, "", {}, []):
                    target_section[field] = value
        target["recruitment"]["jobs"] = _unique_dicts([
            *(target["recruitment"].get("jobs") or []), *(incoming_recruitment.get("jobs") or []),
        ])
        target["recruitment"]["events"] = _unique_dicts([
            *(target["recruitment"].get("events") or []), *(incoming_recruitment.get("events") or []),
        ])

    for item in recruitment:
        for entry in item.get("companies") or []:
            if not isinstance(entry, dict):
                continue
            company = dict(entry.get("company") or {})
            key = (normalize_text_value(company.get("display_name") or company.get("legal_name")) or "").casefold()
            target = by_name.get(key) if key else None
            if target is None:
                if not key:
                    anonymous_entries.append(entry)
                    continue
                target = {"company": company, "recruitment": dict(entry.get("recruitment") or {})}
                target["recruitment"].setdefault("batch", {})
                target["recruitment"].setdefault("shared_details", {})
                target["recruitment"].setdefault("jobs", [])
                target["recruitment"].setdefault("events", [])
                companies.append(target)
                if key:
                    by_name[key] = target
                continue
            merge_entry(target, entry)
    if anonymous_entries:
        if len(companies) == 1:
            for entry in anonymous_entries:
                merge_entry(companies[0], entry)
        else:
            companies.extend(
                {"company": dict(entry.get("company") or {}), "recruitment": dict(entry.get("recruitment") or {})}
                for entry in anonymous_entries
            )
    reasons = [str(item.get("decision_reason") or "").strip() for item in recruitment]
    return {"is_recruitment": True, "decision_reason": "；".join(dict.fromkeys(value for value in reasons if value)), "companies": companies}


def _classify_source(job: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    chunks = _split_text(str(message.get("text") or ""))
    extracted: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        if not _still_active(job["id"]):
            return {"is_recruitment": False, "decision_reason": "canceled", "companies": []}
        if len(chunks) > 1:
            log_processing(job["id"], "classifying", f"识别长文本分段 {index}/{len(chunks)}", details={"characters": len(chunk)})
        part = {**message, "text": chunk, "metadata": {**(message.get("metadata") or {}), "chunk": index, "chunk_count": len(chunks)}}
        try:
            result = classify_messages([part], job_id=job["id"])
        except TypeError as exc:
            if "job_id" not in str(exc):
                raise
            result = classify_messages([part])
        if not isinstance(result.payload, dict) or "companies" not in result.payload:
            raise ValueError(f"Model response did not contain the required Schema for chunk {index}")
        extracted.append(result.payload)
    merged = normalize_recruitment_payload(_merge_extracted_items(extracted))
    validate_recruitment_payload(merged)
    return merged


def _process_classify(job: dict[str, Any]) -> dict[str, Any]:
    raw_row = one("SELECT * FROM raw_messages WHERE id=?", (job["raw_message_id"],))
    if not raw_row:
        raise RuntimeError("Raw message not found")
    raw = dict(raw_row)
    _set_recognition_status(raw["id"], "running")
    try:
        raw_metadata = json.loads(raw.get("metadata_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        raw_metadata = {}
    if is_system_message(raw["message_type"], raw.get("text_content") or "", raw_metadata):
        with connect() as connection:
            connection.execute("UPDATE raw_messages SET is_recruitment=0 WHERE id=?", (raw["id"],))
        _finish(job["id"], {"is_recruitment": False, "decision_reason": "system_message_filtered"})
        job_status = one("SELECT status FROM processing_jobs WHERE id=?", (job["id"],))
        if job_status and job_status["status"] == "succeeded":
            _set_recognition_status(raw["id"], "filtered")
        return {"status": "succeeded", "is_recruitment": False, "filtered": "system_message", "id": job["id"]}
    text, metadata = _extract_source_text(job, raw)
    if not _still_active(job["id"]):
        _set_recognition_status(raw["id"], "canceled")
        return {"status": "canceled", "id": job["id"]}
    engine = str(get_setting("processing_engine", "codex") or "codex")
    processor = "local_codex:gpt-5.6-luna" if engine == "codex" else "generic_llm"
    _stage(job["id"], "classifying", "开始判断招聘信息并提取统一结构", processor)
    message = {"id": raw["id"], "sent_at": raw["sent_at"], "message_type": raw["message_type"], "text": text, "metadata": metadata}
    try:
        item = _classify_source(job, message)
    except RecruitmentPayloadValidationError as exc:
        with connect() as connection:
            connection.execute(
                "UPDATE raw_messages SET is_recruitment=? WHERE id=?",
                (1 if isinstance(exc.payload, dict) and exc.payload.get("is_recruitment") else 0, raw["id"]),
            )
        return _mark_classification_needs_review(
            job,
            exc.payload if isinstance(exc.payload, dict) else {},
            {
                "company_ids": [],
                "job_ids": [],
                "company_names": [],
                "created_company_count": 0,
                "updated_company_count": 0,
                "invalid_company_entries": list(exc.invalid_company_entries),
                "invalid_company_count": len(exc.invalid_company_entries),
                "validation_error": str(exc),
            },
            error=(
                "模型判断为招聘信息，但未产生可持久化企业数据"
                if isinstance(exc.payload, dict) and exc.payload.get("is_recruitment") is True
                else f"模型结果结构一致性校验失败：{exc}"
            ),
        )
    if not _still_active(job["id"]):
        _set_recognition_status(raw["id"], "canceled")
        return {"status": "canceled", "id": job["id"]}
    with connect() as connection:
        connection.execute("UPDATE raw_messages SET is_recruitment=? WHERE id=?", (1 if item.get("is_recruitment") else 0, raw["id"]))
    if not item.get("is_recruitment"):
        result = {
            "is_recruitment": False,
            "decision_reason": item.get("decision_reason", ""),
            "processor": processor,
        }
        _finish(job["id"], result)
        return {"status": "succeeded", "is_recruitment": False, "id": job["id"]}
    _stage(job["id"], "persisting", "写入企业、岗位、时间轴与来源证据")
    persistence_result = apply_model_item(item, raw["id"], raw["sent_at"])
    if persistence_result.get("invalid_company_entries"):
        log_processing(
            job["id"],
            "persisting",
            "部分企业结果无效，未写入这些企业",
            "warning",
            {"invalid_company_entries": persistence_result["invalid_company_entries"]},
        )
    if not persistence_result.get("company_ids"):
        return _mark_classification_needs_review(job, item, persistence_result)
    result = {
        "is_recruitment": True,
        "company_count": len(persistence_result["company_ids"]),
        "company_ids": persistence_result["company_ids"],
        "company_names": persistence_result["company_names"],
        "job_count": len(persistence_result["job_ids"]),
        "job_ids": persistence_result["job_ids"],
        "created_company_count": persistence_result.get("created_company_count", 0),
        "updated_company_count": persistence_result.get("updated_company_count", 0),
        "invalid_company_count": persistence_result.get("invalid_company_count", 0),
        "processor": processor,
    }
    if persistence_result.get("invalid_company_entries"):
        result["invalid_company_entries"] = persistence_result["invalid_company_entries"]
    _finish(job["id"], result)
    return {"status": "succeeded", **result, "id": job["id"]}


def _process_company_consolidation(job: dict[str, Any]) -> dict[str, Any]:
    company_row = one("SELECT * FROM companies WHERE id=?", (job["company_id"],))
    if not company_row:
        raise RuntimeError("Company for consolidation was not found")
    company = dict(company_row)
    for key in list(company):
        if key.endswith("_json"):
            try:
                company[key[:-5]] = json.loads(company.pop(key) or "[]")
            except json.JSONDecodeError:
                pass
    if "company_tags" in company:
        company["tags"] = company.pop("company_tags")
    source_rows = []
    raw_message_ids: list[str] = []
    with connect() as connection:
        rows = connection.execute(
            """SELECT e.id,e.source_type,e.source_url,e.observed_at,e.excerpt,r.id AS raw_message_id,r.text_content,r.sent_at
               FROM evidences e LEFT JOIN raw_messages r ON r.id=e.raw_message_id
               WHERE e.company_id=? ORDER BY e.observed_at""",
            (job["company_id"],),
        ).fetchall()
        for row in rows:
            if row["raw_message_id"]:
                raw_message_ids.append(row["raw_message_id"])
            source_rows.append({"evidence_id": row["id"], "source_type": row["source_type"], "source_url": row["source_url"], "observed_at": row["observed_at"], "text": row["text_content"] or row["excerpt"] or ""})
    _stage(job["id"], "consolidating", "合并企业事实并优化企业介绍")
    result = consolidate_company_profile(company, source_rows, job["id"])
    payload = result.payload
    review_context = {
        "original_messages": [snapshot for raw_id in raw_message_ids if (snapshot := _raw_snapshot(raw_id))],
        "processing_job": _job_snapshot(job["id"]),
        "processing_logs": _processing_log_snapshots(job["id"]),
    }
    with connect() as connection:
        connection.execute(
            "INSERT INTO company_versions(id,company_id,profile_json,decision,reason,processor,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), job["company_id"], json.dumps(payload.get("profile") or {}, ensure_ascii=False), payload.get("decision", "abnormal"), payload.get("reason"), result.provider + ":" + result.model, utc_now()),
        )
        if payload.get("decision") != "normal":
            review_payload = {**payload, **review_context}
            connection.execute(
                "INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid4()), "company_consolidation_abnormal", "company", job["company_id"], json.dumps(review_payload, ensure_ascii=False), utc_now()),
            )
            connection.execute("UPDATE processing_jobs SET status='needs_review',stage='review',error=?,finished_at=?,updated_at=? WHERE id=?", (payload.get("reason") or "Model marked consolidation abnormal", utc_now(), utc_now(), job["id"]))
            abnormal = True
        else:
            abnormal = False
        if abnormal:
            pass
        else:
            profile = payload.get("profile") or {}
            existing_aliases = json.loads(company_row["aliases_json"] or "[]")
            aliases = list(dict.fromkeys([*existing_aliases, *(profile.get("aliases") or [])]))
            industries = [value for value in profile.get("industry_codes") or [] if value in INDUSTRIES]
            existing_industries = [company_row["primary_industry"], *json.loads(company_row["secondary_industries_json"] or "[]")]
            merged_industries = list(dict.fromkeys([*existing_industries, *industries]))
            primary_industry = industries[0] if industries else company_row["primary_industry"]
            secondary_industries = [value for value in merged_industries if value and value != primary_industry]
            existing_tags = json.loads(company_row["company_tags_json"] or "[]")
            tags = normalize_company_tags(
                [*existing_tags, *(profile.get("tags") or [])],
                profile.get("company_nature") or company_row["company_nature"],
                merged_industries,
            )
            summary = company_row["summary"] if company_row["summary_locked"] else profile.get("summary") or company_row["summary"]
            businesses = _merge_json_list(company_row["businesses_json"], profile.get("businesses"))
            highlights = _merge_json_list(company_row["highlights_json"], profile.get("highlights"))
            official_channels = _merge_json_list(company_row["official_channels_json"], profile.get("official_channels"))
            major_requirements = _merge_json_list(company_row["major_requirements_json"], profile.get("major_requirements"))
            connection.execute(
                """UPDATE companies SET display_name=COALESCE(NULLIF(?,''),display_name),legal_name=COALESCE(NULLIF(?,''),legal_name),
                   aliases_json=?,summary=?,primary_industry=COALESCE(?,primary_industry),secondary_industries_json=?,website=COALESCE(NULLIF(?,''),website),
                   company_nature=COALESCE(NULLIF(?,''),company_nature),founded_at=COALESCE(NULLIF(?,''),founded_at),company_size=COALESCE(NULLIF(?,''),company_size),
                   headquarters=COALESCE(NULLIF(?,''),headquarters),businesses_json=?,highlights_json=?,official_channels_json=?,major_requirements_json=?,company_tags_json=?,last_consolidated_at=?,updated_at=? WHERE id=?""",
                (profile.get("display_name"), profile.get("legal_name"), json.dumps(aliases, ensure_ascii=False), summary,
                  primary_industry, json.dumps(secondary_industries, ensure_ascii=False), profile.get("website"),
                  profile.get("company_nature"), profile.get("founded_at"), profile.get("company_size"), profile.get("headquarters"),
                  json.dumps(businesses, ensure_ascii=False), json.dumps(highlights, ensure_ascii=False),
                  json.dumps(official_channels, ensure_ascii=False), json.dumps(major_requirements, ensure_ascii=False),
                  json.dumps(tags, ensure_ascii=False), utc_now(), utc_now(), job["company_id"]),
            )
            apply_company_overrides(connection, job["company_id"], company_overrides(company_row["manual_overrides_json"]))
            deduplicate_company_jobs(connection, job["company_id"])
    if abnormal:
        log_processing(job["id"], "review", "模型判定企业整理结果异常，转入审核", "warning", payload)
        return {"status": "needs_review", "id": job["id"]}
    _finish(job["id"], {"company_id": job["company_id"], "decision": "normal"})
    return {"status": "succeeded", "company_id": job["company_id"], "id": job["id"]}


def _process_company_research(job: dict[str, Any]) -> dict[str, Any]:
    if not one("SELECT id FROM companies WHERE id=?", (job["company_id"],)):
        raise RuntimeError("Company for public research was not found")
    engine = str(get_setting("processing_engine", "codex") or "codex")
    processor = "local_codex:gpt-5.6-luna" if engine == "codex" else "generic_llm"
    _stage(job["id"], "researching", "联网检索企业官网、行业属性和负面公开报道", processor)
    result = execute_company_research(job["company_id"], job["id"], lambda: _still_active(job["id"]))
    if result.get("status") == "canceled" or not _still_active(job["id"]):
        return {"status": "canceled", "id": job["id"]}
    _stage(job["id"], "saving_research", "保存企业概览、标签、风险发现和来源 URL", processor)
    _finish(job["id"], {**result, "id": job["id"]})
    return {**result, "id": job["id"]}


def process_one_job(*, prefer_enrichment: bool = False) -> dict[str, Any] | None:
    job = _claim_one(prefer_enrichment=prefer_enrichment)
    if not job:
        return None
    log_processing(job["id"], "starting", "任务已开始", details={"kind": job["kind"], "attempt": job["attempts"]})
    try:
        if job["kind"] == "classify":
            return _process_classify(job)
        if job["kind"] == "consolidate_company":
            return _process_company_consolidation(job)
        if job["kind"] == "research_company":
            return _process_company_research(job)
        raise RuntimeError(f"Unknown processing job kind: {job['kind']}")
    except Exception as exc:
        return _fail(job, exc)


def process_one_batch(limit: int = 1, *, prefer_enrichment: bool = False) -> dict[str, Any] | None:
    results = []
    for _ in range(max(1, limit)):
        result = process_one_job(prefer_enrichment=prefer_enrichment)
        if result is None:
            break
        results.append(result)
    return {"processed": len(results), "results": results} if results else None


def process_one_enrichment() -> dict[str, Any] | None:
    return None


def import_text(text: str, source_group_id: str | None = None, metadata: dict[str, Any] | None = None) -> str | None:
    message = {"type": "text", "text": text, **(metadata or {})}
    return ingest_message(message, "manual", source_group_id)


def import_url(url: str, source_group_id: str | None = None) -> str | None:
    return ingest_message({"type": "article", "text": "", "url": url}, "manual", source_group_id)


def import_file(filename: str, data: bytes, mime_type: str | None = None, source_group_id: str | None = None) -> dict[str, Any]:
    filename = normalize_file_filename(filename, data, mime_type)
    parsed = extract_file(filename, data, mime_type=mime_type)
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
            (json.dumps({"filename": artifact.get("filename") or filename, "mime_type": artifact.get("mime_type") or mime_type, "artifact_id": artifact["id"], "qr_values": parsed.get("qr_values", [])}, ensure_ascii=False), raw_id),
        )
    return {"raw_message_id": raw_id, "artifact": artifact}
