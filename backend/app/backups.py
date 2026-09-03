from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin
from uuid import uuid4

import httpx

from .config import config
from .db import connect, one, utc_now
from .security import SecretVault


MAGIC = b"JPB1"


def _settings() -> dict:
    row = one("SELECT value_json FROM system_settings WHERE key='backup'")
    return json.loads(row["value_json"]) if row else {}


def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))


def encrypt_backup(payload: bytes, password: str, metadata: dict) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = json.dumps({"version": 1, **metadata}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header_length = len(header).to_bytes(4, "big")
    ciphertext = AESGCM(_derive_key(password, salt)).encrypt(nonce, payload, header)
    return MAGIC + salt + nonce + header_length + header + ciphertext


def decrypt_backup(data: bytes, password: str) -> tuple[bytes, dict]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not data.startswith(MAGIC) or len(data) < 36:
        raise ValueError("Invalid JobPostings backup file")
    salt = data[4:20]
    nonce = data[20:32]
    header_length = int.from_bytes(data[32:36], "big")
    header = data[36 : 36 + header_length]
    ciphertext = data[36 + header_length :]
    payload = AESGCM(_derive_key(password, salt)).decrypt(nonce, ciphertext, header)
    return payload, json.loads(header.decode("utf-8"))


def _snapshot_zip() -> tuple[bytes, dict]:
    config.ensure_dirs()
    snapshot_path = config.data_dir / "temp" / f"snapshot-{uuid4().hex}.sqlite"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(config.db_path)
    destination = sqlite3.connect(snapshot_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    manifest: dict[str, object] = {"created_at": utc_now(), "files": []}
    stream = io.BytesIO()
    try:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            database = snapshot_path.read_bytes()
            archive.writestr("database.sqlite", database)
            manifest["files"].append({"path": "database.sqlite", "size": len(database)})
            if config.blob_dir.exists():
                for path in config.blob_dir.rglob("*"):
                    if path.is_file():
                        relative = Path("blobs") / path.relative_to(config.blob_dir)
                        data = path.read_bytes()
                        archive.writestr(str(relative).replace("\\", "/"), data)
                        manifest["files"].append({"path": str(relative).replace("\\", "/"), "size": len(data)})
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        snapshot_path.unlink(missing_ok=True)
    return stream.getvalue(), manifest


class WebDAVClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/") + "/"
        self.auth = (username, password)

    def url(self, path: str) -> str:
        return urljoin(self.base_url, quote(path.strip("/")) + ("/" if path.endswith("/") else ""))

    def mkdir(self, path: str) -> None:
        response = httpx.request("MKCOL", self.url(path), auth=self.auth, timeout=30)
        if response.status_code not in {200, 201, 204, 405, 409}:
            response.raise_for_status()

    def put(self, path: str, data: bytes) -> None:
        response = httpx.put(self.url(path), content=data, auth=self.auth, timeout=120)
        response.raise_for_status()

    def get(self, path: str) -> bytes:
        response = httpx.get(self.url(path), auth=self.auth, timeout=120)
        response.raise_for_status()
        return response.content

    def delete(self, path: str) -> None:
        response = httpx.delete(self.url(path), auth=self.auth, timeout=30)
        if response.status_code not in {200, 204, 404}:
            response.raise_for_status()


def _backup_credentials() -> tuple[dict, str, str]:
    settings = _settings()
    vault = SecretVault()
    webdav_password = vault.decrypt(settings["password_enc"]) if settings.get("password_enc") else ""
    backup_password = vault.decrypt(settings["backup_password_enc"]) if settings.get("backup_password_enc") else ""
    if not settings.get("webdav_url") or not settings.get("username") or not webdav_password or not backup_password:
        raise RuntimeError("WebDAV URL, username, WebDAV password and backup password are required")
    return settings, webdav_password, backup_password


def create_backup() -> dict:
    settings, webdav_password, backup_password = _backup_credentials()
    backup_id = str(uuid4())
    snapshot_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + backup_id[:8] + ".jpe"
    record_path = f"{settings.get('remote_directory', '/JobPostings').strip('/')}/{snapshot_name}"
    with connect() as connection:
        connection.execute("INSERT INTO backups(id,status,snapshot_name,remote_path,created_at) VALUES(?,?,?,?,?)", (backup_id, "running", snapshot_name, record_path, utc_now()))
    try:
        payload, manifest = _snapshot_zip()
        encrypted = encrypt_backup(payload, backup_password, {"snapshot_name": snapshot_name, "manifest": manifest})
        client = WebDAVClient(settings["webdav_url"], settings["username"], webdav_password)
        client.mkdir(settings.get("remote_directory", "/JobPostings"))
        client.put(record_path, encrypted)
        verified = client.get(record_path)
        if verified != encrypted:
            raise RuntimeError("Remote backup verification failed")
        try:
            retention = max(1, int(settings.get("retention_count", 30)))
        except (TypeError, ValueError):
            retention = 30
        with connect() as connection:
            old = connection.execute("SELECT id,remote_path FROM backups WHERE status='succeeded' ORDER BY created_at DESC").fetchall()
            for row in old[retention - 1 :]:
                client.delete(row["remote_path"])
                connection.execute("DELETE FROM backups WHERE id=?", (row["id"],))
            connection.execute("UPDATE backups SET status='succeeded',manifest_json=?,finished_at=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), utc_now(), backup_id))
        return {"id": backup_id, "status": "succeeded", "remote_path": record_path, "bytes": len(encrypted)}
    except Exception as exc:
        with connect() as connection:
            connection.execute("UPDATE backups SET status='failed',error=?,finished_at=? WHERE id=?", (str(exc), utc_now(), backup_id))
        raise


def list_backups() -> list[dict]:
    with connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM backups ORDER BY created_at DESC").fetchall()]


def validate_remote_backup(remote_path: str, backup_password: str | None = None) -> dict:
    settings, webdav_password, configured_password = _backup_credentials()
    password = backup_password or configured_password
    client = WebDAVClient(settings["webdav_url"], settings["username"], webdav_password)
    encrypted = client.get(remote_path)
    payload, header = decrypt_backup(encrypted, password)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if "database.sqlite" not in names or "manifest.json" not in names:
            raise ValueError("Backup manifest is incomplete")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    return {"ok": True, "remote_path": remote_path, "header": header, "entries": len(manifest.get("files", [])), "bytes": len(encrypted)}
