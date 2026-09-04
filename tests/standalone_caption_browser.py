from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BROWSERS = os.environ.get("INFLOW_PLAYWRIGHT_BROWSERS")
if BROWSERS:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS
from playwright.sync_api import sync_playwright

from tests.extension_test_support import copy_extension_for_browser

SOURCE_EXTENSION = ROOT / "extension"
VIDEO_ID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SECOND_VIDEO_ID = "lmnopqrstuv"
SECOND_URL = f"https://www.youtube.com/watch?v={SECOND_VIDEO_ID}"
SPA_VIDEO_ID = "wxyzABCDE12"
SPA_URL = f"https://www.youtube.com/watch?v={SPA_VIDEO_ID}"

ENGLISH = {
    "events": [
        {"tStartMs": 0, "dDurationMs": 2400, "segs": [{"utf8": "Every small step matters."}]},
        {"tStartMs": 2600, "dDurationMs": 2200, "segs": [{"utf8": "Keep moving forward."}]},
    ]
}
CHINESE = {
    "events": [
        {"tStartMs": 0, "dDurationMs": 2400, "segs": [{"utf8": "每一个小步骤都很重要。"}]},
        {"tStartMs": 2600, "dDurationMs": 2200, "segs": [{"utf8": "继续向前走。"}]},
    ]
}
SPA_ENGLISH = {"events": [{"tStartMs": 0, "dDurationMs": 3000, "segs": [{"utf8": "A new video needs new captions."}]}]}
SPA_CHINESE = {"events": [{"tStartMs": 0, "dDurationMs": 3000, "segs": [{"utf8": "新视频必须使用新字幕。"}]}]}

HTML = f"""<!doctype html><html><head><meta charset='utf-8'><title>Fixture</title></head><body>
<div id='movie_player'></div><video class='html5-main-video'></video>
<script>
globalThis.fixtureResponse = {{ videoDetails: {{ videoId: '{VIDEO_ID}', title: 'Fixture video', lengthSeconds: '60' }}, captions: {{ playerCaptionsTracklistRenderer: {{ captionTracks: [{{ baseUrl: 'https://www.youtube.com/api/timedtext?v={VIDEO_ID}&lang=en', languageCode: 'en', isTranslatable: true }}] }} }} }};
const player = document.querySelector('#movie_player');
player.getPlayerResponse = () => globalThis.fixtureResponse;
const video = document.querySelector('video');
Object.defineProperties(video, {{ duration: {{ value: 60, configurable: true }}, currentTime: {{ value: 0.5, writable: true, configurable: true }}, paused: {{ value: true, configurable: true }}, ended: {{ value: false, configurable: true }} }});
</script></body></html>"""

