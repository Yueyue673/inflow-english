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
PACKS = Path(os.environ["LOCALAPPDATA"]) / "Temp" / "inflow-pack-rejected-final-qa"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(base, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=8) as response:
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
    manifest_path = next(PACKS.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    transcript = json.loads((manifest_path.parent / "transcript.json").read_text(encoding="utf-8"))
    captions = json.loads((manifest_path.parent / "captions-zh.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="inflow-rejected-url-browser-") as temporary:
        port = free_port()
        data_dir = Path(temporary)
        env = os.environ.copy()
        env.update({"INFLOW_ADAPTIVE_DATA_DIR": str(data_dir), "INFLOW_PACKS_DIR": str(PACKS), "INFLOW_ADAPTIVE_QA": "1", "INFLOW_ADAPTIVE_PORT": str(port)})
        process = subprocess.Popen([os.sys.executable, "server.py"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=CREATE_NO_WINDOW)
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
            page_errors = []
            console_errors = []
            media_statuses = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME), args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"])
                page = browser.new_page(viewport={"width": 1360, "height": 900})
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.on("response", lambda response: media_statuses.append(response.status) if f"/api/packs/{manifest['pack_id']}/files/video.mp4" in response.url else None)
                page.goto(base + "/?qa=1", wait_until="networkidle")
                page.locator("#videoUrl").fill(manifest["source_url"])
                page.locator("#prepareButton").click()
                page.get_by_text(f"《{manifest['title']}》准备好了。").wait_for(timeout=5_000)
                page.locator('label:has(input[value="high"])').click()
                page.locator("#startButton").click()
                page.locator("#watchPanel").wait_for(state="visible", timeout=15_000)
                page.wait_for_function("() => document.querySelector('#sourceVideo').duration > 360")
                session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
                session = fetch(base, f"/api/sessions/{session_id}")
                items = sorted(session["items"], key=lambda row: row["anchor_sec"])
                if len(items) != 4 or len({round(item["anchor_sec"], 3) for item in items}) != 4:
                    raise AssertionError({"items": items})
                learned = []
                for item in items:
                    page.locator("#sourceVideo").evaluate("(video, anchor) => { video.currentTime = anchor - 0.35; return video.play(); }", item["anchor_sec"])
                    page.locator("#mappingContinue:not([disabled])").wait_for(state="visible", timeout=20_000)
                    if page.locator(".mapping-context").inner_text() != item["phrase_text"] or page.locator(".mapping-translation").inner_text() != item["phrase_zh"]:
                        raise AssertionError({"item": item, "english": page.locator(".mapping-context").inner_text(), "chinese": page.locator(".mapping-translation").inner_text()})
                    page.wait_for_timeout(900)
                    if not page.locator("#mappingContinue").is_visible():
                        raise AssertionError("mapping_auto_dismissed")
                    page.locator("#mappingContinue").click()
                    page.wait_for_function("() => !document.querySelector('#sourceVideo').paused")
                    page.locator("#learningLayer").wait_for(state="hidden", timeout=5_000)
                    learned.append(item["id"])
                page.locator("#sourceVideo").evaluate("video => { video.currentTime = video.duration - 0.1; return video.play(); }")
                page.locator("#completePanel").wait_for(state="visible", timeout=15_000)
                screenshot = ROOT / "data" / "rejected-url-now-works.png"
                page.screenshot(path=str(screenshot), full_page=True)
                browser.close()
            final_session = fetch(base, f"/api/sessions/{session_id}")
            if final_session["interaction_summary"]["completed"] != 4:
                raise AssertionError(final_session["interaction_summary"])
            rebuild = subprocess.run([os.sys.executable, "rebuild_profile.py", "--data-dir", str(data_dir)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW)
            if rebuild.returncode != 0 or not json.loads(rebuild.stdout.strip().splitlines()[-1])["matches_existing"]:
                raise AssertionError({"stdout": rebuild.stdout, "stderr": rebuild.stderr})
            if not media_statuses or not all(status in {200, 206} for status in media_statuses):
                raise AssertionError(media_statuses)
            if page_errors or console_errors:
                raise AssertionError({"page_errors": page_errors, "console_errors": console_errors})
            print(json.dumps({"ok": True, "pack_id": manifest["pack_id"], "title": manifest["title"], "cue_source": transcript["cue_source"], "caption_source": captions["source"], "candidate_count": len(manifest["candidates"]), "learned": learned, "interaction_summary": final_session["interaction_summary"], "rebuild_matches": True, "media_statuses": media_statuses, "page_errors": page_errors, "console_errors": console_errors, "screenshot": str(screenshot)}, ensure_ascii=False, indent=2))
        finally:
            stop_tree(process.pid)


if __name__ == "__main__":
    main()
