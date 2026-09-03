from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import mimetypes
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_html(html: str) -> dict[str, Any]:
    parser = TextExtractor()
    parser.feed(html)
    text = "\n".join(parser.parts)
    title = parser.parts[0] if parser.parts else ""
    return {"title": title[:300], "text": text[:2_000_000], "links": parser.links[:100]}


def _safe_decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-16", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_file(filename: str, data: bytes) -> dict[str, Any]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".log", ".csv", ".tsv"}:
        text = _safe_decode(data)
        if suffix in {".csv", ".tsv"}:
            delimiter = "\t" if suffix == ".tsv" else ","
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))[:20_000]
            text = "\n".join(" | ".join(row) for row in rows)
        return {"text": text[:2_000_000], "metadata": {"format": suffix[1:]}}
    if suffix == ".docx":
        try:
            from docx import Document

            document = Document(io.BytesIO(data))
            paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
            tables = []
            for table in document.tables:
                for row in table.rows:
                    tables.append(" | ".join(cell.text for cell in row.cells))
            return {"text": "\n".join(paragraphs + tables)[:2_000_000], "metadata": {"format": "docx"}}
        except Exception as exc:
            return {"text": "", "metadata": {"format": "docx", "error": str(exc)}}
    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            lines: list[str] = []
            for sheet in workbook.worksheets[:20]:
                lines.append(f"[工作表: {sheet.title}]")
                for row in list(sheet.iter_rows(values_only=True))[:20_000]:
                    values = [str(value) for value in row if value is not None]
                    if values:
                        lines.append(" | ".join(values))
            return {"text": "\n".join(lines)[:2_000_000], "metadata": {"format": "xlsx"}}
        except Exception as exc:
            return {"text": "", "metadata": {"format": "xlsx", "error": str(exc)}}
    if suffix == ".pdf":
        try:
            import fitz

            document = fitz.open(stream=data, filetype="pdf")
            lines = []
            for page in list(document)[:200]:
                lines.append(page.get_text("text"))
            return {"text": "\n".join(lines)[:2_000_000], "metadata": {"format": "pdf", "pages": len(document)}}
        except Exception as exc:
            return {"text": "", "metadata": {"format": "pdf", "error": str(exc)}}
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        return process_image(data, suffix[1:])
    return {"text": "", "metadata": {"format": suffix.lstrip(".") or "unknown", "unsupported": True}}


def process_image(data: bytes, image_format: str) -> dict[str, Any]:
    """Run optional local OCR and QR decoding without making either mandatory."""
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return {"text": "", "metadata": {"format": image_format, "decode_error": "invalid image"}}
        qr_values: list[str] = []
        detector = cv2.QRCodeDetector()
        try:
            ok, decoded, _, _ = detector.detectAndDecodeMulti(image)
            if ok:
                qr_values.extend(value for value in decoded if value)
            else:
                value, _, _ = detector.detectAndDecode(image)
                if value:
                    qr_values.append(value)
        except Exception:
            pass
        ocr_lines: list[str] = []
        try:
            from rapidocr_onnxruntime import RapidOCR

            result, _ = RapidOCR()(image)
            for item in result or []:
                if len(item) >= 2 and item[1]:
                    ocr_lines.append(str(item[1]))
        except Exception:
            pass
        return {
            "text": "\n".join(ocr_lines)[:2_000_000],
            "metadata": {"format": image_format, "needs_ocr": False},
            "qr_values": list(dict.fromkeys(qr_values)),
        }
    except Exception as exc:
        return {"text": "", "metadata": {"format": image_format, "ocr_error": str(exc)}, "qr_values": []}


def validate_public_url(url: str) -> str:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError("Only public HTTP(S) URLs are supported")
    if parsed_url.username or parsed_url.password:
        raise ValueError("URLs with embedded credentials are not supported")
    host = parsed_url.hostname.lower()
    if host in {"localhost", "metadata.google.internal"}:
        raise ValueError("Local and metadata hosts are blocked")
    try:
        port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("Invalid URL port") from exc
    try:
        address = ipaddress.ip_address(host)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise ValueError("Private network targets are blocked")
    except ValueError as exc:
        if str(exc) == "Private network targets are blocked":
            raise
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        if not addresses:
            raise ValueError("Unable to resolve public URL")
        for resolved in addresses:
            address = ipaddress.ip_address(resolved)
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                raise ValueError("URL resolves to a private network target")
    except socket.gaierror as exc:
        raise ValueError("Unable to resolve public URL") from exc
    return url


def fetch_public_http(url: str, timeout: float = 30, max_bytes: int = 10 * 1024 * 1024) -> httpx.Response:
    current_url = url
    for _ in range(6):
        validate_public_url(current_url)
        response = httpx.get(
            current_url,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "JobPostings/0.1 (+local recruitment archive)"},
        )
        if 300 <= response.status_code < 400:
            location = response.headers.get("location")
            if not location:
                raise ValueError("Redirect response has no location")
            current_url = urljoin(current_url, location)
            continue
        response.raise_for_status()
        if len(response.content) > max_bytes:
            raise ValueError(f"Response is larger than {max_bytes // (1024 * 1024)} MB")
        return response
    raise ValueError("Too many redirects")


def fetch_public_url(url: str) -> dict[str, Any]:
    response = fetch_public_http(url)
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and not response.text.lstrip().startswith("<"):
        return {"url": str(response.url), "text": response.text[:2_000_000], "content_type": content_type}
    result = extract_html(response.text)
    result.update({"url": str(response.url), "content_type": content_type})
    return result


def parse_message_payload(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    message_type = str(message.get("type") or message.get("msgType") or "text").lower()
    text = str(message.get("text") or message.get("content") or "")
    metadata = dict(message)
    metadata.pop("text", None)
    metadata.pop("content", None)
    if message_type in {"link", "url", "article"} and message.get("url"):
        metadata["url"] = message["url"]
    return normalize_text(text), metadata
