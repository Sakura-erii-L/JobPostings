from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from .auth import authenticate_password, create_session, initial_admin_password_required, local_bootstrap_allowed, otp_login_enabled, public_user, request_code, require_admin, require_scope, require_user, set_initial_admin_password, set_user_password, verify_code
from .backups import WebDAVClient, _backup_credentials, create_backup, list_backups, validate_remote_backup
from .catalog import COMPANY_OVERRIDE_COLUMNS, INDUSTRIES, CompanyManagementConflict, CompanyManagementNotFound, CompanyManagementValidationError, _parse_reliable_datetime, apply_company_overrides, company_management_impact, company_overrides, queue_company_management, recruitment_event_sort_key, recruitment_event_state, refresh_expiration
from .config import config
from .db import all_rows, connect, init_db, one, utc_now
from .events import events
from .exports import export_jobs
from .company_research import ensure_company_research_jobs
from .maintenance import repair_event_company_assignments, repair_source_urls, repair_tracememo_file_attachments, reset_recruitment_data
from .local_storage import clear_cache, clear_chat_records, delete_local_database_backup, storage_snapshot
from .parsers import is_file_message, is_image_message, parse_message_time
from .processing import attach_artifact, enrich_review_payload, import_file, import_text, import_url, ingest_message, log_processing, process_one_batch, process_one_enrichment, queue_is_running
from .security import SecretVault, hash_password, hash_value, token
from .tracememo import TraceMemoClient, normalize_group, tracememo_filename, tracememo_inline_media, tracememo_local_media, tracememo_media_references
from .tracememo_cache import has_cached_group, load_cached_messages, store_messages


class BootstrapRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class CodeRequest(BaseModel):
    email: EmailStr


class PasswordLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class PasswordSetRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class InitialAdminPasswordRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class VerifyRequest(BaseModel):
    challenge_id: str
    code: str = Field(min_length=6, max_length=6)


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = "member"
    password: str = Field(min_length=8, max_length=128)


class TextImportRequest(BaseModel):
    text: str = Field(min_length=1)
    source_group_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class UrlImportRequest(BaseModel):
    url: str
    source_group_id: str | None = None


class SyncRequest(BaseModel):
    force: bool = False


class CompanyResearchRequest(BaseModel):
    force: bool = False


class GroupSelection(BaseModel):
    groups: list[dict[str, Any]]


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class CompanyUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=300)
    legal_name: str | None = Field(default=None, max_length=300)
    aliases: list[str] | None = Field(default=None, max_length=100)
    summary: str | None = Field(default=None, max_length=100_000)
    primary_industry: str | None = Field(default=None, max_length=80)
    secondary_industries: list[str] | None = Field(default=None, max_length=100)
    website: str | None = Field(default=None, max_length=2_000)
    company_nature: str | None = Field(default=None, max_length=300)
    founded_at: str | None = Field(default=None, max_length=100)
    company_size: str | None = Field(default=None, max_length=300)
    headquarters: str | None = Field(default=None, max_length=500)
    businesses: list[str] | None = Field(default=None, max_length=100)
    highlights: list[str] | None = Field(default=None, max_length=100)
    official_channels: list[str] | None = Field(default=None, max_length=100)


class CompanySelectionRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class CompanyManagementPreviewRequest(CompanySelectionRequest):
    operation: str


class JobStateRequest(BaseModel):
    state: str | None = None
    favorite: bool | None = None


class NoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class FollowRequest(BaseModel):
    followed: bool = True


class ReviewRequest(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class TokenRequest(BaseModel):
    name: str = "Agent Token"
    scopes: list[str] = ["catalog:read"]
    expires_days: int = Field(default=30, ge=1, le=90)


class BackupValidationRequest(BaseModel):
    remote_path: str
    backup_password: str | None = None


class QueueControlRequest(BaseModel):
    action: str


class QueueTextRequest(BaseModel):
    text: str = Field(min_length=1)


class QueueBulkRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)


class TraceMemoMessageImportRequest(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=200)


background_tasks: set[asyncio.Task[Any]] = set()
_sync_lock = threading.Lock()


async def worker_loop() -> None:
    running: set[asyncio.Task[Any]] = set()
    while True:
        try:
            finished = {task for task in running if task.done()}
            for task in finished:
                running.remove(task)
                result = task.result()
                if result:
                    await events.publish("processing.updated", result)
            if queue_is_running():
                engine = str(_setting_value("processing_engine", "codex") or "codex")
                limit_key = "codex_concurrency" if engine == "codex" else "model_concurrency"
                maximum = max(1, min(4 if engine == "codex" else 8, int(_setting_value(limit_key, 1))))
                while len(running) < maximum:
                    running.add(asyncio.create_task(asyncio.to_thread(process_one_batch, 1, prefer_enrichment=True)))
            if not running:
                await asyncio.to_thread(refresh_expiration)
        except Exception as exc:
            await events.publish("sync.failed", {"error": str(exc)})
        await asyncio.sleep(1 if running else 3)


async def retention_loop() -> None:
    while True:
        try:
            with connect() as connection:
                connection.execute(
                    """DELETE FROM raw_messages
                       WHERE (is_recruitment=0 OR is_recruitment IS NULL) AND retention_until<?
                         AND NOT EXISTS (SELECT 1 FROM processing_jobs p WHERE p.raw_message_id=raw_messages.id)""",
                    (utc_now(),),
                )
                connection.execute(
                    "DELETE FROM artifacts WHERE raw_message_id NOT IN (SELECT id FROM raw_messages)"
                )
                days = max(1, int(_setting_value("processing_log_retention_days", 30)))
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
                connection.execute("DELETE FROM processing_logs WHERE created_at<?", (cutoff,))
        except Exception:
            pass
        await asyncio.sleep(3600)


async def notification_loop() -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            rows = all_rows(
                "SELECT s.user_id,j.id,j.canonical_title,j.explicit_deadline,c.display_name FROM user_job_states s JOIN jobs j ON j.id=s.job_id JOIN companies c ON c.id=j.company_id WHERE s.favorite=1 AND j.explicit_deadline IS NOT NULL",
            )
            with connect() as connection:
                for row in rows:
                    day = str(row["explicit_deadline"])
                    deadline = _parse_reliable_datetime(day)
                    if deadline is None:
                        continue
                    days_left = (deadline.date() - now.date()).days
                    if days_left < 0 or days_left > 7:
                        continue
                    if days_left not in {7, 3, 1}:
                        continue
                    kind = f"deadline_d{days_left}"
                    exists = connection.execute("SELECT id FROM notifications WHERE user_id=? AND kind=? AND body LIKE ?", (row["user_id"], kind, f"%{row['id']}%" )).fetchone()
                    if not exists:
                        connection.execute("INSERT INTO notifications(id,user_id,kind,title,body,created_at) VALUES(?,?,?,?,?,?)", (str(uuid4()), row["user_id"], kind, f"收藏岗位将在 D-{days_left} 截止", f"{row['canonical_title']}（{row['id']}）截止日期：{day}。", utc_now()))
        except Exception:
            pass
        await asyncio.sleep(3600)


async def usage_warning_loop() -> None:
    while True:
        try:
            from .model_provider import create_usage_warning_notifications

            await asyncio.to_thread(create_usage_warning_notifications)
        except Exception:
            pass
        await asyncio.sleep(30)


async def auto_backup_loop() -> None:
    last_run_day = ""
    while True:
        try:
            backup = _setting_value("backup", {}) or {}
            schedule = str(backup.get("schedule", "02:00"))
            local_now = datetime.now()
            day_key = local_now.strftime("%Y-%m-%d")
            try:
                scheduled_at = datetime.strptime(schedule, "%H:%M").replace(
                    year=local_now.year, month=local_now.month, day=local_now.day
                )
            except ValueError:
                raise RuntimeError("Backup schedule must use HH:MM")
            if backup.get("enabled") and last_run_day != day_key:
                latest = one("SELECT created_at FROM backups WHERE status='succeeded' ORDER BY created_at DESC LIMIT 1")
                if latest:
                    try:
                        if datetime.fromisoformat(latest["created_at"]).astimezone().date().isoformat() == day_key:
                            last_run_day = day_key
                    except ValueError:
                        pass
            if backup.get("enabled") and local_now >= scheduled_at and last_run_day != day_key:
                last_run_day = day_key
                result = await asyncio.to_thread(create_backup)
                await events.publish("backup.completed", result)
        except Exception as exc:
            await events.publish("backup.failed", {"error": str(exc)})
        await asyncio.sleep(30)


