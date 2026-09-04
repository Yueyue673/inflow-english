from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Browser, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BASE = "http://127.0.0.1:8772"


def request_json(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


@contextmanager
def isolated_server(content_ids: list[str] | None = None):
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="inflow-adaptive-failure-") as data_dir:
        env = os.environ.copy()
        if content_ids:
            source_content = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))
            source_content["items"] = [item for item in source_content["items"] if item["id"] in set(content_ids)]
            content_path = Path(data_dir) / "content.json"
            content_path.write_text(json.dumps(source_content, ensure_ascii=False), encoding="utf-8")
            env["INFLOW_ADAPTIVE_CONTENT_PATH"] = str(content_path)
        else:
            env.pop("INFLOW_ADAPTIVE_CONTENT_PATH", None)
        env.update(
            {
                "INFLOW_ADAPTIVE_DATA_DIR": data_dir,
                "INFLOW_ADAPTIVE_QA": "1",
                "INFLOW_ADAPTIVE_PORT": "8772",
            }
        )
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
                    if request_json("/api/health").get("ok"):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError("server did not become ready")
            yield Path(data_dir)
        finally:
            try:
                request_json("/api/health")
            except Exception:
                pass
            time.sleep(0.6)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            time.sleep(0.2)


def start_page(browser: Browser, case: str):
    context = browser.new_context(viewport={"width": 1280, "height": 820})
    page = context.new_page()
    page.goto(f"{BASE}/?qa=1&case={case}&ts={time.time_ns()}", wait_until="networkidle")
    page.locator("#startButton").click()
    page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
    session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
    session = request_json(f"/api/sessions/{session_id}")
    allowed = sorted(
        [item for item in session["items"] if item["min_intensity"] in {"low", "medium"}],
        key=lambda item: item["anchor_sec"],
    )
    return context, page, session_id, allowed


def move_to(page, item: dict) -> None:
    page.evaluate(
        """item => {
          const video = document.querySelector('#sourceVideo');
          video.currentTime = Math.max(0, item.anchor_sec - 0.35);
          return video.play();
        }""",
        item,
    )


def test_audio_failure_keeps_meaning(browser: Browser) -> dict:
    with isolated_server():
        context, page, session_id, allowed = start_page(browser, "audio-failure")
        item = allowed[0]
        page.route(f"**/{item['phrase_audio']}", lambda route: route.abort("failed"))
        move_to(page, item)
        page.locator("#mappingContinue").wait_for(state="visible", timeout=10_000)
        if page.locator(".mapping-gloss").inner_text() != item["gloss_zh"]:
            raise AssertionError("meaning missing after audio failure")
        if page.locator(".mapping-translation").inner_text() != item["phrase_zh"]:
            raise AssertionError("matching Chinese excerpt missing after audio failure")
        if "声音没有正常播放" not in page.locator("#mappedAudioState").inner_text():
            raise AssertionError("audio failure was not stated on the stable card")
        if page.locator("#learningTitle").inner_text().strip():
            raise AssertionError("obsolete teaching title remained on the stable card")
        page.wait_for_timeout(2200)
        if not page.locator(".mapping-gloss").is_visible():
            raise AssertionError("meaning disappeared during audio failure")
        page.locator("#mappingContinue").click()
        page.locator("#learningLayer").wait_for(state="hidden", timeout=10_000)
        state = request_json(f"/api/sessions/{session_id}")
        if state["interaction_summary"]["technical_failure"] != 1:
            raise AssertionError(state["interaction_summary"])
        profile = request_json("/api/profile")
        if profile["reading_samples"] != 0:
            raise AssertionError("technical failure trained reading pace")
        context.close()
        return {"item": item["id"], "meaning_visible": True, "technical_failure": 1}


