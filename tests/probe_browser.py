from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PORT = 8776
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(ROOT))
from adaptive_core import (  # noqa: E402
    POLICY_VERSION,
    apply_interaction,
    atomic_write_json,
    isoformat,
    new_profile,
    utc_now,
)


def request_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as response:
        return json.load(response)


def seed_due_profile(data_dir: Path) -> None:
    content = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))
    now = utc_now()
    created_at = now - timedelta(hours=25)
    taught_at = now - timedelta(hours=24)
    profile = new_profile(content, {"detailed"}, now=created_at)
    profile = apply_interaction(
        profile,
        "refine",
        outcome="completed",
        dwell_ms=6400,
        phrase_confirmed=True,
        replays=0,
        now=taught_at,
    )
    atomic_write_json(data_dir / "profile.json", profile)
    events = [
        {
            "schema_version": 1,
            "event_id": "seed-profile-created",
            "type": "profile_created",
            "at": isoformat(created_at),
            "session_id": None,
            "policy_version": POLICY_VERSION,
            "data": {},
        },
        {
            "schema_version": 1,
            "event_id": "seed-refine-taught",
            "type": "interaction_completed",
            "at": isoformat(taught_at),
            "session_id": "f" * 32,
            "policy_version": POLICY_VERSION,
            "data": {
                "item_id": "refine",
                "outcome": "completed",
                "dwell_ms": 6400,
                "phrase_confirmed": True,
                "replays": 0,
                "preference_epoch": 1,
            },
        },
    ]
    (data_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


@contextmanager
def seeded_server():
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="inflow-probe-browser-") as raw_dir:
        data_dir = Path(raw_dir)
        seed_due_profile(data_dir)
        env = os.environ.copy()
        env.update(
            {
                "INFLOW_ADAPTIVE_DATA_DIR": str(data_dir),
                "INFLOW_ADAPTIVE_QA": "1",
                "INFLOW_ADAPTIVE_PORT": str(PORT),
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
            for _ in range(80):
                try:
                    if request_json("/api/health").get("ok"):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError("probe QA server did not become ready")
            yield data_dir
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            time.sleep(0.4)


def run_success_and_reload(browser) -> dict:
    with seeded_server() as data_dir:
        page_errors: list[str] = []
        console_errors: list[str] = []
        context = browser.new_context(viewport={"width": 1280, "height": 820})
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.goto(f"{BASE}/?qa=1&case=probe-success&ts={time.time_ns()}", wait_until="networkidle")
        if page.locator("#probePanel").is_visible():
            raise AssertionError("probe appeared before the user started")
        page.locator("#startButton").click()
        page.get_by_role("heading", name="先听一句").wait_for(timeout=10_000)
        initial_text = page.locator("#probePanel").inner_text().lower()
        if "refine" in initial_text or "细化" in initial_text or page.locator(".probe-choice").count():
            raise AssertionError(f"answer leaked before audio completed: {initial_text}")
        page.locator(".probe-choice").first.wait_for(state="visible", timeout=10_000)
        if page.locator(".probe-choice").count() != 3:
            raise AssertionError("probe did not expose exactly three choices")
        if page.evaluate("document.activeElement?.id") != "probeTitle":
            raise AssertionError("a choice received default focus and could bias the answer")
        question_text = page.locator("#probePanel").inner_text().lower()
        if "refine" in question_text:
            raise AssertionError("English surface leaked before response")
        question_screenshot = ROOT / "data" / "probe-question.png"
        page.screenshot(path=str(question_screenshot), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
            raise AssertionError("probe question overflows on a narrow screen")
        mobile_boxes = page.locator(".probe-choice").evaluate_all(
            "buttons => buttons.map(button => { const r = button.getBoundingClientRect(); return [r.top, r.bottom, r.width]; })"
        )
        if len(mobile_boxes) != 3 or not all(mobile_boxes[index][1] <= mobile_boxes[index + 1][0] for index in range(2)):
            raise AssertionError(mobile_boxes)
        page.screenshot(path=str(ROOT / "data" / "probe-question-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1280, "height": 820})
        session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
        stored_path = data_dir / "sessions" / f"{session_id}.json"
        stored = json.loads(stored_path.read_text(encoding="utf-8"))
        probe_id = stored["probe"]["probe_id"]
        correct_choice_id = stored["probe"]["correct_choice_id"]
        if "refine" in request_json(f"/api/sessions/{session_id}")["probe"]["audio_url"].lower():
            raise AssertionError("opaque audio URL leaked the answer")
        second_page = context.new_page()
        second_page.goto(f"{BASE}/?qa=1&case=probe-second-tab&ts={time.time_ns()}", wait_until="networkidle")
        second_page.get_by_role("heading", name="这次观看已在另一个页面打开").wait_for(timeout=10_000)
        if not page.locator(".probe-choice").first.is_visible():
            raise AssertionError("second tab disturbed the original probe")
        if request_json(f"/api/sessions/{session_id}")["probe"]["result"] is not None:
            raise AssertionError("second tab scored or cancelled the probe")
        second_page.close()

        before_answer = request_json(f"/api/sessions/{session_id}")
        if before_answer["probe"]["result"] is not None or before_answer["probe"]["presentation_count"] != 1:
            raise AssertionError("first presentation state was not preserved")
        page.locator(f'.probe-choice[data-choice-id="{correct_choice_id}"]').click()
        page.get_by_role("heading", name="这次声音和意思对上了").wait_for(timeout=10_000)
        if page.locator(".mapping-word").inner_text() != "refine":
            raise AssertionError("feedback did not reveal the English surface")
        if page.locator(".mapping-gloss").inner_text() != "细化；改进":
            raise AssertionError("feedback did not reveal the correct meaning")
        feedback_screenshot = ROOT / "data" / "probe-feedback.png"
        page.screenshot(path=str(feedback_screenshot), full_page=True)
        page.wait_for_timeout(1800)
        if not page.locator("#probeContinueToVideo").is_visible():
            raise AssertionError("probe feedback auto-dismissed")

        page.reload(wait_until="networkidle")
        page.get_by_role("heading", name="这次声音和意思对上了").wait_for(timeout=10_000)
        persisted = request_json(f"/api/sessions/{session_id}")
        if persisted["stage"] != "probe_feedback" or persisted["probe"]["result"]["evidence_quality"] != "WEAK_SUCCESS":
            raise AssertionError(persisted)
        page.locator("#probeContinueToVideo").click()
        page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
        page.wait_for_function("() => !document.querySelector('#sourceVideo').paused")
        watching = request_json(f"/api/sessions/{session_id}")
        if watching["stage"] != "watch":
            raise AssertionError(watching)
        if "refine" in {item["id"] for item in watching["items"]}:
            raise AssertionError("probed item was immediately scheduled for teaching")
        allowed = sorted(
            [item for item in watching["items"] if item["min_intensity"] in {"low", "medium"}],
            key=lambda item: item["anchor_sec"],
        )
        if not allowed:
            raise AssertionError("no teaching candidate remained after the probe")
        taught_item = allowed[0]
        page.evaluate(
            """item => {
              const video = document.querySelector('#sourceVideo');
              video.currentTime = Math.max(0, item.anchor_sec - 0.35);
              return video.play();
            }""",
            taught_item,
        )
        page.locator("#mappingContinue").wait_for(state="visible", timeout=10_000)
        page.locator("#mappingContinue").click()
        page.locator("#learningLayer").wait_for(state="hidden", timeout=10_000)
        page.evaluate(
            """() => {
              const video = document.querySelector('#sourceVideo');
              video.currentTime = Math.max(0, video.duration - 0.12);
              return video.play();
            }"""
        )
        page.locator("#completePanel").wait_for(state="visible", timeout=15_000)
        completed_session = request_json(f"/api/sessions/{session_id}")
        if completed_session["stage"] != "complete" or completed_session["interaction_summary"]["completed"] < 1:
            raise AssertionError(completed_session)
        profile = request_json("/api/profile")
        if profile["recognition_successes"] != 1 or profile["recognition_failures"] != 0:
            raise AssertionError(profile)
        events = [
            json.loads(line)
            for line in (data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if sum(event["type"] == "probe_completed" and event["data"]["probe_id"] == probe_id for event in events) != 1:
            raise AssertionError("probe event was not exactly-once")
        rebuild = subprocess.run(
            [sys.executable, str(ROOT / "rebuild_profile.py"), "--data-dir", str(data_dir)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if rebuild.returncode != 0:
            raise AssertionError({"stdout": rebuild.stdout, "stderr": rebuild.stderr})
        rebuilt = json.loads(rebuild.stdout)
        if not rebuilt.get("matches_existing"):
            raise AssertionError(rebuilt)
        if page_errors or console_errors:
            raise AssertionError({"page_errors": page_errors, "console_errors": console_errors})
        context.close()
        return {
            "answer_hidden_until_audio": True,
            "choices": 3,
            "first_presentation_count": 1,
            "second_tab_disturbed_probe": False,
            "evidence_quality": "WEAK_SUCCESS",
            "feedback_held_ms": 1800,
            "reload_feedback_persisted": True,
            "probe_events": 1,
            "teaching_after_probe": taught_item["id"],
            "session_completed": True,
            "rebuild_matches": True,
        }


def run_reload_before_answer(browser) -> dict:
    with seeded_server() as data_dir:
        context = browser.new_context(viewport={"width": 1280, "height": 820})
        page = context.new_page()
        page.goto(f"{BASE}/?qa=1&case=probe-reload&ts={time.time_ns()}", wait_until="networkidle")
        page.locator("#startButton").click()
        page.locator(".probe-choice").first.wait_for(state="visible", timeout=10_000)
        session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
        stored_path = data_dir / "sessions" / f"{session_id}.json"
        stored = json.loads(stored_path.read_text(encoding="utf-8"))
        correct_choice_id = stored["probe"]["correct_choice_id"]
        page.reload(wait_until="networkidle")
        page.locator(".probe-choice").first.wait_for(state="visible", timeout=10_000)
        before_answer = request_json(f"/api/sessions/{session_id}")
        if before_answer["probe"]["result"] is not None or before_answer["probe"]["presentation_count"] != 2:
            raise AssertionError(before_answer)
        page.locator(f'.probe-choice[data-choice-id="{correct_choice_id}"]').click()
        page.get_by_role("heading", name="这次声音和意思对上了").wait_for(timeout=10_000)
        if "辅助练习" not in page.locator("#probePanel").inner_text():
            raise AssertionError("replayed presentation was not explained as assisted practice")
        result = request_json(f"/api/sessions/{session_id}")["probe"]["result"]
        if result["evidence_quality"] != "ASSISTED_PRACTICE" or result["first_presentation"]:
            raise AssertionError(result)
        profile = request_json("/api/profile")
        if profile["recognition_successes"] != 0 or profile["recognition_failures"] != 0 or profile["assisted_probes"] != 1:
            raise AssertionError(profile)
        rebuilt = subprocess.run(
            [sys.executable, str(ROOT / "rebuild_profile.py"), "--data-dir", str(data_dir)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if rebuilt.returncode != 0 or not json.loads(rebuilt.stdout).get("matches_existing"):
            raise AssertionError({"stdout": rebuilt.stdout, "stderr": rebuilt.stderr})
        context.close()
        return {
            "reload_scored_before_answer": False,
            "presentation_count": 2,
            "evidence_quality": "ASSISTED_PRACTICE",
            "recognition_successes": 0,
            "assisted_probes": 1,
            "rebuild_matches": True,
        }


def run_claim_during_probe(browser) -> dict:
    with seeded_server() as data_dir:
        context = browser.new_context(viewport={"width": 1280, "height": 820})
        first_page = context.new_page()
        first_page.goto(f"{BASE}/?qa=1&case=probe-owner-one&ts={time.time_ns()}", wait_until="networkidle")
        first_page.locator("#startButton").click()
        first_page.locator(".probe-choice").first.wait_for(state="visible", timeout=10_000)
        session_id = first_page.evaluate("localStorage.getItem('inflow-adaptive-session')")
        stored = json.loads((data_dir / "sessions" / f"{session_id}.json").read_text(encoding="utf-8"))
        correct_choice_id = stored["probe"]["correct_choice_id"]
        second_page = context.new_page()
        second_page.goto(f"{BASE}/?qa=1&case=probe-owner-two&ts={time.time_ns()}", wait_until="networkidle")
        second_page.get_by_role("heading", name="这次观看已在另一个页面打开").wait_for(timeout=10_000)
        second_page.locator("#claimSession").click()
        second_page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
        second_page.locator("#resumeButton").click()
        second_page.wait_for_function("() => !document.querySelector('#sourceVideo').paused")
        second_page.wait_for_timeout(1500)
        second_page.evaluate(
            """() => {
              const video = document.querySelector('#sourceVideo');
              video.currentTime = Math.max(0, video.duration - 0.12);
              return video.play();
            }"""
        )
        second_page.locator("#completePanel").wait_for(state="visible", timeout=15_000)
        completed = request_json(f"/api/sessions/{session_id}")
        if completed["stage"] != "complete" or completed["probe"]["result"]["outcome"] != "technical_failure":
            raise AssertionError(completed)
        first_page.locator(f'.probe-choice[data-choice-id="{correct_choice_id}"]').click()
        first_page.get_by_role("heading", name="观看已由另一个页面完成").wait_for(timeout=10_000)
        if first_page.locator("#claimSession").count():
            raise AssertionError("claim button still shown after the viewing finished")
        after_stale = request_json(f"/api/sessions/{session_id}")
        if after_stale["probe"]["result"]["outcome"] != "technical_failure" or after_stale["stage"] != "complete":
            raise AssertionError(after_stale)
        profile = request_json("/api/profile")
        if any(profile[key] for key in ("recognition_successes", "recognition_failures", "recognition_abstentions", "assisted_probes")):
            raise AssertionError(profile)
        events = [
            json.loads(line)
            for line in (data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if sum(event["type"] == "probe_completed" for event in events) != 1:
            raise AssertionError(events)
        context.close()
        return {
            "explicit_claim_required": True,
            "claim_resolution": "technical_failure",
            "old_owner_answer_accepted": False,
            "memory_evidence": False,
            "probe_events": 1,
        }


def run_audio_failure(browser) -> dict:
    with seeded_server() as data_dir:
        context = browser.new_context(viewport={"width": 1280, "height": 820})
        page = context.new_page()
        page.route("**/probe/audio/**", lambda route: route.abort("failed"))
        page.goto(f"{BASE}/?qa=1&case=probe-failure&ts={time.time_ns()}", wait_until="networkidle")
        page.locator("#startButton").click()
        page.get_by_role("heading", name="这次不会记成对或错").wait_for(timeout=10_000)
        if page.locator(".probe-choice").count():
            raise AssertionError("choices appeared after failed audio")
        session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
        page.locator("#probeSkip").click()
        page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
        session = request_json(f"/api/sessions/{session_id}")
        if session["probe"]["result"]["outcome"] != "technical_failure":
            raise AssertionError(session)
        profile = json.loads((data_dir / "profile.json").read_text(encoding="utf-8"))
        state = profile["items"]["refine"]
        if state["recognition_success_count"] or state["recognition_failure_count"] or state["used_probe_variants"]:
            raise AssertionError(state)
        if any(row.get("kind") == "VERIFY" for row in state["evidence"]):
            raise AssertionError("technical failure entered the memory evidence list")
        context.close()
        return {
            "choices_after_failure": 0,
            "outcome": "technical_failure",
            "memory_evidence": False,
            "video_continued": True,
        }


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(CHROME),
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        try:
            result = {
                "ok": True,
                "success_first_presentation": run_success_and_reload(browser),
                "reload_before_answer": run_reload_before_answer(browser),
                "claim_during_probe": run_claim_during_probe(browser),
                "audio_failure": run_audio_failure(browser),
            }
        finally:
            browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
