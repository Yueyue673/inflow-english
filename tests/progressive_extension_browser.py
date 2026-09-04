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

from adaptive_core import atomic_write_json
from caption_preview import caption_preview_id
from progressive_video_pack import ProgressiveVideoPackBuilder
from tests.extension_test_support import copy_extension_for_browser
from tests.live_data_guard import snapshot_live_tree
from tests.test_progressive_video_pack import FakeRangeMedia, caption_document
from tests.test_video_pack import FakeTranslator
from video_pack import VideoPackBuilder

SOURCE_EXTENSION = ROOT / "extension"
SOURCE_VIDEO = ROOT.parent / "mechanism-experiment" / "media" / "source.mp4"
AUDIO_FIXTURE = ROOT.parent / "mechanism-experiment" / "media" / "phrases" / "collectively-clause.mp3"
VIDEO_ID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def dense_window_transcript(start_offset: float = 20.0):
    segments = []
    for index in range(30):
        start = start_offset + index * 10.0
        tokens = ["Careful", "listeners", "notice", f"pattern{index}", "today."]
        words = []
        for token_index, token in enumerate(tokens):
            word_start = start + token_index * 0.78
            words.append({"word": token, "start": word_start, "end": word_start + 0.7, "probability": 0.99})
        segments.append({"id": index, "start": words[0]["start"], "end": words[-1]["end"], "text": " ".join(tokens), "words": words})
    return {"language": "en", "language_probability": 0.99, "duration_sec": 330.0, "segments": segments}


class SequencedWindowTranscriber:
    identity = "fake-sequenced-window-transcriber:v1"

    def __init__(self):
        self.calls = 0

    def transcribe(self, _wav_path, *, cancel_callback=None):
        if cancel_callback and cancel_callback():
            raise RuntimeError("cancelled")
        self.calls += 1
        return dense_window_transcript(12.0 if self.calls == 1 else 20.0)


