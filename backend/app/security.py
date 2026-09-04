from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
from pathlib import Path

from .config import config


def token() -> str:
    return secrets.token_urlsafe(32)


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.urlsafe_b64encode(salt).decode("ascii") + "$" + base64.urlsafe_b64encode(derived).decode("ascii")


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        scheme, n_value, r_value, p_value, salt_value, digest_value = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_value.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_value),
            r=int(r_value),
            p=int(p_value),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def hmac_value(value: str, key: bytes) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def random_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt_protect = ctypes.windll.crypt32.CryptProtectData
    crypt_protect.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(Blob)]
    source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source_blob = Blob(len(data), source)
    output = Blob()
    if not crypt_protect(ctypes.byref(source_blob), "JobPostings", None, None, None, 0, ctypes.byref(output)):
        raise OSError("CryptProtectData failed")
    result = ctypes.string_at(output.pbData, output.cbData)
    ctypes.windll.kernel32.LocalFree(output.pbData)
    return result


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt_unprotect = ctypes.windll.crypt32.CryptUnprotectData
    crypt_unprotect.argtypes = [ctypes.POINTER(Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(Blob), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(Blob)]
    source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source_blob = Blob(len(data), source)
    output = Blob()
    if not crypt_unprotect(ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(output)):
        raise OSError("CryptUnprotectData failed")
    result = ctypes.string_at(output.pbData, output.cbData)
    ctypes.windll.kernel32.LocalFree(output.pbData)
    return result


class SecretVault:
    """Local AES-GCM vault with a Windows DPAPI-wrapped key."""

    def __init__(self, path: Path | None = None):
        self.path = path or config.secret_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key = self._load_or_create_key()

    def _load_or_create_key(self) -> bytes:
        if self.path.exists():
            return _dpapi_unprotect(base64.b64decode(self.path.read_bytes()))
        key = os.urandom(32)
        self.path.write_bytes(base64.b64encode(_dpapi_protect(key)))
        return key

    def encrypt(self, value: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, value.encode("utf-8"), b"JobPostings:v1")
        return "v1:" + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def decrypt(self, value: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not value.startswith("v1:"):
            raise ValueError("Unsupported secret version")
        raw = base64.urlsafe_b64decode(value[3:].encode("ascii"))
        return AESGCM(self._key).decrypt(raw[:12], raw[12:], b"JobPostings:v1").decode("utf-8")


def normalize_contact(value: str) -> str:
    return "".join(value.strip().lower().split())


def make_redaction(value: str, kind: str, index_key: bytes) -> str:
    return f"[{kind}_{hmac_value(normalize_contact(value), index_key)[:4].upper()}]"


def redact_text(text: str, index_key: bytes) -> str:
    import re

    patterns = [
        (r"(?<!\d)1[3-9]\d{9}(?!\d)", "PHONE"),
        (r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "EMAIL"),
        (r"(?i)(微信|vx|v信|qq)\s*[:：]?\s*[\w-]{4,}", "CONTACT"),
        (r"(?<!\d)\d{17}[\dXx](?!\d)", "ID"),
    ]
    result = text
    for pattern, kind in patterns:
        result = re.sub(pattern, lambda match: make_redaction(match.group(0), kind, index_key), result)
    return result
