from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import psutil
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
URL = "https://www.youtube.com/watch?v=aQ89CG_Yu6I"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(base, path):
    with urllib.request.urlopen(base + path, timeout=8) as response:
        return json.load(response)


def stop_tree(pid):
    try:
        root = psutil.Process(pid)
        targets = root.children(recursive=True) + [root]
    except psutil.Error:
        return
    for target in targets:
        try:
            target.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(targets, timeout=8)
    for target in alive:
        try:
            target.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=5)


def main():
    with tempfile.TemporaryDirectory(prefix="inflow-real-import-") as temporary:
        root = Path(temporary)
        data_dir = root / "live"
        packs_dir = root / "packs"
        port = free_port()
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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        base = f"http://127.0.0.1:{port}"
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

            stages = []
            page_errors = []
            console_errors = []
            started = time.perf_counter()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    executable_path=str(CHROME),
                    args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
                )
                page = browser.new_page(viewport={"width": 1360, "height": 900})
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.goto(base + "/?qa=1", wait_until="networkidle")
                if page.locator("#libraryCount").inner_text() != "0 个真实视频":
                    raise AssertionError("test did not start from an empty library")
                page.locator("#videoUrl").fill(URL)
                page.locator("#prepareButton").click()

                deadline = time.time() + 330
                last = None
                while time.time() < deadline:
                    text = page.locator("#importStatus").inner_text().strip()
                    if text and text != last:
                        stages.append(text.splitlines()[0])
                        last = text
                    if "准备好了" in text:
                        break
                    if page.locator("#importStatus").get_attribute("class") and "is-error" in page.locator("#importStatus").get_attribute("class"):
                        job_diagnostics = [
                            json.loads(path.read_text(encoding="utf-8"))
                            for path in (data_dir / "import-jobs").glob("*.json")
                        ]
                        raise AssertionError({"import_failed": text, "jobs": job_diagnostics})
                    page.wait_for_timeout(500)
                else:
                    raise AssertionError({"import_timeout": stages})
                ready_seconds = round(time.perf_counter() - started, 2)
                if ready_seconds > 300:
                    raise AssertionError({"ready_seconds": ready_seconds, "stages": stages})
                page.get_by_text("The Young and The End", exact=True).wait_for(timeout=5_000)
                if page.locator(".pack-option[aria-selected='true']").get_attribute("data-pack-id") == "fixture-nasa-lro-v1":
                    raise AssertionError("new pack was not selected")
                page.screenshot(path=str(ROOT / "data" / "universal-import-ready.png"), full_page=True)

                page.locator("#startButton").click()
                page.locator("#watchPanel").wait_for(state="visible", timeout=15_000)
                page.wait_for_function("() => document.querySelector('#sourceVideo').duration > 700")
                session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
                session = fetch(base, f"/api/sessions/{session_id}")
                allowed = [item for item in session["items"] if item["min_intensity"] in {"low", "medium"}]
                item = sorted(allowed, key=lambda row: row["anchor_sec"])[0]
                page.locator("#sourceVideo").evaluate(
                    "(video, anchor) => { video.currentTime = anchor - 0.35; return video.play(); }",
                    item["anchor_sec"],
                )
                page.locator("#mappingContinue:not([disabled])").wait_for(state="visible", timeout=20_000)
                if page.locator(".mapping-context").inner_text() != item["phrase_text"] or page.locator(".mapping-translation").inner_text() != item["phrase_zh"]:
                    raise AssertionError("first imported teaching card is not pack-grounded")
                page.wait_for_timeout(1200)
                if not page.locator("#mappingContinue").is_visible():
                    raise AssertionError("imported mapping auto-dismissed")
                page.locator("#mappingContinue").click()
                page.wait_for_function("() => !document.querySelector('#sourceVideo').paused")
                browser.close()

            packs = fetch(base, "/api/packs")["packs"]
            job_files = list((data_dir / "import-jobs").glob("*.json"))
            job = json.loads(job_files[0].read_text(encoding="utf-8"))
            if job["status"] != "ready" or job["cached"]:
                raise AssertionError(job)
            manifest = json.loads((packs_dir / job["pack_id"] / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("translator") not in {
                "dsh:headless:reasoning-none:video-pack-translation-selection/9",
                "google-translate-web+wordfreq-context/v3",
                "argos-en-zh-offline+wordfreq-context/v1",
            }:
                raise AssertionError({"translator": manifest.get("translator")})
            if len([row for row in packs if not row["fixture"]]) != 1:
                raise AssertionError(packs)
            if page_errors or console_errors:
                raise AssertionError({"page_errors": page_errors, "console_errors": console_errors})
            print(
                json.dumps(
                    {
                        "ok": True,
                        "ready_seconds": ready_seconds,
                        "stages_seen": stages,
                        "pack_id": job["pack_id"],
                        "translator_runtime": manifest["translator"],
                        "candidate_count": len(manifest["candidates"]),
                        "first_teaching_item": item["id"],
                        "mapping_held_until_click": True,
                        "page_errors": page_errors,
                        "console_errors": console_errors,
                        "screenshot": str(ROOT / "data" / "universal-import-ready.png"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            stop_tree(process.pid)


if __name__ == "__main__":
    main()
