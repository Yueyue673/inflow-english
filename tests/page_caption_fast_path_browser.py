from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BROWSERS = os.environ.get("INFLOW_PLAYWRIGHT_BROWSERS")
if BROWSERS:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS
from playwright.sync_api import sync_playwright

from tests.extension_test_support import copy_extension_for_browser
from tests.live_data_guard import snapshot_live_tree

SOURCE_EXTENSION = ROOT / "extension"
VIDEO_ID = "arj7oStGLkU"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def milestone(name: str, started: float, **details) -> None:
    print(json.dumps({"milestone": name, "elapsed_sec": round(time.perf_counter() - started, 2), **details}, ensure_ascii=False), flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(base: str, path: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=8) as response:
        return json.load(response)


def stop_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
        targets = root.children(recursive=True) + [root]
    except psutil.Error:
        return
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


def main() -> None:
    live_root = ROOT / "data" / "live"
    live_before = snapshot_live_tree(live_root)
    with tempfile.TemporaryDirectory(prefix="inflow-page-caption-") as temporary:
        temp_root = Path(temporary)
        extension = temp_root / "extension"
        profile_dir = temp_root / "chrome-profile"
        data_dir = temp_root / "data"
        packs_dir = temp_root / "packs"
        packs_dir.mkdir(parents=True)
        copy_extension_for_browser(SOURCE_EXTENSION, extension, open_shadow_for_test=True)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        worker_path = extension / "service-worker.js"
        worker_path.write_text(worker_path.read_text(encoding="utf-8").replace("http://127.0.0.1:8767", base), encoding="utf-8")
        manifest_path = extension / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["host_permissions"] = ["https://www.youtube.com/*", f"{base}/*"]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        env = os.environ.copy()
        env.update({
            "INFLOW_ADAPTIVE_DATA_DIR": str(data_dir),
            "INFLOW_PACKS_DIR": str(packs_dir),
            "INFLOW_ADAPTIVE_QA": "1",
            "INFLOW_ADAPTIVE_PORT": str(port),
        })
        server = subprocess.Popen(
            [sys.executable, "server.py"], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        page_errors: list[str] = []
        worker_errors: list[str] = []
        started = time.perf_counter()
        try:
            for _ in range(120):
                try:
                    if fetch(base, "/api/health").get("ok"):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError("isolated_server_not_ready")
            milestone("server_ready", started, port=port)

            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir), headless=False,
                    args=[
                        "--window-position=-32000,-32000",
                        "--window-size=1280,900",
                        f"--disable-extensions-except={extension}",
                        f"--load-extension={extension}",
                        "--no-first-run",
                        "--autoplay-policy=no-user-gesture-required",
                        "--mute-audio",
                        "--proxy-server=http://127.0.0.1:7890",
                    ],
                )
                milestone("browser_launched", started)
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15_000)
                worker.on("console", lambda message: worker_errors.append(message.text) if message.type == "error" else None)
                worker.evaluate("chrome.storage.local.set({autoMode:true,displaySize:'medium'})")
                page = context.pages[0] if context.pages else context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                navigation_started = time.perf_counter()
                page.goto(URL, wait_until="commit", timeout=90_000)
                milestone("navigation_committed", started)
                page.locator("video").wait_for(state="attached", timeout=60_000)
                host = page.locator("#inflow-extension-root")
                host.wait_for(state="attached", timeout=15_000)
                host_ms = round((time.perf_counter() - navigation_started) * 1000)
                milestone("host_visible", started, host_ms=host_ms)
                pill = host.locator("#pill")
                box = pill.bounding_box()
                if not box or box["width"] < 110 or box["height"] < 30:
                    raise AssertionError({"status_not_visibly_sized": box})
                content_deadline = time.perf_counter() + 75
                while time.perf_counter() < content_deadline:
                    playback = page.evaluate("""() => ({
                      ad: document.querySelector('#movie_player')?.classList.contains('ad-showing') || false,
                      duration: Number(document.querySelector('video')?.duration || 0),
                      paused: document.querySelector('video')?.paused ?? true,
                    })""")
                    if playback["paused"]:
                        page.locator("video").evaluate("video => video.play().catch(() => {})")
                    skip = page.locator(".ytp-skip-ad-button, .ytp-ad-skip-button, button[id^='skip-button']").first
                    if playback["ad"] and skip.count() and skip.is_visible():
                        skip.click()
                    if not playback["ad"] and playback["duration"] > 100:
                        break
                    page.wait_for_timeout(250)
                else:
                    raise AssertionError({"content_player_not_ready": playback})
                content_ready_at = time.perf_counter()
                milestone("content_ready", started, playback=playback)
                try:
                    page.wait_for_function(
                        "() => ['InFlow 字幕已就绪','InFlow 正在工作'].includes(document.querySelector('#inflow-extension-root')?.shadowRoot?.querySelector('#pill')?.textContent)",
                        timeout=12_000,
                    )
                except Exception as exc:
                    diagnostic = page.evaluate("""() => {
                      const root = document.querySelector('#inflow-extension-root')?.shadowRoot;
                      return root ? {
                        pill: root.querySelector('#pill')?.textContent,
                        panel: root.querySelector('#panelMessage')?.textContent,
                        caption_hidden: root.querySelector('#caption')?.hidden,
                        native_cc: document.querySelector('button.ytp-subtitles-button')?.getAttribute('aria-pressed'),
                        ad: document.querySelector('#movie_player')?.classList.contains('ad-showing'),
                        duration: document.querySelector('video')?.duration,
                      } : null;
                    }""")
                    jobs = [json.loads(path.read_text(encoding="utf-8")) for path in (data_dir / "caption-jobs").glob("*.json")] if (data_dir / "caption-jobs").exists() else []
                    raise AssertionError({"fast_path_state": diagnostic, "caption_jobs": jobs, "page_errors": page_errors, "worker_errors": worker_errors}) from exc
                page.locator("video").evaluate("video => video.pause()")
                ready_ms = round((time.perf_counter() - navigation_started) * 1000)
                ready_after_content_ms = round((time.perf_counter() - content_ready_at) * 1000)
                milestone("captions_ready", started, ready_after_content_ms=ready_after_content_ms)
                state = page.evaluate("""() => {
                  const root = document.querySelector('#inflow-extension-root')?.shadowRoot;
                  const pill = root?.querySelector('#pill');
                  return {pill: pill?.textContent, width: pill?.getBoundingClientRect().width, height: pill?.getBoundingClientRect().height};
                }""")
                screenshot = ROOT / "data" / "page-caption-fast-path.png"
                page.screenshot(path=str(screenshot), full_page=False)
                milestone("closing_browser", started)
                context.close()
                milestone("browser_closed", started)

            caption_jobs = list((data_dir / "caption-jobs").glob("*.json")) if (data_dir / "caption-jobs").exists() else []
            if caption_jobs:
                raise AssertionError({"yt_dlp_fallback_started_despite_page_caption_success": [path.name for path in caption_jobs]})
            if page_errors or worker_errors:
                raise AssertionError({"page_errors": page_errors, "worker_errors": worker_errors})
            if snapshot_live_tree(live_root) != live_before:
                raise AssertionError("formal_live_tree_changed")
            print(json.dumps({
                "ok": True,
                "video_id": VIDEO_ID,
                "host_visible_ms": host_ms,
                "page_caption_ready_ms": ready_ms,
                "page_caption_ready_after_content_ms": ready_after_content_ms,
                "status": state,
                "yt_dlp_fallback_jobs": 0,
                "formal_live_tree_unchanged": True,
                "screenshot": str(screenshot),
                "elapsed_sec": round(time.perf_counter() - started, 2),
            }, ensure_ascii=False, indent=2))
        finally:
            stop_tree(server.pid)


if __name__ == "__main__":
    main()
