from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import config
from .db import connect, utc_now


def export_jobs(fmt: str, user_id: str | None = None) -> Path:
    config.download_dir.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        rows = connection.execute(
            "SELECT c.display_name AS company_name, c.primary_industry, j.* "
            "FROM jobs j JOIN companies c ON c.id=j.company_id ORDER BY c.display_name,j.canonical_title"
        ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        value = dict(row)
        for key in list(value):
            if key.endswith("_json"):
                try:
                    value[key[:-5]] = json.loads(value.pop(key))
                except json.JSONDecodeError:
                    value[key[:-5]] = value.pop(key)
        records.append(value)
    path = config.download_dir / f"jobs-{utc_now().replace(':', '-')}-{uuid4().hex[:6]}.{fmt}"
    if fmt == "json":
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    elif fmt == "csv":
        fields = sorted({key for record in records for key in record})
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for record in records:
                writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for key, value in record.items()})
    elif fmt == "xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "岗位"
        fields = sorted({key for record in records for key in record})
        sheet.append(fields)
        for record in records:
            sheet.append([json.dumps(record.get(key), ensure_ascii=False) if isinstance(record.get(key), (list, dict)) else record.get(key) for key in fields])
        workbook.save(path)
    else:
        raise ValueError("format must be xlsx, csv or json")
    return path
