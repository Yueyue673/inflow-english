from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from caption_preview import _clean_english_caption, _known_sound_translation, _merge_caption_fragments, _merge_tracks, _sound_prefix, build_caption_preview, caption_preview_id
from video_pack import SourceValidationError, VideoPackError, validate_source_metadata, validate_youtube_url


URL = "https://www.youtube.com/watch?v=abcdefghijk"


def json3(lines):
    events = []
    for index, (start, end, text) in enumerate(lines):
        events.append({
            "tStartMs": int(start * 1000),
            "dDurationMs": int((end - start) * 1000),
            "segs": [{"utf8": text}],
            "wWinId": index,
        })
    return {"events": events}


class FakeMedia:
    def __init__(self, *, duration=1800.0, english=True, chinese=False):
        self.duration = duration
        self.english = english
        self.chinese = chinese
        self.calls = []

    def inspect(self, url):
        self.calls.append("inspect")
        return {
            "id": "abcdefghijk",
            "availability": "public",
            "live_status": "not_live",
            "is_live": False,
            "was_live": False,
            "duration": self.duration,
            "age_limit": 0,
            "title": "Caption first fixture",
            "categories": ["Education"],
            "webpage_url": URL,
        }

    def download_english_captions(self, url, destination):
        self.calls.append("english")
        if not self.english:
            raise VideoPackError("english_auto_captions_missing")
        destination.write_text(json.dumps(json3([(0, 2, "Hello there."), (2, 4, "We keep watching.")])), encoding="utf-8")

    def download_chinese_captions(self, url, destination):
        self.calls.append("chinese")
        if not self.chinese:
            raise VideoPackError("chinese_auto_captions_missing")
        destination.write_text(json.dumps(json3([(0, 2, "你好。"), (2, 4, "我们继续观看。")])), encoding="utf-8")


class FakeTranslator:
    def __init__(self):
        self.calls = []

    def translate_all(self, rows, *, batch_size, max_workers):
        self.calls.append([dict(row) for row in rows])
        return {row["cue_id"]: f"译文{index}" for index, row in enumerate(rows, start=1)}


class CaptionPreviewTests(unittest.TestCase):
    def test_english_is_atomically_published_before_chinese_translation(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "preview.json"
            media = FakeMedia(chinese=False)
            translator = FakeTranslator()
            snapshots = []
            result = build_caption_preview(
                URL,
                output,
                media_tools=media,
                translator=translator,
                on_publish=lambda payload: snapshots.append(deepcopy(payload)),
            )
            self.assertGreaterEqual(len(snapshots), 2)
            self.assertEqual(snapshots[0]["status"], "english_ready")
            self.assertTrue(all(row["text_en"] for row in snapshots[0]["segments"]))
            self.assertTrue(all(row["text_zh"] is None for row in snapshots[0]["segments"]))
            self.assertEqual(result["status"], "ready")
            self.assertTrue(all(row["text_zh"] for row in result["segments"]))
            self.assertEqual(len(translator.calls), 1)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["revision"], result["revision"])

    def test_existing_chinese_track_replaces_english_without_calling_translator(self):
        with tempfile.TemporaryDirectory() as temporary:
            media = FakeMedia(chinese=True)
            translator = FakeTranslator()
            snapshots = []
            result = build_caption_preview(
                URL,
                Path(temporary) / "preview.json",
                media_tools=media,
                translator=translator,
                on_publish=lambda payload: snapshots.append(deepcopy(payload)),
            )
            self.assertEqual(len(snapshots), 2)
            self.assertEqual(snapshots[0]["status"], "english_ready")
            self.assertEqual(snapshots[0]["translated_segments"], 0)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["coverage_fraction"], 1.0)
            self.assertEqual(translator.calls, [])

    def test_caption_preview_accepts_thirty_minutes_while_v1_learning_pack_does_not(self):
        reference = validate_youtube_url(URL)
        raw = FakeMedia(duration=1800).inspect(URL)
        with self.assertRaisesRegex(SourceValidationError, "source_duration_out_of_range"):
            validate_source_metadata(reference, raw)
        with tempfile.TemporaryDirectory() as temporary:
            result = build_caption_preview(URL, Path(temporary) / "preview.json", media_tools=FakeMedia(duration=1800), translator=FakeTranslator())
        self.assertEqual(result["duration_sec"], 1800.0)

    def test_neither_timed_track_never_publishes_empty_caption_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "preview.json"
            with self.assertRaisesRegex(VideoPackError, "timed_captions_unavailable"):
                build_caption_preview(URL, output, media_tools=FakeMedia(english=False, chinese=False), translator=FakeTranslator())
            self.assertFalse(output.exists())

    def test_overlapping_chinese_fragments_are_combined_not_randomly_paired(self):
        english = [{"id": "e1", "start": 1.0, "end": 5.0, "text_en": "[Music] there's a word in Korean", "text_zh": None}]
        chinese = [
            {"id": "z0", "start": 0.0, "end": 2.0, "text_en": None, "text_zh": "[掌声]"},
            {"id": "z1", "start": 1.0, "end": 2.0, "text_en": None, "text_zh": "[音乐]"},
            {"id": "z2", "start": 2.0, "end": 5.0, "text_en": None, "text_zh": "韩语里有个词"},
        ]
        merged = _merge_tracks(english, chinese)
        self.assertEqual(merged[0]["text_zh"], "[音乐] 韩语里有个词")

    def test_marginal_previous_caption_tail_is_not_merged_into_current_sentence(self):
        english = [{"id": "e1", "start": 18.98, "end": 22.62, "text_en": "Every no brings you one step closer to a yes.", "text_zh": None}]
        chinese = [
            {"id": "z7", "start": 14.16, "end": 19.279, "text_en": None, "text_zh": "没有被忽略过，就没有被选中的机会。"},
            {"id": "z8", "start": 16.56, "end": 23.039, "text_en": None, "text_zh": "。 每一次"},
            {"id": "z9", "start": 19.279, "end": 25.72, "text_en": None, "text_zh": "拒绝都让你离成功更近一步。"},
        ]
        merged = _merge_tracks(english, chinese)
        self.assertEqual(merged[0]["text_zh"], "每一次拒绝都让你离成功更近一步。")
        self.assertNotIn("忽略", merged[0]["text_zh"])

    def test_chinese_fragments_join_without_artificial_word_spaces(self):
        self.assertEqual(_merge_caption_fragments(["每一次", "被拒绝", "都让你更近一步"]), "每一次被拒绝都让你更近一步")
        self.assertEqual(_merge_caption_fragments(["[音乐]", "韩语里有个词"]), "[音乐] 韩语里有个词")

    def test_youtube_foreign_marker_does_not_pollute_sound_caption(self):
        self.assertEqual(_clean_english_caption("foreign [Applause]"), "[Applause]")
        self.assertEqual(_known_sound_translation("foreign [Applause]"), "[掌声]")
        self.assertEqual(_sound_prefix("[Music] there's a word in Korean"), ("[音乐]", "there's a word in Korean"))
        self.assertEqual(_clean_english_caption("foreign policy matters"), "foreign policy matters")

    def test_preview_id_is_stable_and_video_namespaced(self):
        self.assertEqual(caption_preview_id(URL), caption_preview_id(URL))
        self.assertRegex(caption_preview_id(URL), r"^yt-abcdefghijk-[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