def _setting_value(key: str, default: Any) -> Any:
    row = one("SELECT value_json FROM system_settings WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except json.JSONDecodeError:
        return default


def _import_days() -> int:
    value = _setting_value("import_days", None)
    if value is None:
        value = _setting_value("initial_import_days", 30)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 30


def _sync_cursor_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _tracememo_content_data(message: dict[str, Any]) -> dict[str, Any]:
    content_data = message.get("contentData") or message.get("content_data")
    if isinstance(content_data, dict):
        return content_data
    if isinstance(content_data, str):
        try:
            parsed = json.loads(content_data)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tracememo_message_text(message: dict[str, Any]) -> str:
    content_data = _tracememo_content_data(message)
    values = (
        message.get("text"),
        message.get("content"),
        message.get("title"),
        content_data.get("title"),
        content_data.get("description"),
        content_data.get("des"),
        message.get("url"),
        content_data.get("url"),
    )
    unique_values: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in unique_values:
            unique_values.append(text)
    return "\n".join(unique_values)


def _tracememo_message_sender(message: dict[str, Any]) -> str:
    return str(message.get("sender") or message.get("talker") or message.get("name") or "").strip()


def _tracememo_media_id(message: dict[str, Any]) -> str:
    references = tracememo_media_references(message)
    return references[0] if references else ""


def _attach_tracememo_media(client: TraceMemoClient, message: dict[str, Any], raw_id: str, stats: dict[str, int]) -> None:
    message_type = str(message.get("type") or message.get("msgType") or "").lower()
    references = tracememo_media_references(message)
    inline_media = tracememo_inline_media(message) or tracememo_local_media(message)
    if not (references or inline_media) or not (is_image_message(message_type, message) or is_file_message(message_type, message)):
        return
    inline_error: Exception | None = None
    if inline_media:
        try:
            media, filename, mime_type = inline_media
            attach_artifact(raw_id, filename or tracememo_filename(message), media, mime_type)
            stats["media_attached"] = stats.get("media_attached", 0) + 1
            return
        except Exception as exc:
            inline_error = exc
    last_error: Exception | None = None
    for reference in references:
        try:
            media, suggested_name = client.media(reference)
            filename = tracememo_filename(message, suggested_name)
            mime_type = message.get("mime_type") or message.get("mimeType")
            content_data = _tracememo_content_data(message)
            mime_type = mime_type or content_data.get("mime_type") or content_data.get("mimeType")
            attach_artifact(raw_id, filename, media, mime_type)
            stats["media_attached"] = stats.get("media_attached", 0) + 1
            return
        except Exception as exc:
            last_error = exc
    stats["media_failed"] = stats.get("media_failed", 0) + 1
    processing_job = one("SELECT id FROM processing_jobs WHERE raw_message_id=? AND kind='classify'", (raw_id,))
    if processing_job and (last_error or inline_error):
        log_processing(
            processing_job["id"],
            "extracting",
            "TraceMemo 媒体下载失败，后续将保留原消息并尝试其他提取方式",
            "warning",
            {"media_references_attempted": len(references), "error": str(last_error or inline_error)},
        )


def sync_tracememo_once(force: bool = False, incremental: bool = False) -> dict[str, Any]:
    with _sync_lock:
        return _sync_tracememo_once(force, incremental)


def _sync_tracememo_once(force: bool = False, incremental: bool = False) -> dict[str, Any]:
    row = one("SELECT * FROM connectors WHERE kind='tracememo' AND enabled=1")
    if not row:
        return {"status": "disabled", "fetched": 0, "groups": 0}
    settings = json.loads(row["config_json"])
    client = TraceMemoClient(row["base_url"], SecretVault().decrypt(settings["token"]) if settings.get("token") else "")
    groups = all_rows("SELECT * FROM source_groups WHERE connector_id=? AND selected=1 AND enabled=1", (row["id"],))
    if not groups:
        return {
            "status": "no_groups",
            "fetched": 0,
            "groups": 0,
            "message": "没有已选中的微信群，请先读取并保存招聘群选择",
        }
    reset_result = reset_recruitment_data() if force else None
    fetched = 0
    remote_fetches = 0
    cached_groups = 0
    cached_messages = 0
    import_days = _import_days()
    ingest_stats: dict[str, int] = {"created": 0, "updated": 0, "duplicates": 0, "recognized_skipped": 0, "ignored": 0, "filtered_system": 0, "media_attached": 0, "media_failed": 0, "outside_window": 0, "missing_source_time": 0}
    for group in groups:
        end = datetime.now(timezone.utc)
        cursor = one("SELECT cursor_time FROM sync_cursors WHERE source_group_id=?", (group["id"],))
        cursor_time = _sync_cursor_datetime(cursor["cursor_time"] if cursor else None)
        if incremental and not force and cursor_time and cursor_time <= end:
            start = cursor_time - timedelta(minutes=1)
        else:
            start = end - timedelta(days=import_days)
        use_cache = not force and not incremental and has_cached_group(row["id"], group["id"])
        if use_cache:
            messages = load_cached_messages(row["id"], group["id"])
            cached_groups += 1
            cached_messages += len(messages)
        else:
            messages = client.messages(group["external_id"], start, end)
            remote_fetches += 1
            store_messages(row["id"], group["id"], messages, start, end)
        for message in messages:
            if not isinstance(message, dict):
                ingest_stats["ignored"] += 1
                continue
            fetched += 1
            source_time = parse_message_time(message)
            if not source_time:
                ingest_stats["missing_source_time"] += 1
                continue
            try:
                message_time = datetime.fromisoformat(source_time)
            except ValueError:
                ingest_stats["missing_source_time"] += 1
                continue
            if message_time < start or message_time > end:
                ingest_stats["outside_window"] += 1
                continue
            if force:
                continue
            raw_id = ingest_message(message, row["id"], group["id"], ingest_stats)
            if raw_id:
                _attach_tracememo_media(client, message, raw_id, ingest_stats)
        if not use_cache:
            with connect() as connection:
                connection.execute("INSERT OR REPLACE INTO sync_cursors(source_group_id,cursor_time,cursor_message_id,updated_at) VALUES(?,?,?,?)", (group["id"], end.isoformat(), None, utc_now()))
    return {
        "status": "completed",
        "fetched": fetched,
        "added": ingest_stats["created"] + ingest_stats["updated"],
        "created": ingest_stats["created"],
        "updated": ingest_stats["updated"],
        "duplicates": ingest_stats["duplicates"],
        "recognized_skipped": ingest_stats["recognized_skipped"],
        "ignored": ingest_stats["ignored"],
        "filtered_system": ingest_stats["filtered_system"],
        "media_attached": ingest_stats["media_attached"],
        "media_failed": ingest_stats["media_failed"],
        "outside_window": ingest_stats["outside_window"],
        "missing_source_time": ingest_stats["missing_source_time"],
        "import_days": import_days,
        "force": force,
        "incremental": incremental,
        "cache_mode": "mixed" if remote_fetches and cached_groups else ("tracememo" if remote_fetches else "cache"),
        "remote_fetches": remote_fetches,
        "cached_groups": cached_groups,
        "cached_messages": cached_messages,
        "reset": reset_result,
        "manual_import_pending": force,
        "groups": len(groups),
    }


def tracememo_messages(days: int = 0, limit: int = 100, query: str = "", _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """List recent messages from the currently selected TraceMemo groups without importing them."""
    connector = one("SELECT * FROM connectors WHERE kind='tracememo'")
    if not connector:
        raise HTTPException(400, "TraceMemo is not configured")
    groups = all_rows(
        "SELECT id,external_id,name FROM source_groups WHERE connector_id=? AND selected=1 AND enabled=1 ORDER BY name COLLATE NOCASE",
        (connector["id"],),
    )
    if not groups:
        raise HTTPException(400, "没有已勾选的招聘群，请先在系统设置中保存群聊选择")
    days = max(1, min(days or _import_days(), 90))
    limit = max(1, min(limit, 200))
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    state_groups = {
        row["source_group_id"]
        for row in all_rows(
            "SELECT source_group_id FROM tracememo_cache_state WHERE connector_id=?",
            (connector["id"],),
        )
    }
    if any(group["id"] not in state_groups for group in groups):
        if not connector["enabled"]:
            raise HTTPException(400, "TraceMemo 未启用，且当前勾选群聊没有可用缓存")
        settings = json.loads(connector["config_json"] or "{}")
        client = TraceMemoClient(connector["base_url"], SecretVault().decrypt(settings["token"]) if settings.get("token") else "")
        for group in groups:
            if group["id"] in state_groups:
                continue
            messages = client.messages(group["external_id"], start, now)
            store_messages(connector["id"], group["id"], messages, start, now)

    group_ids = [group["id"] for group in groups]
    placeholders = ",".join("?" for _ in group_ids)
    rows = all_rows(
        f"""SELECT cache.id,cache.source_group_id,cache.external_message_id,cache.source_time,cache.message_json,
                   groups.name AS group_name,groups.external_id AS group_external_id,
                   EXISTS(SELECT 1 FROM raw_messages raw
                          WHERE raw.connector_id=cache.connector_id
                            AND raw.source_group_id=cache.source_group_id
                            AND raw.external_message_id=cache.external_message_id
                            AND COALESCE(raw.recognition_status,'') <> 'canceled') AS imported
            FROM tracememo_message_cache cache
            JOIN source_groups groups ON groups.id=cache.source_group_id
            WHERE cache.connector_id=? AND cache.source_group_id IN ({placeholders})
              AND (cache.source_time IS NULL OR (cache.source_time>=? AND cache.source_time<=?))
            ORDER BY CASE WHEN cache.source_time IS NULL THEN 1 ELSE 0 END, cache.source_time DESC, cache.id DESC""",
        (connector["id"], *group_ids, start.isoformat(), now.isoformat()),
    )
    needle = query.strip().casefold()
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            message = json.loads(row["message_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict):
            continue
        text = _tracememo_message_text(message)
        sender = _tracememo_message_sender(message)
        if needle and needle not in " ".join((row["group_name"], row["group_external_id"], sender, text)).casefold():
            continue
        items.append(
            {
                "id": row["id"],
                "external_message_id": row["external_message_id"],
                "source_group_id": row["source_group_id"],
                "group_name": row["group_name"],
                "sent_at": row["source_time"] or parse_message_time(message),
                "sender": sender,
                "message_type": str(message.get("type") or message.get("msgType") or "text"),
                "text_preview": text[:300],
                "imported": bool(row["imported"]),
            }
        )
    return {"days": days, "start_at": start.isoformat(), "end_at": now.isoformat(), "groups": len(groups), "total": len(items), "items": items[:limit]}


def import_selected_tracememo_messages(body: TraceMemoMessageImportRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    message_ids = list(dict.fromkeys(body.message_ids))
    placeholders = ",".join("?" for _ in message_ids)
    rows = all_rows(
        f"""SELECT cache.id,cache.connector_id,cache.source_group_id,cache.message_json
            FROM tracememo_message_cache cache
            JOIN source_groups groups ON groups.id=cache.source_group_id
            WHERE cache.id IN ({placeholders}) AND groups.selected=1 AND groups.enabled=1""",
        tuple(message_ids),
    )
    if len(rows) != len(message_ids):
        raise HTTPException(400, "只能导入当前已勾选群聊中的消息，请刷新列表后重试")
    connector = one("SELECT * FROM connectors WHERE id=?", (rows[0]["connector_id"],))
    if not connector:
        raise HTTPException(400, "TraceMemo 连接器不存在")
    settings = json.loads(connector["config_json"] or "{}")
    client = TraceMemoClient(connector["base_url"], SecretVault().decrypt(settings["token"]) if settings.get("token") else "")
    stats: dict[str, int] = {"created": 0, "updated": 0, "duplicates": 0, "recognized_skipped": 0, "filtered_system": 0, "media_attached": 0, "media_failed": 0}
    raw_ids: list[str] = []
    for row in rows:
        try:
            message = json.loads(row["message_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, f"缓存消息内容无效：{row['id']}") from exc
        if not isinstance(message, dict):
            raise HTTPException(400, f"缓存消息内容无效：{row['id']}")
        raw_id = ingest_message(message, row["connector_id"], row["source_group_id"], stats)
        if raw_id:
            raw_ids.append(raw_id)
            _attach_tracememo_media(client, message, raw_id, stats)
    return {"status": "queued", "requested": len(message_ids), "raw_message_ids": list(dict.fromkeys(raw_ids)), **stats}


async def auto_sync_loop() -> None:
    while True:
        interval = max(1, int(_setting_value("sync_interval_minutes", 10)))
        await asyncio.sleep(interval * 60)
        try:
            await events.publish("sync.started", {"interval_minutes": interval})
            result = await asyncio.to_thread(sync_tracememo_once, False, True)
            if result.get("status") == "completed":
                await events.publish("sync.completed", result)
        except Exception as exc:
            await events.publish("sync.failed", {"error": str(exc)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    repair_source_urls()
    try:
        repair_tracememo_file_attachments()
    except Exception as exc:
        print(f"TraceMemo 历史附件修复失败: {exc}", file=sys.stderr)
    ensure_company_research_jobs()
    config.ensure_dirs()
    SecretVault()
    with connect() as connection:
        connection.execute(
            "UPDATE processing_jobs SET status='pending',stage='queued',lease_until=NULL,processor=NULL,result_json=NULL,started_at=NULL,finished_at=NULL,updated_at=? WHERE status='running' AND cancel_requested=0",
            (utc_now(),),
        )
        connection.execute(
            """UPDATE raw_messages SET recognition_status='pending',recognized_at=NULL,recognition_error=NULL
               WHERE id IN (SELECT raw_message_id FROM processing_jobs WHERE status='pending' AND kind='classify' AND raw_message_id IS NOT NULL)"""
        )
    task1 = asyncio.create_task(worker_loop())
    task2 = asyncio.create_task(retention_loop())
    task3 = asyncio.create_task(auto_sync_loop())
    task4 = asyncio.create_task(notification_loop())
    task5 = asyncio.create_task(auto_backup_loop())
    task6 = asyncio.create_task(usage_warning_loop())
    background_tasks.update({task1, task2, task3, task4, task5, task6})
    yield
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    background_tasks.clear()


app = FastAPI(title="JobPostings", version="0.1.0", lifespan=lifespan)
app.add_api_route("/api/v1/admin/tracememo/messages", tracememo_messages, methods=["GET"])
app.add_api_route("/api/v1/admin/tracememo/messages/import", import_selected_tracememo_messages, methods=["POST"])


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok", "version": app.version, "port": config.port}
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "error", "error": str(exc)})


@app.post("/api/v1/bootstrap")
def bootstrap(body: BootstrapRequest, request: Request) -> dict[str, Any]:
    if not local_bootstrap_allowed(request):
        raise HTTPException(403, "Bootstrap is only available from the local machine")
    if one("SELECT id FROM users LIMIT 1"):
        raise HTTPException(409, "Bootstrap has already been completed")
    user_id = str(uuid4())
    with connect() as connection:
        connection.execute("INSERT INTO users(id,email,role,password_hash,created_at) VALUES(?,?,?,?,?)", (user_id, str(body.email).lower(), "admin", hash_password(body.password), utc_now()))
    session = create_session(user_id)
    response = JSONResponse({"user": {"id": user_id, "email": str(body.email).lower(), "role": "admin", "password_configured": True}})
    response.set_cookie("jp_session", session, httponly=True, samesite="lax", secure=config.public_base_url.startswith("https://"), max_age=60 * 60 * 24 * 7)
    return response


@app.get("/api/v1/bootstrap/status")
def bootstrap_status() -> dict[str, bool]:
    return {"initialized": bool(one("SELECT id FROM users LIMIT 1"))}


@app.get("/api/v1/auth/options")
def auth_options(request: Request) -> dict[str, bool]:
    return {
        "password_login_enabled": True,
        "otp_login_enabled": otp_login_enabled(),
        "initial_admin_password_required": initial_admin_password_required(),
        "local_password_setup_allowed": local_bootstrap_allowed(request),
    }


@app.post("/api/v1/auth/login")
def auth_login(body: PasswordLoginRequest) -> JSONResponse:
    user, session = authenticate_password(str(body.email), body.password)
    response = JSONResponse({"user": user})
    response.set_cookie("jp_session", session, httponly=True, samesite="lax", secure=config.public_base_url.startswith("https://"), max_age=60 * 60 * 24 * 7)
    return response


@app.post("/api/v1/auth/password")
def auth_set_password(body: PasswordSetRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    set_user_password(user["id"], body.password)
    return {"ok": True, "password_configured": True}


@app.post("/api/v1/auth/initial-password")
def auth_set_initial_password(body: InitialAdminPasswordRequest, request: Request) -> JSONResponse:
    if not local_bootstrap_allowed(request):
        raise HTTPException(status_code=403, detail="管理员初始密码只能在运行服务的本机设置")
    user, session = set_initial_admin_password(str(body.email), body.password)
    response = JSONResponse({"user": user})
    response.set_cookie("jp_session", session, httponly=True, samesite="lax", secure=config.public_base_url.startswith("https://"), max_age=60 * 60 * 24 * 7)
    return response


@app.post("/api/v1/auth/request-code")
def auth_request_code(body: CodeRequest) -> dict[str, Any]:
    return request_code(str(body.email))


@app.post("/api/v1/auth/verify-code")
def auth_verify_code(body: VerifyRequest) -> JSONResponse:
    user, session = verify_code(body.challenge_id, body.code)
    response = JSONResponse({"user": user})
    response.set_cookie("jp_session", session, httponly=True, samesite="lax", secure=config.public_base_url.startswith("https://"), max_age=60 * 60 * 24 * 7)
    return response


@app.post("/api/v1/auth/logout")
def auth_logout(request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, bool]:
    value = request.cookies.get("jp_session")
    if value:
        with connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (hash_value(value),))
    return {"ok": True}


@app.get("/api/v1/auth/me")
def auth_me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"user": public_user(user)}


@app.post("/api/v1/admin/invitations")
def create_invitation(body: InviteRequest, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    invite = token()
    invitation_id = str(uuid4())
    with connect() as connection:
        connection.execute(
            "INSERT INTO invitations(id,email,token_hash,role,password_hash,expires_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (invitation_id, str(body.email).lower(), hash_value(invite), body.role if body.role in {"admin", "member"} else "member", hash_password(body.password), (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat(timespec="seconds"), user["id"], utc_now()),
        )
    from .auth import _send_email

    try:
        _send_email(
            str(body.email).lower(),
            "JobPostings 邀请",
            f"你已被邀请使用 JobPostings。请使用此邮箱和管理员提供的初始密码登录：{str(body.email).lower()}\n邀请有效期 72 小时。",
        )
    except Exception:
        pass
    return {"id": invitation_id, "email": str(body.email).lower(), "expires_in_hours": 72}


@app.get("/api/v1/admin/invitations")
def list_invitations(_: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    return [dict(row) for row in all_rows("SELECT id,email,role,expires_at,used_at,created_at FROM invitations ORDER BY created_at DESC")]


@app.get("/api/v1/admin/settings")
def get_settings(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for row in all_rows("SELECT key,value_json FROM system_settings"):
        value = json.loads(row["value_json"])
        if row["key"] == "llm_provider":
            value.pop("api_key_enc", None)
            value["api_key_configured"] = bool(value.get("api_key_configured"))
        if row["key"] == "smtp":
            value.pop("password_enc", None)
            value["password_configured"] = bool(value.get("password_configured"))
        if row["key"] == "backup":
            value.pop("password_enc", None)
            value.pop("backup_password_enc", None)
            value["webdav_password_configured"] = bool(value.get("webdav_password_configured"))
            value["backup_password_configured"] = bool(value.get("backup_password_configured"))
        result[row["key"]] = value
    return result


@app.put("/api/v1/admin/settings")
def update_settings(body: SettingsUpdate, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    allowed = {
        "sync_interval_minutes", "initial_import_days", "import_days", "redaction_enabled", "local_ocr_fallback_enabled", "llm_input_budget",
        "llm_output_budget", "llm_budget_warning_percent", "ordinary_retention_days",
        "possibly_expired_days", "smtp", "llm_provider", "search", "backup", "agent_api_enabled",
        "processing_engine", "model_concurrency", "codex_concurrency", "extract_concurrency",
        "processing_log_retention_days", "otp_login_enabled",
    }
    vault = SecretVault()
    with connect() as connection:
        for key, value in body.values.items():
            if key not in allowed:
                continue
            if key == "llm_provider":
                value = dict(value)
                if value.get("api_key"):
                    value["api_key_enc"] = vault.encrypt(str(value.pop("api_key")))
                    value["api_key_configured"] = True
                else:
                    old = connection.execute("SELECT value_json FROM system_settings WHERE key=?", (key,)).fetchone()
                    if old:
                        old_value = json.loads(old["value_json"])
                        value["api_key_enc"] = old_value.get("api_key_enc", "")
                        value["api_key_configured"] = bool(old_value.get("api_key_enc"))
            elif key == "smtp":
                value = dict(value)
                if value.get("password"):
                    value["password_enc"] = vault.encrypt(str(value.pop("password")))
                    value["password_configured"] = True
                else:
                    old = connection.execute("SELECT value_json FROM system_settings WHERE key=?", (key,)).fetchone()
                    if old:
                        old_value = json.loads(old["value_json"])
                        value["password_enc"] = old_value.get("password_enc", "")
                        value["password_configured"] = bool(old_value.get("password_enc"))
            elif key == "backup":
                value = dict(value)
                if value.get("webdav_password"):
                    value["password_enc"] = vault.encrypt(str(value.pop("webdav_password")))
                    value["webdav_password_configured"] = True
                if value.get("backup_password"):
                    value["backup_password_enc"] = vault.encrypt(str(value.pop("backup_password")))
                    value["backup_password_configured"] = True
                old = connection.execute("SELECT value_json FROM system_settings WHERE key=?", (key,)).fetchone()
                if old:
                    old_value = json.loads(old["value_json"])
                    value.setdefault("password_enc", old_value.get("password_enc", ""))
                    value.setdefault("backup_password_enc", old_value.get("backup_password_enc", ""))
                    value.setdefault("webdav_password_configured", bool(old_value.get("password_enc")))
                    value.setdefault("backup_password_configured", bool(old_value.get("backup_password_enc")))
            value_json = json.dumps(value, ensure_ascii=False)
            connection.execute("INSERT OR REPLACE INTO system_settings(key,value_json,updated_at) VALUES(?,?,?)", (key, value_json, utc_now()))
            if key in {"initial_import_days", "import_days"}:
                alias = "import_days" if key == "initial_import_days" else "initial_import_days"
                connection.execute("INSERT OR REPLACE INTO system_settings(key,value_json,updated_at) VALUES(?,?,?)", (alias, value_json, utc_now()))
    return get_settings(_)


@app.post("/api/v1/admin/models/test")
def test_model(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    from .model_provider import test_provider_connection

    try:
        return test_provider_connection()
    except Exception as exc:
        raise HTTPException(502, f"Model connection test failed: {exc}") from exc


@app.get("/api/v1/admin/connectors")
def list_connectors(_: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    return [dict(row) for row in all_rows("SELECT id,kind,base_url,enabled,updated_at FROM connectors ORDER BY kind")]


@app.put("/api/v1/admin/connectors/tracememo")
def update_tracememo(body: dict[str, Any], _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    base_url = body.get("base_url") or "http://127.0.0.1:6131/api/v1"
    connector_id = one("SELECT id FROM connectors WHERE kind='tracememo'")
    cid = connector_id["id"] if connector_id else str(uuid4())
    vault = SecretVault()
    previous_config: dict[str, Any] = {}
    if connector_id:
        previous = one("SELECT config_json FROM connectors WHERE id=?", (cid,))
        if previous:
            try:
                previous_config = json.loads(previous["config_json"] or "{}")
            except json.JSONDecodeError:
                previous_config = {}
    safe_config = {"token": vault.encrypt(str(body["token"])) if body.get("token") else previous_config.get("token", "")}
    with connect() as connection:
        if connector_id:
            connection.execute(
                "UPDATE connectors SET base_url=?,enabled=?,config_json=?,updated_at=? WHERE id=?",
                (base_url, int(bool(body.get("enabled", True))), json.dumps(safe_config), utc_now(), cid),
            )
        else:
            connection.execute(
                "INSERT INTO connectors(id,kind,base_url,enabled,config_json,updated_at) VALUES(?,?,?,?,?,?)",
                (cid, "tracememo", base_url, int(bool(body.get("enabled", True))), json.dumps(safe_config), utc_now()),
            )
    return {"id": cid, "kind": "tracememo", "base_url": base_url, "enabled": bool(body.get("enabled", True))}


@app.post("/api/v1/admin/connectors/tracememo/test")
def test_tracememo(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    row = one("SELECT * FROM connectors WHERE kind='tracememo'")
    if not row:
        raise HTTPException(400, "TraceMemo is not configured")
    settings = json.loads(row["config_json"])
    token_value = SecretVault().decrypt(settings["token"]) if settings.get("token") else ""
    try:
        return {"ok": True, "health": TraceMemoClient(row["base_url"], token_value).health()}
    except Exception as exc:
        raise HTTPException(502, f"TraceMemo health check failed: {exc}") from exc


@app.get("/api/v1/admin/connectors/tracememo/groups")
def tracememo_groups(_: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    row = one("SELECT * FROM connectors WHERE kind='tracememo'")
    if not row:
        raise HTTPException(400, "TraceMemo is not configured")
    settings = json.loads(row["config_json"])
    saved_groups = all_rows(
        "SELECT id,external_id,name,selected,enabled FROM source_groups WHERE connector_id=? ORDER BY name COLLATE NOCASE",
        (row["id"],),
    )

    def saved_group_payload() -> list[dict[str, Any]]:
        return [
            {
                "id": group["id"],
                "external_id": group["external_id"],
                "name": group["name"],
                "avatar": None,
                "selected": bool(group["selected"]),
                "enabled": bool(group["enabled"]),
            }
            for group in saved_groups
        ]

    client = TraceMemoClient(row["base_url"], SecretVault().decrypt(settings["token"]) if settings.get("token") else "")
    try:
        groups = client.groups()
    except Exception as exc:
        if saved_groups:
            return saved_group_payload()
        raise HTTPException(502, f"TraceMemo group query failed: {exc}") from exc
    if not groups and saved_groups:
        return saved_group_payload()
    result = []
    with connect() as connection:
        for group in groups:
            if not isinstance(group, dict):
                continue
            normalized = normalize_group(group)
            external_id = normalized["external_id"]
            if not external_id:
                continue
            name = normalized["name"] or external_id
            existing = connection.execute("SELECT id,selected,enabled FROM source_groups WHERE connector_id=? AND external_id=?", (row["id"], external_id)).fetchone()
            group_id = existing["id"] if existing else str(uuid4())
            if not existing:
                connection.execute("INSERT INTO source_groups(id,connector_id,external_id,name,created_at,updated_at) VALUES(?,?,?,?,?,?)", (group_id, row["id"], external_id, name, utc_now(), utc_now()))
            else:
                connection.execute("UPDATE source_groups SET name=?,updated_at=? WHERE id=?", (name, utc_now(), group_id))
            result.append({"id": group_id, "external_id": external_id, "name": name, "avatar": normalized["avatar"], "selected": bool(existing["selected"]) if existing else False, "enabled": bool(existing["enabled"]) if existing else True})
        connection.execute("UPDATE source_groups SET selected=0,enabled=0,updated_at=? WHERE connector_id=? AND TRIM(external_id)=''", (utc_now(), row["id"]))
    return result


@app.put("/api/v1/admin/source-groups")
def select_groups(body: GroupSelection, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    selected_count = sum(1 for group in body.groups if group.get("selected"))
    if selected_count > 20:
        raise HTTPException(400, "At most 20 recruitment groups can be selected")
    with connect() as connection:
        for group in body.groups:
            connection.execute("UPDATE source_groups SET selected=?,enabled=?,updated_at=? WHERE id=?", (int(bool(group.get("selected", False))), int(bool(group.get("enabled", True))), utc_now(), group["id"]))
    return {"ok": True}


@app.post("/api/v1/admin/sync")
async def manual_sync(body: SyncRequest | None = None, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    result = await asyncio.to_thread(sync_tracememo_once, bool(body and body.force))
    if result.get("status") == "disabled":
        raise HTTPException(400, "Enabled TraceMemo connector not found")
    if result.get("status") == "no_groups":
        raise HTTPException(400, result["message"])
    await events.publish("sync.completed", result)
    return result


@app.post("/api/v1/admin/company-research")
def trigger_company_research(body: CompanyResearchRequest | None = None, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    result = ensure_company_research_jobs(bool(body and body.force))
    return {"status": "queued", **result}


@app.post("/api/v1/admin/maintenance/event-companies")
def repair_event_companies(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Explicitly repair historical event ownership after reviewing the scope."""
    return {"status": "repaired", "events_reassigned": repair_event_company_assignments()}


@app.post("/api/v1/admin/maintenance/tracememo-files")
def repair_tracememo_files(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Retry missing or unreadable TraceMemo file attachments."""
    return repair_tracememo_file_attachments()


_QUEUE_CHILD_KINDS_SQL = "'consolidate_company','research_company'"


def _queue_original_text(current_text: Any, metadata_json: Any) -> str:
    try:
        metadata = json.loads(metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    candidates = [
        metadata.get("_original_text_content") if isinstance(metadata, dict) else None,
        current_text,
        metadata.get("_parsed_text_content") if isinstance(metadata, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


@app.get("/api/v1/admin/processing-queue")
def processing_queue(status: str | None = None, limit: int = 100, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    allowed_statuses = {"pending", "running", "succeeded", "needs_review", "paused_quota", "failed", "canceled"}
    if status and status not in allowed_statuses:
        raise HTTPException(400, f"Unknown processing status: {status}")
    limit = min(max(limit, 1), 200)
    query_params: list[Any] = []
    where = "WHERE p.status <> 'canceled'"
    if status:
        where = "WHERE p.status=?"
        query_params.append(status)
    legacy_parent_expression = """COALESCE(
        p.parent_job_id,
        (SELECT parent.id
           FROM processing_jobs parent
          WHERE parent.kind='classify'
            AND parent.raw_message_id=p.raw_message_id
          ORDER BY parent.created_at DESC,parent.id DESC
          LIMIT 1),
        (SELECT parent.id
           FROM evidences evidence
           JOIN processing_jobs parent
             ON parent.kind='classify' AND parent.raw_message_id=evidence.raw_message_id
          WHERE evidence.company_id=p.company_id
            AND evidence.raw_message_id IS NOT NULL
          ORDER BY evidence.observed_at DESC,parent.created_at DESC,parent.id DESC
          LIMIT 1)
    )"""
    resolved_parent_expression = f"CASE WHEN p.kind IN ({_QUEUE_CHILD_KINDS_SQL}) THEN {legacy_parent_expression} ELSE NULL END"
    root_expression = f"CASE WHEN p.kind IN ({_QUEUE_CHILD_KINDS_SQL}) THEN {legacy_parent_expression} ELSE p.id END"
    root_rows = all_rows(
        f"""
        SELECT {root_expression} AS root_id,MAX(p.updated_at) AS last_updated_at,MAX(p.created_at) AS last_created_at
        FROM processing_jobs p
        {where}
        GROUP BY {root_expression}
        HAVING {root_expression} IS NOT NULL
        ORDER BY last_updated_at DESC,last_created_at DESC,root_id DESC
        LIMIT ?
        """,
        tuple([*query_params, limit]),
    )
    total_row = one(
        f"""
        SELECT COUNT(*) AS count FROM (
            SELECT {root_expression} AS root_id
            FROM processing_jobs p
            {where}
            GROUP BY {root_expression}
            HAVING {root_expression} IS NOT NULL
        ) roots
        """,
        tuple(query_params),
    )
    job_total_row = one(f"SELECT COUNT(*) AS count FROM processing_jobs p {where}", tuple(query_params))
    root_ids = [row["root_id"] for row in root_rows if row["root_id"]]
    rows = []
    if root_ids:
        placeholders = ",".join("?" for _ in root_ids)
        visible_rows_where = f"WHERE {root_expression} IN ({placeholders})"
        rows = all_rows(
            f"""
            SELECT p.id,p.kind,p.raw_message_id,p.company_id,p.payload_json,p.parent_job_id,{resolved_parent_expression} AS resolved_parent_job_id,p.status,p.stage,p.attempts,p.lease_until,p.next_attempt_at,
                   p.cancel_requested,p.processor,p.error,p.result_json,p.created_at,p.updated_at,p.started_at,p.finished_at,
                   r.connector_id,r.source_group_id,r.message_type,r.sender,r.sent_at,r.recognition_status,r.recognized_at,r.recognition_error,
                   r.text_content,r.metadata_json,
                   substr(COALESCE(r.text_content,''),1,240) AS text_preview,
                   sg.name AS source_group_name
            FROM processing_jobs p
            LEFT JOIN raw_messages r ON r.id=p.raw_message_id
            LEFT JOIN source_groups sg ON sg.id=r.source_group_id
            {visible_rows_where}
            ORDER BY p.updated_at DESC,p.created_at DESC,p.id DESC
            """,
            tuple(root_ids),
        )
    stats = {name: 0 for name in allowed_statuses}
    for row in all_rows("SELECT status,COUNT(*) AS count FROM processing_jobs GROUP BY status"):
        stats[row["status"]] = row["count"]
    def empty_stats() -> dict[str, int]:
        return {name: 0 for name in allowed_statuses}

    stats_by_kind: dict[str, dict[str, int]] = {}
    for row in all_rows("SELECT kind,status,COUNT(*) AS count FROM processing_jobs GROUP BY kind,status"):
        stats_by_kind.setdefault(row["kind"], empty_stats())[row["status"]] = row["count"]
    source_recognition = stats_by_kind.get("classify", empty_stats())
    background_by_kind = {
        kind: values for kind, values in stats_by_kind.items() if kind != "classify"
    }
    background_tasks = empty_stats()
    for values in background_by_kind.values():
        for name in allowed_statuses:
            background_tasks[name] += values.get(name, 0)
    control = one("SELECT state,updated_at FROM queue_control WHERE id=1")
    raw_items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        current_text = item.pop("text_content", None)
        metadata_json = item.pop("metadata_json", None)
        resolved_parent_job_id = item.pop("resolved_parent_job_id", None)
        if item["kind"] in {"consolidate_company", "research_company"} and not item.get("parent_job_id"):
            item["parent_job_id"] = resolved_parent_job_id
        task_payload_json = item.pop("payload_json", None)
        if task_payload_json:
            try:
                task_payload = json.loads(task_payload_json)
            except (TypeError, json.JSONDecodeError):
                task_payload = None
            item["task_payload"] = task_payload if isinstance(task_payload, dict) else None
            if isinstance(task_payload, dict) and item["kind"] in {"merge_company", "delete_company"}:
                names = [str(value) for value in task_payload.get("company_names") or [] if str(value).strip()]
                if item["kind"] == "merge_company":
                    item["text_preview"] = f"主企业：{task_payload.get('primary_company_name') or (names[0] if names else '—')}；待合并：{'、'.join(names[1:]) or '—'}"
                else:
                    item["text_preview"] = f"待删除企业：{'、'.join(names) or '—'}"
        else:
            item["task_payload"] = None
        item["original_text"] = _queue_original_text(current_text, metadata_json) if item.get("raw_message_id") else ""
        raw_result = item.get("result_json")
        if raw_result:
            try:
                item["result"] = json.loads(raw_result)
            except (TypeError, json.JSONDecodeError):
                item["result"] = None
        else:
            item["result"] = None
        raw_items.append(item)
    items_by_id = {item["id"]: item for item in raw_items}
    child_by_parent: dict[str, list[dict[str, Any]]] = {}
    for item in raw_items:
        if item["kind"] not in {"consolidate_company", "research_company"}:
            continue
        parent_id = item.get("parent_job_id")
        parent = items_by_id.get(parent_id) if parent_id else None
        if parent and parent["kind"] == "classify":
            child_by_parent.setdefault(parent_id, []).append(item)
    items: list[dict[str, Any]] = []
    for root_id in root_ids:
        item = items_by_id.get(root_id)
        if not item or item["kind"] in {"consolidate_company", "research_company"}:
            continue
        subtasks = child_by_parent.get(item["id"], [])
        if subtasks:
            item["subtasks"] = sorted(subtasks, key=lambda child: (child.get("created_at") or "", child["id"]))
        else:
            item["subtasks"] = []
        items.append(item)
    return {
        "state": control["state"] if control else "paused",
        "state_updated_at": control["updated_at"] if control else None,
        "stats": stats,
        "stats_by_kind": stats_by_kind,
        "source_recognition": source_recognition,
        "background_tasks": background_tasks,
        "background_by_kind": background_by_kind,
        "items": items,
        "total": total_row["count"] if total_row else 0,
        "job_total": job_total_row["count"] if job_total_row else 0,
    }


@app.post("/api/v1/admin/processing-queue/control")
def control_processing_queue(body: QueueControlRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if body.action not in {"run", "pause", "cancel_all"}:
        raise HTTPException(400, "action must be run, pause or cancel_all")
    state = "running" if body.action == "run" else "paused"
    with connect() as connection:
        connection.execute("UPDATE queue_control SET state=?,updated_at=? WHERE id=1", (state, utc_now()))
        running_ids: list[str] = []
        canceled_ids: list[str] = []
        if body.action == "cancel_all":
            running_ids = [row["id"] for row in connection.execute("SELECT id FROM processing_jobs WHERE status='running'").fetchall()]
            canceled_ids = [row["id"] for row in connection.execute("SELECT id FROM processing_jobs WHERE status IN ('pending','running','needs_review','paused_quota','failed')").fetchall()]
            connection.execute(
                "UPDATE processing_jobs SET status='canceled',stage='canceled',cancel_requested=1,lease_until=NULL,finished_at=?,updated_at=? WHERE status IN ('pending','running','needs_review','paused_quota','failed')",
                (utc_now(), utc_now()),
            )
            if canceled_ids:
                connection.execute(
                    """UPDATE raw_messages SET recognition_status='canceled',recognized_at=NULL,recognition_error=NULL
                       WHERE id IN (
                           SELECT raw_message_id FROM processing_jobs
                           WHERE id IN ({}) AND kind='classify' AND raw_message_id IS NOT NULL
                       )""".format(",".join("?" for _ in canceled_ids)),
                    tuple(canceled_ids),
                )
    if body.action == "cancel_all":
        from .codex_agent import cancel_codex_job

        for job_id in canceled_ids:
            log_processing(job_id, "canceled", "管理员取消了任务", "warning")
        for job_id in running_ids:
            cancel_codex_job(job_id)
    return {"state": state, "action": body.action, "canceled": len(canceled_ids) if body.action == "cancel_all" else 0}


@app.post("/api/v1/admin/processing-queue/cancel")
def cancel_processing_jobs(body: QueueBulkRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    ids = list(dict.fromkeys(body.ids))
    placeholders = ",".join("?" for _ in ids)
    with connect() as connection:
        rows = connection.execute(
            f"SELECT id,status FROM processing_jobs WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
        cancellable = [row["id"] for row in rows if row["status"] not in {"succeeded", "canceled"}]
        running_ids = [row["id"] for row in rows if row["status"] == "running"]
        if cancellable:
            cancellable_placeholders = ",".join("?" for _ in cancellable)
            connection.execute(
                f"UPDATE processing_jobs SET status='canceled',stage='canceled',cancel_requested=1,lease_until=NULL,finished_at=?,updated_at=? WHERE id IN ({cancellable_placeholders})",
                (utc_now(), utc_now(), *cancellable),
            )
            connection.execute(
                f"""UPDATE raw_messages SET recognition_status='canceled',recognized_at=NULL,recognition_error=NULL
                   WHERE id IN (
                       SELECT raw_message_id FROM processing_jobs
                       WHERE id IN ({cancellable_placeholders}) AND kind='classify' AND raw_message_id IS NOT NULL
                   )""",
                tuple(cancellable),
            )
    from .codex_agent import cancel_codex_job

    for job_id in cancellable:
        log_processing(job_id, "canceled", "管理员批量取消了任务", "warning")
    for job_id in running_ids:
        cancel_codex_job(job_id)
    return {"canceled": len(cancellable), "ids": cancellable}


@app.post("/api/v1/admin/processing-queue/{job_id}/cancel")
def cancel_processing_job(job_id: str, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    job = one("SELECT id,status FROM processing_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "Processing job not found")
    if job["status"] in {"succeeded", "canceled"}:
        raise HTTPException(409, "Completed or canceled task cannot be canceled")
    with connect() as connection:
        connection.execute(
            "UPDATE processing_jobs SET status='canceled',stage='canceled',cancel_requested=1,lease_until=NULL,finished_at=?,updated_at=? WHERE id=?",
            (utc_now(), utc_now(), job_id),
        )
        connection.execute(
            """UPDATE raw_messages SET recognition_status='canceled',recognized_at=NULL,recognition_error=NULL
               WHERE id=(SELECT raw_message_id FROM processing_jobs WHERE id=? AND kind='classify')""",
            (job_id,),
        )
    from .codex_agent import cancel_codex_job

    cancel_codex_job(job_id)
    log_processing(job_id, "canceled", "管理员取消了任务", "warning")
    return {"id": job_id, "status": "canceled"}


@app.get("/api/v1/admin/processing-queue/{job_id}/logs")
def processing_job_logs(job_id: str, _: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    if not one("SELECT id FROM processing_jobs WHERE id=?", (job_id,)):
        raise HTTPException(404, "Processing job not found")
    result = []
    for row in all_rows("SELECT * FROM processing_logs WHERE processing_job_id=? ORDER BY created_at", (job_id,)):
        value = dict(row)
        value["details"] = json.loads(value.pop("details_json") or "{}")
        result.append(value)
    return result


@app.put("/api/v1/admin/processing-queue/{job_id}/text")
def update_processing_text(job_id: str, body: QueueTextRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    job = one("SELECT raw_message_id FROM processing_jobs WHERE id=?", (job_id,))
    if not job or not job["raw_message_id"]:
        raise HTTPException(404, "Processing source was not found")
    with connect() as connection:
        connection.execute("UPDATE raw_messages SET text_content=? WHERE id=?", (body.text, job["raw_message_id"]))
        connection.execute(
            "UPDATE raw_messages SET recognition_status='pending',recognized_at=NULL,recognition_error=NULL WHERE id=?",
            (job["raw_message_id"],),
        )
        connection.execute("UPDATE processing_jobs SET status='pending',stage='queued',attempts=0,cancel_requested=0,error=NULL,next_attempt_at=NULL,lease_until=NULL,processor=NULL,result_json=NULL,started_at=NULL,finished_at=NULL,updated_at=? WHERE id=?", (utc_now(), job_id))
    return {"id": job_id, "status": "pending"}


@app.post("/api/v1/admin/processing-queue/{job_id}/retry")
def retry_processing_job(job_id: str, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    job = one("SELECT id,status FROM processing_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "Processing job not found")
    if job["status"] not in {"needs_review", "paused_quota", "failed", "canceled"}:
        raise HTTPException(409, "Only failed processing jobs can be retried")
    with connect() as connection:
        connection.execute(
            "UPDATE processing_jobs SET status='pending',stage='queued',attempts=0,cancel_requested=0,lease_until=NULL,next_attempt_at=NULL,processor=NULL,result_json=NULL,started_at=NULL,finished_at=NULL,error=NULL,updated_at=? WHERE id=?",
            (utc_now(), job_id),
        )
        connection.execute(
            """UPDATE raw_messages SET recognition_status='pending',recognized_at=NULL,recognition_error=NULL
               WHERE id=(SELECT raw_message_id FROM processing_jobs WHERE id=? AND kind='classify')""",
            (job_id,),
        )
    log_processing(job_id, "queued", "管理员将任务重新加入队列")
    return {"id": job_id, "status": "pending"}


@app.post("/api/v1/imports/text")
def import_text_endpoint(body: TextImportRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {"raw_message_id": import_text(body.text, body.source_group_id, body.metadata)}


@app.post("/api/v1/imports/url")
def import_url_endpoint(body: UrlImportRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        return {"raw_message_id": import_url(body.url, body.source_group_id)}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/imports/files")
async def import_file_endpoint(file: UploadFile = File(...), _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "File is larger than 50 MB")
    try:
        return import_file(file.filename or "upload.bin", data, file.content_type)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/admin/companies/impact")
def company_management_impact_endpoint(body: CompanyManagementPreviewRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        return company_management_impact(body.ids, body.operation)
    except CompanyManagementNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except CompanyManagementValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/admin/companies/merge")
async def merge_companies_endpoint(body: CompanySelectionRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        result = queue_company_management(body.ids, "merge")
    except CompanyManagementConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except CompanyManagementNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except CompanyManagementValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    await events.publish("processing.updated", result)
    return result


@app.delete("/api/v1/admin/companies")
async def delete_companies_endpoint(body: CompanySelectionRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        result = queue_company_management(body.ids, "delete")
    except CompanyManagementConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except CompanyManagementNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except CompanyManagementValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    await events.publish("processing.updated", result)
    return result


@app.get("/api/v1/companies")
def companies(q: str | None = None, industry: str | None = None, _: dict[str, Any] = Depends(require_scope("catalog:read"))) -> list[dict[str, Any]]:
    params: list[Any] = []
    sql = "SELECT c.*, COUNT(CASE WHEN j.status <> 'superseded' THEN j.id END) AS job_count FROM companies c LEFT JOIN jobs j ON j.company_id=c.id WHERE 1=1"
    if q:
        sql += " AND (c.display_name LIKE ? OR c.legal_name LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if industry:
        sql += " AND c.primary_industry=?"
        params.append(industry)
    sql += " GROUP BY c.id ORDER BY c.updated_at DESC"
    result = []
    for row in all_rows(sql, tuple(params)):
        value = dict(row)
        value["job_count"] = row["job_count"]
        value.pop("manual_overrides_json", None)
        value["aliases"] = json.loads(value.pop("aliases_json"))
        value["secondary_industries"] = json.loads(value.pop("secondary_industries_json"))
        value["major_requirements"] = json.loads(value.pop("major_requirements_json", "[]") or "[]")
        value["tags"] = json.loads(value.pop("company_tags_json", "[]") or "[]")
        result.append(value)
    return result


def _deduplicate_evidences(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        source_url = str(row.get("source_url") or "").strip()
        raw_message_id = str(row.get("raw_message_id") or "").strip()
        if source_url:
            identity = ("source_url", source_url)
        elif raw_message_id:
            identity = ("raw_message_id", raw_message_id)
        else:
            result.append(row)
            continue
        if identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


@app.get("/api/v1/companies/{company_id}")
def company_detail(company_id: str, _: dict[str, Any] = Depends(require_scope("catalog:read"))) -> dict[str, Any]:
    company = one("SELECT * FROM companies WHERE id=?", (company_id,))
    if not company:
        raise HTTPException(404, "Company not found")
    jobs = [dict(row) for row in all_rows("SELECT * FROM jobs WHERE company_id=? AND status<>'superseded' ORDER BY updated_at DESC", (company_id,))]
    for job in jobs:
        for key in list(job):
            if key.endswith("_json"):
                try:
                    job[key[:-5]] = json.loads(job.pop(key))
                except json.JSONDecodeError:
                    pass
    evidences = _deduplicate_evidences([dict(row) for row in all_rows("""SELECT * FROM evidences
        WHERE company_id=? OR job_id IN (SELECT id FROM jobs WHERE company_id=?)
        ORDER BY CASE WHEN source_type='wechat_group' OR lower(COALESCE(source_url,'')) LIKE '%weixin%' THEN 0 ELSE 1 END,
                 observed_at DESC""", (company_id, company_id))])
    public_findings = [dict(row) for row in all_rows("SELECT id,finding_type,title,summary,source_title,source_url,resolved_url,published_at,severity,retrieved_at FROM company_public_findings WHERE company_id=? ORDER BY retrieved_at DESC", (company_id,))]
    shared_details = [dict(row) for row in all_rows(
        """SELECT d.*,b.name AS batch_name,b.year AS batch_year,b.season AS batch_season,
                  b.recruitment_type,e.source_type,e.source_url
           FROM recruitment_shared_details d
           LEFT JOIN recruitment_batches b ON b.id=d.batch_id
           LEFT JOIN evidences e ON e.id=d.evidence_id
           WHERE d.company_id=? ORDER BY d.observed_at DESC,d.id""",
        (company_id,),
    )]
    for detail in shared_details:
        json_fields = {
            "locations_json": ("locations", []),
            "salary_json": ("salary", {}),
            "target_graduation_years_json": ("target_graduation_years", []),
            "education_requirements_json": ("education_requirements", []),
            "major_requirements_json": ("major_requirements", []),
            "process_json": ("process", []),
            "benefits_json": ("benefits", []),
        }
        for column, (field, default) in json_fields.items():
            raw_value = detail.pop(column, None)
            try:
                detail[field] = json.loads(raw_value or json.dumps(default, ensure_ascii=False))
            except (TypeError, json.JSONDecodeError):
                detail[field] = default
    result = dict(company)
    result.pop("manual_overrides_json", None)
    result["aliases"] = json.loads(result.pop("aliases_json"))
    result["secondary_industries"] = json.loads(result.pop("secondary_industries_json"))
    result["major_requirements"] = json.loads(result.pop("major_requirements_json", "[]") or "[]")
    result["tags"] = json.loads(result.pop("company_tags_json", "[]") or "[]")
    for key in ("businesses_json", "highlights_json", "official_channels_json"):
        result[key[:-5]] = json.loads(result.pop(key) or "[]")
    result["jobs"] = jobs
    result["evidences"] = evidences
    result["public_findings"] = public_findings
    result["recruitment_shared_details"] = shared_details
    result["events"] = recruitment_events(company_id=company_id, _=_)
    return result


@app.put("/api/v1/companies/{company_id}")
async def update_company(company_id: str, body: CompanyUpdate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(400, "No company fields were provided")
    list_fields = {"aliases", "secondary_industries", "businesses", "highlights", "official_channels"}
    normalized: dict[str, Any] = {}
    for field, value in values.items():
        if field not in COMPANY_OVERRIDE_COLUMNS:
            continue
        if field in list_fields:
            if value is None:
                normalized[field] = []
            else:
                normalized[field] = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        elif field == "primary_industry":
            normalized[field] = value or "other"
            if normalized[field] not in INDUSTRIES:
                raise HTTPException(400, "Unknown primary industry")
        elif field == "display_name":
            normalized[field] = str(value).strip()
            if not normalized[field]:
                raise HTTPException(400, "Company display name cannot be empty")
        elif value is None:
            normalized[field] = None
        else:
            normalized[field] = str(value).strip()
    if not normalized:
        raise HTTPException(400, "No editable company fields were provided")
    now = utc_now()
    with connect() as connection:
        company = connection.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        if not company:
            raise HTTPException(404, "Company not found")
        overrides = {**company_overrides(company["manual_overrides_json"]), **normalized}
        apply_company_overrides(connection, company_id, normalized, now)
        connection.execute(
            "UPDATE companies SET manual_overrides_json=?,summary_locked=CASE WHEN ? THEN 1 ELSE summary_locked END,updated_at=? WHERE id=?",
            (json.dumps(overrides, ensure_ascii=False), int("summary" in normalized), now, company_id),
        )
        updated = connection.execute("SELECT display_name,summary FROM companies WHERE id=?", (company_id,)).fetchone()
        indexed = connection.execute(
            "UPDATE search_index SET title=?,body=? WHERE entity_type='company' AND entity_id=?",
            (updated["display_name"], f"{updated['display_name']} {updated['summary'] or ''}".strip(), company_id),
        ).rowcount
        if not indexed:
            connection.execute(
                "INSERT INTO search_index(entity_type,entity_id,title,body) VALUES('company',?,?,?)",
                (company_id, updated["display_name"], f"{updated['display_name']} {updated['summary'] or ''}".strip()),
            )
        connection.execute(
            "INSERT INTO company_versions(id,company_id,profile_json,decision,reason,processor,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), company_id, json.dumps(normalized, ensure_ascii=False), "manual_edit", "管理员手动编辑企业资料", f"admin:{user['id']}", now),
        )
    await events.publish("company.updated", {"company_id": company_id})
    return company_detail(company_id, user)


@app.get("/api/v1/evidences/{evidence_id}")
def evidence_detail(evidence_id: str, _: dict[str, Any] = Depends(require_scope("catalog:read"))) -> dict[str, Any]:
    row = one(
        """SELECT e.*,r.text_content AS raw_text,r.sender,r.sent_at,r.message_type,r.metadata_json,
                  sg.name AS source_group_name,a.filename,a.mime_type,a.ocr_text,a.qr_values_json
           FROM evidences e LEFT JOIN raw_messages r ON r.id=e.raw_message_id
           LEFT JOIN source_groups sg ON sg.id=r.source_group_id
           LEFT JOIN artifacts a ON a.id=e.artifact_id WHERE e.id=?""",
        (evidence_id,),
    )
    if not row:
        raise HTTPException(404, "Evidence not found")
    value = dict(row)
    value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
    first_qr_values = json.loads(value.pop("qr_values_json") or "[]")
    artifact_rows = all_rows("SELECT id,qr_values_json FROM artifacts WHERE raw_message_id=? ORDER BY created_at", (value.get("raw_message_id"),)) if value.get("raw_message_id") else []
    value["artifact_ids"] = [artifact["id"] for artifact in artifact_rows]
    value["qr_values"] = list(dict.fromkeys([
        *first_qr_values,
        *[
            qr
            for artifact in artifact_rows
            for qr in json.loads(artifact["qr_values_json"] or "[]")
        ],
        *((value["metadata"].get("qr_values") or []) if isinstance(value["metadata"], dict) else []),
    ]))
    return value


@app.get("/api/v1/artifacts/{artifact_id}")
def artifact_download(artifact_id: str, _: dict[str, Any] = Depends(require_scope("catalog:read"))) -> FileResponse:
    row = one("SELECT path,filename,mime_type FROM artifacts WHERE id=?", (artifact_id,))
    if not row or not Path(row["path"]).exists():
        raise HTTPException(404, "Artifact not found")
    return FileResponse(row["path"], media_type=row["mime_type"], filename=row["filename"])


@app.get("/api/v1/recruitment-events")
def recruitment_events(company_id: str | None = None, _: dict[str, Any] = Depends(require_scope("catalog:read"))) -> list[dict[str, Any]]:
    params: tuple[Any, ...] = (company_id,) if company_id else ()
    where = "WHERE e.company_id=?" if company_id else ""
    now = datetime.now(timezone.utc)
    rows = all_rows(
        f"""SELECT e.*,c.display_name AS company_name,b.name AS batch_name
            FROM recruitment_events e JOIN companies c ON c.id=e.company_id
            LEFT JOIN recruitment_batches b ON b.id=e.batch_id {where}
            ORDER BY e.updated_at DESC,e.id""",
        params,
    )
    result = []
    for row in rows:
        value = dict(row)
        value["job_ids"] = json.loads(value.pop("job_ids_json") or "[]")
        value["evidence_ids"] = [item["evidence_id"] for item in all_rows("SELECT evidence_id FROM recruitment_event_evidences WHERE event_id=?", (row["id"],))]
        value["status"] = recruitment_event_state(value, now)
        result.append(value)
    return sorted(result, key=lambda event: recruitment_event_sort_key(event, now))


@app.get("/api/v1/jobs")
def jobs(q: str | None = None, state: str | None = None, _: dict[str, Any] = Depends(require_scope("catalog:read"))) -> list[dict[str, Any]]:
    params: list[Any] = []
    sql = "SELECT j.*, c.display_name AS company_name FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.status <> 'superseded'"
    if q:
        sql += " AND (j.canonical_title LIKE ? OR j.requirements LIKE ? OR j.locations_json LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if state:
        sql += " AND j.status=?"
        params.append(state)
    sql += " ORDER BY j.updated_at DESC"
    return [dict(row) for row in all_rows(sql, tuple(params))]


@app.put("/api/v1/me/jobs/{job_id}/state")
def update_job_state(job_id: str, body: JobStateRequest, user: dict[str, Any] = Depends(require_scope("application:write"))) -> dict[str, Any]:
    if not one("SELECT id FROM jobs WHERE id=?", (job_id,)):
        raise HTTPException(404, "Job not found")
    with connect() as connection:
        current = connection.execute("SELECT * FROM user_job_states WHERE user_id=? AND job_id=?", (user["id"], job_id)).fetchone()
        state = body.state or (current["state"] if current else "interested")
        favorite = int(body.favorite if body.favorite is not None else (current["favorite"] if current else False))
        connection.execute("INSERT OR REPLACE INTO user_job_states(user_id,job_id,state,favorite,updated_at) VALUES(?,?,?,?,?)", (user["id"], job_id, state, favorite, utc_now()))
        if body.state and (not current or current["state"] != state):
            connection.execute("INSERT INTO application_events(id,user_id,job_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)", (str(uuid4()), user["id"], job_id, "state_changed", json.dumps({"from": current["state"] if current else None, "to": state}, ensure_ascii=False), utc_now()))
    return {"job_id": job_id, "state": state, "favorite": bool(favorite)}


@app.get("/api/v1/me/applications")
def applications(user: dict[str, Any] = Depends(require_scope("application:read"))) -> list[dict[str, Any]]:
    return [dict(row) for row in all_rows("SELECT s.*, j.canonical_title, c.display_name AS company_name FROM user_job_states s JOIN jobs j ON j.id=s.job_id JOIN companies c ON c.id=j.company_id WHERE s.user_id=? ORDER BY s.updated_at DESC", (user["id"],))]


@app.put("/api/v1/me/jobs/{job_id}/favorite")
def favorite_job(job_id: str, body: JobStateRequest, user: dict[str, Any] = Depends(require_scope("application:write"))) -> dict[str, Any]:
    if not one("SELECT id FROM jobs WHERE id=?", (job_id,)):
        raise HTTPException(404, "Job not found")
    with connect() as connection:
        current = connection.execute("SELECT state FROM user_job_states WHERE user_id=? AND job_id=?", (user["id"], job_id)).fetchone()
        connection.execute("INSERT OR REPLACE INTO user_job_states(user_id,job_id,state,favorite,updated_at) VALUES(?,?,?,?,?)", (user["id"], job_id, current["state"] if current else "interested", int(bool(body.favorite)), utc_now()))
    return {"job_id": job_id, "favorite": bool(body.favorite)}


@app.post("/api/v1/me/jobs/{job_id}/notes")
def add_note(job_id: str, body: NoteRequest, user: dict[str, Any] = Depends(require_scope("application:write"))) -> dict[str, Any]:
    if not one("SELECT id FROM jobs WHERE id=?", (job_id,)):
        raise HTTPException(404, "Job not found")
    note_id = str(uuid4())
    with connect() as connection:
        connection.execute("INSERT INTO user_notes(id,user_id,job_id,content,created_at,updated_at) VALUES(?,?,?,?,?,?)", (note_id, user["id"], job_id, body.content, utc_now(), utc_now()))
        connection.execute("INSERT INTO application_events(id,user_id,job_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)", (str(uuid4()), user["id"], job_id, "note_added", json.dumps({"note_id": note_id}, ensure_ascii=False), utc_now()))
    return {"id": note_id, "job_id": job_id, "content": body.content}


@app.get("/api/v1/me/timeline")
def timeline(user: dict[str, Any] = Depends(require_scope("application:read"))) -> list[dict[str, Any]]:
    rows = all_rows("SELECT e.*,j.canonical_title,c.display_name AS company_name FROM application_events e JOIN jobs j ON j.id=e.job_id JOIN companies c ON c.id=j.company_id WHERE e.user_id=? ORDER BY e.created_at DESC", (user["id"],))
    result = []
    for row in rows:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        result.append(value)
    return result


@app.get("/api/v1/notifications")
def notifications(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [dict(row) for row in all_rows("SELECT * FROM notifications WHERE user_id=? AND kind<>'usage_warning_snooze' ORDER BY created_at DESC LIMIT 100", (user["id"],))]


@app.post("/api/v1/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, bool]:
    with connect() as connection:
        connection.execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=?", (utc_now(), notification_id, user["id"]))
    return {"ok": True}


@app.post("/api/v1/notifications/{notification_id}/snooze-day")
def snooze_notification_for_day(notification_id: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, bool]:
    from .model_provider import _day_start_utc

    day_start = _day_start_utc()
    with connect() as connection:
        notification = connection.execute(
            "SELECT kind FROM notifications WHERE id=? AND user_id=?",
            (notification_id, user["id"]),
        ).fetchone()
        if not notification or notification["kind"] == "usage_warning_snooze" or not str(notification["kind"]).startswith("usage_warning"):
            raise HTTPException(404, "Usage warning notification not found")
        exists = connection.execute(
            "SELECT id FROM notifications WHERE user_id=? AND kind='usage_warning_snooze' AND created_at>=? LIMIT 1",
            (user["id"], day_start),
        ).fetchone()
        if not exists:
            connection.execute(
                "INSERT INTO notifications(id,user_id,kind,title,body,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid4()), user["id"], "usage_warning_snooze", "今日不再提醒", "usage_warning", utc_now()),
            )
        connection.execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=?", (utc_now(), notification_id, user["id"]))
    return {"ok": True}


@app.put("/api/v1/me/companies/{company_id}/follow")
def follow_company(company_id: str, body: FollowRequest, user: dict[str, Any] = Depends(require_scope("application:write"))) -> dict[str, Any]:
    if not one("SELECT id FROM companies WHERE id=?", (company_id,)):
        raise HTTPException(404, "Company not found")
    with connect() as connection:
        if body.followed:
            connection.execute("INSERT OR IGNORE INTO user_follows(user_id,company_id,created_at) VALUES(?,?,?)", (user["id"], company_id, utc_now()))
        else:
            connection.execute("DELETE FROM user_follows WHERE user_id=? AND company_id=?", (user["id"], company_id))
    return {"company_id": company_id, "followed": body.followed}


@app.get("/api/v1/admin/review-items")
def review_items(_: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    rows = all_rows("SELECT * FROM review_items WHERE status='open' ORDER BY created_at DESC")
    result = []
    for row in rows:
        value = dict(row)
        payload_json = value.pop("payload_json")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            payload = {"error": {"type": "invalid_review_payload", "message": payload_json}}
        if not isinstance(payload, dict):
            payload = {
                "error": {"type": "invalid_review_payload", "message": "审核载荷不是 JSON 对象"},
                "raw_payload": payload,
            }
        value["payload"] = enrich_review_payload(payload, value.get("entity_type"), value.get("entity_id"))
        result.append(value)
    return result


@app.post("/api/v1/admin/review-items/{review_id}/resolve")
def resolve_review(review_id: str, body: ReviewRequest, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with connect() as connection:
        connection.execute("UPDATE review_items SET status=?,resolved_by=?,resolved_at=? WHERE id=?", (body.action, user["id"], utc_now(), review_id))
    return {"ok": True}


@app.post("/api/v1/exports")
def create_export(fmt: str = "xlsx", user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if fmt not in {"xlsx", "csv", "json"}:
        raise HTTPException(400, "format must be xlsx, csv or json")
    path = export_jobs(fmt, user["id"])
    return {"format": fmt, "path": str(path), "download_url": f"/api/v1/exports/download/{path.name}"}


@app.post("/api/v1/admin/backups/test")
def test_backup(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        settings, webdav_password, _ = _backup_credentials()
        client = WebDAVClient(settings["webdav_url"], settings["username"], webdav_password)
        directory = settings.get("remote_directory", "/JobPostings")
        client.mkdir(directory)
        return {"ok": True, "remote_directory": directory}
    except Exception as exc:
        raise HTTPException(502, f"WebDAV test failed: {exc}") from exc


@app.get("/api/v1/admin/local-storage")
def local_storage(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return storage_snapshot()


@app.delete("/api/v1/admin/local-storage/cache")
def clear_local_cache(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {"deleted": clear_cache(), "storage": storage_snapshot()}


@app.delete("/api/v1/admin/local-storage/chat-records")
async def clear_local_chat_records(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    deleted = await asyncio.to_thread(clear_chat_records)
    return {"deleted": deleted, "storage": storage_snapshot()}


@app.delete("/api/v1/admin/local-storage/backups/{filename}")
def delete_local_backup(filename: str, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        deleted = delete_local_database_backup(filename)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"deleted": deleted, "storage": storage_snapshot()}


@app.post("/api/v1/admin/backups/run")
async def run_backup(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(create_backup)
    except Exception as exc:
        raise HTTPException(502, f"Backup failed: {exc}") from exc


@app.get("/api/v1/admin/backups")
def backups(_: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    return list_backups()


@app.post("/api/v1/admin/backups/restore/validate")
def validate_backup(body: BackupValidationRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        return validate_remote_backup(body.remote_path, body.backup_password)
    except Exception as exc:
        raise HTTPException(400, f"Backup validation failed: {exc}") from exc


@app.get("/api/v1/exports/download/{filename}")
def download_export(filename: str, _: dict[str, Any] = Depends(require_user)) -> FileResponse:
    path = (config.download_dir / filename).resolve()
    if path.parent != config.download_dir.resolve() or not path.exists():
        raise HTTPException(404, "Export not found")
    return FileResponse(path)


@app.post("/api/v1/api-tokens")
def create_api_token(body: TokenRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    enabled = one("SELECT value_json FROM system_settings WHERE key='agent_api_enabled'")
    if not enabled or not json.loads(enabled["value_json"]):
        raise HTTPException(409, "Agent API is disabled")
    allowed = {"catalog:read", "application:read", "application:write"}
    scopes = [scope for scope in body.scopes if scope in allowed]
    value = token()
    with connect() as connection:
        connection.execute("INSERT INTO api_tokens(id,user_id,name,token_hash,scopes_json,expires_at,created_at) VALUES(?,?,?,?,?,?,?)", (str(uuid4()), user["id"], body.name, hash_value(value), json.dumps(scopes), (datetime.now(timezone.utc) + timedelta(days=body.expires_days)).isoformat(timespec="seconds"), utc_now()))
    return {"token": value, "scopes": scopes, "warning": "The token is shown only once"}


@app.get("/api/v1/api-tokens")
def list_api_tokens(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [dict(row) for row in all_rows("SELECT id,name,scopes_json,expires_at,revoked_at,created_at FROM api_tokens WHERE user_id=? ORDER BY created_at DESC", (user["id"],))]


@app.delete("/api/v1/api-tokens/{token_id}")
def revoke_api_token(token_id: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, bool]:
    with connect() as connection:
        connection.execute("UPDATE api_tokens SET revoked_at=? WHERE id=? AND user_id=?", (utc_now(), token_id, user["id"]))
    return {"ok": True}


@app.get("/api/v1/events")
async def event_stream(_: dict[str, Any] = Depends(require_user)) -> StreamingResponse:
    return StreamingResponse(events.stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _runtime_path(*parts: str) -> Path:
    roots = [Path(__file__).resolve().parents[2]]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        roots.append(Path(bundle_root))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    for root in roots:
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return candidate
    return roots[0].joinpath(*parts)


STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
DIST_DIR = _runtime_path("frontend", "dist")
if (DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="frontend-assets")


@app.get("/{path:path}", response_class=HTMLResponse, response_model=None)
def frontend(path: str = "") -> HTMLResponse | FileResponse:
    dist_index = DIST_DIR / "index.html"
    if dist_index.exists():
        return FileResponse(dist_index)
    fallback = STATIC_DIR / "index.html"
    if fallback.exists():
        return FileResponse(fallback)
    return HTMLResponse("<h1>JobPostings</h1><p>Frontend has not been built.</p>", status_code=200)


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=config.host, port=config.port, reload=False)
