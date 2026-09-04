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
PACKS = Path(os.environ["LOCALAPPDATA"]) / "Temp" / "inflow-short-video-qa"
URL = "https://www.youtube.com/watch?v=UwMS0J2eTXE"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(base: str, path: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=8) as response:
        return json.load(response)


def stop_tree(pid: int) -> None:
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


def main() -> None:
    manifest_path = next(PACKS.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="inflow-short-frequency-") as temporary:
        port = free_port()
        data_dir = Path(temporary)
        env = os.environ.copy()
        env.update(
            {
                "INFLOW_ADAPTIVE_DATA_DIR": str(data_dir),
                "INFLOW_PACKS_DIR": str(PACKS),
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
            for _ in range(100):
                try:
                    if fetch(base, "/api/health").get("ok"):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError("server_not_ready")

            page_errors: list[str] = []
            console_errors: list[str] = []
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
                if page.locator('fieldset[aria-label="介入频率"]').count() != 1:
                    raise AssertionError("frequency_control_not_renamed")
                medium_copy = "0:58 · 发现 4 个 · 当前可选 4 个 · 中频预计介入 1 次 · 已准备"
                page.get_by_text(medium_copy, exact=True).wait_for(timeout=8_000)

                page.locator("#videoUrl").fill(URL)
                page.locator("#prepareButton").click()
                page.get_by_text(f"《{manifest['title']}》准备好了。").wait_for(timeout=8_000)

                page.locator('label:has(input[value="high"])').click()
                high_copy = "0:58 · 发现 4 个 · 当前可选 4 个 · 高频预计介入 1 次 · 已准备"
                page.get_by_text(high_copy, exact=True).wait_for(timeout=8_000)
                home_screenshot = ROOT / "data" / "short-video-frequency-home.png"
                page.screenshot(path=str(home_screenshot), full_page=True)
                page.locator("#startButton").click()
                page.locator("#watchPanel").wait_for(state="visible", timeout=15_000)
                session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
                session = fetch(base, f"/api/sessions/{session_id}")
                if session["candidate_pool_count"] != 4:
                    raise AssertionError(session)
                if session["intervention_budgets"] != {"low": 1, "medium": 1, "high": 1}:
                    raise AssertionError(session["intervention_budgets"])
                if len(session["items"]) != 1:
                    raise AssertionError(session["items"])
                item = session["items"][0]
                page.locator("#sourceVideo").evaluate(
                    "(video, anchor) => { video.currentTime = anchor - 0.35; return video.play(); }",
                    item["anchor_sec"],
                )
                page.locator("#mappingContinue:not([disabled])").wait_for(state="visible", timeout=20_000)
                if page.locator(".mapping-context").inner_text() != item["phrase_text"]:
                    raise AssertionError("phrase_text_mismatch")
                page.locator("#mappingContinue").click()
                page.locator("#learningLayer").wait_for(state="hidden", timeout=5_000)
                page.locator("#sourceVideo").evaluate("video => { video.currentTime = video.duration - 0.1; return video.play(); }")
                page.locator("#completePanel").wait_for(state="visible", timeout=15_000)
                screenshot = ROOT / "data" / "short-video-frequency-final.png"
                page.screenshot(path=str(screenshot), full_page=True)
                browser.close()

            final_session = fetch(base, f"/api/sessions/{session_id}")
            if final_session["interaction_summary"] != {"completed": 1, "skipped": 0, "technical_failure": 0}:
                raise AssertionError(final_session["interaction_summary"])
            rebuild = subprocess.run(
                [os.sys.executable, "rebuild_profile.py", "--data-dir", str(data_dir)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=CREATE_NO_WINDOW,
            )
            rebuilt = json.loads(rebuild.stdout.strip().splitlines()[-1])
            if rebuild.returncode != 0 or not rebuilt["matches_existing"]:
                raise AssertionError({"stdout": rebuild.stdout, "stderr": rebuild.stderr})
            if page_errors or console_errors:
                raise AssertionError({"page_errors": page_errors, "console_errors": console_errors})
            print(
                json.dumps(
                    {
                        "ok": True,
                        "pack_id": manifest["pack_id"],
                        "title": manifest["title"],
                        "duration_sec": manifest["duration_sec"],
                        "effective_speech_sec": manifest["effective_speech_sec"],
                        "candidate_pool_count": 4,
                        "intervention_budgets": {"low": 1, "medium": 1, "high": 1},
                        "completed": 1,
                        "rebuild_matches": True,
                        "page_errors": page_errors,
                        "console_errors": console_errors,
                        "home_screenshot": str(home_screenshot),
                        "screenshot": str(screenshot),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            stop_tree(process.pid)


if __name__ == "__main__":
    main()
