from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BASE = "http://127.0.0.1:8773"


def request_json(path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def live_snapshot() -> dict[str, str]:
    live_root = ROOT / "data" / "live"
    if not live_root.exists():
        return {}
    return {
        path.relative_to(live_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in live_root.rglob("*")
        if path.is_file() and path.name != "server.lock"
    }


def main() -> None:
    before_live = live_snapshot()
    before_sessions = [name for name in before_live if name.startswith("sessions/")]
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="inflow-adaptive-production-") as data_dir:
        env = os.environ.copy()
        env.pop("INFLOW_ADAPTIVE_QA", None)
        env.update({"INFLOW_ADAPTIVE_DATA_DIR": data_dir, "INFLOW_ADAPTIVE_PORT": "8773"})
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            cwd=str(ROOT),
            env=env,
            creationflags=create_no_window,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(60):
                try:
                    status, health = request_json("/api/health")
                    if status == 200 and health.get("ok"):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError("production-shape server did not start")
            if health["qa_allowed"]:
                raise AssertionError("QA enabled in production shape")
            qa_status, _ = request_json("/api/sessions", "POST", {"qa": True})
            if qa_status != 403:
                raise AssertionError("production shape accepted QA session")

            page_errors: list[str] = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    executable_path=str(CHROME),
                    args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
                )
                page = browser.new_page(viewport={"width": 1280, "height": 820})
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.goto(BASE + "/", wait_until="networkidle")
                page.locator("#startButton").click()
                page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
                session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
                _, session = request_json(f"/api/sessions/{session_id}")
                item = sorted(
                    [row for row in session["items"] if row["min_intensity"] in {"low", "medium"}],
                    key=lambda row: row["anchor_sec"],
                )[0]
                page.evaluate(
                    """item => {
                      const video = document.querySelector('#sourceVideo');
                      video.currentTime = Math.max(0, item.anchor_sec - 0.35);
                      return video.play();
                    }""",
                    item,
                )
                page.locator("#mappingContinue").wait_for(state="visible", timeout=10_000)
                page.wait_for_timeout(2600)
                if page.locator(".mapping-gloss").inner_text() != item["gloss_zh"]:
                    raise AssertionError("production meaning not visible")
                page.locator("#mappingContinue").click()
                page.locator("#learningLayer").wait_for(state="hidden", timeout=10_000)
                browser.close()
            if page_errors:
                raise AssertionError(page_errors)
            _, profile = request_json("/api/profile")
            if profile["reading_samples"] != 1:
                raise AssertionError(profile)
        finally:
            time.sleep(0.5)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            time.sleep(0.2)

    after_live = live_snapshot()
    if after_live != before_live:
        raise AssertionError({"before": before_live, "after": after_live})
    print(
        json.dumps(
            {
                "ok": True,
                "qa_allowed": False,
                "meaning_visible_until_user_action": True,
                "reading_samples": 1,
                "real_live_sessions": len(before_sessions),
                "real_live_unchanged": True,
                "isolated_data_dir": data_dir,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
