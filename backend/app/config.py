from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, PrivateAttr


def _expand_path(value: str) -> Path:
    value = os.path.expandvars(value)
    return Path(value).expanduser()


class AppConfig(BaseModel):
    _db_path_override: Path | None = PrivateAttr(default=None)
    app_env: str = Field(default_factory=lambda: os.getenv("JOBPOSTINGS_APP_ENV", "development"))
    host: str = Field(default_factory=lambda: os.getenv("JOBPOSTINGS_HOST", "127.0.0.1"))
    port: int = Field(default_factory=lambda: int(os.getenv("JOBPOSTINGS_PORT", "17879")))
    data_dir: Path = Field(
        default_factory=lambda: _expand_path(
            os.getenv("JOBPOSTINGS_DATA_DIR", r"%LOCALAPPDATA%\JobPostings")
        )
    )
    public_base_url: str = Field(
        default_factory=lambda: os.getenv("JOBPOSTINGS_PUBLIC_BASE_URL", "http://127.0.0.1:17879")
    )
    trusted_proxy_mode: str = Field(
        default_factory=lambda: os.getenv("JOBPOSTINGS_TRUSTED_PROXY_MODE", "none")
    )
    dev_show_otp: bool = Field(
        default_factory=lambda: os.getenv("JOBPOSTINGS_DEV_SHOW_OTP", "false").lower() == "true"
    )

    @property
    def db_path(self) -> Path:
        return self._db_path_override or (self.data_dir / "data" / "jobpostings.db")

    @db_path.setter
    def db_path(self, value: Path) -> None:
        self._db_path_override = Path(value)

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "data" / "blobs"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "secrets" / "vault.dat"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    def ensure_dirs(self) -> None:
        for path in (self.db_path.parent, self.blob_dir, self.secret_path.parent, self.log_dir, self.export_dir):
            path.mkdir(parents=True, exist_ok=True)


config = AppConfig()


def load_json_setting(value: str | None, default: object) -> object:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
