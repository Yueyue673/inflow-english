from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BROWSERS = os.environ.get("INFLOW_PLAYWRIGHT_BROWSERS")
if BROWSERS:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS
from playwright.sync_api import sync_playwright

from adaptive_core import atomic_write_json, knowledge_key_for_item, new_profile, seed_lexicon_state
from lexical_sense import context_fingerprint
from tests.extension_test_support import copy_extension_for_browser
from tests.live_data_guard import snapshot_live_tree

SOURCE_EXTENSION = ROOT / "extension"
SOURCE_VIDEO = ROOT.parent / "mechanism-experiment" / "media" / "source.mp4"
VIDEO_ID = "bankctx0001"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
CLIENT_ID = "1" * 32
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FIRST = "They sat by the bank and watched the river."
SECOND = "She called the bank about her account."


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(base: str, path: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=10) as response:
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


def main() -> None:
    live_root = ROOT / "data" / "live"
    live_snapshot_before = snapshot_live_tree(live_root)
    with tempfile.TemporaryDirectory(prefix="inflow-lexicon-context-") as temporary:
        work = Path(temporary)
        data_dir = work / "data"
        packs_dir = work / "packs"
        extension_dir = work / "extension"
        copy_extension_for_browser(SOURCE_EXTENSION, extension_dir, open_shadow_for_test=True)
        content = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))
        profile = new_profile(content)
        first_item = {"id": "subtitle:river-bank", "surface": "bank", "phrase_text": FIRST}
        second_item = {"id": "subtitle:financial-bank", "surface": "bank", "phrase_text": SECOND}
        first_key = knowledge_key_for_item(first_item)
        second_key = knowledge_key_for_item(second_item)
        first_context = context_fingerprint(FIRST)
        second_context = context_fingerprint(SECOND)
        first_entry = seed_lexicon_state(first_key, "bank", "河岸", known=True, context_id=first_context)
        first_entry["occurrence_ids"] = [first_item["id"]]
        second_entry = seed_lexicon_state(second_key, "bank", "银行", context_id=second_context)
        second_entry["occurrence_ids"] = [second_item["id"]]
        profile["lexicon"][first_key] = first_entry
        profile["lexicon"][second_key] = second_entry
        atomic_write_json(data_dir / "profile.json", profile)

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        worker = extension_dir / "service-worker.js"
        worker.write_text(worker.read_text(encoding="utf-8").replace("http://127.0.0.1:8767", base), encoding="utf-8")
        manifest_path = extension_dir / "manifest.json"
        extension_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        extension_manifest["host_permissions"] = ["https://www.youtube.com/*", f"{base}/*"]
        manifest_path.write_text(json.dumps(extension_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        env = os.environ.copy()
        env.update({"INFLOW_ADAPTIVE_DATA_DIR": str(data_dir), "INFLOW_PACKS_DIR": str(packs_dir), "INFLOW_ADAPTIVE_QA": "1", "INFLOW_ADAPTIVE_PORT": str(port)})
        server = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=CREATE_NO_WINDOW)
        try:
            for _ in range(150):
                try:
                    if fetch(base, "/api/health").get("ok"):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                output = server.stdout.read() if server.poll() is not None and server.stdout else "process_alive_without_health"
                raise AssertionError({"server_not_ready": output})

            caption_response = {
                "videoDetails": {"videoId": VIDEO_ID, "title": "Lexicon context fixture", "lengthSeconds": "154.5"},
                "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{
                    "baseUrl": f"https://www.youtube.com/api/timedtext?v={VIDEO_ID}&lang=en",
                    "languageCode": "en",
                    "isTranslatable": True,
                }]}},
            }
            english_payload = {"events": [
                {"tStartMs": 0, "dDurationMs": 6000, "segs": [{"utf8": FIRST}]},
                {"tStartMs": 6000, "dDurationMs": 6000, "segs": [{"utf8": SECOND}]},
            ]}
            chinese_payload = {"events": [
                {"tStartMs": 0, "dDurationMs": 6000, "segs": [{"utf8": "他们坐在河岸边看河水。"}]},
                {"tStartMs": 6000, "dDurationMs": 6000, "segs": [{"utf8": "她打电话给银行询问账户。"}]},
            ]}
            html = f"""<!doctype html><html><head><meta charset='utf-8'><style>html,body{{margin:0;background:#111}}#movie_player{{width:960px;height:540px;position:relative}}video{{width:960px;height:540px}}.ytp-subtitles-button{{position:absolute;right:8px;bottom:8px}}</style></head><body><div id='movie_player'><video class='html5-main-video' muted autoplay controls src='https://www.youtube.com/context-source.mp4'></video><button class='ytp-subtitles-button' aria-pressed='false'>CC</button></div><script>const response={json.dumps(caption_response)};const player=document.querySelector('#movie_player');player.getPlayerResponse=()=>response;document.querySelector('.ytp-subtitles-button').onclick=e=>e.currentTarget.setAttribute('aria-pressed',e.currentTarget.getAttribute('aria-pressed')==='true'?'false':'true')</script></body></html>"""
            page_errors = []
            worker_errors = []
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(work / "chrome-profile"), headless=False,
                    args=["--window-position=-32000,-32000", "--window-size=1280,900", f"--disable-extensions-except={extension_dir}", f"--load-extension={extension_dir}", "--no-first-run", "--autoplay-policy=no-user-gesture-required", "--mute-audio"],
                )
                service_worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15_000)
                service_worker.on("console", lambda message: worker_errors.append(message.text) if message.type == "error" else None)
                service_worker.evaluate("chrome.storage.local.set({autoMode:true,autoLearning:true,displaySize:'medium'})")
                page = context.pages[0] if context.pages else context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.route("https://www.youtube.com/context-source.mp4", lambda route: route.fulfill(path=str(SOURCE_VIDEO), content_type="video/mp4"))
                page.route(URL, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                page.route(
                    "https://www.youtube.com/api/timedtext**",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(chinese_payload if parse_qs(urlsplit(route.request.url).query).get("tlang") else english_payload, ensure_ascii=False),
                    ),
                )
                page.goto(URL, wait_until="load", timeout=30_000)
                page.locator("video").wait_for(state="attached", timeout=10_000)
                page.wait_for_function("() => document.querySelector('video')?.duration > 100", timeout=20_000)
                host = page.locator("#inflow-extension-root")
                host.wait_for(state="attached", timeout=10_000)

                page.locator("video").evaluate("video => { video.pause(); video.currentTime=2; }")
                try:
                    page.wait_for_function("() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#captionEnglish')?.textContent.includes('watched the river')", timeout=15_000)
                except Exception as exc:
                    state = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; const video=document.querySelector('video'); return {time:video?.currentTime,paused:video?.paused,pill:root?.querySelector('#pill')?.textContent,message:root?.querySelector('#message')?.textContent,captionHidden:root?.querySelector('#caption')?.hidden,english:root?.querySelector('#captionEnglish')?.textContent,chinese:root?.querySelector('#captionChinese')?.textContent,cc:document.querySelector('.ytp-subtitles-button')?.getAttribute('aria-pressed')}; }""")
                    raise AssertionError({"state": state, "page_errors": page_errors, "worker_errors": worker_errors}) from exc
                first_bank = host.locator('#captionEnglish .token').filter(has_text="bank")
                first_bank.wait_for(state="visible", timeout=5_000)
                if first_bank.get_attribute("data-status") != "known" or first_bank.get_attribute("data-knowledge-key") != first_key:
                    raise AssertionError({"first_status": first_bank.get_attribute("data-status"), "first_key": first_bank.get_attribute("data-knowledge-key")})

                page.locator("video").evaluate("video => video.play()")
                page.wait_for_function("() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#captionEnglish')?.textContent.includes('called the bank')", timeout=10_000)
                page.locator("video").evaluate("video => video.pause()")
                second_bank = host.locator('#captionEnglish .token').filter(has_text="bank")
                second_bank.wait_for(state="visible", timeout=5_000)
                if second_bank.get_attribute("data-status") != "unseen" or second_bank.get_attribute("data-knowledge-key") != second_key:
                    raise AssertionError({"second_status": second_bank.get_attribute("data-status"), "second_key": second_bank.get_attribute("data-knowledge-key")})
                second_bank.click()
                host.locator("#wordPanel").wait_for(state="visible", timeout=5_000)
                if "银行" not in host.locator("#wordGloss").inner_text():
                    raise AssertionError(host.locator("#wordGloss").inner_text())
                host.locator('[data-word-feedback="familiar"]').click()
                for _ in range(80):
                    entries = fetch(base, "/api/lexicon")["entries"]
                    states = {entry["knowledge_key"]: entry["status"] for entry in entries}
                    if states.get(second_key) == "familiar":
                        break
                    page.wait_for_timeout(100)
                else:
                    raise AssertionError("second_context_feedback_not_saved")
                if states.get(first_key) != "known":
                    raise AssertionError(states)
                page.wait_for_function("() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#wordPanel').hidden", timeout=5_000)

                screenshot = ROOT / "data" / "lexicon-context-e2e.png"
                page.screenshot(path=str(screenshot), full_page=True)
                context.close()

            live_snapshot_after = snapshot_live_tree(live_root)
            if live_snapshot_after != live_snapshot_before:
                changed = sorted(path for path in set(live_snapshot_before) | set(live_snapshot_after) if live_snapshot_before.get(path) != live_snapshot_after.get(path))
                raise AssertionError({"formal_live_tree_changed": changed})
            if page_errors or worker_errors:
                raise AssertionError({"page_errors": page_errors, "worker_errors": worker_errors})
            print(json.dumps({
                "ok": True,
                "surface": "bank",
                "first_context": {"key": first_key, "status": "known"},
                "second_context": {"key": second_key, "status": "familiar"},
                "keys_are_distinct": first_key != second_key,
                "formal_live_tree_unchanged": True,
                "page_errors": page_errors,
                "worker_errors": worker_errors,
                "screenshot": str(screenshot),
            }, ensure_ascii=False, indent=2))
        finally:
            stop_tree(server.pid)


if __name__ == "__main__":
    main()
