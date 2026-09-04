from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
BROWSERS = os.environ.get("INFLOW_PLAYWRIGHT_BROWSERS")
if BROWSERS:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS
EXPECTED_ID = "hfkkhkdpakcmpokgbihceoppleeokifd"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="inflow-extension-profile-") as profile:
        page_errors: list[str] = []
        console_errors: list[str] = []
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                profile,
                headless=False,
                args=[
                    "--window-position=-32000,-32000",
                    "--window-size=900,700",
                    f"--disable-extensions-except={EXTENSION}",
                    f"--load-extension={EXTENSION}",
                    "--no-first-run",
                ],
            )
            service_worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15_000)
            extension_id = service_worker.url.split("/")[2]
            if extension_id != EXPECTED_ID:
                raise AssertionError({"extension_id": extension_id, "expected": EXPECTED_ID})
            worker_health = service_worker.evaluate("""async () => {
              try {
                const response = await fetch('http://127.0.0.1:8767/api/health', {cache:'no-store'});
                return {ok: response.ok, status: response.status, text: await response.text()};
              } catch (error) {
                return {ok: false, error: String(error)};
              }
            }""")
            if not worker_health.get("ok"):
                raise AssertionError({"worker_health": worker_health})
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.goto(f"chrome-extension://{extension_id}/popup.html", wait_until="networkidle")
            page.get_by_text("InFlow English", exact=True).wait_for(timeout=5_000)
            if page.locator('input[name="frequency"]').count() != 3:
                raise AssertionError("popup_frequency_control_invalid")
            screenshot = ROOT / "data" / "extension-popup.png"
            page.screenshot(path=str(screenshot), full_page=True)
            if page_errors or console_errors:
                raise AssertionError({"page_errors": page_errors, "console_errors": console_errors})
            context.close()
        print(json.dumps({"ok": True, "extension_id": EXPECTED_ID, "service_worker": service_worker.url, "worker_health": worker_health, "page_errors": page_errors, "console_errors": console_errors, "screenshot": str(screenshot)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
