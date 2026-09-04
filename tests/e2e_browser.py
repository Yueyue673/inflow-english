from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8771"
ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def fetch(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as response:
        return json.load(response)


def run_case() -> dict:
    page_errors: list[str] = []
    console_errors: list[str] = []
    failed_requests: list[str] = []
    requested_urls: list[str] = []

    def record_failed_request(request) -> None:
        reason = request.failure or "unknown"
        if request.url.endswith("/media/source.mp4") and "ERR_ABORTED" in reason:
            return
        failed_requests.append(f"{request.url} :: {reason}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(CHROME),
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 820})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("requestfailed", record_failed_request)
        page.on("request", lambda request: requested_urls.append(request.url))

        page.goto(f"{BASE}/?qa=1&ts={time.time_ns()}", wait_until="networkidle")
        if page.locator('input[name="intensity"]').count() != 3:
            raise AssertionError("intensity control is not exactly low/medium/high")
        if page.locator("select").count() != 0:
            raise AssertionError("unexpected complex selector exposed")
        if not page.locator('input[name="intensity"][value="medium"]').is_checked():
            raise AssertionError("medium is not the stable default")

        page.locator("#startButton").click()
        page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
        if page.locator("#probePanel").is_visible():
            raise AssertionError("probe appeared without an eligible delayed item")
        session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
        if not session_id:
            raise AssertionError("session id not persisted")
        session = fetch(f"/api/sessions/{session_id}")
        allowed = sorted(
            [item for item in session["items"] if item["min_intensity"] in {"low", "medium"}],
            key=lambda item: item["anchor_sec"],
        )
        if len(allowed) != 2:
            raise AssertionError(f"medium should allow two items, got {len(allowed)}")

        held_for_ms = []
        pause_drift_ms = []
        resume_latency_ms = []
        source_to_helper_ms = []
        learned = []
        for index, item in enumerate(allowed):
            page.evaluate(
                """() => {
                  window.__inflowTransitionTimes = { sourcePause: null, helperPlaying: null };
                  document.querySelector('#sourceVideo').addEventListener('pause', () => {
                    window.__inflowTransitionTimes.sourcePause ??= performance.now();
                  }, { once: true });
                  document.querySelector('#helperAudio').addEventListener('playing', () => {
                    window.__inflowTransitionTimes.helperPlaying ??= performance.now();
                  }, { once: true });
                }"""
            )
            page.evaluate(
                """item => {
                  const video = document.querySelector('#sourceVideo');
                  video.playbackRate = 1;
                  video.currentTime = Math.max(0, item.anchor_sec - 0.35);
                  return video.play();
                }""",
                item,
            )
            page.locator("#learningLayer").wait_for(state="visible", timeout=10_000)
            page.locator("#mappingContinue").wait_for(state="visible", timeout=10_000)
            paused_at = page.locator("#sourceVideo").evaluate("video => video.currentTime")
            pause_drift = round((paused_at - float(item["anchor_sec"])) * 1000)
            pause_drift_ms.append(pause_drift)
            if abs(pause_drift) > 350:
                raise AssertionError({"item": item["id"], "pause_drift_ms": pause_drift})
            transition_times = page.evaluate("window.__inflowTransitionTimes")
            if transition_times["sourcePause"] is None or transition_times["helperPlaying"] is None:
                raise AssertionError({"item": item["id"], "transition_times": transition_times})
            source_to_helper = round(transition_times["helperPlaying"] - transition_times["sourcePause"])
            source_to_helper_ms.append(source_to_helper)
            if source_to_helper < 0 or source_to_helper > 180:
                raise AssertionError({"item": item["id"], "source_to_helper_ms": source_to_helper})
            transition_duration = page.locator("#learningLayer").evaluate("node => getComputedStyle(node).transitionDuration")
            if transition_duration not in {"0.14s", "0s"}:
                raise AssertionError({"transition_duration": transition_duration})
            if page.locator("text=听到就知道").count() or page.locator("text=有点模糊").count():
                raise AssertionError("per-word self-report leaked into adaptive teaching")
            visible_word = page.locator(".mapping-word").inner_text()
            visible_gloss = page.locator(".mapping-gloss").inner_text()
            if visible_word != item["surface"] or visible_gloss != item["gloss_zh"]:
                raise AssertionError({"expected": item, "word": visible_word, "gloss": visible_gloss})
            if page.locator(".mapping-context").inner_text() != item["phrase_text"]:
                raise AssertionError("displayed English excerpt does not match the played audio contract")
            if page.locator(".mapping-translation").inner_text() != item["phrase_zh"]:
                raise AssertionError("displayed Chinese excerpt does not match the played audio contract")
            if not page.locator("#sourceVideo").evaluate("video => video.paused"):
                raise AssertionError("video resumed while the stable mapping was still visible")
            started = time.perf_counter()
            hold_ms = 5200 if index == 0 else 1600
            page.wait_for_timeout(hold_ms)
            if not page.locator("#mappingContinue").is_visible() or page.locator(".mapping-gloss").inner_text() != item["gloss_zh"]:
                raise AssertionError("meaning auto-dismissed before the user continued")
            if index == 0:
                mapping_screenshot = ROOT / "data" / "adaptive-mapping.png"
                page.screenshot(path=str(mapping_screenshot), full_page=True)
            held_for_ms.append(round((time.perf_counter() - started) * 1000))
            if index == 0:
                page.locator("#mappingReplay").click()
                page.locator("#mappingReplay:not([disabled])").wait_for(timeout=8_000)
            resume_started = time.perf_counter()
            page.locator("#mappingContinue").click()
            page.wait_for_function("() => !document.querySelector('#sourceVideo').paused")
            resume_latency = round((time.perf_counter() - resume_started) * 1000)
            resume_latency_ms.append(resume_latency)
            if resume_latency > 500:
                raise AssertionError({"item": item["id"], "resume_latency_ms": resume_latency})
            page.locator("#learningLayer").wait_for(state="hidden", timeout=10_000)
            learned.append(item["id"])

        if any("/media/words/" in url for url in requested_urls):
            raise AssertionError("isolated hard-cut word audio was requested")

        page.evaluate(
            """() => {
              const video = document.querySelector('#sourceVideo');
              video.currentTime = Math.max(0, video.duration - 0.12);
              return video.play();
            }"""
        )
        page.locator("#completePanel").wait_for(state="visible", timeout=15_000)
        page.get_by_role("heading", name="这次学了 2 个原声表达").wait_for(timeout=5_000)
        screenshot = ROOT / "data" / "adaptive-e2e-final.png"
        page.screenshot(path=str(screenshot), full_page=True)
        page.evaluate("sessionId => localStorage.setItem('inflow-adaptive-session', sessionId)", session_id)
        page.reload(wait_until="networkidle")
        page.locator("#home").wait_for(state="visible", timeout=10_000)
        if page.locator("#completePanel").is_visible():
            raise AssertionError("completed session trapped a later app launch")
        browser.close()

    profile = fetch("/api/profile")
    session = fetch(f"/api/sessions/{session_id}")
    if session["interaction_summary"]["completed"] != 2:
        raise AssertionError(session["interaction_summary"])
    if profile["reading_samples"] != 2:
        raise AssertionError(profile)
    if profile["explicit_intensity"] != "medium" or profile["effective_intensity"] != "medium":
        raise AssertionError(profile)
    if page_errors or console_errors or failed_requests:
        raise AssertionError({"page_errors": page_errors, "console_errors": console_errors, "failed_requests": failed_requests})

    print(
        json.dumps(
            {
                "ok": True,
                "session_id": session_id,
                "learned": learned,
                "meaning_held_until_user_action_ms": held_for_ms,
                "pause_drift_ms": pause_drift_ms,
                "resume_latency_ms": resume_latency_ms,
                "source_to_helper_ms": source_to_helper_ms,
                "transition_duration": transition_duration,
                "isolated_word_requests": 0,
                "intensity": profile["explicit_intensity"],
                "interaction_summary": session["interaction_summary"],
                "reading_samples": profile["reading_samples"],
                "page_errors": page_errors,
                "console_errors": console_errors,
                "failed_requests": failed_requests,
                "screenshot": str(screenshot),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="inflow-adaptive-e2e-") as data_dir:
        env = os.environ.copy()
        env.update(
            {
                "INFLOW_ADAPTIVE_DATA_DIR": data_dir,
                "INFLOW_ADAPTIVE_QA": "1",
                "INFLOW_ADAPTIVE_PORT": "8771",
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
                    if fetch("/api/health").get("ok"):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError("QA server did not become ready")
            run_case()
            rebuild = subprocess.run(
                [sys.executable, str(ROOT / "rebuild_profile.py"), "--data-dir", data_dir],
                cwd=str(ROOT),
                check=False,
                capture_output=True,
                text=True,
            )
            if rebuild.returncode != 0:
                raise AssertionError({"stdout": rebuild.stdout, "stderr": rebuild.stderr})
            rebuild_payload = json.loads(rebuild.stdout)
            if not rebuild_payload.get("matches_existing"):
                raise AssertionError(rebuild_payload)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            time.sleep(0.4)


if __name__ == "__main__":
    main()
