from __future__ import annotations

import hashlib
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
PACKS = Path(os.environ["INFLOW_A24_REVIEWED_PACKS"]) if os.environ.get("INFLOW_A24_REVIEWED_PACKS") else None
CHROME = Path(os.environ.get("INFLOW_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
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
        try: target.terminate()
        except psutil.Error: pass
    _, alive = psutil.wait_procs(targets, timeout=8)
    for target in alive:
        try: target.kill()
        except psutil.Error: pass
    psutil.wait_procs(alive, timeout=5)


def main() -> None:
    if PACKS is None:
        raise SystemExit("Set INFLOW_A24_REVIEWED_PACKS to run the private reviewed-media test.")
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(PACKS.glob("*/manifest.json"))]
    if len(manifests) != 2:
        raise AssertionError({"manifest_count": len(manifests)})
    live_events = ROOT / "data" / "live" / "events.jsonl"
    live_hash_before = hashlib.sha256(live_events.read_bytes()).hexdigest() if live_events.exists() else None
    with tempfile.TemporaryDirectory(prefix="inflow-a24-reviewed-") as temporary:
        data_dir = Path(temporary)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update({"INFLOW_ADAPTIVE_DATA_DIR": str(data_dir), "INFLOW_PACKS_DIR": str(PACKS), "INFLOW_ADAPTIVE_QA": "1", "INFLOW_ADAPTIVE_PORT": str(port)})
        server = subprocess.Popen([os.sys.executable, "server.py"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=CREATE_NO_WINDOW)
        try:
            for _ in range(100):
                try:
                    if fetch(base, "/api/health").get("ok"): break
                except Exception: time.sleep(0.1)
            else: raise AssertionError("server_not_ready")
            page_errors = []
            console_errors = []
            results = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME), args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"])
                page = browser.new_page(viewport={"width": 1360, "height": 900})
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.goto(base + "/?qa=1", wait_until="networkidle")
                page.locator('label:has(input[value="high"])').click()
                for manifest_index, manifest in enumerate(manifests):
                    page.locator(f'.pack-option[data-pack-id="{manifest["pack_id"]}"]').click()
                    packs_payload = fetch(base, "/api/packs")
                    pack_row = next(row for row in packs_payload["packs"] if row["pack_id"] == manifest["pack_id"])
                    expected = int(pack_row["intervention_budgets"]["high"])
                    meta = page.locator(f'.pack-option[data-pack-id="{manifest["pack_id"]}"] .pack-meta').inner_text()
                    if f"找到 {len(manifest['candidates'])} 个可用表达" not in meta or f"高频预计介入 {expected} 次" not in meta:
                        raise AssertionError({"meta": meta, "expected": expected})
                    page.locator("#startButton").click()
                    page.locator("#watchPanel").wait_for(state="visible", timeout=15_000)
                    page.wait_for_function("() => document.querySelector('#sourceVideo').duration > 0")
                    session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
                    session = fetch(base, f"/api/sessions/{session_id}")
                    if len(session["items"]) != expected:
                        raise AssertionError({"items": len(session["items"]), "expected": expected})
                    completed = []
                    for item_index, item in enumerate(sorted(session["items"], key=lambda row: row["anchor_sec"])):
                        page.locator("#sourceVideo").evaluate("(video, anchor) => { video.currentTime = Math.max(0, anchor - .35); return video.play(); }", item["anchor_sec"])
                        page.locator("#mappingContinue:not([disabled])").wait_for(state="visible", timeout=20_000)
                        if page.locator(".mapping-word").inner_text() != item["surface"] or page.locator(".mapping-gloss").inner_text() != item["gloss_zh"]:
                            raise AssertionError({"item": item, "word": page.locator(".mapping-word").inner_text(), "gloss": page.locator(".mapping-gloss").inner_text()})
                        if page.locator(".mapping-context").inner_text() != item["phrase_text"] or page.locator(".mapping-translation").inner_text() != item["phrase_zh"]:
                            raise AssertionError("mapping_scope_mismatch")
                        if manifest_index == 1 and item_index == 0:
                            screenshot = ROOT / "data" / "a24-reviewed-mapping.png"
                            page.screenshot(path=str(screenshot), full_page=True)
                        page.locator("#mappingContinue").click()
                        page.locator("#learningLayer").wait_for(state="hidden", timeout=5_000)
                        completed.append(item["id"])
                    page.locator("#sourceVideo").evaluate("video => { video.currentTime = Math.max(0, video.duration - .1); return video.play(); }")
                    page.locator("#completePanel").wait_for(state="visible", timeout=15_000)
                    final = fetch(base, f"/api/sessions/{session_id}")
                    if final["interaction_summary"]["completed"] != expected:
                        raise AssertionError(final["interaction_summary"])
                    results.append({"pack_id": manifest["pack_id"], "title": manifest["title"], "candidate_pool": len(manifest["candidates"]), "budget_high": expected, "completed": completed})
                    page.locator("#homeButton").click()
                    page.locator("#home").wait_for(state="visible", timeout=10_000)
                browser.close()
            rebuild = subprocess.run([os.sys.executable, "rebuild_profile.py", "--data-dir", str(data_dir)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW)
            rebuilt = json.loads(rebuild.stdout.strip().splitlines()[-1])
            if rebuild.returncode != 0 or not rebuilt["matches_existing"]: raise AssertionError(rebuilt)
            live_hash_after = hashlib.sha256(live_events.read_bytes()).hexdigest() if live_events.exists() else None
            if live_hash_before != live_hash_after: raise AssertionError("formal_events_changed")
            if page_errors or console_errors: raise AssertionError({"page_errors": page_errors, "console_errors": console_errors})
            print(json.dumps({"ok": True, "packs": results, "rebuild_matches": True, "formal_events_unchanged": True, "page_errors": page_errors, "console_errors": console_errors, "screenshot": str(screenshot)}, ensure_ascii=False, indent=2))
        finally:
            stop_tree(server.pid)


if __name__ == "__main__":
    main()
