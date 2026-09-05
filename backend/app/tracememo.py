from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from functools import lru_cache


class TraceMemoClient:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = httpx.get(self.base_url + path, headers=self.headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def health(self) -> Any:
        return self._get("/health")

    def groups(self) -> list[dict[str, Any]]:
        data = self._get("/chatroom")
        if isinstance(data, dict):
            groups = data.get("data") or data.get("chatrooms") or data.get("items") or data.get("contacts") or []
            if isinstance(groups, dict):
                groups = groups.get("data") or groups.get("chatrooms") or groups.get("items") or groups.get("contacts") or []
            return groups if isinstance(groups, list) else []
        return data or []

    def messages(self, talker: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        data = self._get(
            "/chatlog",
            {
                "talker": talker,
                "startTime": start.astimezone(timezone.utc).isoformat(),
                "endTime": end.astimezone(timezone.utc).isoformat(),
            },
        )
        if isinstance(data, dict):
            return data.get("data") or data.get("messages") or data.get("items") or []
        return data or []

    def recent(self, talker: str, minutes: int = 15) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc)
        return self.messages(talker, end - timedelta(minutes=minutes), end)

    def media(self, message_id: str) -> tuple[bytes, str | None]:
        reference = str(message_id or "").strip()
        if not reference:
            raise ValueError("TraceMemo media reference is empty")
        if reference.startswith(("http://", "https://")):
            base = urlsplit(self.base_url)
            target = urlsplit(reference)
            if (target.scheme, target.hostname, target.port) != (base.scheme, base.hostname, base.port):
                raise ValueError("TraceMemo media URL must use the configured service origin")
            media_url = reference
        elif reference.startswith("/"):
            base = urlsplit(self.base_url)
            media_url = f"{base.scheme}://{base.netloc}{reference}"
        else:
            media_url = self.base_url + "/media/" + quote(reference, safe="")
        response = httpx.get(media_url, headers=self.headers, timeout=60)
        response.raise_for_status()
        content_type = response.headers.get("content-type")
        if content_type and "json" in content_type:
            value = response.json()
            import base64

            encoded = value.get("data") or value.get("base64") or value.get("content")
            if not encoded:
                raise ValueError("TraceMemo media response has no data")
            return base64.b64decode(encoded), value.get("filename")
        header = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", header, re.I)
        return response.content, unquote(match.group(1)) if match else None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _append_reference(references: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in references:
        references.append(text)


def tracememo_media_references(message: dict[str, Any]) -> list[str]:
    """Return media endpoints and message identifiers in stable fallback order."""
    nested = _mapping(message.get("contentData") or message.get("content_data"))
    media = _mapping(message.get("media"))
    containers = (message, nested, media)
    references: list[str] = []
    for container in containers:
        for key in ("media_id", "mediaId", "attachment_id", "attachmentId", "file_id", "fileId"):
            _append_reference(references, container.get(key))
    for container in containers:
        for key in ("media_url", "mediaUrl", "file_url", "fileUrl", "attachment_url", "attachmentUrl", "url"):
            value = container.get(key)
            if isinstance(value, str) and value.strip().startswith(("http://", "https://", "/")):
                _append_reference(references, value)
    message_type = str(message.get("type") or message.get("msgType") or "").strip().lower()
    nested_type = str(nested.get("type") or "").strip().lower()
    file_hint = message_type in {"file", "attachment", "document", "文件", "文档"} or nested_type in {"file", "attachment", "document"}
    if not file_hint:
        file_hint = any(Path(str(container.get(key) or "")).suffix.lower() in {".doc", ".docx", ".pdf", ".xls", ".xlsx"} for container in containers for key in ("filename", "fileName", "file_name", "title"))
    identifier_keys = (
        ("serverId", "server_id", "messageId", "message_id", "id", "localId", "local_id")
        if file_hint
        else ("id", "messageId", "message_id", "serverId", "server_id", "localId", "local_id")
    )
    for container in containers:
        for key in identifier_keys:
            _append_reference(references, container.get(key))
    return references


def tracememo_inline_media(message: dict[str, Any], max_bytes: int = 50 * 1024 * 1024) -> tuple[bytes, str | None, str | None] | None:
    """Read media embedded directly in a TraceMemo message, when available."""
    nested = _mapping(message.get("contentData") or message.get("content_data"))
    media = _mapping(message.get("media"))
    containers = (message, nested, media)
    for container in containers:
        for key in ("path", "file_path", "filePath", "local_path", "localPath"):
            value = str(container.get(key) or "").strip()
            if not value:
                continue
            path = Path(value)
            try:
                if path.is_file() and path.stat().st_size <= max_bytes:
                    return path.read_bytes(), tracememo_filename(message, path.name), container.get("mime_type") or container.get("mimeType")
            except OSError:
                continue
    for container in containers:
        for key in ("base64", "base64Data", "data"):
            value = container.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            encoded = value.strip()
            if encoded.startswith("data:") and "," in encoded:
                header, encoded = encoded.split(",", 1)
                mime_type = header[5:].split(";", 1)[0] or None
            else:
                mime_type = container.get("mime_type") or container.get("mimeType")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error):
                continue
            if data and len(data) <= max_bytes:
                return data, tracememo_filename(message), mime_type
    return None


def tracememo_local_media_roots() -> list[Path]:
    """Discover TraceMemo account roots that may contain original chat files."""
    roots: list[Path] = []
    for key in ("TRACEMEMO_MEDIA_ROOT", "TRACEMEMO_ACCOUNT_ROOT"):
        value = str(os.getenv(key) or "").strip()
        if value:
            roots.append(Path(value))
    appdata = str(os.getenv("APPDATA") or "").strip()
    if appdata:
        bootstrap = Path(appdata) / "TraceMemo" / "cache" / "bootstrap"
        for startup in bootstrap.glob("*/startup.json"):
            try:
                value = json.loads(startup.read_text(encoding="utf-8")).get("accountRoot")
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if value:
                roots.append(Path(str(value)))
    return list(dict.fromkeys(root for root in roots if root.exists() and root.is_dir()))


@lru_cache(maxsize=4)
def _tracememo_local_file_index(root: str) -> dict[str, tuple[str, ...]]:
    index: dict[str, list[str]] = {}
    try:
        paths = (path for path in Path(root).rglob("*") if path.is_file())
        for path in paths:
            if path.suffix.lower() not in {".pdf", ".doc", ".docx", ".xls", ".xlsx"}:
                continue
            index.setdefault(path.name.casefold(), []).append(str(path))
    except OSError:
        return {}
    return {name: tuple(paths) for name, paths in index.items()}


def tracememo_local_media(message: dict[str, Any], roots: list[Path] | None = None, max_bytes: int = 50 * 1024 * 1024) -> tuple[bytes, str | None, str | None] | None:
    """Read a uniquely named local TraceMemo file when the API has no file endpoint."""
    filename = tracememo_filename(message)
    if not filename or Path(filename).suffix.lower() not in {".pdf", ".doc", ".docx", ".xls", ".xlsx"}:
        return None
    for root in roots if roots is not None else tracememo_local_media_roots():
        paths = _tracememo_local_file_index(str(root)).get(Path(filename).name.casefold(), ())
        if len(paths) != 1:
            continue
        path = Path(paths[0])
        try:
            if path.stat().st_size > max_bytes:
                continue
            return path.read_bytes(), path.name, mimetypes.guess_type(path.name)[0]
        except OSError:
            continue
    return None


def tracememo_filename(message: dict[str, Any], suggested_name: str | None = None) -> str:
    """Prefer connector-provided names and document titles over opaque media ids."""
    nested = _mapping(message.get("contentData") or message.get("content_data"))
    media = _mapping(message.get("media"))
    candidates = [
        message.get("filename"),
        message.get("fileName"),
        message.get("file_name"),
        nested.get("filename"),
        nested.get("fileName"),
        nested.get("file_name"),
        nested.get("title"),
        media.get("filename"),
        media.get("fileName"),
        suggested_name,
    ]
    for candidate in candidates:
        value = Path(str(candidate or "")).name.strip()
        if value and Path(value).suffix:
            return value
    return "attachment.bin"


def normalize_group(group: dict[str, Any]) -> dict[str, str | None]:
    """Map TraceMemo and compatible chatroom fields to the local group shape."""
    def first_value(*keys: str) -> str:
        for key in keys:
            value = group.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    external_id = first_value(
        "m_nsUsrName",
        "userName",
        "username",
        "talker",
        "wxid",
        "id",
        "chatroomId",
        "chatroom_id",
        "md5",
    )
    name = first_value(
        "m_nsNickName",
        "name",
        "nickName",
        "nickname",
        "wechatNickname",
        "displayName",
        "remark",
    )
    avatar = first_value("avatar", "avatarUrl", "avatar_url")
    return {
        "external_id": external_id,
        "name": name or external_id or "未命名群聊",
        "avatar": avatar or None,
    }
