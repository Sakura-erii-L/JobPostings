from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote

import httpx


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
            return data.get("data") or data.get("chatrooms") or data.get("items") or []
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
        response = httpx.get(self.base_url + "/media/" + quote(message_id, safe=""), headers=self.headers, timeout=60)
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
