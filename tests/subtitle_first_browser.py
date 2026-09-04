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

from tests.extension_test_support import copy_extension_for_browser
from tests.live_data_guard import snapshot_live_tree

SOURCE_EXTENSION = ROOT / "extension"
SOURCE_VIDEO = ROOT.parent / "mechanism-experiment" / "media" / "source.mp4"
VIDEO_ID = "moUP4npVea8"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(base: str, path: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=10) as response:
        return json.load(response)


def post(base: str, path: str, payload: dict):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=10) as response:
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
    live_root = ROOT / "data" / "live"
    live_snapshot_before = snapshot_live_tree(live_root)
    with tempfile.TemporaryDirectory(prefix="inflow-subtitle-first-") as temporary:
        work = Path(temporary)
        extension = work / "extension"
        profile_dir = work / "chrome-profile"
        data_dir = work / "data"
        packs_dir = work / "empty-packs"
        packs_dir.mkdir()
        copy_extension_for_browser(SOURCE_EXTENSION, extension, open_shadow_for_test=True)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        worker_path = extension / "service-worker.js"
        worker_path.write_text(worker_path.read_text(encoding="utf-8").replace("http://127.0.0.1:8767", base), encoding="utf-8")
        manifest_path = extension / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["host_permissions"] = ["https://www.youtube.com/*", f"{base}/*"]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        env = os.environ.copy()
        env.update({
            "INFLOW_ADAPTIVE_DATA_DIR": str(data_dir),
            "INFLOW_PACKS_DIR": str(packs_dir),
            "INFLOW_ADAPTIVE_QA": "1",
            "INFLOW_ADAPTIVE_PORT": str(port),
        })
        server = subprocess.Popen(
            [os.sys.executable, "server.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            for _ in range(120):
                try:
                    if fetch(base, "/api/health").get("ok"):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError("isolated_server_not_ready")

            page_errors = []
            extension_errors = []
            bridge_seen = False
            english_text = "A complete English sentence for subtitle-first timing."
            chinese_text = "这是一句用于字幕优先时序验证的完整中文。"
            english_payload = {"events": [{"tStartMs": 1000, "dDurationMs": 5000, "segs": [{"utf8": english_text}]}]}
            chinese_payload = {"events": [{"tStartMs": 1000, "dDurationMs": 5000, "segs": [{"utf8": chinese_text}]}]}
            player_response = {
                "videoDetails": {"videoId": VIDEO_ID, "title": "Subtitle first fixture", "lengthSeconds": "154.5"},
                "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{
                    "baseUrl": f"https://www.youtube.com/api/timedtext?v={VIDEO_ID}&lang=en",
                    "languageCode": "en",
                    "isTranslatable": True,
                }]}},
            }
            html = f"""<!doctype html><html><head><meta charset='utf-8'><style>html,body{{margin:0;background:#111}}#movie_player{{width:960px;height:540px;position:relative}}video{{width:960px;height:540px}}.ytp-subtitles-button{{position:absolute;right:8px;bottom:8px}}</style></head><body><div id='movie_player'><video class='html5-main-video' muted controls src='https://www.youtube.com/subtitle-first-source.mp4'></video><button class='ytp-subtitles-button' aria-pressed='false'>CC</button></div><script>const response={json.dumps(player_response)};const player=document.querySelector('#movie_player');player.getPlayerResponse=()=>response;document.querySelector('.ytp-subtitles-button').onclick=e=>e.currentTarget.setAttribute('aria-pressed',e.currentTarget.getAttribute('aria-pressed')==='true'?'false':'true')</script></body></html>"""
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=False,
                    args=[
                        "--window-position=-32000,-32000",
                        "--window-size=1280,900",
                        f"--disable-extensions-except={extension}",
                        f"--load-extension={extension}",
                        "--no-first-run",
                        "--autoplay-policy=no-user-gesture-required", "--mute-audio",
                        "--proxy-server=http://127.0.0.1:7890",
                    ],
                )
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15_000)
                worker.evaluate("chrome.storage.local.set({autoMode:true,autoLearning:true,displaySize:'medium'})")
                page = context.pages[0] if context.pages else context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("console", lambda message: extension_errors.append(message.text) if message.type == "error" and (message.location.get("url") or "").startswith("chrome-extension://") else None)
                page.route(URL, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                page.route("https://www.youtube.com/subtitle-first-source.mp4", lambda route: route.fulfill(path=str(SOURCE_VIDEO), content_type="video/mp4"))
                page.route(
                    "https://www.youtube.com/api/timedtext**",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(chinese_payload if parse_qs(urlsplit(route.request.url).query).get("tlang") else english_payload, ensure_ascii=False),
                    ),
                )
                page.goto(URL, wait_until="load", timeout=30_000)
                video = page.locator("video")
                video.wait_for(state="attached", timeout=60_000)
                page.wait_for_function("() => document.querySelector('video')?.duration > 100", timeout=60_000)
                video.evaluate("video => video.pause()")
                host = page.locator("#inflow-extension-root")
                host.wait_for(state="attached", timeout=15_000)

                try:
                    page.wait_for_function("() => document.querySelector('button.ytp-subtitles-button')?.getAttribute('aria-pressed') === 'true'", timeout=8_000)
                    bridge_seen = True
                except Exception:
                    bridge_seen = False

                page.wait_for_function(
                    "() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent === 'InFlow 字幕已就绪'",
                    timeout=90_000,
                )
                if list((data_dir / "import-jobs").glob("*.json")):
                    raise AssertionError("learning_import_started_before_continuous_playback")
                if list((data_dir / "caption-jobs").glob("*.json")):
                    raise AssertionError("page_caption_path_created_backend_caption_job")
                caption_ready_before_learning = True
                cue_time = 3.5
                video.evaluate("(video,time) => { video.currentTime=time; return video.play(); }", cue_time)
                page.wait_for_function(
                    "() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; const c=root?.querySelector('#caption'); return c && !c.hidden && !root.querySelector('#captionEnglish').hidden && !root.querySelector('#captionChinese').hidden; }",
                    timeout=10_000,
                )
                visible_caption = host.locator("#caption").inner_text()
                hierarchy = page.evaluate("""() => {
                  const root=document.querySelector('#inflow-extension-root').shadowRoot;
                  const box=root.querySelector('#caption').getBoundingClientRect();
                  const en=getComputedStyle(root.querySelector('#captionEnglish'));
                  const zh=getComputedStyle(root.querySelector('#captionChinese'));
                  return {width:box.width,englishWeight:Number(en.fontWeight),chineseWeight:Number(zh.fontWeight),languages:root.querySelector('#caption').dataset.languages};
                }""")
                if hierarchy["width"] > 761 or hierarchy["chineseWeight"] <= hierarchy["englishWeight"] or hierarchy["languages"] != "bilingual":
                    raise AssertionError({"subtitle_hierarchy": hierarchy})

                before_replay = float(video.evaluate("video => video.currentTime"))
                page.keyboard.press("s")
                page.wait_for_timeout(350)
                after_replay = float(video.evaluate("video => video.currentTime"))
                if not after_replay < before_replay - 0.5:
                    raise AssertionError({"subtitle_replay_before": before_replay, "subtitle_replay_after": after_replay})
                video.evaluate("video => video.pause()")
                page.wait_for_timeout(1200)
                protected_time = float(video.evaluate("(video,time) => { video.currentTime=time; video.pause(); const input=document.createElement('input'); input.id='inflow-shortcut-test'; document.body.appendChild(input); input.focus(); return video.currentTime; }", cue_time))
                page.keyboard.press("s")
                page.wait_for_timeout(200)
                protected_result = page.evaluate("""() => ({time:document.querySelector('video').currentTime,value:document.querySelector('#inflow-shortcut-test')?.value})""")
                if abs(float(protected_result["time"]) - protected_time) > 0.08 or protected_result["value"] != "s":
                    raise AssertionError({"input_shortcut_protection": protected_result, "expected_time": protected_time})
                page.evaluate("document.querySelector('#inflow-shortcut-test')?.remove(); document.body.focus()")
                if bridge_seen and page.locator("button.ytp-subtitles-button").get_attribute("aria-pressed") != "false":
                    raise AssertionError("owned_native_caption_bridge_not_released")
                screenshot = ROOT / "data" / "subtitle-first-real.png"
                page.screenshot(path=str(screenshot), full_page=False)

                video.evaluate("video => video.play()")
                for _ in range(180):
                    imports = list((data_dir / "import-jobs").glob("*.json"))
                    if imports:
                        break
                    page.wait_for_timeout(100)
                else:
                    raise AssertionError("learning_import_not_started_after_eight_seconds")
                learning_job = json.loads(imports[0].read_text(encoding="utf-8"))
                post(base, f"/api/imports/{learning_job['job_id']}/cancel", {})
                context.close()

            live_snapshot_after = snapshot_live_tree(live_root)
            if live_snapshot_after != live_snapshot_before:
                changed = sorted(path for path in set(live_snapshot_before) | set(live_snapshot_after) if live_snapshot_before.get(path) != live_snapshot_after.get(path))
                raise AssertionError({"formal_live_tree_changed": changed})
            if page_errors or extension_errors:
                raise AssertionError({"page_errors": page_errors, "extension_errors": extension_errors})
            print(json.dumps({
                "ok": True,
                "video_id": VIDEO_ID,
                "native_caption_bridge_seen": bridge_seen,
                "page_caption_ready_before_learning": caption_ready_before_learning,
                "backend_caption_jobs": 0,
                "caption_source": "youtube_page_player",
                "visible_caption": visible_caption,
                "subtitle_hierarchy": hierarchy,
                "subtitle_replay_seconds_back": round(before_replay - after_replay, 3),
                "input_shortcut_protected": True,
                "learning_import_count_before_play": 0,
                "learning_import_started_after_continuous_playback": True,
                "formal_live_tree_unchanged": True,
                "page_errors": page_errors,
                "extension_errors": extension_errors,
                "screenshot": str(screenshot),
            }, ensure_ascii=False, indent=2))
        finally:
            stop_tree(server.pid)


if __name__ == "__main__":
    main()
