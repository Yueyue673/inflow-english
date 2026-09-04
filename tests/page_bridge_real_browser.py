from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BROWSERS = os.environ.get("INFLOW_PLAYWRIGHT_BROWSERS")
if BROWSERS:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS
from playwright.sync_api import sync_playwright

SOURCE_EXTENSION = ROOT / "extension"
EXTENSION_ID = "hfkkhkdpakcmpokgbihceoppleeokifd"
VIDEO_ID = os.environ.get("INFLOW_REAL_VIDEO_ID", "arj7oStGLkU")
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
CHROME = Path(os.environ.get("INFLOW_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"))


def main() -> None:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="inflow-page-bridge-") as temporary:
        extension = Path(temporary) / "extension"
        profile = Path(temporary) / "profile"
        shutil.copytree(SOURCE_EXTENSION, extension)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=False,
                executable_path=str(CHROME) if CHROME.is_file() else None,
                args=[
                    "--window-position=-32000,-32000",
                    "--window-size=1280,900",
                    f"--disable-extensions-except={extension}",
                    f"--load-extension={extension}",
                    "--no-first-run",
                    "--mute-audio",
                    "--proxy-server=http://127.0.0.1:7890",
                ],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(URL, wait_until="commit", timeout=90_000)
            try:
                page.wait_for_function(
                    """videoId => {
                      const player = document.querySelector('#movie_player');
                      const responses = [typeof player?.getPlayerResponse === 'function' ? player.getPlayerResponse() : null, globalThis.ytInitialPlayerResponse];
                      return responses.some(response => response?.videoDetails?.videoId === videoId && response?.captions?.playerCaptionsTracklistRenderer?.captionTracks?.length);
                    }""",
                    arg=VIDEO_ID,
                    timeout=60_000,
                )
            except Exception as exc:
                state = page.evaluate("""videoId => {
                  const player=document.querySelector('#movie_player');
                  const current=typeof player?.getPlayerResponse==='function' ? player.getPlayerResponse() : null;
                  const initial=globalThis.ytInitialPlayerResponse;
                  const summary=response => ({videoId:response?.videoDetails?.videoId || null, tracks:response?.captions?.playerCaptionsTracklistRenderer?.captionTracks?.length || 0, playability:response?.playabilityStatus?.status || null, reason:response?.playabilityStatus?.reason || null});
                  return {title:document.title,url:location.href,expected:videoId,player:summary(current),initial:summary(initial),hasVideo:Boolean(document.querySelector('video')),body:(document.body?.innerText || '').slice(0,600)};
                }""", VIDEO_ID)
                context.close()
                raise AssertionError({"player_caption_tracks_missing": state}) from exc
            payload = page.evaluate(
                """({src, videoId}) => new Promise((resolve, reject) => {
                  const nonce = crypto.randomUUID();
                  const timer = setTimeout(() => { cleanup(); reject(new Error('bridge_timeout')); }, 8000);
                  const onMessage = event => {
                    const data = event.data;
                    if (event.source !== window || event.origin !== location.origin || data?.source !== 'inflow-page-caption-bridge' || data?.nonce !== nonce) return;
                    cleanup(); resolve(data);
                  };
                  const script = document.createElement('script');
                  const cleanup = () => { clearTimeout(timer); window.removeEventListener('message', onMessage); script.remove(); };
                  script.src = src;
                  script.dataset.inflowNonce = nonce;
                  script.dataset.inflowVideoId = videoId;
                  script.onerror = () => { cleanup(); reject(new Error('bridge_load_failed')); };
                  window.addEventListener('message', onMessage);
                  document.documentElement.appendChild(script);
                })""",
                {"src": f"chrome-extension://{EXTENSION_ID}/page-bridge.js", "videoId": VIDEO_ID},
            )
            context.close()
    if not payload.get("ok"):
        raise AssertionError(payload)
    english_events = (payload.get("english") or {}).get("events") or []
    chinese_events = (payload.get("chinese") or {}).get("events") or []
    if not english_events:
        raise AssertionError({"english_events": 0, "payload_keys": sorted(payload)})
    print(json.dumps({
        "ok": True,
        "video_id": payload.get("video_id"),
        "duration_sec": payload.get("duration_sec"),
        "english_events": len(english_events),
        "chinese_events": len(chinese_events),
        "caption_source": payload.get("caption_source"),
        "elapsed_sec": round(time.perf_counter() - started, 2),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