HTML_MISSING = f"""<!doctype html><html><head><meta charset='utf-8'><title>No captions fixture</title></head><body>
<div id='movie_player'></div><video class='html5-main-video'></video>
<script>
globalThis.fixtureResponse = {{ videoDetails: {{ videoId: '{SECOND_VIDEO_ID}', title: 'No captions', lengthSeconds: '60' }}, captions: {{ playerCaptionsTracklistRenderer: {{ captionTracks: [] }} }} }};
const player = document.querySelector('#movie_player');
player.getPlayerResponse = () => globalThis.fixtureResponse;
const video = document.querySelector('video');
Object.defineProperties(video, {{ duration: {{ value: 60, configurable: true }}, currentTime: {{ value: 0.5, writable: true, configurable: true }}, paused: {{ value: true, configurable: true }}, ended: {{ value: false, configurable: true }} }});
</script></body></html>"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="inflow-standalone-caption-") as temporary:
        temp = Path(temporary)
        extension = temp / "extension"
        profile = temp / "profile"
        copy_extension_for_browser(SOURCE_EXTENSION, extension, open_shadow_for_test=True)
        worker = extension / "service-worker.js"
        worker.write_text(worker.read_text(encoding="utf-8").replace("http://127.0.0.1:8767", "http://127.0.0.1:65534"), encoding="utf-8")
        manifest_path = extension / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["host_permissions"] = ["https://www.youtube.com/*"]
        manifest["optional_host_permissions"] = ["http://127.0.0.1:65534/*"]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=False,
                args=[
                    "--window-position=-32000,-32000",
                    "--window-size=1280,900",
                    f"--disable-extensions-except={extension}",
                    f"--load-extension={extension}",
                    "--no-first-run",
                    "--mute-audio",
                ],
            )
            context.route(URL, lambda route: route.fulfill(status=200, content_type="text/html", body=HTML))
            context.route(SECOND_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=HTML_MISSING))

            def captions(route):
                query = parse_qs(urlsplit(route.request.url).query)
                if query.get("v") == [SPA_VIDEO_ID]:
                    payload = SPA_CHINESE if query.get("tlang") else SPA_ENGLISH
                else:
                    payload = CHINESE if query.get("tlang") else ENGLISH
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

            context.route("https://www.youtube.com/api/timedtext**", captions)
            service_worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15_000)
            page = context.pages[0] if context.pages else context.new_page()
            started = time.perf_counter()
            page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
            host = page.locator("#inflow-extension-root")
            host.wait_for(state="attached", timeout=3_000)
            host_ms = round((time.perf_counter() - started) * 1000)
            pill = host.locator("#pill")
            page.wait_for_function(
                "() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent === 'InFlow 字幕已就绪'",
                timeout=7_000,
            )
            ready_ms = round((time.perf_counter() - started) * 1000)
            state = page.evaluate("""() => {
              const root = document.querySelector('#inflow-extension-root').shadowRoot;
              const pill = root.querySelector('#pill');
              return {
                pill: pill.textContent,
                width: pill.getBoundingClientRect().width,
                height: pill.getBoundingClientRect().height,
                english: root.querySelector('#captionEnglish').textContent,
                chinese: root.querySelector('#captionChinese').textContent,
                caption_hidden: root.querySelector('#caption').hidden,
                panel: root.querySelector('#panelMessage').textContent,
              };
            }""")
            ad_time_before = page.locator("video").evaluate("video => video.currentTime")
            page.locator("#movie_player").evaluate("player => player.classList.add('ad-showing')")
            try:
                page.wait_for_function(
                    "() => { const root=document.querySelector('#inflow-extension-root').shadowRoot; return root.querySelector('#caption').hidden && root.querySelector('#pill').textContent === 'InFlow 已备好 · 等正片'; }",
                    timeout=2_000,
                )
            except Exception as error:
                ad_state = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root').shadowRoot; return {playerClass:document.querySelector('#movie_player').className,pill:root.querySelector('#pill').textContent,subtitleState:root.querySelector('#pill').dataset.state,captionHidden:root.querySelector('#caption').hidden}; }""")
                raise AssertionError({"ad_entry_state": ad_state}) from error
            page.keyboard.press("s")
            page.wait_for_timeout(150)
            ad_time_after = page.locator("video").evaluate("video => video.currentTime")
            if abs(ad_time_after - ad_time_before) > 0.01:
                raise AssertionError({"caption_replay_changed_ad_time": [ad_time_before, ad_time_after]})
            page.locator("#movie_player").evaluate("player => player.classList.remove('ad-showing')")
            page.wait_for_function(
                "() => { const root=document.querySelector('#inflow-extension-root').shadowRoot; return !root.querySelector('#caption').hidden && root.querySelector('#pill').textContent === 'InFlow 字幕已就绪'; }",
                timeout=2_000,
            )
            ad_transition_ok = True

            spa_started = time.perf_counter()
            page.evaluate(
                """({url, videoId}) => {
                  history.pushState({}, '', url);
                  globalThis.fixtureResponse = {
                    videoDetails: {videoId, title: 'SPA fixture', lengthSeconds: '60'},
                    captions: {playerCaptionsTracklistRenderer: {captionTracks: [{
                      baseUrl: `https://www.youtube.com/api/timedtext?v=${videoId}&lang=en`,
                      languageCode: 'en',
                      isTranslatable: true,
                    }]}}
                  };
                  document.querySelector('video').currentTime = 0.5;
                  window.dispatchEvent(new Event('yt-navigate-finish'));
                }""",
                {"url": SPA_URL, "videoId": SPA_VIDEO_ID},
            )
            page.wait_for_function(
                """() => {
                  const root=document.querySelector('#inflow-extension-root')?.shadowRoot;
                  return root?.querySelector('#pill')?.textContent === 'InFlow 字幕已就绪'
                    && root?.querySelector('#captionEnglish')?.textContent === 'A new video needs new captions.'
                    && root?.querySelector('#captionChinese')?.textContent === '新视频必须使用新字幕。';
                }""",
                timeout=5_000,
            )
            spa_ready_ms = round((time.perf_counter() - spa_started) * 1000)
            if "Every small step" in page.locator("#inflow-extension-root").locator("#captionEnglish").inner_text():
                raise AssertionError("stale_caption_survived_spa_navigation")

            failure_started = time.perf_counter()
            page.goto(SECOND_URL, wait_until="domcontentloaded", timeout=30_000)
            second_host = page.locator("#inflow-extension-root")
            second_host.wait_for(state="attached", timeout=3_000)
            page.wait_for_function(
                "() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent === 'InFlow 需要查看'",
                timeout=7_000,
            )
            failure_ms = round((time.perf_counter() - failure_started) * 1000)
            second_pill = second_host.locator("#pill")
            second_pill.click()
            retry_button = second_host.locator("#panelPrimary")
            failure_panel = second_host.locator("#panelMessage").inner_text()
            if retry_button.inner_text() != "重试字幕":
                raise AssertionError({"retry_button": retry_button.inner_text()})
            page.evaluate(f"""() => {{
              globalThis.fixtureResponse.captions.playerCaptionsTracklistRenderer.captionTracks = [{{
                baseUrl: 'https://www.youtube.com/api/timedtext?v={SECOND_VIDEO_ID}&lang=en',
                languageCode: 'en',
                isTranslatable: true,
              }}];
            }}""")
            retry_started = time.perf_counter()
            if page.locator("video").get_attribute("data-inflow-bound") != "1":
                raise AssertionError("failed_video_play_listener_not_bound")
            page.evaluate("""() => {
              const video=document.querySelector('video');
              Object.defineProperty(video,'paused',{value:false,configurable:true});
              video.dispatchEvent(new Event('play'));
            }""")
            try:
                page.wait_for_function(
                    "() => document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent === 'InFlow 字幕已就绪'",
                    timeout=7_000,
                )
            except Exception as exc:
                auto_state = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; const video=document.querySelector('video'); return {bound:video?.dataset.inflowBound,paused:video?.paused,pill:root?.querySelector('#pill')?.textContent,message:root?.querySelector('#panelMessage')?.textContent}; }""")
                runtime_state = service_worker.evaluate("""async () => { const [tab]=await chrome.tabs.query({active:true,currentWindow:true}); return chrome.tabs.sendMessage(tab.id,{type:'inflow:getState'}); }""")
                raise AssertionError({"automatic_play_retry_failed": auto_state, "runtime_state": runtime_state}) from exc
            page.evaluate("""() => Object.defineProperty(document.querySelector('video'),'paused',{value:true,configurable:true})""")
            retry_ms = round((time.perf_counter() - retry_started) * 1000)
            recovered_panel = second_host.locator("#panelMessage").inner_text()
            runtime_metrics = service_worker.evaluate("""async () => { const [tab]=await chrome.tabs.query({active:true,currentWindow:true}); const state=await chrome.tabs.sendMessage(tab.id,{type:'inflow:getState'}); return state.performanceMetrics; }""")
            context.close()

    if host_ms > 1000:
        raise AssertionError({"host_visible_too_slow_ms": host_ms})
    if ready_ms > 5500:
        raise AssertionError({"page_caption_too_slow_ms": ready_ms})
    if state["width"] < 110 or state["height"] < 30:
        raise AssertionError({"status_not_visible": state})
    if state["caption_hidden"] or state["english"] != "Every small step matters." or state["chinese"] != "每一个小步骤都很重要。":
        raise AssertionError(state)
    if "本机学习服务未连接" not in state["panel"]:
        raise AssertionError({"offline_copy_missing": state["panel"]})
    if failure_ms > 5000 or "youtube_caption_tracks_missing" not in failure_panel:
        raise AssertionError({"failure_ms": failure_ms, "failure_panel": failure_panel})
    if retry_ms > 5500 or "本机学习服务未连接" not in recovered_panel:
        raise AssertionError({"retry_ms": retry_ms, "recovered_panel": recovered_panel})
    if runtime_metrics.get("status_visible_ms") is None or runtime_metrics.get("status_visible_ms") > 1000 or runtime_metrics.get("caption_attempt_ms") is None or runtime_metrics.get("caption_attempt_ms") > 5500:
        raise AssertionError({"invalid_runtime_metrics": runtime_metrics})
    print(json.dumps({
        "ok": True,
        "host_visible_ms": host_ms,
        "caption_ready_ms": ready_ms,
        "failure_visible_ms": failure_ms,
        "automatic_play_retry_ready_ms": retry_ms,
        "runtime_metrics": runtime_metrics,
        "ad_transition_ok": ad_transition_ok,
        "ad_replay_blocked": True,
        "spa_caption_ready_ms": spa_ready_ms,
        "stale_spa_caption_blocked": True,
        "failure_panel": failure_panel,
        "recovered_panel": recovered_panel,
        **state,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
