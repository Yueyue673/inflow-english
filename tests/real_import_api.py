from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.real_import_browser import CREATE_NO_WINDOW, URL, fetch, free_port, stop_tree


def post(base: str, path: str, payload: dict):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=8) as response:
        return json.load(response)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="inflow-real-import-api-") as temporary:
        root = Path(temporary)
        data_dir = root / "live"
        packs_dir = root / "packs"
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update(
            {
                "INFLOW_ADAPTIVE_DATA_DIR": str(data_dir),
                "INFLOW_PACKS_DIR": str(packs_dir),
                "INFLOW_ADAPTIVE_QA": "1",
                "INFLOW_ADAPTIVE_PORT": str(port),
            }
        )
        process = subprocess.Popen(
            [os.sys.executable, "server.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    if fetch(base, "/api/health").get("ok"):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError("server_not_ready")
            job = post(base, "/api/imports", {"url": URL})
            job_id = job["job_id"]
            stages = []
            started = time.perf_counter()
            deadline = time.time() + 330
            while time.time() < deadline:
                job = fetch(base, f"/api/imports/{job_id}")
                if not stages or stages[-1] != job["stage"]:
                    stages.append(job["stage"])
                if job["status"] == "ready":
                    break
                if job["status"] in {"failed", "cancelled"}:
                    raise AssertionError({"job": job, "stages": stages})
                time.sleep(0.25)
            else:
                raise AssertionError({"timeout": True, "job": job, "stages": stages})
            manifest = json.loads((packs_dir / job["pack_id"] / "manifest.json").read_text(encoding="utf-8"))
            print(
                json.dumps(
                    {
                        "ok": True,
                        "elapsed_sec": round(time.perf_counter() - started, 2),
                        "stages": stages,
                        "pack_id": job["pack_id"],
                        "translator_runtime": manifest["translator"],
                        "candidate_count": len(manifest["candidates"]),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            stop_tree(process.pid)


if __name__ == "__main__":
    main()
