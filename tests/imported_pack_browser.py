from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import psutil
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
REAL_PACKS = Path(os.environ.get("INFLOW_REAL_PACK_QA_ROOT", Path(os.environ["LOCALAPPDATA"]) / "Temp" / "inflow-pack-real-qa"))
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RANK = {"low": 0, "medium": 1, "high": 2}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=8) as response:
        return json.load(response)


def wait_health(base: str, process: subprocess.Popen, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited: {process.returncode}")
        try:
            return fetch(base, "/api/health")
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not become ready")


def main() -> None:
    manifests = sorted(REAL_PACKS.glob("*/manifest.json"))
    if len(manifests) != 1:
        raise RuntimeError(f"expected one real QA pack under {REAL_PACKS}, found {len(manifests)}")
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    pack_id = manifest["pack_id"]
    pack_root = manifests[0].parent
    captions_doc = json.loads((pack_root / "captions-zh.json").read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="inflow-imported-browser-") as temporary:
        data_dir = Path(temporary)
        port = free_port()
        env = os.environ.copy()
        env.update(
            {
                "INFLOW_ADAPTIVE_DATA_DIR": str(data_dir),
                "INFLOW_PACKS_DIR": str(REAL_PACKS),
                "INFLOW_ADAPTIVE_QA": "1",
                "INFLOW_ADAPTIVE_PORT": str(port),
            }
        )
        process = subprocess.Popen(
            [os.sys.executable, "server.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            wait_health(base, process)
            page_errors: list[str] = []
            console_errors: list[str] = []
            media_statuses: list[int] = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    executable_path=str(CHROME),
                    args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
                )
                page = browser.new_page(viewport={"width": 1360, "height": 900})
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.on(
                    "response",
                    lambda response: media_statuses.append(response.status)
                    if f"/api/packs/{pack_id}/files/video.mp4" in response.url
                    else None,
                )
                page.goto(base + "/?qa=1", wait_until="networkidle")
                page.get_by_role("heading", name="粘贴链接，准备好就看").wait_for(timeout=10_000)
                page.get_by_text(manifest["title"], exact=True).wait_for(timeout=5_000)
                page.locator("#videoUrl").fill(manifest["source_url"])
                page.locator("#prepareButton").click()
                page.get_by_text(f"《{manifest['title']}》准备好了。").wait_for(timeout=5_000)
                if page.locator(".pack-option[aria-selected='true']").get_attribute("data-pack-id") != pack_id:
                    raise AssertionError("real pack was not selected by default")
                page.screenshot(path=str(ROOT / "data" / "universal-player-home.png"), full_page=True)

                page.locator("#startButton").click()
                page.locator("#watchPanel").wait_for(state="visible", timeout=15_000)
                page.wait_for_function("() => document.querySelector('#sourceVideo').duration > 700")
                session_id = page.evaluate("localStorage.getItem('inflow-adaptive-session')")
                session = fetch(base, f"/api/sessions/{session_id}")
                if session["pack_id"] != pack_id:
                    raise AssertionError(session)
                if session["video"]["title"] != manifest["title"]:
                    raise AssertionError(session["video"])

                # Rolling YouTube captions overlap; the newest active line must win.
                rows = captions_doc["segments"]
                overlap = next(
                    (rows[index - 1], rows[index])
                    for index in range(1, len(rows))
                    if rows[index]["start"] + 0.1 < rows[index - 1]["end"]
                )
                caption_time = overlap[1]["start"] + 0.1
                page.locator("#sourceVideo").evaluate("(video, time) => { video.currentTime = time; video.pause(); }", caption_time)
                page.wait_for_timeout(250)
                expected_caption = max(
                    (row for row in rows if row["start"] <= caption_time <= row["end"]),
                    key=lambda row: row["start"],
                )["text"]
                if page.locator("#captionLine").inner_text() != expected_caption:
                    raise AssertionError({"expected_caption": expected_caption, "actual": page.locator("#captionLine").inner_text()})

                expected_medium = int(session["teaching_budgets"]["medium"])
                allowed = [item for item in session["items"] if RANK[item["min_intensity"]] <= RANK["medium"]]
                if len(allowed) != expected_medium:
                    raise AssertionError({"medium_items": allowed, "budget": session["teaching_budgets"]})
                learned = []
                restored_delta_ms = None
                for index, item in enumerate(allowed):
                    page.locator("#sourceVideo").evaluate(
                        """(video, anchor) => {
                          video.currentTime = Math.max(0, anchor - 0.35);
                          return video.play();
                        }""",
                        float(item["anchor_sec"]),
                    )
                    page.locator("#mappingContinue:not([disabled])").wait_for(state="visible", timeout=20_000)
                    if page.locator(".mapping-word").inner_text() != item["surface"]:
                        raise AssertionError("surface mismatch")
                    if page.locator(".mapping-gloss").inner_text() != item["gloss_zh"]:
                        raise AssertionError("gloss mismatch")
                    if page.locator(".mapping-context").inner_text() != item["phrase_text"]:
                        raise AssertionError("phrase text mismatch")
                    if page.locator(".mapping-translation").inner_text() != item["phrase_zh"]:
                        raise AssertionError("phrase translation mismatch")
                    page.wait_for_timeout(1500)
                    if not page.locator("#mappingContinue").is_visible():
                        raise AssertionError("mapping auto-dismissed")
                    if index == 0:
                        page.screenshot(path=str(ROOT / "data" / "universal-player-mapping.png"), full_page=True)
                    page.locator("#mappingContinue").click()
                    page.wait_for_function("() => !document.querySelector('#sourceVideo').paused")
                    page.locator("#learningLayer").wait_for(state="hidden", timeout=5_000)
                    learned.append(item["id"])

                    if index == 0:
                        before_switch = page.locator("#sourceVideo").evaluate("video => video.currentTime")
                        page.locator("#libraryButton").click()
                        page.locator("#home").wait_for(state="visible", timeout=10_000)
                        if "继续" not in page.locator("#startButton").inner_text():
                            raise AssertionError("active imported session was not shown as resumable")
                        page.locator("#startButton").click()
                        page.locator("#watchPanel").wait_for(state="visible", timeout=10_000)
                        page.wait_for_function("() => !document.querySelector('#sourceVideo').paused")
                        after_switch = page.locator("#sourceVideo").evaluate("video => video.currentTime")
                        restored_delta_ms = round((after_switch - before_switch) * 1000)
                        if abs(restored_delta_ms) > 500:
                            raise AssertionError({"before": before_switch, "after": after_switch, "delta_ms": restored_delta_ms})

                page.locator("#sourceVideo").evaluate(
                    """video => {
                      video.currentTime = Math.max(0, video.duration - 0.1);
                      return video.play();
                    }"""
                )
                page.locator("#completePanel").wait_for(state="visible", timeout=15_000)
                page.screenshot(path=str(ROOT / "data" / "universal-player-complete.png"), full_page=True)
                browser.close()

            profile = fetch(base, "/api/profile")
            final_session = fetch(base, f"/api/sessions/{session_id}")
            event_rows = [json.loads(line) for line in (data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            if final_session["interaction_summary"]["completed"] != expected_medium:
                raise AssertionError(final_session["interaction_summary"])
            if pack_id in profile.get("active_sessions", {}):
                raise AssertionError(profile["active_sessions"])
            if not any(row.get("type") == "session_created" and row.get("data", {}).get("pack_id") == pack_id for row in event_rows):
                raise AssertionError("pack_id missing from event ledger")
            if not media_statuses or not any(status in {200, 206} for status in media_statuses):
                raise AssertionError({"media_statuses": media_statuses})
            if page_errors or console_errors:
                raise AssertionError({"page_errors": page_errors, "console_errors": console_errors})
            rebuild = subprocess.run(
                [os.sys.executable, "rebuild_profile.py", "--data-dir", str(data_dir)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
            if rebuild.returncode != 0:
                raise AssertionError({"rebuild_stdout": rebuild.stdout, "rebuild_stderr": rebuild.stderr})
            rebuild_result = json.loads(rebuild.stdout.strip().splitlines()[-1])
            if not rebuild_result.get("matches_existing"):
                raise AssertionError(rebuild_result)

            print(
                json.dumps(
                    {
                        "ok": True,
                        "pack_id": pack_id,
                        "title": manifest["title"],
                        "duration_sec": manifest["duration_sec"],
                        "caption_segments": len(captions_doc["segments"]),
                        "effective_speech_sec": session["effective_speech_sec"],
                        "intervention_budgets": session["intervention_budgets"],
                        "url_entry_cache_ready": True,
                        "learned": learned,
                        "restored_delta_ms": restored_delta_ms,
                        "media_statuses": media_statuses,
                        "interaction_summary": final_session["interaction_summary"],
                        "rebuild_matches": True,
                        "page_errors": page_errors,
                        "console_errors": console_errors,
                        "screenshots": [
                            str(ROOT / "data" / "universal-player-home.png"),
                            str(ROOT / "data" / "universal-player-mapping.png"),
                            str(ROOT / "data" / "universal-player-complete.png"),
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            try:
                root_process = psutil.Process(process.pid)
                targets = root_process.children(recursive=True) + [root_process]
            except psutil.Error:
                targets = []
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
            psutil.wait_procs(alive, timeout=5)


if __name__ == "__main__":
    main()
