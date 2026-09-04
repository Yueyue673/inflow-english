from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

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
PACKS = ROOT / "data" / "packs"
VIDEO_ID = "UwMS0J2eTXE"
SECOND_VIDEO_ID = "8kQTKYgHI1E"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SECOND_URL = f"https://www.youtube.com/watch?v={SECOND_VIDEO_ID}"
FIRST_PACK = PACKS / "yt-UwMS0J2eTXE-6c5e46cf287a"
SECOND_PACK = PACKS / "yt-8kQTKYgHI1E-353d2534a76d"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def youtube_fixture(video_id: str, media_url: str) -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>InFlow fixture</title>
<style>html,body{{margin:0;background:#111}}#movie_player{{position:relative;width:960px;height:540px}}video{{display:block;width:960px;height:540px}}</style>
</head><body><div id='movie_player'><video class='html5-main-video' src='{media_url}'></video></div></body></html>"""


def range_media(data: bytes):
    def handler(route) -> None:
        size = len(data)
        range_header = route.request.headers.get("range", "")
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
        if match:
            start = int(match.group(1) or 0)
            end = int(match.group(2) or size - 1)
            if start >= size or end < start:
                route.fulfill(status=416, headers={"Content-Range": f"bytes */{size}"}, body=b"")
                return
            end = min(end, size - 1)
            body = data[start : end + 1]
            route.fulfill(
                status=206,
                content_type="video/mp4",
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Content-Length": str(len(body)),
                },
                body=b"" if route.request.method == "HEAD" else body,
            )
            return
        route.fulfill(
            status=200,
            content_type="video/mp4",
            headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
            body=b"" if route.request.method == "HEAD" else data,
        )

    return handler


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
    live_root = ROOT / "data" / "live"
    live_snapshot_before = snapshot_live_tree(live_root)
    with tempfile.TemporaryDirectory(prefix="inflow-extension-youtube-") as temporary:
        temporary_root = Path(temporary)
        extension = temporary_root / "extension"
        profile_dir = temporary_root / "chrome-profile"
        data_dir = temporary_root / "data"
        copy_extension_for_browser(SOURCE_EXTENSION, extension, open_shadow_for_test=True)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        worker_path = extension / "service-worker.js"
        worker_source = worker_path.read_text(encoding="utf-8").replace("http://127.0.0.1:8767", base)
        worker_path.write_text(worker_source, encoding="utf-8")
        manifest_path = extension / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["host_permissions"] = ["https://www.youtube.com/*", f"{base}/*"]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "INFLOW_ADAPTIVE_DATA_DIR": str(data_dir),
                "INFLOW_PACKS_DIR": str(PACKS),
                "INFLOW_ADAPTIVE_QA": "1",
                "INFLOW_ADAPTIVE_PORT": str(port),
            }
        )
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

            page_errors: list[str] = []
            console_errors: list[dict[str, str]] = []
            worker_errors: list[str] = []
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
                        "--disable-background-timer-throttling",
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--autoplay-policy=no-user-gesture-required", "--mute-audio",
                        "--proxy-server=http://127.0.0.1:7890",
                    ],
                )
                service_worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15_000)
                service_worker.on("console", lambda message: worker_errors.append(message.text) if message.type == "error" else None)
                service_worker.evaluate("chrome.storage.local.set({autoMode:true,autoLearning:false,displaySize:'medium'})")
                context.route(URL, lambda route: route.fulfill(status=200, content_type="text/html", body=youtube_fixture(VIDEO_ID, "https://www.youtube.com/inflow-first.mp4")))
                context.route(SECOND_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=youtube_fixture(SECOND_VIDEO_ID, "https://www.youtube.com/inflow-second.mp4")))
                context.route("https://www.youtube.com/inflow-first.mp4", range_media((FIRST_PACK / "video.mp4").read_bytes()))
                context.route("https://www.youtube.com/inflow-second.mp4", range_media((SECOND_PACK / "video.mp4").read_bytes()))
                page = context.pages[0] if context.pages else context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("console", lambda message: console_errors.append({"text": message.text, "url": str(message.location.get("url") or "")}) if message.type == "error" else None)
                page.goto(URL, wait_until="commit", timeout=90_000)
                page.locator("video").wait_for(state="attached", timeout=60_000)
                page.wait_for_function("() => document.querySelector('video') && document.querySelector('video').duration > 50", timeout=60_000)
                host = page.locator("#inflow-extension-root")
                host.wait_for(state="attached", timeout=15_000)
                page.locator("video").evaluate("video => video.pause()")
                pill = host.locator("#pill")
                try:
                    pill.get_by_text("InFlow 字幕已就绪", exact=True).wait_for(timeout=15_000)
                except Exception as exc:
                    shadow_state = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; return root ? {pill:root.querySelector('#pill')?.textContent,panel:root.querySelector('#panelMessage')?.textContent} : null; }""")
                    raise AssertionError({"subtitle_start_state": shadow_state, "worker_errors": worker_errors}) from exc
                if list((data_dir / "sessions").glob("*.json")):
                    raise AssertionError("session_started_before_continuous_playback")
                page.locator("video").evaluate("video => video.play()")
                page.wait_for_timeout(8500)
                if list((data_dir / "sessions").glob("*.json")) or list((data_dir / "import-jobs").glob("*.json")):
                    raise AssertionError("learning_started_without_explicit_opt_in")
                learning_enabled_at = time.perf_counter()
                service_worker.evaluate("chrome.storage.local.set({autoLearning:true})")
                try:
                    page.wait_for_function(
                        "() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent === 'InFlow 正在工作'",
                        timeout=45_000,
                    )
                except Exception as exc:
                    shadow_state = page.evaluate("""() => {
                      const root=document.querySelector('#inflow-extension-root')?.shadowRoot;
                      return root ? {pill:root.querySelector('#pill')?.textContent,panel:root.querySelector('#panelMessage')?.textContent,overlayHidden:root.querySelector('#overlay')?.hidden} : null;
                    }""")
                    jobs = [json.loads(path.read_text(encoding="utf-8")) for path in (data_dir / "import-jobs").glob("*.json")]
                    raise AssertionError({"activation_state": shadow_state, "jobs": jobs, "worker_errors": worker_errors}) from exc
                gate_to_working_ms = round((time.perf_counter() - learning_enabled_at) * 1000)
                if gate_to_working_ms < 7750 or gate_to_working_ms > 12_000:
                    raise AssertionError({"learning_gate_to_working_ms": gate_to_working_ms})

                session_files = list((data_dir / "sessions").glob("*.json"))
                if len(session_files) != 1:
                    raise AssertionError({"session_files": [str(path) for path in session_files]})
                session_id = session_files[0].stem
                session = fetch(base, f"/api/sessions/{session_id}")
                if session["pack_id"].split("-")[1] != VIDEO_ID or len(session["items"]) != 1:
                    raise AssertionError(session)
                item = session["items"][0]
                service_worker.evaluate("chrome.storage.local.set({displaySize:'large'})")
                page.wait_for_function("() => document.querySelector('#inflow-extension-root')?.style.getPropertyValue('--caption-en-size') === '25px'", timeout=8_000)

                pill.click()
                panel_copy = host.locator("#panelMessage").inner_text()
                if "找到 4 个表达" not in panel_copy or "当前可选 4 个" not in panel_copy or "预计介入 1 次" not in panel_copy:
                    raise AssertionError({"panel": panel_copy})
                pill.click()

                page.wait_for_function("() => !document.querySelector('#movie_player')?.classList.contains('ad-showing')", timeout=60_000)
                page.wait_for_function(
                    "anchor => { const video=document.querySelector('video'); return video?.seekable?.length && video.seekable.end(video.seekable.length - 1) >= anchor; }",
                    arg=float(item["anchor_sec"]),
                    timeout=30_000,
                )
                page.locator("video").evaluate(
                    "(video, anchor) => { video.pause(); video.currentTime = Math.max(0, anchor - 2); }",
                    float(item["anchor_sec"]),
                )
                page.wait_for_function(
                    "anchor => Math.abs(document.querySelector('video').currentTime - Math.max(0, anchor - 2)) < 0.4",
                    arg=float(item["anchor_sec"]),
                    timeout=10_000,
                )
                page.wait_for_function(
                    "phrase => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#captionEnglish')?.textContent === phrase",
                    arg=item["phrase_text"],
                    timeout=3_000,
                )
                expected_mapping_zh = host.locator("#captionChinese").inner_text()
                page.locator("video").evaluate("video => { video.playbackRate = 0.5; return video.play(); }")
                try:
                    page.wait_for_function(
                        "() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; const button=root?.querySelector('#continueLearning'); return root && !root.querySelector('#overlay').hidden && button && !button.disabled; }",
                        timeout=30_000,
                    )
                except Exception as exc:
                    state = page.evaluate("""() => {
                      const root=document.querySelector('#inflow-extension-root')?.shadowRoot;
                      const video=document.querySelector('video');
                      return {time:video?.currentTime,paused:video?.paused,pill:root?.querySelector('#pill')?.textContent,overlayHidden:root?.querySelector('#overlay')?.hidden,phase:root?.querySelector('#phase')?.textContent,title:root?.querySelector('#title')?.textContent,continueDisabled:root?.querySelector('#continueLearning')?.disabled};
                    }""")
                    raise AssertionError({"interaction_wait_state": state, "item": item, "session": fetch(base, f"/api/sessions/{session_id}"), "worker_errors": worker_errors}) from exc
                context_text = host.locator(".phrase").inner_text()
                translation_text = host.locator(".translation").inner_text()
                geometry = page.evaluate("""() => {
                  const video=document.querySelector('video').getBoundingClientRect();
                  const overlay=document.querySelector('#inflow-extension-root').shadowRoot.querySelector('#overlay').getBoundingClientRect();
                  const visible={left:Math.max(0,video.left),top:Math.max(0,video.top),right:Math.min(innerWidth,video.right),bottom:Math.min(innerHeight,video.bottom)};
                  return {video:{x:visible.left,y:visible.top,width:visible.right-visible.left,height:visible.bottom-visible.top},overlay:{x:overlay.x,y:overlay.y,width:overlay.width,height:overlay.height}};
                }""")
                if any(abs(geometry["video"][field] - geometry["overlay"][field]) > 2 for field in ("x", "y", "width", "height")):
                    raise AssertionError({"video_center_geometry": geometry})
                if context_text != item["phrase_text"] or translation_text != expected_mapping_zh:
                    raise AssertionError({"expected_phrase": item["phrase_text"], "expected_translation": expected_mapping_zh, "context": context_text, "translation": translation_text})
                page.wait_for_timeout(1200)
                if host.locator("#overlay").is_hidden():
                    raise AssertionError("mapping_auto_dismissed")
                screenshot = ROOT / "data" / "extension-youtube-mapping.png"
                page.screenshot(path=str(screenshot), full_page=True)
                host.locator("#replay").click()
                page.wait_for_function("() => { const b=document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#replay'); return b && !b.disabled; }", timeout=20_000)
                page.evaluate("""() => {
                  window.__inflowInputProbe = [];
                  document.addEventListener('keydown', event => window.__inflowInputProbe.push({type:'keydown',key:event.key,trusted:event.isTrusted}), true);
                  document.addEventListener('click', event => window.__inflowInputProbe.push({type:'click',trusted:event.isTrusted}), true);
                }""")
                page.keyboard.press("Enter")
                try:
                    page.wait_for_function("() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#overlay').hidden", timeout=8_000)
                except Exception as exc:
                    input_state = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root').shadowRoot; return {probe:window.__inflowInputProbe,activeTag:document.activeElement?.tagName,continueDisabled:root.querySelector('#continueLearning')?.disabled,overlayHidden:root.querySelector('#overlay').hidden}; }""")
                    raise AssertionError({"mapping_continue_input": input_state}) from exc
                page.wait_for_function("() => !document.querySelector('video').paused", timeout=8_000)

                for _ in range(80):
                    mapped_session = fetch(base, f"/api/sessions/{session_id}")
                    if mapped_session["interaction_summary"]["completed"] == 1:
                        break
                    page.wait_for_timeout(100)
                else:
                    raise AssertionError("mapping_continue_not_saved")
                after_mapping = fetch(base, "/api/lexicon")["entries"]
                mapped_entry = next((row for row in after_mapping if row["knowledge_key"] == item["knowledge_key"]), None)
                if not mapped_entry or mapped_entry["status"] != "unseen" or mapped_entry["explicit_known"]:
                    raise AssertionError({"mapping_should_not_write_familiarity": mapped_entry})
                transcript = fetch(base, session["video"]["transcript"])
                target_cue = next(cue for cue in transcript["cues"] if item["surface"].casefold() in cue["text"].casefold())
                cue_time = (float(target_cue["start_sec"]) + float(target_cue["end_sec"])) / 2
                page.locator("video").evaluate("(video, time) => { video.pause(); video.currentTime=time; }", cue_time)
                unseen_token = host.locator('.token[data-status="unseen"]').filter(has_text=item["surface"])
                try:
                    unseen_token.wait_for(state="visible", timeout=10_000)
                    subtitle_zh = host.locator("#captionChinese").inner_text()
                    if "拒绝" not in subtitle_zh or "更近" not in subtitle_zh or "忽略" in subtitle_zh or "选中" in subtitle_zh or subtitle_zh.lstrip().startswith(("。", "•", "·")):
                        raise AssertionError({"incomplete_chinese_subtitle": subtitle_zh, "item": item})
                except Exception as exc:
                    ui_state = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; return {caption:root?.querySelector('#captionEnglish')?.textContent,chinese:root?.querySelector('#captionChinese')?.textContent,tokens:[...(root?.querySelectorAll('#captionEnglish .token')||[])].map(x=>({text:x.textContent,status:x.dataset.status,key:x.dataset.knowledgeKey||null}))}; }""")
                    raise AssertionError({"item": item, "entry": mapped_entry, "target_cue": target_cue, "ui_state": ui_state}) from exc
                page.locator("video").evaluate("video => video.pause()")
                unseen_token.click()
                host.locator("#wordPanel").wait_for(state="visible", timeout=5_000)
                if item["gloss_zh"] not in host.locator("#wordGloss").inner_text():
                    raise AssertionError({"word_gloss": host.locator("#wordGloss").inner_text(), "item": item})
                host.locator('[data-word-feedback="known"]').click()
                for _ in range(80):
                    subtitle_entry = next((row for row in fetch(base, "/api/lexicon")["entries"] if row["knowledge_key"] == item["knowledge_key"]), None)
                    if subtitle_entry and subtitle_entry["status"] == "known":
                        break
                    page.wait_for_timeout(100)
                else:
                    raise AssertionError("subtitle_known_feedback_not_saved")
                subtitle_screenshot = ROOT / "data" / "extension-youtube-subtitle-known.png"
                page.screenshot(path=str(subtitle_screenshot), full_page=False)

                page.locator("video").evaluate("video => { video.currentTime = Math.max(0, video.duration - 0.15); return video.play(); }")
                for _ in range(100):
                    final_session = fetch(base, f"/api/sessions/{session_id}")
                    if final_session["stage"] == "complete":
                        break
                    page.wait_for_timeout(100)
                else:
                    raise AssertionError("extension_session_did_not_complete")
                if final_session["interaction_summary"] != {"completed": 1, "skipped": 0, "technical_failure": 0}:
                    raise AssertionError(final_session["interaction_summary"])
                final_item = next(row for row in final_session["items"] if row["id"] == item["id"])
                if final_item["familiarity_status"] != "known" or not final_item["explicit_known"]:
                    raise AssertionError(final_item)
                lexical = next(row for row in fetch(base, "/api/lexicon")["entries"] if row["knowledge_key"] == final_item["knowledge_key"])
                if lexical["status"] != "known" or lexical["replay_count"] != 1:
                    raise AssertionError(lexical)

                page.goto(SECOND_URL, wait_until="commit", timeout=90_000)
                page.locator("video").wait_for(state="attached", timeout=60_000)
                page.wait_for_function("() => document.querySelector('video') && document.querySelector('video').duration > 300", timeout=60_000)
                second_host = page.locator("#inflow-extension-root")
                second_host.wait_for(state="attached", timeout=15_000)
                second_pill = second_host.locator("#pill")
                page.locator("video").evaluate("video => video.play()")
                page.wait_for_function(
                    "() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent === 'InFlow 正在工作'",
                    timeout=45_000,
                )
                second_sessions = list((data_dir / "sessions").glob("*.json"))
                if len(second_sessions) != 2:
                    raise AssertionError({"sessions_after_navigation": [path.name for path in second_sessions]})
                second_session_path = next(path for path in second_sessions if path.stem != session_id)
                second_session = fetch(base, f"/api/sessions/{second_session_path.stem}")
                if second_session["pack_id"].split("-")[1] != SECOND_VIDEO_ID or second_session["stage"] != "watch":
                    raise AssertionError(second_session)
                service_worker.evaluate("chrome.storage.local.set({autoLearning:false})")
                page.wait_for_function(
                    "() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent === 'InFlow 字幕已就绪'",
                    timeout=10_000,
                )
                learning_off_state = service_worker.evaluate("""async () => {
                  const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
                  return chrome.tabs.sendMessage(tab.id,{type:'inflow:getState'});
                }""")
                if learning_off_state.get("enabled") or not learning_off_state.get("subtitleActive") or learning_off_state.get("autoLearning"):
                    raise AssertionError({"learning_off_should_preserve_subtitles": learning_off_state})
                second_pill.click()
                second_host.locator("#panelPrimary").click()
                page.wait_for_function(
                    "() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent === 'InFlow 已暂停'",
                    timeout=10_000,
                )
                context.close()

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
            live_snapshot_after = snapshot_live_tree(live_root)
            if live_snapshot_after != live_snapshot_before:
                changed = sorted(path for path in set(live_snapshot_before) | set(live_snapshot_after) if live_snapshot_before.get(path) != live_snapshot_after.get(path))
                raise AssertionError({"formal_live_tree_changed": changed})
            relevant_console_errors = [
                row for row in console_errors
                if row["url"].startswith(("chrome-extension://", base)) or "inflow" in row["text"].casefold()
            ]
            if page_errors or relevant_console_errors or worker_errors:
                raise AssertionError({"page_errors": page_errors, "console_errors": relevant_console_errors, "worker_errors": worker_errors})
            print(json.dumps({"ok": True, "video_id": VIDEO_ID, "pack_id": final_session["pack_id"], "auto_mode_without_per_video_click": True, "automatic_learning_requires_opt_in": True, "learning_gate_to_working_ms": gate_to_working_ms, "display_size_changed_to": "large", "video_center_geometry": geometry, "candidate_pool_count": final_session["candidate_pool_count"], "intervention_budgets": final_session["intervention_budgets"], "interaction_summary": final_session["interaction_summary"], "mapping_familiarity_written": False, "subtitle_feedback": lexical["status"], "manual_replays": lexical["replay_count"], "second_video_id": SECOND_VIDEO_ID, "second_pack_id": second_session["pack_id"], "second_session_preserved": second_session["stage"] == "watch", "learning_off_preserved_subtitles": True, "rebuild_matches": True, "formal_live_tree_unchanged": True, "page_errors": page_errors, "extension_console_errors": relevant_console_errors, "external_youtube_console_errors": len(console_errors) - len(relevant_console_errors), "worker_errors": worker_errors, "mapping_screenshot": str(screenshot), "subtitle_screenshot": str(subtitle_screenshot)}, ensure_ascii=False, indent=2))
        finally:
            stop_tree(server.pid)


if __name__ == "__main__":
    main()
