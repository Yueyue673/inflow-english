from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from adaptive_core import atomic_write_json
from progressive_video_pack import (
    ProgressiveVideoPackBuilder,
    _caption_alignment_is_better,
    _cue_set_is_sparse,
    verify_window_shard,
)
from video_pack import SourceValidationError, VideoPackBuilder
from tests.test_video_pack import FakeTranscriber, FakeTranslator, sample_transcript

VIDEO_ID = "abcdefghijk"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
FIXED_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def offset_transcript(seconds: float = 20.0):
    payload = copy.deepcopy(sample_transcript())
    payload["duration_sec"] = 40.0
    for segment in payload["segments"]:
        for field in ("start", "end"):
            if field in segment:
                segment[field] += seconds
        for word in segment["words"]:
            word["start"] += seconds
            word["end"] += seconds
    return payload


def caption_document(duration: float = 1800.0):
    rows = []
    clock = 0.0
    index = 1
    while clock < duration:
        end = min(duration, clock + 12.0)
        rows.append({
            "id": f"caption-{index:05d}",
            "start": clock,
            "end": end,
            "text_en": f"Reliable caption sentence number {index}.",
            "text_zh": f"第 {index} 条可靠字幕。",
        })
        clock = end
        index += 1
    return {
        "schema_version": "inflow.caption-preview/1",
        "preview_id": "yt-abcdefghijk-0000000000000000",
        "video_id": VIDEO_ID,
        "source_url": VIDEO_URL,
        "title": "Long fixture",
        "duration_sec": duration,
        "revision": 2,
        "segments": rows,
    }


class FakeRangeMedia:
    identity = "fake-range-media:v1"

    def __init__(self, fail_first=False):
        self.range_durations = {}
        self.clip_durations = {}
        self.downloaded_ranges = []
        self.fail_first = fail_first

    def download_range(self, _url, start_sec, end_sec, destination):
        self.downloaded_ranges.append((float(start_sec), float(end_sec)))
        if self.fail_first and len(self.downloaded_ranges) == 1:
            raise SourceValidationError("injected_range_failure")
        destination.write_bytes(b"range-media" * 64)
        self.range_durations[Path(destination)] = float(end_sec) - float(start_sec)

    def extract_audio(self, _video_path, wav_path):
        wav_path.write_bytes(b"range-wav" * 64)

    def probe(self, media_path):
        path = Path(media_path)
        if path.suffix == ".mp4":
            return {"duration_sec": self.range_durations[path], "height": 360, "has_video": True, "has_audio": True}
        return {"duration_sec": self.clip_durations[path], "height": None, "has_video": False, "has_audio": True}

    def clip_phrase(self, _video_path, start_sec, end_sec, destination):
        destination.write_bytes(f"clip:{start_sec:.3f}:{end_sec:.3f}".encode("ascii") * 20)
        self.clip_durations[Path(destination)] = float(end_sec) - float(start_sec)


