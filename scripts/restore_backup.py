from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from zipfile import ZipFile

from app.backups import WebDAVClient, decrypt_backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and restore a JobPostings WebDAV backup")
    parser.add_argument("--webdav-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--webdav-password", required=True)
    parser.add_argument("--remote-path", required=True)
    parser.add_argument("--backup-password", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm", action="store_true", help="write the validated archive to --output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    encrypted = WebDAVClient(args.webdav_url, args.username, args.webdav_password).get(args.remote_path)
    payload, header = decrypt_backup(encrypted, args.backup_password)
    with ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if "database.sqlite" not in names or "manifest.json" not in names:
            raise RuntimeError("Backup archive is incomplete")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        print(f"Validated {header.get('snapshot_name', args.remote_path)} with {len(manifest.get('files', []))} entries")
        if not args.confirm:
            print("Dry run only. Add --confirm to write the validated archive.")
            return 0
        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=True)
        for name in names:
            target = (output / name).resolve()
            if target != output and output not in target.parents:
                raise RuntimeError(f"Unsafe archive member: {name}")
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    print(f"Restored validated files to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

