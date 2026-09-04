from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "InFlow-English"
LEGACY_DATA_DIR = ROOT / "data" / "live"
DATA_DIR = Path(
    os.environ.get(
        "INFLOW_ADAPTIVE_DATA_DIR",
        str(LEGACY_DATA_DIR if LEGACY_DATA_DIR.exists() else LOCAL_DATA_ROOT / "data"),
    )
).resolve()
PORT = int(os.environ.get("INFLOW_ADAPTIVE_PORT", "8767"))
URL = f"http://127.0.0.1:{PORT}/"
HEALTH = URL + "api/health"
EXPECTED_PRODUCT = "inflow-english"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_LAUNCHER_LOG_BYTES = 512 * 1024


def open_launcher_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a"
    try:
        if path.stat().st_size >= MAX_LAUNCHER_LOG_BYTES:
            mode = "w"
    except FileNotFoundError:
        pass
    log = path.open(mode, encoding="utf-8")
    if mode == "w":
        log.write("[launcher log reset after size limit]\n")
        log.flush()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return log


def health_payload() -> dict | None:
    try:
        with urllib.request.urlopen(HEALTH, timeout=1.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def healthy() -> bool:
    payload = health_payload()
    return bool(payload and payload.get("ok") is True and payload.get("product") == EXPECTED_PRODUCT)


def message(text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, "InFlow English", 0x10)
    except Exception:
        pass


def main() -> None:
    if not healthy():
        existing = health_payload()
        if existing:
            message(f"{PORT} 端口上仍是旧版 InFlow。请重新双击一次；如果仍出现，告诉我即可。")
            return
        log_path = DATA_DIR / "launcher.log"
        log = open_launcher_log(log_path)
        subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
        log.close()
        for _ in range(80):
            if healthy():
                break
            time.sleep(0.25)
        else:
            message(f"InFlow 没有启动。日志保存在：{log_path}")
            return
    if os.environ.get("INFLOW_LAUNCHER_NO_BROWSER") != "1":
        webbrowser.open(URL)


if __name__ == "__main__":
    main()
