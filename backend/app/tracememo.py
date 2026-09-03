from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

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

