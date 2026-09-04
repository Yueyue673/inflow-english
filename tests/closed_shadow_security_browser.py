from __future__ import annotations

import json
import os
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
from tests.standalone_caption_browser import CHINESE, ENGLISH, HTML, SOURCE_EXTENSION, URL


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="inflow-closed-shadow-") as temporary:
        temp = Path(temporary)
        extension = temp / "extension"
        profile = temp / "profile"
        copy_extension_for_browser(SOURCE_EXTENSION, extension, open_shadow_for_test=False)
        worker = extension / "service-worker.js"
        worker.write_text(worker.read_text(encoding="utf-8").replace("http://127.0.0.1:8767", "http://127.0.0.1:65534"), encoding="utf-8")
        manifest_path = extension / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["host_permissions"] = ["https://www.youtube.com/*"]
        manifest["optional_host_permissions"] = ["http://127.0.0.1:65534/*"]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
            context.route(URL, lambda route: route.fulfill(status=200, content_type="text/html", body=HTML))

            def captions(route):
                query = parse_qs(urlsplit(route.request.url).query)
                payload = CHINESE if query.get("tlang") else ENGLISH
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

            context.route("https://www.youtube.com/api/timedtext**", captions)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
            page.locator("#inflow-extension-root").wait_for(state="attached", timeout=3_000)
            page.wait_for_timeout(1_000)
            exposure = page.evaluate("""() => {
              const host = document.querySelector('#inflow-extension-root');
              return {
                shadowRoot: host.shadowRoot,
                childButton: host.querySelector('button'),
                bodyLeaksCaption: document.body.innerText.includes('Every small step matters.'),
              };
            }""")
            if exposure != {"shadowRoot": None, "childButton": None, "bodyLeaksCaption": False}:
                raise AssertionError({"closed_shadow_exposure": exposure})

            cdp = context.new_cdp_session(page)
            deadline = time.perf_counter() + 4
            state_name = None
            while time.perf_counter() < deadline:
                nodes = cdp.send("Accessibility.getFullAXTree").get("nodes", [])
                names = [str((node.get("name") or {}).get("value") or "") for node in nodes]
                state_name = next((name for name in names if name.startswith("InFlow ")), None)
                if state_name == "InFlow 字幕已就绪":
                    break
                page.wait_for_timeout(100)
            if state_name != "InFlow 字幕已就绪":
                raise AssertionError({"accessible_status": state_name})

            before = page.locator("video").evaluate("video => video.currentTime")
            page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'s',bubbles:true}))")
            page.wait_for_timeout(150)
            after = page.locator("video").evaluate("video => video.currentTime")
            if abs(after - before) > 0.01:
                raise AssertionError({"synthetic_key_changed_time": [before, after]})
            context.close()

    print(json.dumps({
        "ok": True,
        "closed_shadow": True,
        "page_private_state_exposed": False,
        "synthetic_keyboard_blocked": True,
        "accessible_status": state_name,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