class BrowserRangeMedia(FakeRangeMedia):
    def clip_phrase(self, _video_path, start_sec, end_sec, destination):
        shutil.copy2(AUDIO_FIXTURE, destination)
        self.clip_durations[Path(destination)] = float(end_sec) - float(start_sec)


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
    with tempfile.TemporaryDirectory(prefix="inflow-progressive-browser-") as temporary:
        work = Path(temporary)
        data_dir = work / "data"
        packs_dir = work / "packs"
        extension_dir = work / "extension"
        profile_dir = work / "chrome-profile"
        copy_extension_for_browser(SOURCE_EXTENSION, extension_dir, open_shadow_for_test=True)

        document = caption_document()
        preview_id = caption_preview_id(URL)
        document["preview_id"] = preview_id
        caption_path = data_dir / "caption-previews" / f"{preview_id}.json"
        atomic_write_json(caption_path, document)
        now = "2026-09-03T12:00:00Z"
        atomic_write_json(data_dir / "caption-jobs" / f"{preview_id}.json", {
            "preview_id": preview_id,
            "video_id": VIDEO_ID,
            "source_url": URL,
            "title": document["title"],
            "status": "ready",
            "revision": document["revision"],
            "usable": True,
            "language": "zh",
            "coverage_fraction": 1.0,
            "translated_segments": len(document["segments"]),
            "total_segments": len(document["segments"]),
            "error_code": None,
            "retryable": False,
            "created_at": now,
            "updated_at": now,
            "completed_at": now,
        })

        media = BrowserRangeMedia()
        base_builder = VideoPackBuilder(
            packs_dir,
            translator=FakeTranslator(),
            transcriber=SequencedWindowTranscriber(),
            media_tools=media,
        )
        progressive = ProgressiveVideoPackBuilder(packs_dir, base_builder=base_builder)
        manifest = progressive.initialize(URL, document, playhead_sec=0)
        first_manifest = progressive.build_one(manifest["pack_id"])
        first_delta = progressive.delta(manifest["pack_id"], since_revision=0, playhead_sec=0)
        if not first_delta["candidates"]:
            raise AssertionError("first_progressive_window_has_no_candidate")
        import_job_id = "a" * 32
        atomic_write_json(data_dir / "import-jobs" / f"{import_job_id}.json", {
            "job_id": import_job_id,
            "source_url": URL,
            "pack_id": manifest["pack_id"],
            "title": document["title"],
            "status": "partial_ready",
            "stage": "partial_ready",
            "route": "progressive",
            "playhead_sec": 0.0,
            "usable": True,
            "revision": first_manifest["revision"],
            "ready_ranges": first_manifest["ready_ranges"],
            "coverage_fraction": first_manifest["coverage_fraction"],
            "build_complete": False,
            "failed_ranges": [],
            "fraction": first_manifest["coverage_fraction"],
            "detail": "fixture_first_window",
            "cancel_requested": False,
            "error_code": None,
            "retryable": True,
            "cached": True,
            "created_at": now,
            "updated_at": now,
            "started_at": now,
            "completed_at": None,
        })

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        worker_path = extension_dir / "service-worker.js"
        worker_path.write_text(worker_path.read_text(encoding="utf-8").replace("http://127.0.0.1:8767", base), encoding="utf-8")
        manifest_path = extension_dir / "manifest.json"
        extension_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        extension_manifest["host_permissions"] = ["https://www.youtube.com/*", f"{base}/*"]
        manifest_path.write_text(json.dumps(extension_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        env = os.environ.copy()
        env.update({
            "INFLOW_ADAPTIVE_DATA_DIR": str(data_dir),
            "INFLOW_PACKS_DIR": str(packs_dir),
            "INFLOW_ADAPTIVE_QA": "1",
            "INFLOW_ADAPTIVE_PORT": str(port),
        })
        server = subprocess.Popen(
            [os.sys.executable, "tests/run_progressive_fixture_server.py"],
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
                server_output = server.stdout.read() if server.poll() is not None and server.stdout is not None else "process_alive_without_health"
                raise AssertionError({"fixture_server_not_ready": server_output})

            english_payload = {"events": [
                {"tStartMs": round(float(row["start"]) * 1000), "dDurationMs": round((float(row["end"]) - float(row["start"])) * 1000), "segs": [{"utf8": row.get("text_en", "")}]}
                for row in document["segments"] if row.get("text_en")
            ]}
            chinese_payload = {"events": [
                {"tStartMs": round(float(row["start"]) * 1000), "dDurationMs": round((float(row["end"]) - float(row["start"])) * 1000), "segs": [{"utf8": row.get("text_zh", "")}]}
                for row in document["segments"] if row.get("text_zh")
            ]}
            player_response = {
                "videoDetails": {"videoId": VIDEO_ID, "title": document["title"], "lengthSeconds": str(document["duration_sec"])},
                "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{
                    "baseUrl": f"https://www.youtube.com/api/timedtext?v={VIDEO_ID}&lang=en",
                    "languageCode": "en",
                    "isTranslatable": True,
                }]}},
            }
            html = f"""<!doctype html><html><head><meta charset='utf-8'><title>Progressive fixture</title><style>html,body{{margin:0;background:#111}}#movie_player{{width:960px;height:540px;position:relative}}video{{width:960px;height:540px}}.ytp-subtitles-button{{position:absolute;right:8px;bottom:8px}}</style></head><body><div id='movie_player'><video class='html5-main-video' controls muted autoplay src='https://www.youtube.com/fake-inflow-source.mp4'></video><button class='ytp-subtitles-button' aria-pressed='false' type='button'>CC</button></div><script>const response={json.dumps(player_response)};const player=document.querySelector('#movie_player');player.getPlayerResponse=()=>response;document.querySelector('.ytp-subtitles-button').onclick=e=>e.currentTarget.setAttribute('aria-pressed',e.currentTarget.getAttribute('aria-pressed')==='true'?'false':'true');</script></body></html>"""
            page_errors = []
            worker_errors = []
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=False,
                    args=[
                        "--window-position=-32000,-32000",
                        "--window-size=1280,900",
                        f"--disable-extensions-except={extension_dir}",
                        f"--load-extension={extension_dir}",
                        "--no-first-run",
                        "--autoplay-policy=no-user-gesture-required", "--mute-audio",
                    ],
                )
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15_000)
                worker.on("console", lambda message: worker_errors.append(message.text) if message.type == "error" else None)
                worker.evaluate("chrome.storage.local.set({autoMode:true,autoLearning:true,displaySize:'medium'})")
                page = context.pages[0] if context.pages else context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.route("https://www.youtube.com/fake-inflow-source.mp4", lambda route: route.fulfill(path=str(SOURCE_VIDEO), content_type="video/mp4"))
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
                try:
                    page.wait_for_function("() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent === 'InFlow 正在工作'", timeout=30_000)
                except Exception as exc:
                    state = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; const video=document.querySelector('video'); return {time:video?.currentTime,paused:video?.paused,pill:root?.querySelector('#pill')?.textContent,panel:root?.querySelector('#panelMessage')?.textContent,caption:root?.querySelector('#captionEnglish')?.textContent}; }""")
                    jobs = [json.loads(path.read_text(encoding="utf-8")) for path in (data_dir / "import-jobs").glob("*.json")]
                    raise AssertionError({"activation_state": state, "jobs": jobs, "worker_errors": worker_errors}) from exc
                page.locator("video").evaluate("video => video.pause()")
                sessions = list((data_dir / "sessions").glob("*.json"))
                if len(sessions) != 1:
                    raise AssertionError({"sessions": [path.name for path in sessions]})
                session_id = sessions[0].stem
                initial_session = fetch(base, f"/api/sessions/{session_id}")
                if not initial_session["progressive"] or initial_session["progressive_revision"] != 1:
                    raise AssertionError(initial_session)
                initial_count = len(initial_session["items"])
                if initial_count < 1:
                    raise AssertionError("initial_progressive_session_has_no_item")

                second_manifest = progressive.build_one(manifest["pack_id"])
                for _ in range(100):
                    synced = fetch(base, f"/api/sessions/{session_id}")
                    if synced["progressive_revision"] >= second_manifest["revision"] and len(synced["items"]) > initial_count:
                        break
                    page.wait_for_timeout(100)
                else:
                    raise AssertionError({"progressive_sync_timeout": synced, "second_manifest": second_manifest})

                first_item = initial_session["items"][0]
                page.locator("video").evaluate("video => { video.playbackRate=.5; return video.play(); }")
                try:
                    page.wait_for_function("() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; const b=root?.querySelector('#continueLearning'); return root && !root.querySelector('#overlay').hidden && b && !b.disabled; }", timeout=30_000)
                except Exception as exc:
                    state = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; const video=document.querySelector('video'); return {time:video?.currentTime,paused:video?.paused,duration:video?.duration,pill:root?.querySelector('#pill')?.textContent,overlayHidden:root?.querySelector('#overlay')?.hidden,phase:root?.querySelector('#phase')?.textContent,title:root?.querySelector('#title')?.textContent,audioState:root?.querySelector('#audioState')?.textContent}; }""")
                    raise AssertionError({"interaction_state": state, "item": first_item, "session": fetch(base, f"/api/sessions/{session_id}"), "page_errors": page_errors, "worker_errors": worker_errors}) from exc
                host.locator("#continueLearning").click()
                page.wait_for_function("() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#overlay').hidden", timeout=8_000)
                final_session = fetch(base, f"/api/sessions/{session_id}")
                if final_session["interaction_summary"]["completed"] != 1:
                    raise AssertionError(final_session)
                screenshot = ROOT / "data" / "progressive-extension-e2e.png"
                page.screenshot(path=str(screenshot), full_page=True)
                context.close()

            live_snapshot_after = snapshot_live_tree(live_root)
            if live_snapshot_after != live_snapshot_before:
                raise AssertionError({"formal_live_tree_changed": sorted(set(live_snapshot_before) ^ set(live_snapshot_after))})
            if page_errors or worker_errors:
                raise AssertionError({"page_errors": page_errors, "worker_errors": worker_errors})
            print(json.dumps({
                "ok": True,
                "pack_id": manifest["pack_id"],
                "first_revision": first_manifest["revision"],
                "second_revision": second_manifest["revision"],
                "initial_items": initial_count,
                "items_after_delta": len(synced["items"]),
                "completed_interactions": final_session["interaction_summary"]["completed"],
                "no_full_video_in_progressive_pack": not list(packs_dir.rglob("video.mp4")),
                "formal_live_tree_unchanged": True,
                "page_errors": page_errors,
                "worker_errors": worker_errors,
                "screenshot": str(screenshot),
            }, ensure_ascii=False, indent=2))
        finally:
            stop_tree(server.pid)


if __name__ == "__main__":
    main()
