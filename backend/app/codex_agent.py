from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from .config import config
from .prompt_templates import render_prompt_template


_condition = threading.Condition()
_active = 0
_processes: dict[str, subprocess.Popen[str]] = {}


def _image_suffix(source: Path) -> str:
    suffix = source.suffix.lower()
    if suffix:
        return suffix
    try:
        with source.open("rb") as stream:
            header = stream.read(12)
    except OSError:
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith(b"BM"):
        return ".bmp"
    return ".png"


def _setting(key: str, default: Any) -> Any:
    from .model_provider import get_setting

    return get_setting(key, default)


def _acquire_slot() -> None:
    global _active
    with _condition:
        while _active >= max(1, min(4, int(_setting("codex_concurrency", 1)))):
            _condition.wait(timeout=1)
        _active += 1


def _release_slot() -> None:
    global _active
    with _condition:
        _active = max(0, _active - 1)
        _condition.notify_all()


def cancel_codex_job(job_id: str, wait_seconds: float = 5.0) -> bool:
    with _condition:
        process = _processes.get(job_id)
    if not process or process.poll() is not None:
        return False
    process.terminate()
    force_terminated = False
    try:
        process.wait(timeout=max(0.1, wait_seconds))
    except subprocess.TimeoutExpired:
        process.kill()
        force_terminated = True
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    if force_terminated:
        try:
            from .processing import log_processing

            log_processing(
                job_id,
                "canceling",
                "显式中断或重试等待超时，已强制终止本地 Codex 进程",
                "error",
                {"force_terminated": True, "wait_seconds": wait_seconds},
            )
        except Exception:
            pass
    return True


def run_codex_json(
    task: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
    *,
    job_id: str,
    image_paths: list[str] | None = None,
    enable_web: bool = False,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Run an ephemeral, read-only Codex task and return its schema-constrained JSON."""
    executable = os.getenv("CODEX_CLI_PATH") or shutil.which("codex")
    if not executable:
        raise RuntimeError("Local Codex CLI was not found")
    model = "gpt-5.6-luna"
    _acquire_slot()
    try:
        temp_root = config.data_dir / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix="jobpostings-codex-", dir=temp_root))
        try:
            codex_home = temp_dir / "codex-home"
            codex_home.mkdir()
            source_auth = Path.home() / ".codex" / "auth.json"
            if source_auth.exists():
                try:
                    auth = json.loads(source_auth.read_text(encoding="utf-8"))
                    auth.pop("OPENAI_API_KEY", None)
                    if auth.get("tokens"):
                        (codex_home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
                except (OSError, json.JSONDecodeError):
                    pass
            schema_path = temp_dir / "output.schema.json"
            output_path = temp_dir / "result.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            copied_images: list[Path] = []
            for index, value in enumerate(image_paths or []):
                source = Path(value)
                if source.exists() and source.is_file():
                    target = temp_dir / f"input-{index}{_image_suffix(source)}"
                    shutil.copy2(source, target)
                    copied_images.append(target)
            prompt = render_prompt_template(task, payload)
            command = [str(executable)]
            if enable_web:
                command.append("--search")
            command.extend([
                "exec",
                "--model",
                model,
                "--ephemeral",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--cd",
                str(temp_dir),
                "--config",
                'model_reasoning_effort="high"',
                "--config",
                'forced_login_method="chatgpt"',
            ])
            for image_path in copied_images:
                command.extend(["--image", str(image_path)])
            command.append("-")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            child_env = os.environ.copy()
            child_env.pop("OPENAI_API_KEY", None)
            child_env.pop("CODEX_API_KEY", None)
            child_env["CODEX_HOME"] = str(codex_home)
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                env=child_env,
            )
            with _condition:
                _processes[job_id] = process
            try:
                _, stderr = process.communicate(prompt, timeout=max(30, timeout_seconds))
            except subprocess.TimeoutExpired as exc:
                timeout_error = f"Local Codex timed out after {timeout_seconds} seconds"
                try:
                    from .processing import mark_processing_timeout

                    mark_processing_timeout(job_id, timeout_error)
                except Exception:
                    # The task remains governed by the process result even if
                    # the status update cannot be written from this thread.
                    pass
                # Do not terminate here.  An explicit API interrupt/retry can
                # terminate this same process through cancel_codex_job().
                _, stderr = process.communicate()
            finally:
                with _condition:
                    _processes.pop(job_id, None)
            if process.returncode != 0:
                message = (stderr or "Local Codex failed").strip()
                raise RuntimeError(message[-4000:])
            if not output_path.exists():
                raise RuntimeError("Local Codex did not write a result")
            value = json.loads(output_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Local Codex result root must be an object")
            return value
        finally:
            # A Windows sandbox or a lingering child handle can temporarily
            # block deletion. Cleanup is best-effort and must not replace the
            # model result with PermissionError: [WinError 5].
            shutil.rmtree(temp_dir, ignore_errors=True)
    finally:
        _release_slot()
