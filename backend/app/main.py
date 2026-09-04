from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from .auth import create_session, local_bootstrap_allowed, request_code, require_admin, require_scope, require_user, verify_code
from .backups import WebDAVClient, _backup_credentials, create_backup, list_backups, validate_remote_backup
from .catalog import refresh_expiration
from .config import config
from .db import all_rows, connect, init_db, one, utc_now
from .events import events
from .exports import export_jobs
from .processing import attach_artifact, import_file, import_text, import_url, ingest_message, process_one_batch, process_one_enrichment
from .security import SecretVault, hash_value, token
from .tracememo import TraceMemoClient, normalize_group


class BootstrapRequest(BaseModel):
    email: EmailStr


class CodeRequest(BaseModel):
    email: EmailStr


class VerifyRequest(BaseModel):
    challenge_id: str
    code: str = Field(min_length=6, max_length=6)


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = "member"


class TextImportRequest(BaseModel):
    text: str = Field(min_length=1)
    source_group_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class UrlImportRequest(BaseModel):
    url: str
    source_group_id: str | None = None


class GroupSelection(BaseModel):
    groups: list[dict[str, Any]]


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


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


background_tasks: set[asyncio.Task[Any]] = set()


async def worker_loop() -> None:
    while True:
        result = None
        try:
            result = await asyncio.to_thread(process_one_batch, 100)
            if result is None:
                result = await asyncio.to_thread(process_one_enrichment)
            if result:
                await events.publish("processing.updated", result)
            await asyncio.to_thread(refresh_expiration)
        except Exception as exc:
            await events.publish("sync.failed", {"error": str(exc)})
        await asyncio.sleep(2 if result else 30)


async def retention_loop() -> None:
    while True:
        try:
            with connect() as connection:
                connection.execute(
                    "DELETE FROM raw_messages WHERE (is_recruitment=0 OR is_recruitment IS NULL) AND retention_until<?",
                    (utc_now(),),
                )
                connection.execute(
                    "DELETE FROM artifacts WHERE raw_message_id NOT IN (SELECT id FROM raw_messages)"
                )
        except Exception:
            pass
        await asyncio.sleep(3600)


async def notification_loop() -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            rows = all_rows(
                "SELECT s.user_id,j.id,j.canonical_title,j.explicit_deadline,c.display_name FROM user_job_states s JOIN jobs j ON j.id=s.job_id JOIN companies c ON c.id=j.company_id WHERE s.favorite=1 AND j.explicit_deadline IS NOT NULL AND j.explicit_deadline>=? AND j.explicit_deadline<=?",
                (now.date().isoformat(), (now + timedelta(days=7)).date().isoformat()),
            )
            with connect() as connection:
                for row in rows:
                    day = str(row["explicit_deadline"])
                    try:
                        days_left = (datetime.fromisoformat(day).date() - now.date()).days
                    except ValueError:
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


def sync_tracememo_once() -> dict[str, Any]:
    row = one("SELECT * FROM connectors WHERE kind='tracememo' AND enabled=1")
    if not row:
        return {"status": "disabled", "fetched": 0, "groups": 0}
    settings = json.loads(row["config_json"])
    client = TraceMemoClient(row["base_url"], SecretVault().decrypt(settings["token"]) if settings.get("token") else "")
    groups = all_rows("SELECT * FROM source_groups WHERE connector_id=? AND selected=1 AND enabled=1", (row["id"],))
    fetched = 0
    for group in groups:
        cursor = one("SELECT * FROM sync_cursors WHERE source_group_id=?", (group["id"],))
        end = datetime.now(timezone.utc)
        if cursor and cursor["cursor_time"]:
            start = datetime.fromisoformat(cursor["cursor_time"]) - timedelta(minutes=2)
        else:
            start = end - timedelta(days=int(_setting_value("initial_import_days", 30)))
        for message in client.messages(group["external_id"], start, end):
            raw_id = ingest_message(message, row["id"], group["id"])
            if raw_id:
                fetched += 1
                message_type = str(message.get("type") or message.get("msgType") or "").lower()
                media_id = str(message.get("media_id") or message.get("mediaId") or message.get("attachment_id") or (message.get("id") if message_type in {"image", "file", "attachment", "picture", "document"} else "") or "")
                if media_id and message_type in {"image", "file", "attachment", "picture", "document"}:
                    try:
                        media, suggested_name = client.media(media_id)
                        filename = str(message.get("filename") or message.get("fileName") or suggested_name or f"{media_id}.bin")
                        attach_artifact(raw_id, filename, media, message.get("mime_type") or message.get("mimeType"))
                    except Exception:
                        pass
        with connect() as connection:
            connection.execute("INSERT OR REPLACE INTO sync_cursors(source_group_id,cursor_time,cursor_message_id,updated_at) VALUES(?,?,?,?)", (group["id"], end.isoformat(), None, utc_now()))
    return {"status": "completed", "fetched": fetched, "groups": len(groups)}