class ProgressivePackTests(unittest.TestCase):
    def make_builder(self, root: Path, *, media=None):
        base = VideoPackBuilder(
            root,
            translator=FakeTranslator(),
            transcriber=FakeTranscriber(offset_transcript()),
            media_tools=media or FakeRangeMedia(),
            clock=lambda: FIXED_NOW,
        )
        return ProgressiveVideoPackBuilder(root, base_builder=base, clock=lambda: FIXED_NOW)

    def test_sparse_cue_set_prefers_materially_better_caption_alignment(self):
        sparse = [{"start_sec": 114.0, "end_sec": 117.0}]
        aligned = [
            {"start_sec": float(index * 8), "end_sec": float(index * 8 + 4)}
            for index in range(20)
        ]
        self.assertTrue(_cue_set_is_sparse(sparse, effective_speech_sec=357.26, window_duration_sec=390.96))
        self.assertTrue(
            _caption_alignment_is_better(
                sparse,
                aligned,
                effective_speech_sec=357.26,
                window_duration_sec=390.96,
            )
        )
        self.assertFalse(_cue_set_is_sparse(aligned, effective_speech_sec=60.0, window_duration_sec=180.0))
        self.assertFalse(
            _caption_alignment_is_better(
                aligned,
                sparse,
                effective_speech_sec=60.0,
                window_duration_sec=180.0,
            )
        )

    def test_playhead_window_commits_first_without_full_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "packs"
            builder = self.make_builder(root)
            manifest = builder.initialize(VIDEO_URL, caption_document(), playhead_sec=1000)
            containing = next(row for row in manifest["windows"] if row["ownership_start_sec"] <= 1000 < row["ownership_end_sec"])
            updated = builder.build_one(manifest["pack_id"])
            built = next(row for row in updated["windows"] if row["status"] in {"ready", "ready_no_candidate"})
            self.assertEqual(built["window_id"], containing["window_id"])
            self.assertEqual(updated["status"], "partial_ready")
            self.assertTrue(updated["usable"])
            self.assertFalse(updated["build_complete"])
            pack = builder.pack_path(manifest["pack_id"])
            self.assertEqual(list(pack.rglob("source-range.mp4")), [])
            self.assertEqual(list(pack.rglob("source-range.wav")), [])
            self.assertEqual(list(pack.glob("video.mp4")), [])
            shard = verify_window_shard(pack / "shards" / built["window_id"], pack_id=manifest["pack_id"], window_id=built["window_id"])
            self.assertGreater(len(shard["candidates"]), 0)
            self.assertTrue(all(containing["ownership_start_sec"] <= row["anchor_sec"] < containing["ownership_end_sec"] for row in shard["candidates"]))
            self.assertTrue(all(0 <= row["highlight_start_sec"] < row["highlight_end_sec"] <= row["phrase_duration_sec"] for row in shard["candidates"]))

    def test_next_window_is_forward_of_playhead_not_oldest_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = self.make_builder(Path(temporary) / "packs")
            manifest = builder.initialize(VIDEO_URL, caption_document(), playhead_sec=1000)
            first = builder.build_one(manifest["pack_id"])
            first_id = next(row["window_id"] for row in first["windows"] if row["status"] in {"ready", "ready_no_candidate"})
            second = builder.build_one(manifest["pack_id"])
            ready_ids = [row["window_id"] for row in second["windows"] if row["status"] in {"ready", "ready_no_candidate"}]
            self.assertEqual(len(ready_ids), 2)
            self.assertGreater(int(next(value[1:] for value in ready_ids if value != first_id)), int(first_id[1:]))

    def test_focus_epoch_is_monotonic_and_changes_next_priority(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = self.make_builder(Path(temporary) / "packs")
            manifest = builder.initialize(VIDEO_URL, caption_document(), playhead_sec=10)
            unchanged = builder.set_focus(manifest["pack_id"], playhead_sec=1400, focus_epoch=1)
            self.assertEqual(unchanged["focus"]["playhead_sec"], 10)
            focused = builder.set_focus(manifest["pack_id"], playhead_sec=1400, focus_epoch=2)
            self.assertEqual(focused["focus"]["playhead_sec"], 1400)
            built = builder.build_one(manifest["pack_id"])
            selected = next(row for row in built["windows"] if row["status"] in {"ready", "ready_no_candidate"})
            self.assertLessEqual(selected["ownership_start_sec"], 1400)
            self.assertGreater(selected["ownership_end_sec"], 1400)

    def test_worker_stops_after_focus_window_and_one_prefetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = self.make_builder(Path(temporary) / "packs")
            manifest = builder.initialize(VIDEO_URL, caption_document(), playhead_sec=1000)
            first_horizon = builder.build_until_horizon(manifest["pack_id"], prefetch_ahead=1)
            ready = [row["window_id"] for row in first_horizon["windows"] if row["status"] in {"ready", "ready_no_candidate"}]
            self.assertEqual(ready, ["w0002", "w0003"])
            self.assertFalse(first_horizon["build_complete"])
            builder.set_focus(manifest["pack_id"], playhead_sec=100, focus_epoch=2)
            second_horizon = builder.build_until_horizon(manifest["pack_id"], prefetch_ahead=1)
            ready = [row["window_id"] for row in second_horizon["windows"] if row["status"] in {"ready", "ready_no_candidate"}]
            self.assertEqual(ready, ["w0000", "w0001", "w0002", "w0003"])
            self.assertEqual(second_horizon["windows"][-1]["status"], "pending")

    def test_recovery_adopts_verified_orphan_and_resets_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = self.make_builder(Path(temporary) / "packs")
            manifest = builder.initialize(VIDEO_URL, caption_document(), playhead_sec=1000)
            built = builder.build_one(manifest["pack_id"])
            ready = next(row for row in built["windows"] if row["status"] in {"ready", "ready_no_candidate"})
            ready["status"] = "pending"
            running = next(row for row in built["windows"] if row["status"] == "pending" and row["window_id"] != ready["window_id"])
            running["status"] = "running"
            atomic_write_json(builder.manifest_path(manifest["pack_id"]), built)
            recovered = builder.recover(manifest["pack_id"])
            self.assertIn(next(row for row in recovered["windows"] if row["window_id"] == ready["window_id"])["status"], {"ready", "ready_no_candidate"})
            self.assertEqual(next(row for row in recovered["windows"] if row["window_id"] == running["window_id"])["status"], "pending")

    def test_one_failed_window_does_not_block_later_ready_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            media = FakeRangeMedia(fail_first=True)
            builder = self.make_builder(Path(temporary) / "packs", media=media)
            manifest = builder.initialize(VIDEO_URL, caption_document(), playhead_sec=1000)
            failed = builder.build_one(manifest["pack_id"])
            self.assertEqual(sum(row["status"] == "failed" for row in failed["windows"]), 1)
            ready = builder.build_one(manifest["pack_id"])
            self.assertTrue(ready["usable"])
            self.assertEqual(ready["status"], "partial_ready")
            self.assertEqual(len(ready["failed_ranges"]), 1)

    def test_delta_is_revision_gated_and_contains_no_missed_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = self.make_builder(Path(temporary) / "packs")
            manifest = builder.initialize(VIDEO_URL, caption_document(), playhead_sec=1000)
            updated = builder.build_one(manifest["pack_id"])
            delta = builder.delta(manifest["pack_id"], since_revision=0, playhead_sec=1000)
            self.assertTrue(delta["changed"])
            self.assertEqual(delta["revision"], updated["revision"])
            self.assertTrue(delta["captions"])
            self.assertTrue(delta["candidates"])
            same = builder.delta(manifest["pack_id"], since_revision=delta["revision"], playhead_sec=1000)
            self.assertFalse(same["changed"])

    def test_rejects_long_video_without_timed_english_captions(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = self.make_builder(Path(temporary) / "packs")
            document = caption_document()
            for row in document["segments"]:
                row["text_en"] = None
            with self.assertRaisesRegex(SourceValidationError, "long_video_english_captions_required"):
                builder.initialize(VIDEO_URL, document)


if __name__ == "__main__":
    unittest.main()
