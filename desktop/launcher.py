from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx


HOST = "127.0.0.1"
PORT = int(os.getenv("JOBPOSTINGS_PORT", "17879"))
URL = f"http://{HOST}:{PORT}"


def _root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _backend_command() -> list[str]:
    root = _root()
    packaged_candidates = [
        root / "jobpostings-server" / "jobpostings-server.exe",
        root / "jobpostings-server.exe",
    ]
    for packaged in packaged_candidates:
        if packaged.exists():
            return [str(packaged), "--host", HOST, "--port", str(PORT)]
    return [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)]


def _wait_for_server(timeout: int = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(URL + "/health", timeout=1).status_code == 200:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def _run_tray(process: subprocess.Popen[bytes]) -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (79, 104, 232, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 8, 56, 56), radius=14, fill=(42, 54, 125, 255))
        draw.text((25, 18), "J", fill="white")

        def open_app(icon, item) -> None:
            webbrowser.open(URL)

        def exit_app(icon, item) -> None:
            icon.stop()
            if process.poll() is None:
                process.terminate()

        menu = pystray.Menu(pystray.MenuItem("打开 JobPostings", open_app), pystray.MenuItem("退出", exit_app))
        pystray.Icon("JobPostings", image, "JobPostings", menu).run()
    except ImportError:
        webbrowser.open(URL)
        process.wait()


def main() -> int:
    env = os.environ.copy()
    if not env.get("JOBPOSTINGS_DATA_DIR"):
        local_app_data = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        env["JOBPOSTINGS_DATA_DIR"] = str(Path(local_app_data) / "JobPostings")
    if not getattr(sys, "frozen", False):
        env["PYTHONPATH"] = str(_root() / "backend")
    process = subprocess.Popen(_backend_command(), cwd=_root(), env=env)
    if not _wait_for_server():
        process.terminate()
        return 1
    webbrowser.open(URL)
    _run_tray(process)
    return process.wait() if process.poll() is None else int(process.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