def test_reload_during_mapping(browser: Browser) -> dict:
    with isolated_server():
        context, page, session_id, allowed = start_page(browser, "reload-mapping")
        item = allowed[0]
        move_to(page, item)
        page.locator("#mappingContinue").wait_for(state="visible", timeout=10_000)
        page.reload(wait_until="networkidle")
        page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
        page.locator("#resumeButton").wait_for(state="visible", timeout=10_000)
        state = request_json(f"/api/sessions/{session_id}")
        if state["open_interaction"] is not None or state["interaction_summary"]["technical_failure"] != 1:
            raise AssertionError(state)
        context.close()
        return {"item": item["id"], "open_interaction": None, "recovered_as": "technical_failure"}


def test_intensity_is_only_persistent_setting(browser: Browser) -> dict:
    with isolated_server():
        context = browser.new_context(viewport={"width": 1280, "height": 820})
        page = context.new_page()
        page.goto(f"{BASE}/?qa=1&case=intensity&ts={time.time_ns()}", wait_until="networkidle")
        page.locator('label:has(input[value="low"])').click()
        page.wait_for_function("() => document.querySelector('input[value=low]').checked")
        page.reload(wait_until="networkidle")
        if not page.locator('input[value="low"]').is_checked():
            raise AssertionError("low intensity did not persist")
        if page.locator("select").count() or page.locator("input[type=range]").count():
            raise AssertionError("advanced settings leaked into primary UI")
        page.locator("#startButton").click()
        session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
        page.wait_for_function("() => document.querySelector('#watchPanel') && !document.querySelector('#watchPanel').hidden")
        session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
        session = request_json(f"/api/sessions/{session_id}")
        allowed = [item for item in session["items"] if item["min_intensity"] == "low"]
        if len(allowed) != 1 or session["profile"]["effective_intensity"] != "low":
            raise AssertionError({"allowed": len(allowed), "profile": session["profile"]})
        context.close()
        return {"intensity": "low", "allowed_items": 1, "extra_setting_controls": 0}


def test_low_confidence_alignment_is_static_and_explicit(browser: Browser) -> dict:
    with isolated_server(["topography", "intensity", "refine", "rechecked"]):
        context = browser.new_context(viewport={"width": 1280, "height": 820})
        page = context.new_page()
        page.goto(f"{BASE}/?qa=1&case=fallback-alignment&ts={time.time_ns()}", wait_until="networkidle")
        page.locator('label:has(input[value="high"])').click()
        page.locator("#startButton").click()
        page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
        page.wait_for_function("() => document.querySelector('#sourceVideo').readyState >= 1 && !document.querySelector('#sourceVideo').paused")
        session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
        session = request_json(f"/api/sessions/{session_id}")
        item = next(row for row in session["items"] if row["id"] == "topography")
        if item["alignment_quality"] != "manifest_fallback_wide":
            raise AssertionError(item)
        page.evaluate(
            """() => {
              window.__fallbackBecameActive = false;
              const root = document.querySelector('#learningLayer');
              new MutationObserver(() => {
                if (root.querySelector('mark.approximate.active')) window.__fallbackBecameActive = true;
              }).observe(root, { subtree: true, attributes: true, attributeFilter: ['class'] });
            }"""
        )
        move_to(page, item)
        page.locator("#mappingContinue:not([disabled])").wait_for(state="visible", timeout=10_000)
        page.get_by_text("声音位置为大概范围").wait_for(timeout=5_000)
        page.locator("#targetMark.approximate").wait_for(state="visible", timeout=5_000)
        page.wait_for_timeout(1400)
        if not page.locator("#mappingContinue").is_visible():
            raise AssertionError("stable low-confidence mapping auto-dismissed")
        if page.evaluate("window.__fallbackBecameActive"):
            raise AssertionError("fallback alignment used precise timed active highlight")
        page.locator("#mappingContinue").click()
        page.locator("#learningLayer").wait_for(state="hidden", timeout=10_000)
        context.close()
        return {"item": "topography", "quality": "manifest_fallback_wide", "timed_active": False, "copy": "大概位置"}


