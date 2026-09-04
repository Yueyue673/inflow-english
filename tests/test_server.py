from __future__ import annotations

import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

TEST_CLIENT_ID = "1" * 32


class AdaptiveServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ["INFLOW_ADAPTIVE_DATA_DIR"] = self.temp.name
        os.environ["INFLOW_ADAPTIVE_QA"] = "1"
        os.environ["INFLOW_ADAPTIVE_PORT"] = "8771"
        import server

        self.server = importlib.reload(server)
        self.fixture_media = Path(self.temp.name) / "fixture-media"
        for relative in self.server.FIXTURE_MEDIA_ALLOWLIST:
            path = self.fixture_media / relative.removeprefix("media/")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".json":
                path.write_text("{}", encoding="utf-8")
            else:
                path.write_bytes(b"RIFF" + b"\0" * 4096)
        self.server.SOURCE_MEDIA_DIR = self.fixture_media
        self.client = TestClient(self.server.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def seed_due_taught_item(self, item_id: str = "refine"):
        now = self.server.utc_now()
        taught_at = now - timedelta(hours=24)
        profile = self.server.new_profile(self.server.CONTENT, now=taught_at - timedelta(minutes=1))
        profile = self.server.apply_interaction(
            profile,
            item_id,
            outcome="completed",
            dwell_ms=6000,
            phrase_confirmed=True,
            replays=0,
            now=taught_at,
        )
        self.server.commit_state(
            profile=profile,
            events=[
                self.server.build_event("profile_created", {}, at=taught_at - timedelta(minutes=1)),
                self.server.build_event(
                    "interaction_completed",
                    {
                        "item_id": item_id,
                        "outcome": "completed",
                        "dwell_ms": 6000,
                        "phrase_confirmed": True,
                        "replays": 0,
                        "preference_epoch": 1,
                    },
                    session_id="f" * 32,
                    at=taught_at,
                ),
            ],
        )
        return profile

    def confirm_probe_presentation(self, session_id: str, session: dict, owner: dict, presentation_id: str = "3" * 32):
        confirmed = self.client.post(
            f"/api/sessions/{session_id}/probe/presentation",
            json={
                **owner,
                "probe_id": session["probe"]["probe_id"],
                "presentation_id": presentation_id,
            },
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["probe"]["presentation_count"], 1)
        return confirmed.json()

    def build_fake_pack(self):
        from tests.test_video_pack import FIXED_NOW, FakeMediaTools, FakeTranscriber, FakeTranslator, VIDEO_URL
        from video_pack import VideoPackBuilder

        builder = VideoPackBuilder(
            self.server.PACKS_DIR,
            translator=FakeTranslator(),
            transcriber=FakeTranscriber(),
            media_tools=FakeMediaTools(),
            clock=lambda: FIXED_NOW,
        )
        return builder.build(VIDEO_URL)

    def test_caption_preview_queues_without_creating_learning_state(self):
        from tests.test_video_pack import VIDEO_URL

        with mock.patch.object(self.server, "start_caption_thread") as starter:
            response = self.client.post("/api/caption-previews", json={"url": VIDEO_URL})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["status"], "queued")
        self.assertFalse(preview["usable"])
        self.assertIsNone(preview["content_path"])
        starter.assert_called_once_with(preview["preview_id"])
        self.assertFalse(self.server.PROFILE_PATH.exists())
        self.assertFalse(self.server.EVENTS_PATH.exists())
        self.assertEqual(list(self.server.IMPORT_JOBS_DIR.glob("*.json")), [])

    def test_caption_preview_keeps_english_usable_when_chinese_translation_fails(self):
        from tests.test_video_pack import VIDEO_URL

        with mock.patch.object(self.server, "start_caption_thread"):
            preview = self.client.post("/api/caption-previews", json={"url": VIDEO_URL}).json()
        preview_id = preview["preview_id"]
        document = {
            "schema_version": "inflow.caption-preview/1",
            "preview_id": preview_id,
            "video_id": preview["video_id"],
            "source_url": preview["source_url"],
            "title": "Subtitle first",
            "status": "english_ready",
            "revision": 1,
            "translated_segments": 0,
            "total_segments": 1,
            "coverage_fraction": 0.0,
            "segments": [{"id": "caption-00001", "start": 0, "end": 2, "text_en": "Hello there.", "text_zh": None}],
        }

        def fail_after_english(_url, output_path, *, on_publish, **_kwargs):
            self.server.atomic_write_json(output_path, document)
            on_publish(document)
            raise self.server.VideoPackError("argos_translation_failed")

        with mock.patch.object(self.server, "build_caption_preview", side_effect=fail_after_english):
            self.server.run_caption_job(preview_id)
        status = self.client.get(f"/api/caption-previews/{preview_id}")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "degraded")
        self.assertTrue(status.json()["usable"])
        self.assertEqual(status.json()["language"], "en")
        content = self.client.get(status.json()["content_path"])
        self.assertEqual(content.status_code, 200, content.text)
        self.assertEqual(content.json()["segments"][0]["text_en"], "Hello there.")
        self.assertEqual(list(self.server.IMPORT_JOBS_DIR.glob("*.json")), [])
        self.assertFalse(self.server.EVENTS_PATH.exists())

    def test_caption_preview_rejects_unknown_ids_and_path_traversal(self):
        self.assertEqual(self.client.get("/api/caption-previews/not-an-id").status_code, 404)
        self.assertEqual(self.client.get("/api/caption-previews/yt-abcdefghijk-0000000000000000/content/../../profile").status_code, 404)

    def test_verified_pack_manifest_cache_invalidates_on_file_change(self):
        pack = self.build_fake_pack()
        self.server.PACK_MANIFEST_CACHE.clear()
        with mock.patch.object(self.server, "verify_pack_directory", wraps=self.server.verify_pack_directory) as verifier:
            first = self.server.load_pack_manifest(pack.name)
            second = self.server.load_pack_manifest(pack.name)
            self.assertEqual(first["pack_id"], pack.name)
            self.assertEqual(second["pack_id"], pack.name)
            self.assertEqual(verifier.call_count, 1)
            manifest_path = pack / "manifest.json"
            stat = manifest_path.stat()
            os.utime(manifest_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            third = self.server.load_pack_manifest(pack.name)
            self.assertEqual(third["pack_id"], pack.name)
            self.assertEqual(verifier.call_count, 2)

    def test_import_reuses_verified_older_builder_pack_for_same_url(self):
        from tests.test_video_pack import VIDEO_URL

        pack = self.build_fake_pack()
        with mock.patch.object(self.server, "start_import_thread") as starter:
            response = self.client.post("/api/imports", json={"url": VIDEO_URL})
            repeated = self.client.post("/api/imports", json={"url": VIDEO_URL})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        job = response.json()
        self.assertEqual(repeated.json()["job_id"], job["job_id"])
        self.assertEqual(len(list(self.server.IMPORT_JOBS_DIR.glob("*.json"))), 1)
        self.assertEqual(job["status"], "ready")
        self.assertTrue(job["cached"])
        self.assertEqual(job["pack_id"], pack.name)
        starter.assert_not_called()

    def test_import_job_can_queue_cancel_and_never_touch_learning_ledger(self):
        from tests.test_video_pack import VIDEO_URL

        invalid = self.client.post("/api/imports", json={"url": "https://example.com/video"})
        self.assertEqual(invalid.status_code, 422)
        with mock.patch.object(self.server, "start_import_thread") as starter:
            queued = self.client.post("/api/imports", json={"url": VIDEO_URL})
        self.assertEqual(queued.status_code, 200, queued.text)
        job = queued.json()
        self.assertEqual(job["status"], "queued")
        starter.assert_called_once_with(job["job_id"])
        cancelled = self.client.post(f"/api/imports/{job['job_id']}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["cancel_requested"])
        self.assertFalse(self.server.PROFILE_PATH.exists())
        self.assertFalse(self.server.EVENTS_PATH.exists())
        self.assertEqual(list(self.server.SESSIONS_DIR.glob("*.json")), [])

    def test_import_worker_promotes_only_verified_pack_to_ready(self):
        from tests.test_video_pack import VIDEO_URL, source_metadata

        pack = self.build_fake_pack()
        now = self.server.isoformat(self.server.utc_now())
        job = {
            "job_id": "a" * 32,
            "source_url": VIDEO_URL,
            "pack_id": pack.name,
            "title": None,
            "status": "queued",
            "stage": "queued",
            "fraction": 0.0,
            "detail": None,
            "cancel_requested": False,
            "error_code": None,
            "retryable": True,
            "cached": False,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
        }
        self.server.save_import_job(job)

        class ReadyBuilder:
            def __init__(self, _root):
                self.media_tools = type("Inspector", (), {"inspect": staticmethod(lambda _url: source_metadata())})()

            def build(self, _url, *, progress_callback, cancel_callback):
                self.assert_not_cancelled = not cancel_callback()
                progress_callback({"stage": "ready", "fraction": 1.0, "pack_id": pack.name, "cached": True, "detail": "cache_hit"})
                return pack

        with mock.patch.object(self.server, "VideoPackBuilder", ReadyBuilder):
            self.server.run_import_job(job["job_id"])
        ready = self.server.load_import_job(job["job_id"])
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["pack_id"], pack.name)
        self.assertFalse(ready["retryable"])
        self.assertFalse(self.server.PROFILE_PATH.exists())

    def test_unexpected_import_failure_keeps_safe_exception_type(self):
        from tests.test_video_pack import VIDEO_URL, source_metadata

        now = self.server.isoformat(self.server.utc_now())
        job = {
            "job_id": "e" * 32,
            "source_url": VIDEO_URL,
            "pack_id": "future-pack",
            "title": None,
            "status": "queued",
            "stage": "queued",
            "fraction": 0.0,
            "detail": None,
            "cancel_requested": False,
            "error_code": None,
            "retryable": True,
            "cached": False,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
        }
        self.server.save_import_job(job)

        class BrokenBuilder:
            def __init__(self, _root):
                self.media_tools = type("Inspector", (), {"inspect": staticmethod(lambda _url: source_metadata())})()

            def build(self, _url, *, progress_callback, cancel_callback):
                raise KeyError("secret-looking detail must not persist")

        with mock.patch.object(self.server, "VideoPackBuilder", BrokenBuilder):
            self.server.run_import_job(job["job_id"])
        failed = self.server.load_import_job(job["job_id"])
        self.assertEqual(failed["error_code"], "unexpected_KeyError")
        self.assertNotIn("secret-looking", json.dumps(failed))

    def test_heavy_jobs_share_one_worker_and_bound_pending_queue(self):
        gate = threading.Event()
        counter_lock = threading.Lock()
        state = {"active": 0, "peak": 0}

        def slow_runner(_identifier):
            with counter_lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            gate.wait(5)
            with counter_lock:
                state["active"] -= 1

        with mock.patch.object(self.server, "run_caption_job", side_effect=slow_runner), mock.patch.object(self.server, "run_import_job", side_effect=slow_runner):
            accepted = [
                self.server.start_caption_thread("yt-abcdefghijk-0000000000000000"),
                self.server.start_import_thread("1" * 32),
                self.server.start_caption_thread("yt-bcdefghijkl-0000000000000000"),
                self.server.start_import_thread("2" * 32),
                self.server.start_caption_thread("yt-cdefghijklm-0000000000000000"),
            ]
            rejected = self.server.start_import_thread("3" * 32)
            deadline = time.perf_counter() + 2
            while state["active"] != 1 and time.perf_counter() < deadline:
                time.sleep(0.01)
            self.assertEqual(accepted, [True] * 5)
            self.assertFalse(rejected)
            self.assertEqual(len(self.server.HEAVY_RESERVATIONS), 5)
            self.assertEqual(state["peak"], 1)
            threads = list(self.server.CAPTION_THREADS.values()) + list(self.server.IMPORT_THREADS.values())
            gate.set()
            for thread in threads:
                thread.join(timeout=3)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(state["peak"], 1)
            self.assertEqual(len(self.server.HEAVY_RESERVATIONS), 0)

    def test_failed_import_is_circuit_broken_until_explicit_retry(self):
        from tests.test_video_pack import VIDEO_URL

        now = self.server.isoformat(self.server.utc_now())
        failed_job = {
            "job_id": "f" * 32,
            "source_url": VIDEO_URL,
            "pack_id": "failed-pack",
            "title": None,
            "status": "failed",
            "stage": "failed",
            "fraction": 0.0,
            "detail": None,
            "cancel_requested": False,
            "error_code": "yt_dlp_inspect_failed",
            "retryable": True,
            "cached": False,
            "usable": False,
            "build_complete": False,
            "created_at": now,
            "updated_at": now,
            "started_at": now,
            "completed_at": now,
        }
        self.server.save_import_job(failed_job)
        with mock.patch.object(self.server, "start_import_thread") as starter:
            blocked = self.client.post("/api/imports", json={"url": VIDEO_URL})
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertEqual(blocked.json()["job_id"], failed_job["job_id"])
        self.assertTrue(blocked.json()["retry_blocked"])
        self.assertEqual(blocked.json()["detail"], "previous_failure_requires_manual_retry")
        starter.assert_not_called()
        self.assertEqual(len(list(self.server.IMPORT_JOBS_DIR.glob("*.json"))), 1)

        with mock.patch.object(self.server, "start_import_thread") as starter:
            retried = self.client.post("/api/imports", json={"url": VIDEO_URL, "force_retry": True})
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["status"], "queued")
        self.assertNotEqual(retried.json()["job_id"], failed_job["job_id"])
        starter.assert_called_once_with(retried.json()["job_id"])
        self.assertEqual(len(list(self.server.IMPORT_JOBS_DIR.glob("*.json"))), 2)

    def test_progressive_import_publishes_partial_ready_before_complete(self):
        from tests.test_video_pack import VIDEO_URL, source_metadata

        preview_id = self.server.caption_preview_id(VIDEO_URL)
        caption = {
            "schema_version": "inflow.caption-preview/1",
            "preview_id": preview_id,
            "video_id": "aQ89CG_Yu6I",
            "source_url": VIDEO_URL,
            "title": "Long import fixture",
            "duration_sec": 1800.0,
            "revision": 1,
            "segments": [{"id": "caption-00001", "start": 0.0, "end": 1800.0, "text_en": "A long English caption.", "text_zh": "一条长字幕。"}],
        }
        self.server.CAPTION_PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        self.server.atomic_write_json(self.server.caption_content_path(preview_id), caption)
        with mock.patch.object(self.server, "start_import_thread"):
            queued = self.client.post("/api/imports", json={"url": VIDEO_URL, "playhead_sec": 1000}).json()
        published = []

        class LongAtomicBuilder:
            def __init__(self, _root):
                self.media_tools = type("Inspector", (), {"inspect": staticmethod(lambda _url: source_metadata(duration=1800.0))})()

        class FakeProgressiveBuilder:
            def __init__(self, _root, *, base_builder=None):
                self.pack_id = "ytp-aQ89CG_Yu6I-123456789abc"

            def initialize(self, _url, _caption, *, playhead_sec):
                self.assert_playhead = playhead_sec
                return {
                    "pack_id": self.pack_id, "title": "Long import fixture", "status": "building", "usable": False,
                    "revision": 0, "ready_ranges": [], "coverage_fraction": 0.0, "build_complete": False, "failed_ranges": [],
                }

            def build_until_horizon(self, _pack_id, *, prefetch_ahead, cancelled, on_revision):
                if prefetch_ahead != 1:
                    raise AssertionError(prefetch_ahead)
                partial = {
                    "pack_id": self.pack_id, "title": "Long import fixture", "status": "partial_ready", "usable": True,
                    "revision": 1, "ready_ranges": [[720.0, 1080.0]], "coverage_fraction": 0.2, "build_complete": False, "failed_ranges": [],
                }
                on_revision(partial)
                published.append(self.server_status())
                final = {**partial, "status": "complete", "revision": 5, "ready_ranges": [[0.0, 1800.0]], "coverage_fraction": 1.0, "build_complete": True}
                return final

            @staticmethod
            def server_status():
                return self.server.load_import_job(queued["job_id"])["status"]

        FakeProgressiveBuilder.server = self.server
        with mock.patch.object(self.server, "VideoPackBuilder", LongAtomicBuilder), mock.patch.object(self.server, "ProgressiveVideoPackBuilder", FakeProgressiveBuilder):
            self.server.run_import_job(queued["job_id"])
        final = self.server.load_import_job(queued["job_id"])
        self.assertEqual(published, ["partial_ready"])
        self.assertEqual(final["route"], "progressive")
        self.assertEqual(final["status"], "complete")
        self.assertTrue(final["usable"])
        self.assertTrue(final["build_complete"])
        self.assertEqual(final["playhead_sec"], 1000)
        self.assertFalse(self.server.EVENTS_PATH.exists())

    def test_progressive_focus_delta_and_audio_are_bounded(self):
        from tests.test_progressive_video_pack import FakeRangeMedia, VIDEO_URL, caption_document, offset_transcript
        from tests.test_video_pack import FakeTranscriber, FakeTranslator
        from progressive_video_pack import ProgressiveVideoPackBuilder
        from video_pack import VideoPackBuilder

        base = VideoPackBuilder(
            self.server.PACKS_DIR,
            translator=FakeTranslator(),
            transcriber=FakeTranscriber(offset_transcript()),
            media_tools=FakeRangeMedia(),
        )
        builder = ProgressiveVideoPackBuilder(self.server.PACKS_DIR, base_builder=base)
        manifest = builder.initialize(VIDEO_URL, caption_document(), playhead_sec=1000)
        built = builder.build_one(manifest["pack_id"])
        delta = self.client.get(f"/api/progressive/{manifest['pack_id']}/delta?since_revision=0&playhead_sec=1000")
        self.assertEqual(delta.status_code, 200, delta.text)
        self.assertTrue(delta.json()["changed"])
        self.assertTrue(delta.json()["candidates"])
        created = self.client.post("/api/sessions", json={"qa": True, "client_id": TEST_CLIENT_ID, "pack_id": manifest["pack_id"], "playhead_sec": 1000})
        self.assertEqual(created.status_code, 200, created.text)
        started = self.client.post(f"/api/sessions/{created.json()['session_id']}/start", json={"client_id": TEST_CLIENT_ID})
        self.assertEqual(started.status_code, 200, started.text)
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": started.json()["owner_epoch"], "session_id": created.json()["session_id"]}
        job_id = "d" * 32
        self.server.save_import_job({
            "job_id": job_id,
            "pack_id": manifest["pack_id"],
            "build_complete": False,
            "cancel_requested": True,
        })
        with mock.patch.object(self.server, "start_import_thread") as start_worker:
            focus = self.client.post(
                f"/api/progressive/{manifest['pack_id']}/focus",
                json={**owner, "playhead_sec": 1400, "focus_epoch": 2},
            )
        self.assertEqual(focus.status_code, 200, focus.text)
        self.assertEqual(focus.json()["playhead_sec"], 1400)
        self.assertEqual(focus.json()["focus_epoch"], 2)
        self.assertEqual(focus.json()["storage_focus_epoch"], owner["owner_epoch"] * 1_000_000 + 2)
        start_worker.assert_called_once_with(job_id)
        self.assertFalse(self.server.load_import_job(job_id)["cancel_requested"])

        new_client = "2" * 32
        claimed = self.client.post(f"/api/sessions/{created.json()['session_id']}/claim", json={"client_id": new_client})
        self.assertEqual(claimed.status_code, 200, claimed.text)
        stale = self.client.post(
            f"/api/progressive/{manifest['pack_id']}/focus",
            json={**owner, "playhead_sec": 200, "focus_epoch": 999},
        )
        self.assertEqual(stale.status_code, 409)
        current_job = self.server.load_import_job(job_id)
        current_job["build_complete"] = True
        self.server.save_import_job(current_job)
        fresh = self.client.post(
            f"/api/progressive/{manifest['pack_id']}/focus",
            json={"client_id": new_client, "owner_epoch": claimed.json()["owner_epoch"], "session_id": created.json()["session_id"], "playhead_sec": 300, "focus_epoch": 1},
        )
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual(fresh.json()["playhead_sec"], 300)
        self.assertGreater(fresh.json()["storage_focus_epoch"], focus.json()["storage_focus_epoch"])
        candidate = delta.json()["candidates"][0]
        audio = self.client.get(f"/api/progressive/{manifest['pack_id']}/files/{candidate['phrase_audio']}")
        self.assertEqual(audio.status_code, 200, audio.text)
        self.assertEqual(audio.headers["content-type"], "audio/mpeg")
        self.assertEqual(self.client.get(f"/api/progressive/{manifest['pack_id']}/files/shards/w0000/phrases/../../partial-manifest.json").status_code, 404)
        focused_delta = self.client.get(f"/api/progressive/{manifest['pack_id']}/delta?since_revision={built['revision']}&playhead_sec=1400")
        self.assertTrue(focused_delta.json()["changed"])
        self.assertEqual(focused_delta.json()["candidates"], [])

    def test_progressive_session_adds_only_future_window_candidates(self):
        from tests.test_progressive_video_pack import FakeRangeMedia, VIDEO_URL, caption_document, offset_transcript
        from tests.test_video_pack import FakeTranscriber, FakeTranslator
        from progressive_video_pack import ProgressiveVideoPackBuilder
        from video_pack import VideoPackBuilder

        base = VideoPackBuilder(
            self.server.PACKS_DIR,
            translator=FakeTranslator(),
            transcriber=FakeTranscriber(offset_transcript()),
            media_tools=FakeRangeMedia(),
        )
        builder = ProgressiveVideoPackBuilder(self.server.PACKS_DIR, base_builder=base)
        manifest = builder.initialize(VIDEO_URL, caption_document(), playhead_sec=1000)
        builder.build_one(manifest["pack_id"])
        created = self.client.post("/api/sessions", json={"qa": True, "pack_id": manifest["pack_id"], "playhead_sec": 1000})
        self.assertEqual(created.status_code, 200, created.text)
        session = created.json()
        self.assertTrue(session["progressive"])
        self.assertEqual(session["items"], [])
        self.assertTrue(session["video"]["native_youtube"])
        started = self.client.post(f"/api/sessions/{session['session_id']}/start", json={"client_id": TEST_CLIENT_ID})
        self.assertEqual(started.status_code, 200, started.text)
        active = started.json()
        builder.build_one(manifest["pack_id"])
        synced = self.client.post(
            f"/api/sessions/{session['session_id']}/sync-progressive",
            json={"client_id": TEST_CLIENT_ID, "owner_epoch": active["owner_epoch"], "playhead_sec": 1000},
        )
        self.assertEqual(synced.status_code, 200, synced.text)
        self.assertGreater(len(synced.json()["items"]), 0)
        self.assertTrue(all(item["anchor_sec"] > 1000.5 for item in synced.json()["items"]))
        self.assertEqual(synced.json()["progressive_ready_windows"], ["w0002", "w0003"])
        expected_speech = builder.delta(manifest["pack_id"], since_revision=-1, playhead_sec=1000)["effective_speech_sec"]
        self.assertEqual(synced.json()["effective_speech_sec"], expected_speech)
        self.assertEqual(synced.json()["owner_epoch"], active["owner_epoch"])
        item_ids = [item["id"] for item in synced.json()["items"]]
        repeated = self.client.post(
            f"/api/sessions/{session['session_id']}/sync-progressive",
            json={"client_id": TEST_CLIENT_ID, "owner_epoch": active["owner_epoch"], "playhead_sec": 1000},
        )
        self.assertEqual([item["id"] for item in repeated.json()["items"]], item_ids)
        stale = self.client.post(
            f"/api/sessions/{session['session_id']}/sync-progressive",
            json={"client_id": TEST_CLIENT_ID, "owner_epoch": active["owner_epoch"] + 1, "playhead_sec": 1000},
        )
        self.assertEqual(stale.status_code, 409)

    def test_imported_pack_lists_serves_range_and_creates_independent_session(self):
        pack = self.build_fake_pack()
        listed = self.client.get("/api/packs")
        self.assertEqual(listed.status_code, 200, listed.text)
        rows = listed.json()["packs"]
        self.assertEqual(rows[0]["pack_id"], pack.name)
        self.assertEqual(rows[0]["candidate_count"], 4)
        self.assertEqual(rows[0]["intervention_budgets"], {"low": 1, "medium": 1, "high": 1})
        self.assertEqual(rows[0]["expected_interventions"], 1)
        self.assertEqual(listed.json()["frequency_control"], "intervention_frequency")
        self.assertFalse(rows[0]["fixture"])
        self.assertTrue(rows[-1]["fixture"])

        created = self.client.post("/api/sessions", json={"qa": True, "pack_id": pack.name})
        self.assertEqual(created.status_code, 200, created.text)
        session = created.json()
        self.assertEqual(session["pack_id"], pack.name)
        self.assertEqual(session["candidate_pool_count"], 4)
        self.assertEqual(session["intervention_budgets"], {"low": 1, "medium": 1, "high": 1})
        self.assertEqual(len(session["items"]), 1)
        self.assertTrue(session["video"]["source"].startswith(f"/api/packs/{pack.name}/files/"))
        self.assertTrue(session["video"]["captions"].startswith(f"/api/packs/{pack.name}/files/"))
        self.assertTrue(all(item["phrase_zh"] and item["phrase_audio"].startswith("/api/packs/") for item in session["items"]))
        self.assertEqual(session["profile"]["active_sessions"][pack.name], session["session_id"])

        ranged = self.client.get(session["video"]["source"], headers={"Range": "bytes=0-9"})
        self.assertEqual(ranged.status_code, 206, ranged.text)
        self.assertEqual(len(ranged.content), 10)
        self.assertTrue(ranged.headers["content-range"].startswith("bytes 0-9/"))
        captions = self.client.get(session["video"]["captions"])
        self.assertEqual(captions.status_code, 200, captions.text)
        self.assertTrue(captions.json()["segments"])

    def test_completed_legacy_session_projects_missing_phrase_translation_without_rewrite(self):
        created = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = created["session_id"]
        path = Path(self.temp.name) / "sessions" / f"{session_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["stage"] = "complete"
        raw["completed_at"] = self.server.isoformat(self.server.utc_now())
        raw["items"][0].pop("phrase_zh", None)
        self.server.atomic_write_json(path, raw)
        before = path.read_text(encoding="utf-8")
        response = self.client.get(f"/api/sessions/{session_id}")
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertEqual(item["phrase_zh"], raw["items"][0]["sentence_zh"])
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_unfinished_fixture_session_migrates_only_future_item_snapshots(self):
        created = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = created["session_id"]
        path = Path(self.temp.name) / "sessions" / f"{session_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        target_id = raw["items"][0]["id"]
        raw["items"][0]["phrase_text"] = "stale phrase text"
        raw["items"][0].pop("phrase_zh", None)
        raw.pop("pack_id", None)
        self.server.atomic_write_json(path, raw)
        migrated = self.client.get(f"/api/sessions/{session_id}")
        self.assertEqual(migrated.status_code, 200, migrated.text)
        item = next(row for row in migrated.json()["items"] if row["id"] == target_id)
        current = next(row for row in self.server.CONTENT["items"] if row["id"] == target_id)
        self.assertEqual(item["phrase_text"], current["phrase_text"])
        self.assertEqual(item["phrase_zh"], current["phrase_zh"])
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["pack_id"], self.server.FIXTURE_PACK_ID)

    def test_loopback_request_guard_rejects_host_origin_and_cross_site(self):
        bad_host = self.client.get("/api/health", headers={"host": "evil.example"})
        self.assertEqual(bad_host.status_code, 421)
        self.assertEqual(bad_host.json()["detail"], "host_not_allowed")

        bad_origin = self.client.get("/api/health", headers={"origin": "https://evil.example"})
        self.assertEqual(bad_origin.status_code, 403)
        self.assertEqual(bad_origin.json()["detail"], "origin_not_allowed")

        cross_site = self.client.get("/api/health", headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(cross_site.status_code, 403)
        self.assertEqual(cross_site.json()["detail"], "cross_site_request_not_allowed")

        extension = self.client.get(
            "/api/health",
            headers={"origin": f"chrome-extension://{self.server.DEFAULT_EXTENSION_ID}", "sec-fetch-site": "cross-site"},
        )
        self.assertEqual(extension.status_code, 200)
        self.assertEqual(extension.headers["x-content-type-options"], "nosniff")
        self.assertEqual(extension.headers["referrer-policy"], "no-referrer")
        self.assertEqual(extension.headers["cross-origin-resource-policy"], "same-site")

        caption_jobs = Path(self.temp.name) / "caption-jobs"
        before_jobs = sorted(caption_jobs.glob("*.json")) if caption_jobs.exists() else []
        text_plain = self.client.post(
            "/api/caption-previews",
            content=json.dumps({"url": "https://www.youtube.com/watch?v=abcdefghijk"}),
            headers={"content-type": "text/plain"},
        )
        self.assertEqual(text_plain.status_code, 415)
        self.assertEqual(text_plain.json()["detail"], "application_json_required")
        oversized = self.client.post(
            "/api/caption-previews",
            json={"url": "https://www.youtube.com/watch?v=abcdefghijk", "padding": "x" * 33_000},
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.json()["detail"], "json_body_too_large")
        after_jobs = sorted(caption_jobs.glob("*.json")) if caption_jobs.exists() else []
        self.assertEqual(after_jobs, before_jobs)

        allowed_media = self.client.get("/media/source.mp4", headers={"range": "bytes=0-31"})
        self.assertEqual(allowed_media.status_code, 206, allowed_media.text[:200])
        self.assertEqual(len(allowed_media.content), 32)
        self.assertEqual(allowed_media.headers["cache-control"], "private, no-store")
        self.assertEqual(allowed_media.headers["cross-origin-resource-policy"], "same-origin")
        self.assertEqual(self.client.get("/media/followup_voice_ref.wav").status_code, 404)
        self.assertEqual(self.client.get("/media/followup_words/refine.wav").status_code, 404)
        cross_site_media = self.client.get(
            "/media/source.mp4",
            headers={"origin": "https://evil.example", "sec-fetch-site": "cross-site"},
        )
        self.assertEqual(cross_site_media.status_code, 403)

    def test_existing_session_requires_explicit_cross_document_claim(self):
        client_a = "a" * 32
        client_b = "b" * 32
        created = self.client.post("/api/sessions", json={"qa": True, "client_id": client_a})
        self.assertEqual(created.status_code, 200, created.text)
        started = self.client.post(f"/api/sessions/{created.json()['session_id']}/start", json={"client_id": client_a})
        self.assertEqual(started.status_code, 200, started.text)
        initial_epoch = started.json()["owner_epoch"]
        item_id = next(row["id"] for row in started.json()["items"] if row["min_intensity"] in {"low", "medium"})
        opened = self.client.post(
            f"/api/sessions/{created.json()['session_id']}/interactions/start",
            json={"client_id": client_a, "owner_epoch": initial_epoch, "item_id": item_id},
        )
        self.assertEqual(opened.status_code, 200, opened.text)
        interaction_id = opened.json()["interaction"]["interaction_id"]

        same_owner = self.client.post("/api/sessions", json={"qa": True, "client_id": client_a})
        self.assertTrue(same_owner.json()["owner_match"])
        self.assertFalse(same_owner.json()["owner_conflict"])
        self.assertEqual(same_owner.json()["owner_epoch"], initial_epoch)

        other_document = self.client.post("/api/sessions", json={"qa": True, "client_id": client_b})
        self.assertFalse(other_document.json()["owner_match"])
        self.assertTrue(other_document.json()["owner_conflict"])
        self.assertEqual(other_document.json()["owner_epoch"], initial_epoch)
        unchanged = self.client.get(f"/api/sessions/{created.json()['session_id']}").json()
        self.assertEqual(unchanged["owner_client_id"], client_a)
        self.assertEqual(unchanged["owner_epoch"], initial_epoch)
        data_root = Path(self.temp.name)
        profile_before_claim = json.loads((data_root / "profile.json").read_text(encoding="utf-8"))["items"][item_id]

        claimed = self.client.post(f"/api/sessions/{created.json()['session_id']}/claim", json={"client_id": client_b})
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["owner_client_id"], client_b)
        self.assertEqual(claimed.json()["owner_epoch"], initial_epoch + 1)
        self.assertIsNone(claimed.json()["open_interaction"])
        claimed_session = json.loads((data_root / "sessions" / f"{created.json()['session_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(claimed_session["interactions"][item_id]["outcome"], "technical_failure")
        self.assertEqual(claimed_session["interactions"][item_id]["failure_reason"], "owner_claimed")
        forged = self.client.post(
            f"/api/sessions/{created.json()['session_id']}/interactions/{item_id}/complete",
            json={
                "client_id": client_b,
                "owner_epoch": initial_epoch + 1,
                "interaction_id": interaction_id,
                "outcome": "completed",
                "dwell_ms": 6000,
                "phrase_confirmed": True,
                "replays": 0,
                "familiarity_feedback": "known",
            },
        )
        self.assertEqual(forged.status_code, 200, forged.text)
        self.assertIsNone(forged.json()["open_interaction"])
        forged_session = json.loads((data_root / "sessions" / f"{created.json()['session_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(forged_session["interactions"][item_id]["outcome"], "technical_failure")
        self.assertIsNone(forged_session["interactions"][item_id]["familiarity_feedback"])
        self.assertEqual(forged.json()["interaction_summary"]["technical_failure"], 1)
        profile_after_claim = json.loads((data_root / "profile.json").read_text(encoding="utf-8"))["items"][item_id]
        self.assertEqual(profile_after_claim["teach_count"], profile_before_claim["teach_count"])
        events = [json.loads(line) for line in (data_root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        completed_events = [event for event in events if event["type"] == "interaction_completed" and event["data"].get("interaction_id") == interaction_id]
        self.assertEqual(len(completed_events), 1)
        self.assertEqual(completed_events[0]["data"]["outcome"], "technical_failure")
        self.assertIsNone(completed_events[0]["data"]["familiarity_feedback"])

    def test_missing_profile_replays_existing_ledger_without_appending_reset(self):
        created = self.client.post("/api/sessions", json={"qa": True, "client_id": TEST_CLIENT_ID}).json()
        started = self.client.post(f"/api/sessions/{created['session_id']}/start", json={"client_id": TEST_CLIENT_ID}).json()
        item = next(row for row in started["items"] if row["min_intensity"] in {"low", "medium"})
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": started["owner_epoch"]}
        opened = self.client.post(f"/api/sessions/{created['session_id']}/interactions/start", json={**owner, "item_id": item["id"]}).json()
        completed = self.client.post(
            f"/api/sessions/{created['session_id']}/interactions/{item['id']}/complete",
            json={**owner, "interaction_id": opened["interaction"]["interaction_id"], "outcome": "completed", "dwell_ms": 6000, "phrase_confirmed": True, "replays": 0},
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        data_root = Path(self.temp.name)
        before = json.loads((data_root / "profile.json").read_text(encoding="utf-8"))
        event_bytes = (data_root / "events.jsonl").read_bytes()
        (data_root / "profile.json").unlink()

        recovered = self.server.load_profile()
        self.assertEqual(recovered["items"][item["id"]]["teach_count"], before["items"][item["id"]]["teach_count"])
        self.assertEqual(recovered["items"][item["id"]]["aural_stage"], before["items"][item["id"]]["aural_stage"])
        self.assertEqual((data_root / "events.jsonl").read_bytes(), event_bytes)
        event_types = [json.loads(line)["type"] for line in event_bytes.decode("utf-8").splitlines()]
        self.assertEqual(event_types.count("profile_created"), 1)

    def test_missing_profile_fails_closed_when_historical_item_content_is_missing(self):
        data_root = Path(self.temp.name)
        now = self.server.utc_now()
        events = [
            self.server.build_event("profile_created", {"known_ids": []}, at=now - timedelta(minutes=1)),
            self.server.build_event("interaction_completed", {"item_id": "removed-item", "outcome": "completed", "dwell_ms": 6000, "phrase_confirmed": True, "replays": 0}, session_id="a" * 32, at=now),
        ]
        (data_root / "events.jsonl").write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "profile_recovery_missing_content:removed-item"):
            self.server.load_profile()
        self.assertFalse((data_root / "profile.json").exists())

    def test_server_replays_and_backs_up_an_old_reducer_before_serving(self):
        data_root = Path(self.temp.name)
        now = self.server.utc_now()
        teach_at = now - timedelta(hours=24)
        events = [
            self.server.build_event("profile_created", {}, at=teach_at - timedelta(minutes=1)),
            self.server.build_event(
                "interaction_completed",
                {
                    "item_id": "refine",
                    "outcome": "completed",
                    "dwell_ms": 6000,
                    "phrase_confirmed": True,
                    "replays": 0,
                    "familiarity_feedback": None,
                },
                session_id="a" * 32,
                at=teach_at,
            ),
            self.server.build_event(
                "probe_completed",
                {
                    "probe_id": "probe-assisted-migration",
                    "item_id": "refine",
                    "variant_id": "refine:followup-sentence-v1",
                    "outcome": "correct",
                    "audio_confirmed": True,
                    "choice_count": 3,
                    "response_ms": 1800,
                    "delay_hours": 24,
                    "presentation_count": 2,
                },
                session_id="a" * 32,
                at=now,
            ),
        ]
        correct = self.server.rebuild_profile_from_events(
            self.server.CONTENT,
            events,
            known_ids=self.server.load_seed_known_ids(),
        )
        stale = json.loads(json.dumps(correct))
        stale["reducer_version"] = "rules-v5"
        stale_state = stale["items"]["refine"]
        stale_state["last_probe_at"] = self.server.isoformat(now)
        stale_state["last_probe_result"] = "correct"
        stale_state["next_window_start"] = self.server.isoformat(now + timedelta(days=6))
        stale_state["next_window_end"] = self.server.isoformat(now + timedelta(days=10))
        self.server.atomic_write_json(data_root / "profile.json", stale)
        event_bytes = ("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n").encode("utf-8")
        (data_root / "events.jsonl").write_bytes(event_bytes)

        migrated = self.server.load_profile()
        self.assertEqual(migrated["reducer_version"], "rules-v6")
        self.assertEqual(migrated["items"]["refine"]["next_window_start"], correct["items"]["refine"]["next_window_start"])
        self.assertEqual(migrated["items"]["refine"]["last_probe_at"], correct["items"]["refine"]["last_probe_at"])
        self.assertEqual((data_root / "events.jsonl").read_bytes(), event_bytes)
        backups = list((data_root / "backups").glob("profile.rules-v5.pre-rules-v6.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["reducer_version"], "rules-v5")

    def test_profile_intensity_and_full_session_persist(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["items"], 16)
        self.assertEqual(health.json()["builder_version"], "video-pack-builder/1.9.2")
        self.assertEqual(health.json()["extension_version"], "0.2.7")
        self.assertTrue(health.json()["progressive_learning_enabled"])

        profile = self.client.get("/api/profile").json()
        self.assertEqual(profile["control_semantics"], "intervention_frequency")
        self.assertEqual(profile["explicit_frequency"], "medium")
        self.assertEqual(profile["explicit_intensity"], "medium")
        changed = self.client.put("/api/profile/intensity", json={"intensity": "high"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["effective_intensity"], "high")

        created = self.client.post("/api/sessions", json={"qa": True})
        self.assertEqual(created.status_code, 200, created.text)
        session = created.json()
        session_id = session["session_id"]
        self.assertEqual(len(session["items"]), 4)
        self.assertEqual(len(session["encounter_catalog"]), 16)
        self.assertTrue(all("gloss_zh" in item for item in session["items"]))
        self.assertTrue(all(item["alignment_quality"] in {"word_timestamp_high", "manifest_fallback_wide"} for item in session["items"]))

        same = self.client.post("/api/sessions", json={"qa": True}).json()
        self.assertEqual(same["session_id"], session_id)

        session = self.client.post(f"/api/sessions/{session_id}/start", json={"client_id": TEST_CLIENT_ID}).json()
        self.assertEqual(session["stage"], "watch")
        item = session["items"][0]
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}

        without_lease = self.client.post(
            f"/api/sessions/{session_id}/interactions/{item['id']}/complete",
            json={**owner, "outcome": "completed", "dwell_ms": 7000, "phrase_confirmed": True, "replays": 0},
        )
        self.assertEqual(without_lease.status_code, 409)

        lease = self.client.post(
            f"/api/sessions/{session_id}/interactions/start",
            json={**owner, "item_id": item["id"]},
        )
        self.assertEqual(lease.status_code, 200, lease.text)
        interaction_id = lease.json()["interaction"]["interaction_id"]
        forged = self.client.post(
            f"/api/sessions/{session_id}/interactions/{item['id']}/complete",
            json={**owner, "interaction_id": "0" * 32, "outcome": "completed", "dwell_ms": 7000, "phrase_confirmed": True, "replays": 0},
        )
        self.assertEqual(forged.status_code, 409)
        self.assertEqual(forged.json()["detail"], "interaction_token_mismatch")
        saved = self.client.post(
            f"/api/sessions/{session_id}/interactions/{item['id']}/complete",
            json={**owner, "interaction_id": interaction_id, "outcome": "completed", "dwell_ms": 7000, "phrase_confirmed": True, "replays": 1, "familiarity_feedback": "familiar"},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["interaction_summary"]["completed"], 1)
        self.assertEqual(saved.json()["profile"]["reading_samples"], 1)
        saved_item = next(row for row in saved.json()["items"] if row["id"] == item["id"])
        self.assertEqual(saved_item["familiarity_status"], "familiar")
        self.assertFalse(saved_item["explicit_known"])
        lexicon = self.client.get("/api/lexicon").json()
        lexical = next(row for row in lexicon["entries"] if row["knowledge_key"] == saved_item["knowledge_key"])
        self.assertEqual(lexical["status"], "familiar")
        self.assertEqual(lexical["replay_count"], 1)

        newer = self.client.post(
            f"/api/sessions/{session_id}/progress",
            json={**owner, "sequence": 5, "media_time": 72.5},
        )
        self.assertEqual(newer.status_code, 200)
        stale = self.client.post(
            f"/api/sessions/{session_id}/progress",
            json={**owner, "sequence": 4, "media_time": 2},
        )
        self.assertEqual(stale.status_code, 200)
        readback = self.client.get(f"/api/sessions/{session_id}").json()
        self.assertEqual(readback["last_media_time"], 72.5)

        completed = self.client.post(
            f"/api/sessions/{session_id}/complete",
            json={**owner, "source_ended": True, "elapsed_ms": 120000, "manual_seeks": 0, "technical_failures": 0},
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["stage"], "complete")
        final_profile = self.client.get("/api/profile").json()
        self.assertEqual(final_profile["completed_sessions"], 1)
        self.assertIsNone(final_profile["active_session_id"])

        profile_path = Path(self.temp.name) / "profile.json"
        persisted = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["items"][item["id"]]["aural_stage"], "taught")
        self.assertEqual(persisted["items"][item["id"]]["encounter_count"], 1)

        reloaded_server = importlib.reload(self.server)
        with TestClient(reloaded_server.app) as restarted_client:
            restarted = restarted_client.get("/api/profile").json()
        self.assertEqual(restarted["completed_sessions"], 1)
        self.assertEqual(restarted["reading_samples"], 1)
        self.assertEqual(restarted["explicit_intensity"], "high")

    def test_subtitle_feedback_is_append_only_and_known_expression_is_not_recommended(self):
        item = next(row for row in self.server.CONTENT["items"] if row["surface"].casefold() == "reconnaissance")
        initial = self.client.get("/api/lexicon")
        self.assertEqual(initial.status_code, 200)
        entry = next(row for row in initial.json()["entries"] if row["surface"] == item["surface"] and row["gloss_zh"] == item["gloss_zh"])
        lookup = self.client.post(
            "/api/lexicon/lookup",
            json={"client_id": TEST_CLIENT_ID, "surface": item["surface"], "sentence": item["phrase_text"]},
        )
        self.assertEqual(lookup.status_code, 200, lookup.text)
        self.assertEqual(lookup.json()["source"], "local_lexicon")
        self.assertEqual(lookup.json()["knowledge_key"], entry["knowledge_key"])
        mismatch = self.client.post(
            "/api/lexicon/feedback",
            json={
                "client_id": TEST_CLIENT_ID,
                "knowledge_key": "v1:wrong::错误",
                "surface": item["surface"],
                "gloss_zh": item["gloss_zh"],
                "familiarity_feedback": "known",
                "source": "subtitle",
            },
        )
        self.assertEqual(mismatch.status_code, 422)
        saved = self.client.post(
            "/api/lexicon/feedback",
            json={
                "client_id": TEST_CLIENT_ID,
                "knowledge_key": entry["knowledge_key"],
                "surface": item["surface"],
                "gloss_zh": item["gloss_zh"],
                "familiarity_feedback": "known",
                "source": "subtitle",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"], "known")
        self.assertTrue(saved.json()["explicit_known"])
        self.client.put("/api/profile/intensity", json={"intensity": "high"})
        session = self.client.post("/api/sessions", json={"qa": True}).json()
        self.assertNotIn(entry["knowledge_key"], {row["knowledge_key"] for row in session["items"]})
        reset = self.client.post(
            "/api/lexicon/feedback",
            json={
                "client_id": TEST_CLIENT_ID,
                "knowledge_key": entry["knowledge_key"],
                "surface": item["surface"],
                "gloss_zh": item["gloss_zh"],
                "familiarity_feedback": "undo",
                "source": "subtitle",
            },
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(reset.json()["status"], "unseen")
        self.assertFalse(reset.json()["explicit_known"])
        unavailable = self.client.post(
            "/api/lexicon/feedback",
            json={
                "client_id": TEST_CLIENT_ID,
                "knowledge_key": entry["knowledge_key"],
                "surface": item["surface"],
                "gloss_zh": item["gloss_zh"],
                "familiarity_feedback": "undo",
                "source": "subtitle",
            },
        )
        self.assertEqual(unavailable.status_code, 409)
        forbidden_reset = self.client.post(
            "/api/lexicon/feedback",
            json={
                "client_id": TEST_CLIENT_ID,
                "knowledge_key": entry["knowledge_key"],
                "surface": item["surface"],
                "gloss_zh": item["gloss_zh"],
                "familiarity_feedback": "undo",
                "source": "mapping",
            },
        )
        self.assertEqual(forbidden_reset.status_code, 422)
        events = [json.loads(line) for line in (Path(self.temp.name) / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        feedback_events = [event for event in events if event["type"] == "lexicon_feedback"]
        self.assertEqual(len(feedback_events), 2)
        self.assertEqual([event["data"]["familiarity_feedback"] for event in feedback_events], ["known", "undo"])
        self.assertEqual(feedback_events[-1]["data"]["intent"], "subtitle_state_undo")

    def test_no_more_explanations_is_an_explicit_known_override(self):
        item = next(row for row in self.server.CONTENT["items"] if row["surface"].casefold() == "reconnaissance")
        entry = next(row for row in self.client.get("/api/lexicon").json()["entries"] if row["surface"] == item["surface"])
        response = self.client.post(
            "/api/lexicon/feedback",
            json={
                "client_id": TEST_CLIENT_ID,
                "knowledge_key": entry["knowledge_key"],
                "surface": item["surface"],
                "gloss_zh": item["gloss_zh"],
                "sentence": item["phrase_text"],
                "familiarity_feedback": "known",
                "source": "explicit_no_more_explanations",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["explicit_known"])
        profile = self.server.load_profile()
        evidence = profile["lexicon"][entry["knowledge_key"]]["evidence"][-1]
        self.assertEqual(evidence["kind"], "EXPLICIT_KNOWN_OVERRIDE")
        self.assertEqual(evidence["intent"], "no_more_explanations")
        events = [json.loads(line) for line in (Path(self.temp.name) / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(events[-1]["data"]["intent"], "no_more_explanations")

    def test_new_resolved_subtitle_word_accepts_feedback_with_lookup_key(self):
        class Translator:
            def translate_all(self, _cues):
                return {"surface": "光合作用", "sentence": "植物利用光合作用把阳光转化为能量。"}

        sentence = "Plants use photosynthesis to turn sunlight into energy."
        with mock.patch.object(self.server, "LEXICON_TRANSLATOR", Translator()):
            lookup = self.client.post(
                "/api/lexicon/lookup",
                json={"client_id": TEST_CLIENT_ID, "surface": "photosynthesis", "sentence": sentence},
            )
        self.assertEqual(lookup.status_code, 200, lookup.text)
        self.assertTrue(lookup.json()["knowledge_key"].startswith("kv1|"), lookup.json())
        saved = self.client.post(
            "/api/lexicon/feedback",
            json={
                "client_id": TEST_CLIENT_ID,
                "knowledge_key": lookup.json()["knowledge_key"],
                "surface": "photosynthesis",
                "gloss_zh": lookup.json()["gloss_zh"],
                "sentence": sentence,
                "familiarity_feedback": "known",
                "source": "subtitle",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["knowledge_key"], lookup.json()["knowledge_key"])
        self.assertEqual(saved.json()["status"], "known")

    def test_ambiguous_subtitle_word_does_not_merge_across_contexts(self):
        class Translator:
            def translate_all(self, _cues):
                return {"surface": "银行", "sentence": "当前句翻译"}

        first_sentence = "They sat by the bank and watched the river."
        second_sentence = "She called the bank about her account."
        with mock.patch.object(self.server, "LEXICON_TRANSLATOR", Translator()):
            first = self.client.post(
                "/api/lexicon/lookup",
                json={"client_id": TEST_CLIENT_ID, "surface": "bank", "sentence": first_sentence},
            )
            second = self.client.post(
                "/api/lexicon/lookup",
                json={"client_id": TEST_CLIENT_ID, "surface": "bank", "sentence": second_sentence},
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertNotEqual(first.json()["knowledge_key"], second.json()["knowledge_key"])
            saved = self.client.post(
                "/api/lexicon/feedback",
                json={
                    "client_id": TEST_CLIENT_ID,
                    "knowledge_key": first.json()["knowledge_key"],
                    "surface": "bank",
                    "gloss_zh": first.json()["gloss_zh"],
                    "sentence": first_sentence,
                    "familiarity_feedback": "known",
                    "source": "subtitle",
                },
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertTrue(saved.json()["explicit_known"])
            repeated_second = self.client.post(
                "/api/lexicon/lookup",
                json={"client_id": TEST_CLIENT_ID, "surface": "bank", "sentence": second_sentence},
            )
            self.assertEqual(repeated_second.json()["status"], "unseen")
            self.assertEqual(repeated_second.json()["knowledge_key"], second.json()["knowledge_key"])

    def test_delayed_probe_api_hides_answer_scores_once_and_then_starts_video(self):
        self.seed_due_taught_item("refine")
        created = self.client.post("/api/sessions", json={"qa": True})
        self.assertEqual(created.status_code, 200, created.text)
        session = created.json()
        self.assertEqual(session["stage"], "probe_ready")
        self.assertIsNotNone(session["probe"])
        self.assertEqual(len(session["probe"]["choices"]), 3)
        pre_answer = json.dumps(session["probe"], ensure_ascii=False)
        self.assertNotIn("refine", pre_answer.lower())
        self.assertNotIn("correct_choice_id", pre_answer)
        self.assertNotIn("followup_sentence", pre_answer)
        self.assertNotIn("/media/followup/", pre_answer)
        self.assertNotIn("refine", {row["id"] for row in session["items"]})

        session_id = session["session_id"]
        session = self.client.post(
            f"/api/sessions/{session_id}/start",
            json={"client_id": TEST_CLIENT_ID},
        ).json()
        self.assertEqual(session["stage"], "probe")
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}
        audio = self.client.get(session["probe"]["audio_url"])
        self.assertEqual(audio.status_code, 200)
        self.assertGreater(len(audio.content), 1000)
        stored = self.server.load_session(session_id)["probe"]
        correct_choice_id = stored["correct_choice_id"]
        self.server.ITEMS["refine"]["surface"] = "mutated-after-probe"
        self.server.ITEMS["refine"]["gloss_zh"] = "被后续内容修改"
        self.server.ITEMS["refine"]["followup_sentence"] = "Mutated after the probe was created."
        rejected = self.client.post(
            f"/api/sessions/{session_id}/probe/complete",
            json={
                **owner,
                "probe_id": stored["probe_id"],
                "outcome": "selected",
                "selected_choice_id": correct_choice_id,
                "audio_confirmed": False,
                "response_ms": 1200,
            },
        )
        self.assertEqual(rejected.status_code, 422)
        session = self.confirm_probe_presentation(session_id, session, owner)
        duplicate_presentation = self.client.post(
            f"/api/sessions/{session_id}/probe/presentation",
            json={**owner, "probe_id": stored["probe_id"], "presentation_id": "3" * 32},
        )
        self.assertEqual(duplicate_presentation.status_code, 200)
        self.assertEqual(duplicate_presentation.json()["probe"]["presentation_count"], 1)

        body = {
            **owner,
            "probe_id": stored["probe_id"],
            "outcome": "selected",
            "selected_choice_id": correct_choice_id,
            "audio_confirmed": True,
            "response_ms": 2100,
        }
        scored = self.client.post(f"/api/sessions/{session_id}/probe/complete", json=body)
        self.assertEqual(scored.status_code, 200, scored.text)
        scored_session = scored.json()
        self.assertEqual(scored_session["stage"], "probe_feedback")
        self.assertEqual(scored_session["probe"]["result"]["outcome"], "correct")
        self.assertEqual(scored_session["probe"]["result"]["evidence_quality"], "WEAK_SUCCESS")
        self.assertEqual(scored_session["probe"]["feedback"]["surface"], "refine")
        duplicate = self.client.post(f"/api/sessions/{session_id}/probe/complete", json=body)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        conflicting_choice = next(row["choice_id"] for row in stored["choices"] if row["choice_id"] != correct_choice_id)
        conflicting = self.client.post(
            f"/api/sessions/{session_id}/probe/complete",
            json={**body, "selected_choice_id": conflicting_choice},
        )
        self.assertEqual(conflicting.status_code, 409)
        self.assertEqual(conflicting.json()["detail"], "probe_result_conflict")
        profile = self.client.get("/api/profile").json()
        self.assertEqual(profile["recognition_successes"], 1)
        events = [
            json.loads(line)
            for line in (Path(self.temp.name) / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(sum(event["type"] == "probe_completed" for event in events), 1)
        watching = self.client.post(
            f"/api/sessions/{session_id}/start",
            json=owner,
        )
        self.assertEqual(watching.status_code, 200, watching.text)
        self.assertEqual(watching.json()["stage"], "watch")

    def test_wrong_probe_choice_is_recognition_failure_not_clean_recall(self):
        self.seed_due_taught_item("refine")
        session = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = session["session_id"]
        session = self.client.post(
            f"/api/sessions/{session_id}/start",
            json={"client_id": TEST_CLIENT_ID},
        ).json()
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}
        stored = self.server.load_session(session_id)["probe"]
        self.confirm_probe_presentation(session_id, session, owner)
        wrong_choice = next(row["choice_id"] for row in stored["choices"] if row["choice_id"] != stored["correct_choice_id"])
        completed = self.client.post(
            f"/api/sessions/{session_id}/probe/complete",
            json={
                **owner,
                "probe_id": stored["probe_id"],
                "outcome": "selected",
                "selected_choice_id": wrong_choice,
                "audio_confirmed": True,
                "response_ms": 1700,
            },
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["probe"]["result"]["outcome"], "incorrect")
        self.assertEqual(completed.json()["probe"]["result"]["evidence_quality"], "RECOGNITION_FAILURE")
        persisted = json.loads((Path(self.temp.name) / "profile.json").read_text(encoding="utf-8"))
        state = persisted["items"]["refine"]
        self.assertEqual(state["recognition_failure_count"], 1)
        self.assertEqual(state["clean_failure_count"], 0)
        self.assertEqual(state["aural_stage"], "taught")

    def test_dont_know_probe_is_recorded_without_claiming_mastery(self):
        self.seed_due_taught_item("refine")
        session = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = session["session_id"]
        session = self.client.post(
            f"/api/sessions/{session_id}/start",
            json={"client_id": TEST_CLIENT_ID},
        ).json()
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}
        self.confirm_probe_presentation(session_id, session, owner)
        completed = self.client.post(
            f"/api/sessions/{session_id}/probe/complete",
            json={
                **owner,
                "probe_id": session["probe"]["probe_id"],
                "outcome": "dont_know",
                "audio_confirmed": True,
                "response_ms": 2300,
            },
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        result = completed.json()["probe"]["result"]
        self.assertEqual(result["outcome"], "dont_know")
        self.assertEqual(result["evidence_quality"], "ABSTAINED_NONRETRIEVAL")
        profile = self.client.get("/api/profile").json()
        self.assertEqual(profile["recognition_abstentions"], 1)
        self.assertEqual(profile["recognition_failures"], 0)
        self.assertEqual(profile["recognition_successes"], 0)

    def assert_probe_non_evidence(self, requested_outcome: str):
        self.seed_due_taught_item("refine")
        session = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = session["session_id"]
        session = self.client.post(
            f"/api/sessions/{session_id}/start",
            json={"client_id": TEST_CLIENT_ID},
        ).json()
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}
        profile_path = Path(self.temp.name) / "profile.json"
        profile_before = profile_path.read_bytes()
        completed = self.client.post(
            f"/api/sessions/{session_id}/probe/complete",
            json={
                **owner,
                "probe_id": session["probe"]["probe_id"],
                "outcome": requested_outcome,
                "audio_confirmed": False,
                "response_ms": 0,
            },
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        persisted = json.loads(profile_path.read_text(encoding="utf-8"))
        state = persisted["items"]["refine"]
        self.assertEqual(state["recognition_success_count"], 0)
        self.assertEqual(state["recognition_failure_count"], 0)
        self.assertEqual(state["used_probe_variants"], [])
        self.assertEqual(profile_path.read_bytes(), profile_before)

    def test_probe_skip_never_becomes_memory_evidence(self):
        self.assert_probe_non_evidence("skipped")

    def test_probe_technical_failure_never_becomes_memory_evidence(self):
        self.assert_probe_non_evidence("technical_failure")

    def test_probe_transaction_recovers_without_duplicate_score(self):
        self.seed_due_taught_item("refine")
        session = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = session["session_id"]
        session = self.client.post(
            f"/api/sessions/{session_id}/start",
            json={"client_id": TEST_CLIENT_ID},
        ).json()
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}
        stored = self.server.load_session(session_id)["probe"]
        self.confirm_probe_presentation(session_id, session, owner)
        body = {
            **owner,
            "probe_id": stored["probe_id"],
            "outcome": "selected",
            "selected_choice_id": stored["correct_choice_id"],
            "audio_confirmed": True,
            "response_ms": 2000,
        }
        with mock.patch.object(self.server, "append_event_payload", side_effect=RuntimeError("injected_probe_event_failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(f"/api/sessions/{session_id}/probe/complete", json=body)
        self.assertEqual(len(list((Path(self.temp.name) / "transactions").glob("*.json"))), 1)
        recovered = self.client.get(f"/api/sessions/{session_id}")
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["stage"], "probe_feedback")
        self.assertEqual(self.client.get("/api/profile").json()["recognition_successes"], 1)
        events = [
            json.loads(line)
            for line in (Path(self.temp.name) / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(sum(event["type"] == "probe_completed" for event in events), 1)
        self.assertEqual(list((Path(self.temp.name) / "transactions").glob("*.json")), [])

    def test_partial_jsonl_tail_isolated_before_recovery_event(self):
        self.client.get("/api/profile")
        events_path = Path(self.temp.name) / "events.jsonl"
        with events_path.open("ab") as stream:
            stream.write(b'{"partial":')
        changed = self.client.put("/api/profile/intensity", json={"intensity": "high"})
        self.assertEqual(changed.status_code, 200, changed.text)
        valid_events = []
        invalid_lines = 0
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                valid_events.append(json.loads(line))
            except json.JSONDecodeError:
                invalid_lines += 1
        self.assertEqual(invalid_lines, 1)
        self.assertEqual(sum(event["type"] == "explicit_intensity_changed" for event in valid_events), 1)

    def test_claim_during_active_probe_invalidates_measurement_before_transfer(self):
        self.seed_due_taught_item("refine")
        session = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = session["session_id"]
        session = self.client.post(
            f"/api/sessions/{session_id}/start",
            json={"client_id": TEST_CLIENT_ID},
        ).json()
        owner_one = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}
        session = self.confirm_probe_presentation(session_id, session, owner_one)
        stored = self.server.load_session(session_id)["probe"]
        profile_path = Path(self.temp.name) / "profile.json"
        profile_before = profile_path.read_bytes()
        owner_two_id = "2" * 32
        claimed = self.client.post(
            f"/api/sessions/{session_id}/claim",
            json={"client_id": owner_two_id},
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        claimed_session = claimed.json()
        self.assertEqual(claimed_session["stage"], "watch")
        self.assertEqual(claimed_session["probe"]["result"]["outcome"], "technical_failure")
        self.assertEqual(claimed_session["probe"]["result"]["failure_reason"], "owner_claimed")
        self.assertEqual(profile_path.read_bytes(), profile_before)
        stale_answer = self.client.post(
            f"/api/sessions/{session_id}/probe/complete",
            json={
                **owner_one,
                "probe_id": stored["probe_id"],
                "outcome": "selected",
                "selected_choice_id": stored["correct_choice_id"],
                "audio_confirmed": True,
                "response_ms": 1000,
            },
        )
        self.assertEqual(stale_answer.status_code, 409)
        events = [
            json.loads(line)
            for line in (Path(self.temp.name) / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(sum(event["type"] == "probe_completed" for event in events), 1)
        self.assertEqual(sum(event["type"] == "session_claimed" for event in events), 1)

    def test_explicit_claim_prevents_cross_tab_interaction_race(self):
        created = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = created["session_id"]
        session = self.client.post(f"/api/sessions/{session_id}/start", json={"client_id": TEST_CLIENT_ID}).json()
        owner_one = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}
        item = next(row for row in session["items"] if row["min_intensity"] in {"low", "medium"})
        lease = self.client.post(
            f"/api/sessions/{session_id}/interactions/start",
            json={**owner_one, "item_id": item["id"]},
        ).json()["interaction"]
        owner_two_id = "2" * 32
        stolen = self.client.post(
            f"/api/sessions/{session_id}/interactions/{item['id']}/complete",
            json={
                "client_id": owner_two_id,
                "owner_epoch": owner_one["owner_epoch"],
                "interaction_id": lease["interaction_id"],
                "outcome": "completed",
                "dwell_ms": 6000,
                "phrase_confirmed": True,
                "replays": 0,
            },
        )
        self.assertEqual(stolen.status_code, 409)
        claimed = self.client.post(f"/api/sessions/{session_id}/claim", json={"client_id": owner_two_id}).json()
        owner_two = {"client_id": owner_two_id, "owner_epoch": claimed["owner_epoch"]}
        completed = self.client.post(
            f"/api/sessions/{session_id}/interactions/{item['id']}/complete",
            json={
                **owner_two,
                "interaction_id": lease["interaction_id"],
                "outcome": "completed",
                "dwell_ms": 6000,
                "phrase_confirmed": True,
                "replays": 0,
            },
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        stale = self.client.post(
            f"/api/sessions/{session_id}/progress",
            json={**owner_one, "sequence": 99, "media_time": 1},
        )
        self.assertEqual(stale.status_code, 409)

    def test_write_ahead_transaction_recovers_session_profile_gap(self):
        created = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = created["session_id"]
        session = self.client.post(f"/api/sessions/{session_id}/start", json={"client_id": TEST_CLIENT_ID}).json()
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}
        item_id = session["encounter_catalog"][0]["id"]
        original_write = self.server.atomic_write_json
        failed = {"done": False}

        def injected_write(path, payload):
            if Path(path) == self.server.PROFILE_PATH and not failed["done"]:
                failed["done"] = True
                raise RuntimeError("injected_profile_write_failure")
            return original_write(path, payload)

        with mock.patch.object(self.server, "atomic_write_json", side_effect=injected_write):
            with self.assertRaises(RuntimeError):
                self.client.post(f"/api/sessions/{session_id}/encounters/{item_id}", json=owner)
        self.assertEqual(len(list((Path(self.temp.name) / "transactions").glob("*.json"))), 1)
        recovered_session = self.client.get(f"/api/sessions/{session_id}").json()
        self.assertIn(item_id, recovered_session["encountered_ids"])
        profile = json.loads((Path(self.temp.name) / "profile.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["items"][item_id]["encounter_count"], 1)
        events = [
            json.loads(line)
            for line in (Path(self.temp.name) / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(sum(event["type"] == "encounter_recorded" and event["data"]["item_id"] == item_id for event in events), 1)
        self.assertEqual(list((Path(self.temp.name) / "transactions").glob("*.json")), [])

    def test_write_ahead_transaction_recovers_profile_event_gap(self):
        self.client.get("/api/profile")
        with mock.patch.object(self.server, "append_event_payload", side_effect=RuntimeError("injected_append_failure")):
            with self.assertRaises(RuntimeError):
                self.client.put("/api/profile/intensity", json={"intensity": "high"})
        transaction_files = list((Path(self.temp.name) / "transactions").glob("*.json"))
        self.assertEqual(len(transaction_files), 1)
        recovered = self.client.get("/api/profile")
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["explicit_intensity"], "high")
        self.assertEqual(list((Path(self.temp.name) / "transactions").glob("*.json")), [])
        events = [
            json.loads(line)
            for line in (Path(self.temp.name) / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(sum(event["type"] == "explicit_intensity_changed" for event in events), 1)

    def test_intensity_gate_rejects_high_only_item_in_low_mode(self):
        self.client.put("/api/profile/intensity", json={"intensity": "low"})
        created = self.client.post("/api/sessions", json={"qa": True}).json()
        session_id = created["session_id"]
        session = self.client.post(f"/api/sessions/{session_id}/start", json={"client_id": TEST_CLIENT_ID}).json()
        owner = {"client_id": TEST_CLIENT_ID, "owner_epoch": session["owner_epoch"]}
        high_only = next(item for item in session["items"] if item["min_intensity"] == "high")
        rejected = self.client.post(
            f"/api/sessions/{session_id}/interactions/start",
            json={**owner, "item_id": high_only["id"]},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()["detail"], "intensity_does_not_allow_item")


if __name__ == "__main__":
    unittest.main()
