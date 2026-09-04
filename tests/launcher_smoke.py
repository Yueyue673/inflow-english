from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
PORT = 8774
BASE = f"http://127.0.0.1:{PORT}"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def health() -> dict | None:
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=1) as response:
            return json.load(response)
    except Exception:
        return None


def listener_pid() -> int | None:
    return next(
        (
            connection.pid
            for connection in psutil.net_connections(kind="tcp")
            if connection.status == "LISTEN" and connection.laddr and connection.laddr.port == PORT
        ),
        None,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="inflow-adaptive-launcher-") as data_dir:
        env = os.environ.copy()
        env.update(
            {
                "INFLOW_ADAPTIVE_DATA_DIR": data_dir,
                "INFLOW_ADAPTIVE_PORT": str(PORT),
                "INFLOW_LAUNCHER_NO_BROWSER": "1",
            }
        )
        command = [sys.executable, str(ROOT / "launcher.py")]
        subprocess.run(command, cwd=str(ROOT), env=env, check=True, creationflags=CREATE_NO_WINDOW)
        for _ in range(80):
            payload = health()
            if payload and payload.get("product") == "inflow-english":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("launcher did not start the adaptive server")
        first_pid = listener_pid()
        if not first_pid:
            raise AssertionError("listener pid missing")
        competing_env = env.copy()
        competing_env["INFLOW_ADAPTIVE_PORT"] = "8775"
        competing = subprocess.run(
            [sys.executable, str(ROOT / "server.py")],
            cwd=str(ROOT),
            env=competing_env,
            creationflags=CREATE_NO_WINDOW,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if competing.returncode == 0 or "data directory already owned" not in competing.stderr:
            raise AssertionError({"returncode": competing.returncode, "stderr": competing.stderr})
        subprocess.run(command, cwd=str(ROOT), env=env, check=True, creationflags=CREATE_NO_WINDOW)
        time.sleep(0.3)
        second_pid = listener_pid()
        if second_pid != first_pid:
            raise AssertionError({"first": first_pid, "second": second_pid})
        process = psutil.Process(first_pid)
        process.terminate()
        try:
            process.wait(timeout=5)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        time.sleep(0.2)
        print(
            json.dumps(
                {
                    "ok": True,
                    "port": PORT,
                    "first_pid": first_pid,
                    "second_pid": second_pid,
                    "single_instance": first_pid == second_pid,
                    "data_directory_lock": competing.returncode != 0,
                    "product": payload["product"],
                    "console_window_requested": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