def test_second_tab_requires_explicit_claim(browser: Browser) -> dict:
    with isolated_server():
        context, first_page, session_id, allowed = start_page(browser, "first-owner")
        item = allowed[0]
        move_to(first_page, item)
        first_page.locator("#mappingContinue").wait_for(state="visible", timeout=10_000)
        second_page = context.new_page()
        second_page.goto(f"{BASE}/?qa=1&case=second-owner&ts={time.time_ns()}", wait_until="networkidle")
        second_page.get_by_role("heading", name="这次观看已在另一个页面打开").wait_for(timeout=10_000)
        if not first_page.locator("#mappingContinue").is_visible():
            raise AssertionError("second tab silently cancelled the original interaction")
        before_claim = request_json(f"/api/sessions/{session_id}")
        if not before_claim["open_interaction"] or before_claim["interaction_summary"]["technical_failure"] != 0:
            raise AssertionError(before_claim)
        second_page.locator("#claimSession").click()
        second_page.locator("#resumeButton").wait_for(state="visible", timeout=10_000)
        after_claim = request_json(f"/api/sessions/{session_id}")
        if after_claim["open_interaction"] is not None or after_claim["interaction_summary"]["technical_failure"] != 1:
            raise AssertionError(after_claim)
        context.close()
        return {"silent_cancel_before_claim": False, "explicit_claim": True, "recovered_as": "technical_failure"}


def test_explicit_rechoice_resets_session_inference_across_reload(browser: Browser) -> dict:
    with isolated_server():
        context = browser.new_context(viewport={"width": 1280, "height": 820})
        page = context.new_page()
        page.goto(f"{BASE}/?qa=1&case=epoch-reset&ts={time.time_ns()}", wait_until="networkidle")
        page.locator('label:has(input[value="high"])').click()
        page.locator("#startButton").click()
        page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
        session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
        session = request_json(f"/api/sessions/{session_id}")
        items = sorted(session["items"], key=lambda item: item["anchor_sec"])
        for item in items[:2]:
            move_to(page, item)
            page.locator("#mappingContinue").wait_for(state="visible", timeout=10_000)
            page.keyboard.press("Escape")
            page.locator("#learningLayer").wait_for(state="hidden", timeout=10_000)
        page.locator('label:has(input[value="medium"])').click()
        page.locator('label:has(input[value="high"])').click()
        page.wait_for_timeout(300)
        page.reload(wait_until="networkidle")
        page.locator("#home").wait_for(state="visible", timeout=10_000)
        if not page.locator('input[value="high"]').is_checked():
            raise AssertionError("explicit high choice was lost")
        state = request_json(f"/api/sessions/{session_id}")
        if state["current_epoch_skipped"] != 0:
            raise AssertionError(state["current_epoch_skipped"])
        page.locator("#startButton").click()
        page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
        page.wait_for_function("() => !document.querySelector('#sourceVideo').paused")
        target = next(item for item in items[2:] if item["min_intensity"] == "high")
        move_to(page, target)
        page.locator("#mappingContinue").wait_for(state="visible", timeout=10_000)
        page.keyboard.press("Escape")
        page.locator("#learningLayer").wait_for(state="hidden", timeout=10_000)
        context.close()
        return {"old_epoch_skips": 2, "new_epoch_skips_after_reload": 0, "high_item_shown": target["id"]}


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(CHROME),
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        try:
            results = {
                "audio_failure": test_audio_failure_keeps_meaning(browser),
                "reload_mapping": test_reload_during_mapping(browser),
                "intensity": test_intensity_is_only_persistent_setting(browser),
                "fallback_alignment": test_low_confidence_alignment_is_static_and_explicit(browser),
                "second_tab_claim": test_second_tab_requires_explicit_claim(browser),
                "explicit_epoch_reset": test_explicit_rechoice_resets_session_inference_across_reload(browser),
            }
        finally:
            browser.close()
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