async def auto_sync_loop() -> None:
    while True:
        interval = max(1, int(_setting_value("sync_interval_minutes", 10)))
        await asyncio.sleep(interval * 60)
        try:
            await events.publish("sync.started", {"interval_minutes": interval})
            result = await asyncio.to_thread(sync_tracememo_once)
            if result.get("status") == "completed":
                await events.publish("sync.completed", result)
        except Exception as exc:
            await events.publish("sync.failed", {"error": str(exc)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    config.ensure_dirs()
    SecretVault()
    task1 = asyncio.create_task(worker_loop())
    task2 = asyncio.create_task(retention_loop())
    task3 = asyncio.create_task(auto_sync_loop())
    task4 = asyncio.create_task(notification_loop())
    task5 = asyncio.create_task(auto_backup_loop())
    background_tasks.update({task1, task2, task3, task4, task5})
    yield
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    background_tasks.clear()


app = FastAPI(title="JobPostings", version="0.1.0", lifespan=lifespan)


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
        connection.execute("INSERT INTO users(id,email,role,created_at) VALUES(?,?,?,?)", (user_id, str(body.email).lower(), "admin", utc_now()))
    session = create_session(user_id)
    response = JSONResponse({"user": {"id": user_id, "email": str(body.email).lower(), "role": "admin"}})
    response.set_cookie("jp_session", session, httponly=True, samesite="lax", secure=config.public_base_url.startswith("https://"), max_age=60 * 60 * 24 * 7)
    return response


@app.get("/api/v1/bootstrap/status")
def bootstrap_status() -> dict[str, bool]:
    return {"initialized": bool(one("SELECT id FROM users LIMIT 1"))}


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
    return {"user": user}


@app.post("/api/v1/admin/invitations")
def create_invitation(body: InviteRequest, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    invite = token()
    invitation_id = str(uuid4())
    with connect() as connection:
        connection.execute(
            "INSERT INTO invitations(id,email,token_hash,role,expires_at,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (invitation_id, str(body.email).lower(), hash_value(invite), body.role if body.role in {"admin", "member"} else "member", (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat(timespec="seconds"), user["id"], utc_now()),
        )
    from .auth import _send_email

    try:
        _send_email(
            str(body.email).lower(),
            "JobPostings 邀请",
            f"你已被邀请使用 JobPostings。请使用此邮箱申请登录验证码：{str(body.email).lower()}\n邀请有效期 72 小时。",
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
        "sync_interval_minutes", "initial_import_days", "redaction_enabled", "llm_input_budget",
        "llm_output_budget", "llm_budget_warning_percent", "ordinary_retention_days",
        "possibly_expired_days", "smtp", "llm_provider", "search", "backup", "agent_api_enabled",
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
            connection.execute("INSERT OR REPLACE INTO system_settings(key,value_json,updated_at) VALUES(?,?,?)", (key, json.dumps(value, ensure_ascii=False), utc_now()))
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
        connection.execute("INSERT OR REPLACE INTO connectors(id,kind,base_url,enabled,config_json,updated_at) VALUES(?,?,?,?,?,?)", (cid, "tracememo", base_url, int(bool(body.get("enabled", True))), json.dumps(safe_config), utc_now()))
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
    client = TraceMemoClient(row["base_url"], SecretVault().decrypt(settings["token"]) if settings.get("token") else "")
    try:
        groups = client.groups()
    except Exception as exc:
        raise HTTPException(502, f"TraceMemo group query failed: {exc}") from exc
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
async def manual_sync(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    result = await asyncio.to_thread(sync_tracememo_once)
    if result.get("status") == "disabled":
        raise HTTPException(400, "Enabled TraceMemo connector not found")
    await events.publish("sync.completed", result)
    return result


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


@app.get("/api/v1/companies")
def companies(q: str | None = None, industry: str | None = None, _: dict[str, Any] = Depends(require_scope("catalog:read"))) -> list[dict[str, Any]]:
    params: list[Any] = []
    sql = "SELECT c.*, COUNT(j.id) AS job_count FROM companies c LEFT JOIN jobs j ON j.company_id=c.id WHERE 1=1"
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
        value["aliases"] = json.loads(value.pop("aliases_json"))
        value["secondary_industries"] = json.loads(value.pop("secondary_industries_json"))
        result.append(value)
    return result


@app.get("/api/v1/companies/{company_id}")
def company_detail(company_id: str, _: dict[str, Any] = Depends(require_scope("catalog:read"))) -> dict[str, Any]:
    company = one("SELECT * FROM companies WHERE id=?", (company_id,))
    if not company:
        raise HTTPException(404, "Company not found")
    jobs = [dict(row) for row in all_rows("SELECT * FROM jobs WHERE company_id=? ORDER BY updated_at DESC", (company_id,))]
    for job in jobs:
        for key in list(job):
            if key.endswith("_json"):
                try:
                    job[key[:-5]] = json.loads(job.pop(key))
                except json.JSONDecodeError:
                    pass
    evidences = [dict(row) for row in all_rows("SELECT * FROM evidences WHERE company_id=? OR job_id IN (SELECT id FROM jobs WHERE company_id=?) ORDER BY observed_at DESC", (company_id, company_id))]
    result = dict(company)
    result["aliases"] = json.loads(result.pop("aliases_json"))
    result["secondary_industries"] = json.loads(result.pop("secondary_industries_json"))
    result["jobs"] = jobs
    result["evidences"] = evidences
    return result


@app.get("/api/v1/jobs")
def jobs(q: str | None = None, state: str | None = None, _: dict[str, Any] = Depends(require_scope("catalog:read"))) -> list[dict[str, Any]]:
    params: list[Any] = []
    sql = "SELECT j.*, c.display_name AS company_name FROM jobs j JOIN companies c ON c.id=j.company_id WHERE 1=1"
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
    return [dict(row) for row in all_rows("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],))]


@app.post("/api/v1/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, bool]:
    with connect() as connection:
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
        value["payload"] = json.loads(value.pop("payload_json"))
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
