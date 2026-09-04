from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .catalog import apply_model_item
from .db import connect, one, utc_now
from .model_provider import classify_messages, consolidate_company_profile, get_setting
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


def log_processing(job_id: str, stage: str, message: str, level: str = "info", details: dict[str, Any] | None = None) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO processing_logs(id,processing_job_id,stage,level,message,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), job_id, stage, level, message, json.dumps(details or {}, ensure_ascii=False), utc_now()),
        )


def queue_is_running() -> bool:
    row = one("SELECT state FROM queue_control WHERE id=1")
    return bool(row and row["state"] == "running")


def _claim_one() -> dict[str, Any] | None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT * FROM processing_jobs
               WHERE status='pending' AND cancel_requested=0
                 AND (next_attempt_at IS NULL OR next_attempt_at<=?)
               ORDER BY CASE kind WHEN 'classify' THEN 0 ELSE 1 END, created_at LIMIT 1""",
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


def _codex_extract(job: dict[str, Any], raw: dict[str, Any], metadata: dict[str, Any], reason: str) -> str:
    from .codex_agent import run_codex_json

    artifact = one("SELECT * FROM artifacts WHERE raw_message_id=? ORDER BY created_at DESC LIMIT 1", (raw["id"],))
    images = []
    if artifact and str(artifact["mime_type"] or "").startswith("image/"):
        images = [artifact["path"]]
    payload = {
        "reason": reason,
        "source_type": raw["message_type"],
        "url": metadata.get("url"),
        "filename": artifact["filename"] if artifact else metadata.get("filename"),
        "existing_text": raw.get("text_content") or "",
        "instruction": "提取来源中的完整可读正文。网页需要访问原始 URL；图片或附件需要读取内容。不要总结。",
    }
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "source_url": {"type": "string"}, "notes": {"type": "string"}},
        "required": ["text", "source_url", "notes"],
        "additionalProperties": False,
    }
    _stage(job["id"], "codex_fallback", f"本地提取不足，使用 Codex 兜底：{reason}", "local_codex:gpt-5.6-luna")
    result = run_codex_json("source_text_extraction", payload, schema, job_id=job["id"], image_paths=images, enable_web=bool(metadata.get("url")))
    text = str(result.get("text") or "").strip()
    if not text:
        raise RuntimeError("Codex did not extract readable text")
    metadata["codex_fallback"] = True
    metadata["codex_notes"] = result.get("notes")
    with connect() as connection:
        connection.execute(
            "UPDATE raw_messages SET text_content=?,metadata_json=? WHERE id=?",
            (text, json.dumps(metadata, ensure_ascii=False), raw["id"]),
        )
    return text


def _extract_source_text(job: dict[str, Any], raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        metadata = json.loads(raw.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    text = str(raw.get("text_content") or "").strip()
    _stage(job["id"], "extracting", "开始提取来源文字", "local_parser")
    url = metadata.get("url")
    if raw["message_type"] in {"article", "link", "url"} and url and len(text) < 20:
        try:
            parsed = fetch_public_url(str(url))
            text = str(parsed.get("text") or "").strip()
            metadata.update({"url": parsed.get("url", url), "title": parsed.get("title", ""), "backend_fetched": True})
            with connect() as connection:
                connection.execute("UPDATE raw_messages SET text_content=?,metadata_json=? WHERE id=?", (text, json.dumps(metadata, ensure_ascii=False), raw["id"]))
        except Exception as exc:
            log_processing(job["id"], "extracting", "后端网页提取失败", "warning", {"error": str(exc)})
    if len(text) < 20 and not (raw["message_type"] == "text" and text):
        text = _codex_extract(job, raw, metadata, "正文为空、过短或本地解析失败")
    log_processing(job["id"], "extracting", "来源文字提取完成", details={"characters": len(text)})
    return text, metadata


def _finish(job_id: str, result: dict[str, Any]) -> None:
    if not _still_active(job_id):
        log_processing(job_id, "canceled", "任务已取消，忽略迟到结果", "warning")
        return
    with connect() as connection:
        connection.execute(
            "UPDATE processing_jobs SET status='succeeded',stage='completed',lease_until=NULL,error=NULL,result_json=?,finished_at=?,updated_at=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), utc_now(), utc_now(), job_id),
        )
    log_processing(job_id, "completed", "任务处理完成", details=result)


def _fail(job: dict[str, Any], exc: Exception) -> dict[str, Any]:
    error = str(exc)
    current = one("SELECT status,cancel_requested,attempts FROM processing_jobs WHERE id=?", (job["id"],))
    if not current or current["status"] == "canceled" or current["cancel_requested"]:
        return {"status": "canceled", "id": job["id"]}
    attempts = int(current["attempts"])
    delays = [10, 30, 90]
    retry_at: str | None = None
    with connect() as connection:
        if attempts < 3:
            retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delays[attempts - 1])).isoformat(timespec="seconds")
            connection.execute(
                "UPDATE processing_jobs SET status='pending',stage='retry_wait',next_attempt_at=?,lease_until=NULL,error=?,updated_at=? WHERE id=?",
                (retry_at, error, utc_now(), job["id"]),
            )
        else:
            status = "paused_quota" if "budget" in error.lower() else "needs_review"
            connection.execute(
                "UPDATE processing_jobs SET status=?,stage='failed',lease_until=NULL,error=?,finished_at=?,updated_at=? WHERE id=?",
                (status, error, utc_now(), utc_now(), job["id"]),
            )
            entity_id = job.get("raw_message_id") or job.get("company_id") or job["id"]
            connection.execute(
                "INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid4()), "processing_failed", "processing_job", entity_id, json.dumps({"job_id": job["id"], "stage": job.get("stage"), "error": error}, ensure_ascii=False), utc_now()),
            )
    if retry_at:
        log_processing(job["id"], "retry_wait", f"处理失败，将自动进行第 {attempts + 1} 次尝试", "warning", {"error": error, "retry_at": retry_at})
        return {"status": "retry_wait", "id": job["id"], "error": error}
    log_processing(job["id"], "failed", "自动重试已用尽，转入人工处理", "error", {"error": error})
    return {"status": status, "id": job["id"], "error": error}


def _split_text(text: str, limit: int = 50_000, overlap: int = 1_000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def _unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _merge_extracted_items(items: list[dict[str, Any]], message_id: str) -> dict[str, Any]:
    recruitment = [item for item in items if item.get("is_recruitment")]
    if not recruitment:
        first = dict(items[0]) if items else {}
        first.update({"message_id": message_id, "is_recruitment": False})
        return first
    merged = dict(recruitment[0])
    merged["message_id"] = message_id
    merged["is_recruitment"] = True
    reasons = [str(item.get("decision_reason") or "").strip() for item in recruitment]
    merged["decision_reason"] = "；".join(dict.fromkeys(value for value in reasons if value))
    company = dict(merged.get("company") or {})
    for item in recruitment[1:]:
        incoming = item.get("company") or {}
        for key, value in incoming.items():
            if isinstance(value, list):
                company[key] = list(dict.fromkeys([*(company.get(key) or []), *value]))
            elif value not in (None, "", {}, []) and company.get(key) in (None, "", {}, []):
                company[key] = value
    merged["company"] = company
    batch = dict(merged.get("batch") or {})
    for item in recruitment[1:]:
        for key, value in (item.get("batch") or {}).items():
            if value not in (None, "", 0) and batch.get(key) in (None, "", 0):
                batch[key] = value
    merged["batch"] = batch
    merged["jobs"] = _unique_dicts([job for item in recruitment for job in (item.get("jobs") or [])])
    merged["events"] = _unique_dicts([event for item in recruitment for event in (item.get("events") or [])])
    return merged


def _classify_source(job: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    chunks = _split_text(str(message.get("text") or ""))
    extracted: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        if not _still_active(job["id"]):
            return {"message_id": message["id"], "is_recruitment": False, "decision_reason": "canceled"}
        if len(chunks) > 1:
            log_processing(job["id"], "classifying", f"识别长文本分段 {index}/{len(chunks)}", details={"characters": len(chunk)})
        part = {**message, "text": chunk, "metadata": {**(message.get("metadata") or {}), "chunk": index, "chunk_count": len(chunks)}}
        try:
            result = classify_messages([part], job_id=job["id"])
        except TypeError as exc:
            if "job_id" not in str(exc):
                raise
            result = classify_messages([part])
        values = result.payload.get("items") or []
        if not values:
            raise ValueError(f"Model response did not contain an item for chunk {index}")
        extracted.append(values[0])
    return _merge_extracted_items(extracted, message["id"])


def _process_classify(job: dict[str, Any]) -> dict[str, Any]:
    raw_row = one("SELECT * FROM raw_messages WHERE id=?", (job["raw_message_id"],))
    if not raw_row:
        raise RuntimeError("Raw message not found")
    raw = dict(raw_row)
    text, metadata = _extract_source_text(job, raw)
    if not _still_active(job["id"]):
        return {"status": "canceled", "id": job["id"]}
    engine = str(get_setting("processing_engine", "codex") or "codex")
    processor = "local_codex:gpt-5.6-luna" if engine == "codex" else "generic_llm"
    _stage(job["id"], "classifying", "开始判断招聘信息并提取统一结构", processor)
    message = {"id": raw["id"], "sent_at": raw["sent_at"], "message_type": raw["message_type"], "text": text, "metadata": metadata}
    item = _classify_source(job, message)
    item["message_id"] = raw["id"]
    if not _still_active(job["id"]):
        return {"status": "canceled", "id": job["id"]}
    with connect() as connection:
        connection.execute("UPDATE raw_messages SET is_recruitment=? WHERE id=?", (1 if item.get("is_recruitment") else 0, raw["id"]))
    if not item.get("is_recruitment"):
        _finish(job["id"], {"is_recruitment": False, "reason": item.get("decision_reason", "")})
        return {"status": "succeeded", "is_recruitment": False, "id": job["id"]}
    _stage(job["id"], "persisting", "写入企业、岗位、时间轴与来源证据")
    job_ids = apply_model_item(item, raw["id"], raw["sent_at"])
    _finish(job["id"], {"is_recruitment": True, "jobs": job_ids, "processor": processor})
    return {"status": "succeeded", "is_recruitment": True, "jobs": len(job_ids), "id": job["id"]}


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
    source_rows = []
    with connect() as connection:
        rows = connection.execute(
            """SELECT e.id,e.source_type,e.source_url,e.observed_at,e.excerpt,r.text_content,r.sent_at
               FROM evidences e LEFT JOIN raw_messages r ON r.id=e.raw_message_id
               WHERE e.company_id=? ORDER BY e.observed_at""",
            (job["company_id"],),
        ).fetchall()
        for row in rows:
            source_rows.append({"evidence_id": row["id"], "source_type": row["source_type"], "source_url": row["source_url"], "observed_at": row["observed_at"], "text": row["text_content"] or row["excerpt"] or ""})
    _stage(job["id"], "consolidating", "合并企业事实并优化企业介绍")
    result = consolidate_company_profile(company, source_rows, job["id"])
    payload = result.payload
    with connect() as connection:
        connection.execute(
            "INSERT INTO company_versions(id,company_id,profile_json,decision,reason,processor,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), job["company_id"], json.dumps(payload.get("profile") or {}, ensure_ascii=False), payload.get("decision", "abnormal"), payload.get("reason"), result.provider + ":" + result.model, utc_now()),
        )
        if payload.get("decision") != "normal":
            connection.execute(
                "INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid4()), "company_consolidation_abnormal", "company", job["company_id"], json.dumps(payload, ensure_ascii=False), utc_now()),
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
            industries = [value for value in profile.get("industry_codes") or [] if value]
            summary = company_row["summary"] if company_row["summary_locked"] else profile.get("summary") or company_row["summary"]
            connection.execute(
                """UPDATE companies SET display_name=COALESCE(NULLIF(?,''),display_name),legal_name=COALESCE(NULLIF(?,''),legal_name),
                   aliases_json=?,summary=?,primary_industry=COALESCE(?,primary_industry),secondary_industries_json=?,website=COALESCE(NULLIF(?,''),website),
                   company_nature=COALESCE(NULLIF(?,''),company_nature),founded_at=COALESCE(NULLIF(?,''),founded_at),company_size=COALESCE(NULLIF(?,''),company_size),
                   headquarters=COALESCE(NULLIF(?,''),headquarters),businesses_json=?,highlights_json=?,official_channels_json=?,last_consolidated_at=?,updated_at=? WHERE id=?""",
                (profile.get("display_name"), profile.get("legal_name"), json.dumps(aliases, ensure_ascii=False), summary,
                 industries[0] if industries else None, json.dumps(industries[1:], ensure_ascii=False), profile.get("website"),
                 profile.get("company_nature"), profile.get("founded_at"), profile.get("company_size"), profile.get("headquarters"),
                 json.dumps(profile.get("businesses") or [], ensure_ascii=False), json.dumps(profile.get("highlights") or [], ensure_ascii=False),
                 json.dumps(profile.get("official_channels") or [], ensure_ascii=False), utc_now(), utc_now(), job["company_id"]),
            )
    if abnormal:
        log_processing(job["id"], "review", "模型判定企业整理结果异常，转入审核", "warning", payload)
        return {"status": "needs_review", "id": job["id"]}
    _finish(job["id"], {"company_id": job["company_id"], "decision": "normal"})
    return {"status": "succeeded", "company_id": job["company_id"], "id": job["id"]}


def process_one_job() -> dict[str, Any] | None:
    job = _claim_one()
    if not job:
        return None
    log_processing(job["id"], "starting", "任务已开始", details={"kind": job["kind"], "attempt": job["attempts"]})
    try:
        if job["kind"] == "classify":
            return _process_classify(job)
        if job["kind"] == "consolidate_company":
            return _process_company_consolidation(job)
        raise RuntimeError(f"Unknown processing job kind: {job['kind']}")
    except Exception as exc:
        return _fail(job, exc)


def process_one_batch(limit: int = 1) -> dict[str, Any] | None:
    results = []
    for _ in range(max(1, limit)):
        result = process_one_job()
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
