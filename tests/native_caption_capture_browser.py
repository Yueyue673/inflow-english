from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BROWSERS = os.environ.get("INFLOW_PLAYWRIGHT_BROWSERS")
if BROWSERS:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS
from playwright.sync_api import sync_playwright

from tests.extension_test_support import copy_extension_for_browser
from tests.live_data_guard import snapshot_live_tree

SOURCE_EXTENSION = ROOT / "extension"
VIDEO_ID = "captur1234x"
HOME_URL = "https://www.youtube.com/"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
ENGLISH = {"events": [{"tStartMs": 0, "dDurationMs": 4000, "segs": [{"utf8": "The native response is captured once."}]}]}
CHINESE = {"events": [{"tStartMs": 0, "dDurationMs": 4000, "segs": [{"utf8": "原生响应只捕获一次。"}]}]}
HTML = f"""<!doctype html><html><head><meta charset='utf-8'><title>Native capture fixture</title></head><body>
<div id='movie_player'></div><video class='html5-main-video'></video>
<script>
const response={{videoDetails:{{videoId:'{VIDEO_ID}',title:'Native capture',lengthSeconds:'60'}},captions:{{playerCaptionsTracklistRenderer:{{captionTracks:[{{baseUrl:'https://www.youtube.com/api/timedtext?v={VIDEO_ID}&lang=en',languageCode:'en',isTranslatable:true}}]}}}}}};
globalThis.fixtureResponse=null;
const player=document.querySelector('#movie_player');
let currentTrack={{languageCode:'en',translationLanguage:{{languageCode:'zh-Hans'}}}};
player.getPlayerResponse=()=>globalThis.fixtureResponse;
player.getOption=()=>currentTrack;
player.setOption=(_group,_name,track)=>{{
  currentTrack=track;
  const tlang=track?.translationLanguage?.languageCode;
  const url=new URL('https://www.youtube.com/api/timedtext');
  url.searchParams.set('v','{VIDEO_ID}');
  url.searchParams.set('lang',track?.languageCode||'en');
  if(tlang) url.searchParams.set('tlang',tlang);
  fetch(url.toString()).catch(()=>{{}});
}};
const video=document.querySelector('video');
Object.defineProperties(video,{{duration:{{value:60,configurable:true}},currentTime:{{value:0.5,writable:true,configurable:true}},paused:{{value:false,configurable:true}},ended:{{value:false,configurable:true}}}});
globalThis.activateFixture=()=>{{
  globalThis.fixtureResponse=response;
  history.pushState({{}},'',{json.dumps(URL)});
  player.setOption('captions','track',currentTrack);
  window.dispatchEvent(new Event('yt-navigate-finish'));
}};
</script></body></html>"""


@contextmanager
def guard_formal_data():
    live_root = ROOT / "data" / "live"
    before = snapshot_live_tree(live_root)
    try:
        yield
    finally:
        after = snapshot_live_tree(live_root)
        if after != before:
            raise AssertionError("formal_data_tree_changed")


def main() -> None:
    served: dict[tuple[str, str], int] = {}
    requests: list[dict[str, object]] = []
    with guard_formal_data(), tempfile.TemporaryDirectory(prefix="inflow-native-capture-") as temporary:
        temp = Path(temporary)
        extension = temp / "extension"
        profile = temp / "profile"
        copy_extension_for_browser(SOURCE_EXTENSION, extension, open_shadow_for_test=True)
        worker = extension / "service-worker.js"
        worker.write_text(worker.read_text(encoding="utf-8").replace("http://127.0.0.1:8767", "http://127.0.0.1:65534"), encoding="utf-8")

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile),
                headless=False,
                args=[
                    "--window-position=-32000,-32000",
                    "--window-size=1280,900",
                    f"--disable-extensions-except={extension}",
                    f"--load-extension={extension}",
                    "--no-first-run",
                    "--mute-audio",
                ],
            )
            context.route(HOME_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=HTML))

            def timed_text(route) -> None:
                query = parse_qs(urlsplit(route.request.url).query)
                key = ((query.get("lang") or [""])[0], (query.get("tlang") or [""])[0])
                served[key] = served.get(key, 0) + 1
                first_native_response = served[key] == 1
                requests.append({"lang": key[0], "tlang": key[1], "attempt": served[key], "served": first_native_response})
                if first_native_response:
                    payload = CHINESE if key[1].lower().startswith("zh") else ENGLISH
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))
                else:
                    route.fulfill(status=200, content_type="application/json", body="")

            context.route("https://www.youtube.com/api/timedtext**", timed_text)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30_000)
            page.evaluate("globalThis.activateFixture()")
            try:
                page.wait_for_function(
                    """() => {
                      const root=document.querySelector('#inflow-extension-root')?.shadowRoot;
                      return root?.querySelector('#pill')?.textContent === 'InFlow 字幕已就绪'
                        && root?.querySelector('#captionEnglish')?.textContent === 'The native response is captured once.'
                        && root?.querySelector('#captionChinese')?.textContent === '原生响应只捕获一次。';
                    }""",
                    timeout=7_000,
                )
            except Exception as exc:
                debug = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root')?.shadowRoot; return {hook:globalThis.__inflowCaptionCaptureV1===true,pill:root?.querySelector('#pill')?.textContent,message:root?.querySelector('#panelMessage')?.textContent,english:root?.querySelector('#captionEnglish')?.textContent,chinese:root?.querySelector('#captionChinese')?.textContent}; }""")
                context.close()
                raise AssertionError({"native_capture_not_ready": debug, "requests": requests}) from exc
            state = page.evaluate("""() => { const root=document.querySelector('#inflow-extension-root').shadowRoot; return {hook:globalThis.__inflowCaptionCaptureV1===true,path:location.pathname,pill:root.querySelector('#pill').textContent,english:root.querySelector('#captionEnglish').textContent,chinese:root.querySelector('#captionChinese').textContent}; }""")
            worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=5_000)
            worker.evaluate("chrome.storage.local.set({autoMode:false})")
            page.wait_for_function("() => window.fetch.name !== 'inflowObservedFetch'", timeout=3_000)
            capture_disabled = page.evaluate("() => ({fetch:window.fetch.name,stored:localStorage.getItem('inflow:auto-captions-enabled:v1')})")
            worker.evaluate("chrome.storage.local.set({autoMode:true})")
            page.wait_for_function("() => window.fetch.name === 'inflowObservedFetch'", timeout=3_000)
            capture_reenabled = page.evaluate("() => ({fetch:window.fetch.name,stored:localStorage.getItem('inflow:auto-captions-enabled:v1')})")
            context.close()

    if state.get("hook") is not True or state.get("path") != "/watch":
        raise AssertionError({"spa_hook_not_active": state})
    if capture_disabled.get("stored") != "0" or capture_disabled.get("fetch") == "inflowObservedFetch":
        raise AssertionError({"capture_not_disabled": capture_disabled})
    if capture_reenabled != {"fetch": "inflowObservedFetch", "stored": "1"}:
        raise AssertionError({"capture_not_reenabled": capture_reenabled})
    if not any(row["lang"] == "en" and not row["tlang"] and row["served"] for row in requests):
        raise AssertionError({"english_native_response_missing": requests})
    if not any(str(row["tlang"]).lower().startswith("zh") and row["served"] for row in requests):
        raise AssertionError({"chinese_native_response_missing": requests})
    print(json.dumps({"ok": True, "state": state, "capture_disabled": capture_disabled, "capture_reenabled": capture_reenabled, "requests": requests}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
