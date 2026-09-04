from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import subprocess
import tempfile
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_pack import (
    BUILDER_VERSION,
    CREATE_NO_WINDOW,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    AuditError,
    AdaptiveFasterWhisperTranscriber,
    ArgosHeuristicTranslator,
    BuildCancelled,
    CacheIntegrityError,
    CueConstructionError,
    DshTranslator,
    OpenAICompatibleTranslator,
    FasterWhisperTranscriber,
    FallbackTranslator,
    GoogleHeuristicTranslator,
    SourceValidationError,
    SubprocessMediaTools,
    TranslationContractError,
    URLValidationError,
    VideoPackBuilder,
    VideoPackError,
    _locate_surface,
    _reliable_transcript_view,
    build_caption_aligned_cues,
    build_natural_cues,
    build_sparse_natural_cues,
    filter_sparse_cues_by_english_captions,
    parse_youtube_json3_captions,
    select_candidate_cue_shortlist,
    validate_candidate_review,
    validate_source_metadata,
    validate_translation_batch,
    validate_youtube_url,
    verify_pack_directory,
)


VIDEO_ID = "aQ89CG_Yu6I"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
FIXED_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)


def source_metadata(**overrides):
    payload = {
        "id": VIDEO_ID,
        "_type": "video",
        "title": "Offline fixture video",
        "duration": 120.0,
        "availability": "public",
        "is_live": False,
        "was_live": False,
        "live_status": "not_live",
        "age_limit": 0,
        "uploader": "Fixture channel",
        "channel_id": "fixture-channel",
        "upload_date": "20260903",
        "language": "en",
        "categories": ["Education"],
        "webpage_url": VIDEO_URL,
    }
    payload.update(overrides)
    return payload


def timed_word(text, start, end):
    return {"word": text, "start": start, "end": end, "probability": 0.99}


def sample_transcript():
    return {
        "language": "en",
        "language_probability": 0.98,
        "duration_sec": 12.0,
        "segments": [
            {
                "id": 0,
                "words": [
                    timed_word("Curious", 0.40, 0.85),
                    timed_word("explorers", 0.90, 1.35),
                    timed_word("study", 1.40, 1.75),
                    timed_word("distant", 1.80, 2.25),
                    timed_word("worlds.", 2.30, 2.90),
                ],
            },
            {
                "id": 1,
                "words": [
                    timed_word("Careful", 3.20, 3.65),
                    timed_word("listeners", 3.70, 4.20),
                    timed_word("notice", 4.25, 4.65),
                    timed_word("subtle", 4.70, 5.15),
                    timed_word("changes.", 5.20, 5.80),
                ],
            },
            {
                "id": 2,
                "words": [
                    timed_word("Reliable", 6.10, 6.55),
                    timed_word("signals", 6.60, 7.05),
                    timed_word("guide", 7.10, 7.50),
                    timed_word("every", 7.55, 8.00),
                    timed_word("decision.", 8.05, 8.80),
                ],
            },
            {
                "id": 3,
                "words": [
                    timed_word("Patient", 9.10, 9.55),
                    timed_word("practice", 9.60, 10.05),
                    timed_word("builds", 10.10, 10.50),
                    timed_word("lasting", 10.55, 11.00),
                    timed_word("confidence.", 11.05, 11.80),
                ],
            },
        ],
    }


def valid_payload(cues):
    translations = [
        {"cue_id": cue["cue_id"], "text_zh": f"第{index}条自然中文字幕"}
        for index, cue in enumerate(cues, start=1)
    ]
    candidates = []
    for cue in cues[:4]:
        surface = cue["text"].split(" ", 1)[0].strip(".,!?;:")
        candidates.append(
            {
                "cue_id": cue["cue_id"],
                "surface": surface,
                "gloss_zh": "当前语境义项",
                "value_score": 4.0,
            }
        )
    return {"translations": translations, "candidates": candidates}


class FakeTranslator:
    identity = "fake-translator:v1"

    def __init__(self, transform=None):
        self.calls = 0
        self.transform = transform

    def translate(self, cues):
        self.calls += 1
        payload = valid_payload(cues)
        if self.transform:
            payload = self.transform(payload)
        return payload


class FakeTranscriber:
    identity = "fake-faster-whisper:word-timestamps"

    def __init__(self, transcript=None):
        self.calls = 0
        self.transcript = transcript or sample_transcript()

    def transcribe(self, wav_path, *, cancel_callback=None):
        self.calls += 1
        if cancel_callback and cancel_callback():
            raise BuildCancelled("cancelled_in_fake_transcriber")
        return copy.deepcopy(self.transcript)


class FakeMediaTools:
    identity = "fake-media:720p:16khz"

    def __init__(self, metadata=None):
        self.metadata = metadata or source_metadata()
        self.calls = {"inspect": 0, "download": 0, "extract_audio": 0, "probe": 0, "clip": 0}
        self.clip_durations = {}

    def inspect(self, canonical_url):
        self.calls["inspect"] += 1
        return copy.deepcopy(self.metadata)

    def download(self, canonical_url, destination):
        self.calls["download"] += 1
        destination.write_bytes(b"offline-mp4" * 64)

    def extract_audio(self, video_path, wav_path):
        self.calls["extract_audio"] += 1
        wav_path.write_bytes(b"offline-wav" * 64)

    def probe(self, media_path):
        self.calls["probe"] += 1
        if media_path.suffix.lower() == ".mp4":
            return {"duration_sec": 120.0, "height": 720, "has_video": True, "has_audio": True}
        return {
            "duration_sec": self.clip_durations[Path(media_path)],
            "height": None,
            "has_video": False,
            "has_audio": True,
        }

    def clip_phrase(self, video_path, start_sec, end_sec, destination):
        self.calls["clip"] += 1
        destination.write_bytes((f"offline-mp3-{start_sec:.3f}-{end_sec:.3f}").encode("ascii") * 16)
        self.clip_durations[Path(destination)] = round(end_sec - start_sec, 3)


class VideoPackContractTests(unittest.TestCase):
    def make_builder(self, root, *, translator=None, transcriber=None, media=None):
        return VideoPackBuilder(
            root,
            translator=translator or FakeTranslator(),
            transcriber=transcriber or FakeTranscriber(),
            media_tools=media or FakeMediaTools(),
            clock=lambda: FIXED_NOW,
        )

    def test_openai_adapter_is_explicit_and_never_exposes_its_key(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                content = json.dumps({"translations": [], "candidates": []})
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

        class Opener:
            request = None
            timeout = None

            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        secret = "test-secret-must-not-escape"
        translator = OpenAICompatibleTranslator(
            endpoint="https://api.openai.example/v1/chat/completions",
            model="gpt-test",
            api_key=secret,
            timeout_sec=9,
            opener=opener,
        )
        payload = translator._invoke_once("return json")
        self.assertEqual(payload, {"translations": [], "candidates": []})
        self.assertEqual(opener.timeout, 9)
        self.assertEqual(opener.request.get_header("Authorization"), f"Bearer {secret}")
        self.assertNotIn(secret, translator.identity)
        self.assertNotIn(secret, opener.request.data.decode("utf-8"))
        with self.assertRaises(ValueError):
            OpenAICompatibleTranslator(endpoint="http://api.openai.example/v1/chat/completions", model="gpt-test")

    def test_default_builder_never_invokes_dsh_or_exact_sense_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = VideoPackBuilder(Path(temporary), transcriber=FakeTranscriber(), media_tools=FakeMediaTools())
        self.assertIsInstance(builder.translator, FallbackTranslator)
        self.assertIsInstance(builder.translator.primary, GoogleHeuristicTranslator)
        self.assertIsInstance(builder.translator.fallback, ArgosHeuristicTranslator)
        self.assertNotIsInstance(builder.translator.primary, DshTranslator)
        self.assertNotIn("dsh", builder.translator.identity.casefold())
        self.assertEqual(builder.translator.resolve_senses([{"occurrence_id": "x", "surface": "bank", "phrase_text": "by the bank"}]), {})

    def test_strict_youtube_url_acceptance_and_rejection(self):
        expected = validate_youtube_url(VIDEO_URL)
        self.assertEqual(expected.video_id, VIDEO_ID)
        self.assertEqual(expected.canonical_url, VIDEO_URL)
        shared = validate_youtube_url(f"https://youtu.be/{VIDEO_ID}?si=offline")
        self.assertEqual(shared, expected)

        rejected = [
            f"http://www.youtube.com/watch?v={VIDEO_ID}",
            f"https://www.youtube.com/shorts/{VIDEO_ID}",
            f"https://www.youtube.com/live/{VIDEO_ID}",
            f"https://www.youtube.com/embed/{VIDEO_ID}",
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123",
            "https://www.youtube.com/watch?v=too-short",
            f"https://music.youtube.com/watch?v={VIDEO_ID}",
            f"https://user:password@www.youtube.com/watch?v={VIDEO_ID}",
            f"https://www.youtube.com:443/watch?v={VIDEO_ID}",
            f"https://example.com/watch?v={VIDEO_ID}",
            f" {VIDEO_URL}",
            f"{VIDEO_URL}#fragment",
        ]
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(URLValidationError):
                validate_youtube_url(url)

    def test_youtube_chinese_json3_keeps_rolling_segments_for_latest_line_selection(self):
        payload = {
            "events": [
                {"tStartMs": 1000, "dDurationMs": 4000, "segs": [{"utf8": "第一行"}]},
                {"tStartMs": 2500, "dDurationMs": 3500, "segs": [{"utf8": "第二行"}]},
                {"tStartMs": 6000, "segs": [{"utf8": "third without Chinese"}]},
            ]
        }
        rows = parse_youtube_json3_captions(payload, duration_sec=10.0)
        self.assertEqual([row["text"] for row in rows], ["第一行", "第二行"])
        self.assertLess(rows[1]["start"], rows[0]["end"])
        self.assertEqual([row["id"] for row in rows], ["ytzh-0001", "ytzh-0002"])

    def test_candidate_shortlist_is_bounded_distributed_and_safe(self):
        cues = []
        for index in range(36):
            cues.append(
                {
                    "cue_id": f"cue-{index:04d}",
                    "text": f"Observers documented extraordinary phenomenon {index}.",
                    "start_sec": float(index * 10),
                    "end_sec": float(index * 10 + 4),
                    "duration_sec": 4.0,
                    "candidate_allowed": index != 18,
                }
            )
        selected = select_candidate_cue_shortlist(cues)
        self.assertEqual(len(selected), 24)
        self.assertNotIn("cue-0018", {row["cue_id"] for row in selected})
        self.assertLess(float(selected[0]["start_sec"]), 100.0)
        self.assertGreater(float(selected[-1]["start_sec"]), 250.0)

    def test_failed_youtube_captions_fall_back_to_batched_model_captions(self):
        class CaptionFailingMedia(FakeMediaTools):
            def download_chinese_captions(self, _url, _destination):
                raise SourceValidationError("chinese_auto_captions_missing")

        class BatchedTranslator(FakeTranslator):
            def translate_all(self, cues, **_kwargs):
                return {str(cue["cue_id"]): f"{index}号完整中文字幕" for index, cue in enumerate(cues, start=1)}

        with tempfile.TemporaryDirectory() as temporary:
            builder = VideoPackBuilder(
                Path(temporary) / "packs",
                translator=BatchedTranslator(),
                transcriber=FakeTranscriber(),
                media_tools=CaptionFailingMedia(),
                clock=lambda: FIXED_NOW,
            )
            pack = builder.build(VIDEO_URL)
            captions = json.loads((pack / "captions-zh.json").read_text(encoding="utf-8"))
            self.assertEqual(captions["source"], "dsh_batched_transcript_translation")
            self.assertEqual(captions["fallback_reason"], "chinese_auto_captions_missing")
            self.assertEqual(len(captions["segments"]), 4)
            verify_pack_directory(pack)

    def test_caption_whisper_alignment_salvages_clustered_invalid_timestamps(self):
        sentences = [
            "Creative people practice every single day.",
            "Careful listeners notice subtle changes quickly.",
            "Useful references shape original ideas over time.",
            "Small experiments reveal surprising creative directions.",
            "Strong stories connect familiar details naturally.",
        ]
        segments = []
        events = []
        clock = 0.5
        for index, sentence in enumerate(sentences):
            words = []
            for token in sentence.rstrip(".").split():
                words.append(timed_word(token, clock, clock + 0.36))
                clock += 0.43
            segments.append({"id": index, "start": words[0]["start"], "end": words[-1]["end"], "text": sentence, "words": words})
            events.append({"tStartMs": round(words[0]["start"] * 1000), "dDurationMs": round((words[-1]["end"] - words[0]["start"]) * 1000), "segs": [{"utf8": sentence}]})
            clock += 0.32
        invalid_words = [timed_word(f"broken{index}", 100.0, 100.0) for index in range(12)]
        segments.append({"id": 99, "start": 100.0, "end": 100.1, "text": "broken timing cluster", "words": invalid_words})
        transcript = {"language": "en", "language_probability": 0.99, "segments": segments}
        cues = build_caption_aligned_cues(transcript, {"events": events}, video_duration_sec=120.0)
        self.assertGreaterEqual(len(cues), 4)
        self.assertTrue(all(cue["boundary_kind"] == "caption_aligned_sentence" for cue in cues))
        self.assertTrue(all(cue["caption_alignment_coverage"] == 1.0 for cue in cues))
        self.assertTrue(all(cue["pause_gap_sec"] >= 0.2 for cue in cues))
        self.assertTrue(all(cue["pause_delay_sec"] <= 5.0 for cue in cues))

    def test_low_ratio_invalid_words_are_removed_without_dropping_natural_segment(self):
        transcript = sample_transcript()
        transcript["segments"][0]["words"][0]["end"] = transcript["segments"][0]["words"][0]["start"]
        with tempfile.TemporaryDirectory() as temporary:
            packs_root = Path(temporary) / "packs"
            builder = VideoPackBuilder(
                packs_root,
                translator=FakeTranslator(),
                transcriber=FakeTranscriber(transcript),
                media_tools=FakeMediaTools(),
                clock=lambda: FIXED_NOW,
            )
            pack = builder.build(VIDEO_URL)
            document = json.loads((pack / "transcript.json").read_text(encoding="utf-8"))
            self.assertEqual(document["cue_source"], "whisper_natural_partition")
            self.assertEqual(document["timing_quality"]["excluded_segment_count"], 0)
            self.assertEqual(document["timing_quality"]["repaired_segment_count"], 1)
            self.assertEqual(document["timing_quality"]["repaired_removed_word_count"], 1)
            self.assertGreaterEqual(len(document["cues"]), 1)

    def test_high_ratio_invalid_words_still_isolate_the_segment(self):
        transcript = sample_transcript()
        words = transcript["segments"][0]["words"]
        for word in words[: max(1, len(words) // 2)]:
            word["end"] = word["start"]
        _view, quality = _reliable_transcript_view(transcript)
        self.assertEqual(quality["excluded_segment_count"], 1)
        self.assertEqual(quality["repaired_segment_count"], 0)
        self.assertEqual(quality["excluded_segment_ids"], [str(transcript["segments"][0]["id"])])

    def test_low_memory_selects_tiny_cpu_transcriber_without_loading_model(self):
        relevant = {
            "INFLOW_WHISPER_MODEL": None,
            "INFLOW_WHISPER_DEVICE": None,
            "INFLOW_WHISPER_COMPUTE": None,
            "INFLOW_WHISPER_CPU_THREADS": None,
        }
        original = {key: os.environ.get(key) for key in relevant}
        try:
            for key in relevant:
                os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as temporary, patch("video_pack._available_memory_bytes", return_value=4 * 1024**3):
                builder = VideoPackBuilder(Path(temporary) / "packs", translator=FakeTranslator(), media_tools=FakeMediaTools())
                self.assertIsInstance(builder.transcriber, AdaptiveFasterWhisperTranscriber)
                self.assertEqual(builder.transcriber.fallback.model_size, "tiny.en")
                self.assertEqual(builder.transcriber.fallback.device, "cpu")
                self.assertEqual(builder.transcriber.fallback.compute_type, "int8")
                self.assertEqual(builder.transcriber.fallback.cpu_threads, 1)
                self.assertTrue(builder.transcriber._prefer_fallback_now())
            with tempfile.TemporaryDirectory() as temporary, patch("video_pack._available_memory_bytes", return_value=12 * 1024**3):
                builder = VideoPackBuilder(Path(temporary) / "packs", translator=FakeTranslator(), media_tools=FakeMediaTools())
                self.assertFalse(builder.transcriber._prefer_fallback_now())
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_runtime_memory_failure_falls_back_with_stable_chain_identity(self):
        class Primary:
            identity = "primary-large"
            model_size = "whisper-large-v3"
            device = "cuda"
            compute_type = "float16"
            _model = object()

            def transcribe(self, _path, *, cancel_callback=None):
                raise VideoPackError("transcriber_memory_unavailable")

        class Fallback:
            identity = "fallback-tiny"
            model_size = "tiny.en"
            device = "cpu"
            compute_type = "int8"

            def transcribe(self, _path, *, cancel_callback=None):
                return {"language": "en", "segments": []}

            def audit_phrase(self, _path):
                return "fallback phrase"

        transcriber = AdaptiveFasterWhisperTranscriber(Primary(), Fallback())
        identity_before = transcriber.identity
        with patch("video_pack._available_memory_bytes", return_value=8 * 1024**3):
            result = transcriber.transcribe(Path("unused.wav"))
        self.assertEqual(result["language"], "en")
        self.assertEqual(transcriber.runtime_identity, "fallback-tiny")
        self.assertEqual(transcriber.identity, identity_before)
        self.assertEqual(transcriber.audit_phrase(Path("unused.mp3")), "fallback phrase")

    def test_transcriber_release_drops_loaded_models_without_forgetting_selected_runtime(self):
        primary = FasterWhisperTranscriber("large", device="cuda", compute_type="float16", model_factory=lambda *_args, **_kwargs: object())
        fallback = FasterWhisperTranscriber("tiny", device="cpu", compute_type="int8", model_factory=lambda *_args, **_kwargs: object())
        primary._model = object()
        fallback._model = object()
        adaptive = AdaptiveFasterWhisperTranscriber(primary, fallback)
        adaptive.active = fallback
        adaptive.release_model()
        self.assertIsNone(primary._model)
        self.assertIsNone(fallback._model)
        self.assertIs(adaptive.active, fallback)
        self.assertEqual(adaptive.runtime_identity, fallback.identity)

    def test_source_metadata_rejects_private_live_duration_and_music(self):
        reference = validate_youtube_url(VIDEO_URL)
        invalid_rows = [
            source_metadata(availability="private"),
            source_metadata(is_live=True, live_status="is_live"),
            source_metadata(was_live=True),
            source_metadata(duration=19.999),
            source_metadata(duration=900.001),
            source_metadata(categories=["Music"]),
            source_metadata(age_limit=18),
        ]
        for metadata in invalid_rows:
            with self.subTest(metadata=metadata), self.assertRaises(SourceValidationError):
                validate_source_metadata(reference, metadata)
        accepted = validate_source_metadata(reference, source_metadata(duration=20))
        self.assertEqual(accepted["duration_sec"], 20.0)

    def test_natural_cues_use_real_boundaries_and_merge_short_tail(self):
        transcript = {
            "segments": [
                {
                    "id": 0,
                    "words": [
                        timed_word("We", 0.00, 0.35),
                        timed_word("need", 0.40, 0.75),
                        timed_word("to", 0.80, 1.05),
                        timed_word("move", 1.10, 1.55),
                        timed_word("quickly.", 1.60, 2.40),
                    ],
                },
                {
                    "id": 1,
                    "words": [
                        timed_word("Right", 2.70, 3.05),
                        timed_word("now.", 3.10, 3.70),
                    ],
                },
                {
                    "id": 2,
                    "words": [
                        timed_word("The", 3.90, 4.20),
                        timed_word("weather", 4.25, 4.75),
                        timed_word("is", 4.80, 5.00),
                        timed_word("changing", 5.05, 5.70),
                        timed_word("fast.", 5.75, 6.50),
                    ],
                },
            ]
        }
        cues = build_natural_cues(transcript)
        self.assertEqual(len(cues), 2)
        self.assertEqual(
            " ".join(cue["text"] for cue in cues),
            "We need to move quickly. Right now. The weather is changing fast.",
        )
        self.assertNotIn("Right now.", [cue["text"] for cue in cues])
        self.assertTrue(all(2.0 <= cue["duration_sec"] <= 8.0 for cue in cues))
        self.assertTrue(all(cue["text"].endswith((".", "?", "!")) for cue in cues))
        self.assertNotIn("to", [cue["text"].split()[-1] for cue in cues])

    def test_natural_cues_refuse_arbitrary_hard_cut(self):
        words = [timed_word(f"word{index}", index * 1.0, index * 1.0 + 0.95) for index in range(10)]
        with self.assertRaises(CueConstructionError):
            build_natural_cues({"segments": [{"id": 0, "words": words}]})

    def test_sparse_cue_can_wait_for_later_safe_pause_without_extending_phrase(self):
        first_words = [
            timed_word("This", 0.0, 0.4),
            timed_word("sentence", 0.5, 1.0),
            timed_word("ends", 1.1, 1.5),
            timed_word("clearly.", 1.6, 2.0),
        ]
        second_words = [
            timed_word("Then", 2.05, 2.45),
            timed_word("speech", 2.5, 2.9),
            timed_word("continues", 3.0, 3.5),
            timed_word("briefly.", 3.6, 4.0),
        ]
        third_words = [
            timed_word("Another", 4.5, 4.9),
            timed_word("sentence", 5.0, 5.5),
            timed_word("ends", 5.6, 6.0),
            timed_word("later.", 6.1, 6.5),
        ]
        transcript = {
            "segments": [
                {"id": "a", "text": "This sentence ends clearly.", "words": first_words},
                {"id": "b", "text": "Then speech continues briefly.", "words": second_words},
                {"id": "c", "text": "Another sentence ends later.", "words": third_words},
            ]
        }
        immediate_only = build_sparse_natural_cues(transcript)
        delayed = build_sparse_natural_cues(transcript, max_pause_delay_sec=5.0)
        self.assertNotIn("This sentence ends clearly.", {row["text"] for row in immediate_only})
        first = next(row for row in delayed if row["text"] == "This sentence ends clearly.")
        self.assertEqual(first["end_sec"], 2.0)
        self.assertGreater(first["pause_anchor_sec"], 4.0)
        self.assertLessEqual(first["pause_delay_sec"], 5.0)
        self.assertGreaterEqual(first["pause_gap_sec"], 0.2)

    def test_sparse_cues_keep_local_natural_segments_without_covering_fragments(self):
        transcript = {
            "segments": [
                {"id": "fragment", "words": [timed_word("and", 0.0, 0.3), timed_word("unfinished", 0.35, 0.9), timed_word("thought", 0.95, 1.4)]},
                {"id": "good-1", "words": [timed_word("How", 2.0, 2.4), timed_word("do", 2.45, 2.7), timed_word("you", 2.75, 3.0), timed_word("imagine", 3.05, 3.6), timed_word("it'll", 3.65, 4.0), timed_word("be?", 4.05, 4.5)]},
                {"id": "comma", "words": [timed_word("I", 5.1, 5.3), timed_word("think", 5.35, 5.7), timed_word("adults", 5.75, 6.2), timed_word("are", 6.25, 6.5), timed_word("boss,", 6.55, 7.1)]},
                {"id": "good-2", "words": [timed_word("What", 8.0, 8.35), timed_word("will", 8.4, 8.7), timed_word("stay", 8.75, 9.1), timed_word("with", 9.15, 9.4), timed_word("you?", 9.45, 10.0)]},
            ]
        }
        cues = build_sparse_natural_cues(transcript)
        self.assertEqual([cue["text"] for cue in cues], ["How do you imagine it'll be?", "What will stay with you?"])
        self.assertTrue(all(cue["boundary_kind"] == "whisper_sparse_segment" for cue in cues))
        self.assertTrue(all(cue["candidate_allowed"] for cue in cues))

    def test_sparse_caption_corroboration_rejects_asr_content_word_substitution(self):
        cues = [
            {"cue_id": "cue-0001", "start_sec": 1.0, "end_sec": 4.0, "text": "You may see a pile of proceeds."},
            {"cue_id": "cue-0002", "start_sec": 8.0, "end_sec": 11.0, "text": "The auditor will call tomorrow."},
        ]
        captions = {
            "events": [
                {"tStartMs": 800, "dDurationMs": 4000, "segs": [{"utf8": "You may see a pile of receipts."}]},
                {"tStartMs": 7800, "dDurationMs": 4000, "segs": [{"utf8": "The auditor will call tomorrow."}]},
            ]
        }
        kept = filter_sparse_cues_by_english_captions(cues, captions)
        self.assertEqual([row["text"] for row in kept], ["The auditor will call tomorrow."])
        self.assertEqual(kept[0]["english_caption_alignment_coverage"], 1.0)

    def test_natural_cues_do_not_end_on_dangling_preposition(self):
        transcript = {
            "segments": [
                {
                    "id": 0,
                    "words": [
                        timed_word("We", 0.0, 0.4),
                        timed_word("are", 0.45, 0.8),
                        timed_word("ready", 0.85, 1.5),
                        timed_word("to", 1.55, 2.2),
                    ],
                },
                {
                    "id": 1,
                    "words": [
                        timed_word("move", 2.3, 2.8),
                        timed_word("across", 2.85, 3.4),
                        timed_word("the", 3.45, 3.75),
                        timed_word("valley.", 3.8, 4.6),
                    ],
                },
            ]
        }
        cues = build_natural_cues(transcript)
        self.assertEqual([cue["text"] for cue in cues], ["We are ready to move across the valley."])
        self.assertFalse(any(cue["text"].endswith(" to") for cue in cues))

    def test_longer_video_candidate_contract_accepts_more_than_eight_without_forcing_fill(self):
        transcript = {"segments": []}
        terms = [
            "adaptability", "clarity", "deliberately", "resourceful", "nuance", "sustainable",
            "perspective", "resilient", "coherent", "initiative", "constraint", "iterate",
        ]
        for index, term in enumerate(terms):
            start = index * 3.2
            transcript["segments"].append(
                {
                    "id": index,
                    "words": [
                        timed_word(term.capitalize(), start, start + 0.55),
                        timed_word("shapes", start + 0.60, start + 1.05),
                        timed_word("better", start + 1.10, start + 1.55),
                        timed_word("creative", start + 1.60, start + 2.10),
                        timed_word("work.", start + 2.15, start + 2.70),
                    ],
                }
            )
        cues = build_natural_cues(transcript)
        self.assertEqual(len(cues), 12)
        payload = {
            "translations": [{"cue_id": cue["cue_id"], "text_zh": f"第{index}条翻译"} for index, cue in enumerate(cues)],
            "candidates": [
                {"cue_id": cue["cue_id"], "surface": cue["text"].split()[0], "gloss_zh": "有用表达", "value_score": 4.0}
                for cue in cues
            ],
        }
        validated = validate_translation_batch(cues, payload)
        self.assertEqual(len(validated.candidates), 12)
        prompt_cues = [{"cue_id": f"prompt-{index:04d}", "text": "A useful expression appears here.", "candidate_allowed": True} for index in range(24)]
        self.assertIn("between 1 and 10", DshTranslator._prompt(prompt_cues))

    def test_google_heuristic_fallback_is_offline_testable_and_surface_grounded(self):
        class OfflineGoogle(GoogleHeuristicTranslator):
            def _translate_text(self, text):
                return "译：" + str(text)

            def _translate_batch_texts(self, texts):
                return ["译：" + str(text) for text in texts]

        cues = build_natural_cues(sample_transcript())
        translator = OfflineGoogle(proxy="")
        self.assertEqual(translator._candidate_options("I understand your questions."), [])
        advanced = translator._candidate_options("The auditor noticed a stark difference.")
        self.assertEqual(advanced[0][1], "stark difference")
        payload = translator.translate(cues)
        validated = validate_translation_batch(cues, payload)
        self.assertGreaterEqual(len(validated.candidates), 1)
        cue_text = {cue["cue_id"]: cue["text"] for cue in cues}
        self.assertTrue(all(row.surface in cue_text[row.cue_id] for row in validated.candidates))
        reviewed = translator.review_selected([
            {
                "cue_id": row.cue_id,
                "surface": row.surface,
                "phrase_text": cue_text[row.cue_id],
                "gloss_zh": row.gloss_zh,
                "phrase_zh": payload["translations"][0]["text_zh"],
            }
            for row in validated.candidates
        ])
        self.assertTrue(all(item["gloss_zh"].startswith("译：") for item in reviewed["items"]))

    def test_google_fallback_batches_twelve_rows_with_verified_markers(self):
        class TaggedGoogle(GoogleHeuristicTranslator):
            def __init__(self):
                super().__init__(proxy="")
                self.calls = []

            def _translate_text(self, text):
                self.calls.append(text)
                if not str(text).startswith("INFLOW"):
                    return "第 0 句翻译"
                lines = []
                for index, line in enumerate(str(text).splitlines()):
                    marker = line.split(":", 1)[0]
                    lines.append(f"{marker}：第{index}句翻译")
                return "\n".join(lines)

        translator = TaggedGoogle()
        rows = [{"cue_id": f"cue-{index:02d}", "text": f"Sentence number {index}."} for index in range(13)]
        translated = translator.translate_all(rows)
        self.assertEqual(len(translated), 13)
        self.assertEqual(len(translator.calls), 2)
        self.assertEqual(translated["cue-00"], "第 0 句翻译")
        self.assertEqual(translated["cue-12"], "第 0 句翻译")

    def test_argos_adapter_batches_text_and_reuses_memory_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python_path = root / "python.exe"
            worker_path = root / "worker.py"
            package_dir = root / "packages"
            python_path.write_bytes(b"test")
            worker_path.write_text("# test", encoding="utf-8")
            package_dir.mkdir()
            calls = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                texts = json.loads(kwargs["input"])["texts"]
                return SimpleNamespace(returncode=0, stdout=json.dumps({"translations": [f"译文{index}" for index, _ in enumerate(texts)]}, ensure_ascii=False))

            translator = ArgosHeuristicTranslator(
                python_executable=python_path,
                worker_path=worker_path,
                package_dir=package_dir,
                runner=runner,
            )
            rows = [{"cue_id": "a", "text": "First sentence."}, {"cue_id": "b", "text": "Second sentence."}]
            first = translator.translate_all(rows)
            second = translator.translate_all(rows)
            self.assertEqual(first, {"a": "译文 0", "b": "译文 1"})
            self.assertEqual(second, first)
            self.assertEqual(len(calls), 1)
            command, kwargs = calls[0]
            self.assertEqual(command, [str(python_path), str(worker_path)])
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["env"]["ARGOS_PACKAGES_DIR"], str(package_dir))

    def test_argos_runtime_failure_retries_once_but_missing_model_does_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python_path = root / "python.exe"
            worker_path = root / "worker.py"
            package_dir = root / "packages"
            python_path.write_bytes(b"test")
            worker_path.write_text("# test", encoding="utf-8")
            package_dir.mkdir()
            calls = []

            def transient_runner(_command, **_kwargs):
                calls.append(1)
                if len(calls) == 1:
                    return SimpleNamespace(returncode=2, stdout="", stderr=json.dumps({"ok": False, "error_code": "argos_translation_runtime_failed"}))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"translations": ["成功翻译"]}), stderr="")

            translator = ArgosHeuristicTranslator(python_executable=python_path, worker_path=worker_path, package_dir=package_dir, runner=transient_runner)
            self.assertEqual(translator.translate_all([{"cue_id": "a", "text": "Test sentence."}]), {"a": "成功翻译"})
            self.assertEqual(len(calls), 2)

            missing_calls = []

            def missing_runner(_command, **_kwargs):
                missing_calls.append(1)
                return SimpleNamespace(returncode=2, stdout="", stderr=json.dumps({"ok": False, "error_code": "argos_en_zh_model_missing"}))

            missing = ArgosHeuristicTranslator(python_executable=python_path, worker_path=worker_path, package_dir=package_dir, runner=missing_runner)
            with self.assertRaisesRegex(TranslationContractError, "argos_en_zh_model_missing"):
                missing.translate_all([{"cue_id": "a", "text": "Test sentence."}])
            self.assertEqual(len(missing_calls), 1)

    def test_translator_chain_switches_once_after_primary_failure(self):
        class BrokenPrimary:
            identity = "broken-primary"

            def translate(self, _cues):
                raise TranslationContractError("quota")

        class WorkingFallback:
            identity = "working-fallback"

            def __init__(self):
                self.calls = 0

            def translate(self, cues):
                self.calls += 1
                return valid_payload(cues)

        fallback = WorkingFallback()
        chain = FallbackTranslator(BrokenPrimary(), fallback)
        cues = build_natural_cues(sample_transcript())
        first = chain.translate(cues)
        second = chain.translate(cues)
        self.assertEqual(len(first["candidates"]), 4)
        self.assertEqual(len(second["candidates"]), 4)
        self.assertEqual(fallback.calls, 2)
        self.assertEqual(chain.runtime_identity, "working-fallback")

        nested = FallbackTranslator(BrokenPrimary(), FallbackTranslator(BrokenPrimary(), fallback))
        nested.translate(cues)
        self.assertEqual(nested.runtime_identity, "working-fallback")

    def test_candidate_review_can_only_change_chinese_fields(self):
        requested = [
            {
                "cue_id": "cue-0001",
                "surface": "yard sale",
                "phrase_text": "He bought it at a yard sale.",
                "gloss_zh": "旧货摊",
                "phrase_zh": "他在旧货摊买的。",
            }
        ]
        valid = {"items": [{"cue_id": "cue-0001", "surface": "yard sale", "gloss_zh": "庭院旧货出售", "phrase_zh": "他是在一次庭院旧货出售中买到的。"}]}
        reviewed = validate_candidate_review(requested, valid)
        self.assertEqual(reviewed[("cue-0001", "yard sale")]["gloss_zh"], "庭院旧货出售")
        changed_surface = copy.deepcopy(valid)
        changed_surface["items"][0]["surface"] = "garage sale"
        with self.assertRaisesRegex(TranslationContractError, "identity_changed"):
            validate_candidate_review(requested, changed_surface)
        extra_timing = copy.deepcopy(valid)
        extra_timing["items"][0]["start_sec"] = 1.0
        with self.assertRaisesRegex(TranslationContractError, "row_invalid"):
            validate_candidate_review(requested, extra_timing)

    def test_sense_selector_failure_keeps_pack_building_with_safe_keys(self):
        class FailingSenseTranslator(FakeTranslator):
            def resolve_senses(self, _rows):
                raise TranslationContractError("synthetic_quota_failure")

        with tempfile.TemporaryDirectory() as temporary:
            builder = self.make_builder(Path(temporary), translator=FailingSenseTranslator())
            pack = builder.build(VIDEO_URL)
            manifest = verify_pack_directory(pack, require_audit=True)
            self.assertGreater(len(manifest["candidates"]), 0)
            self.assertTrue(all(row["knowledge_key"].startswith(("kv1|", "prov1|")) for row in manifest["candidates"]))
            self.assertTrue(all(row["sense_status"] in {"resolved", "ambiguous", "provisional"} for row in manifest["candidates"]))

    def test_candidate_cannot_select_inside_hyphenated_compound(self):
        tokens = ["We", "are", "co", "-designing", "across", "layers."]
        words = [timed_word(token, index * 0.5, index * 0.5 + 0.4) for index, token in enumerate(tokens)]
        cue = build_natural_cues({"segments": [{"id": 0, "words": words}]})[0]
        with self.assertRaisesRegex(TranslationContractError, "inside_hyphenated_unit"):
            _locate_surface(cue, "designing")

    def test_candidate_without_200ms_safe_pause_is_rejected(self):
        cues = build_natural_cues(sample_transcript())
        cues[0]["following_gap_sec"] = 0.08
        cues[0]["candidate_allowed"] = False
        payload = valid_payload(cues)
        with self.assertRaisesRegex(TranslationContractError, "safe_pause"):
            validate_translation_batch(cues, payload)

    def test_random_and_hallucinated_surfaces_and_model_timing_are_rejected(self):
        cues = build_natural_cues(sample_transcript())
        payload = valid_payload(cues)
        validate_translation_batch(cues, payload)

        rng = random.Random(20260903)
        for _ in range(20):
            hallucination = "hallucinated-" + "".join(rng.choice("abcdef0123456789") for _ in range(12))
            bad = copy.deepcopy(payload)
            bad["candidates"][0]["surface"] = hallucination
            with self.assertRaises(TranslationContractError):
                validate_translation_batch(cues, bad)

        extra_timing = copy.deepcopy(payload)
        extra_timing["candidates"][0]["start_sec"] = 0.4
        with self.assertRaises(TranslationContractError):
            validate_translation_batch(cues, extra_timing)

        partial_word = copy.deepcopy(payload)
        partial_word["candidates"][0]["surface"] = "Curio"
        with self.assertRaises(TranslationContractError):
            validate_translation_batch(cues, partial_word)

        too_many_words = copy.deepcopy(payload)
        too_many_words["candidates"][0]["surface"] = "Curious explorers study distant worlds"
        filtered = validate_translation_batch(cues, too_many_words)
        self.assertEqual(len(filtered.candidates), 3)
        self.assertNotIn("Curious explorers study distant worlds", {row.surface for row in filtered.candidates})

        duplicate_cue = copy.deepcopy(payload)
        duplicate_cue["candidates"][1]["cue_id"] = cues[0]["cue_id"]
        duplicate_cue["candidates"][1]["surface"] = "explorers"
        deduplicated = validate_translation_batch(cues, duplicate_cue)
        self.assertEqual(len(deduplicated.candidates), 3)

    def test_cancel_removes_staging_and_never_creates_final_pack(self):
        with tempfile.TemporaryDirectory() as temporary:
            packs_root = Path(temporary) / "packs"
            builder = self.make_builder(packs_root)
            current_stage = {"value": None}

            def progress(event):
                current_stage["value"] = event["stage"]

            def cancel():
                return current_stage["value"] == "clipping"

            with self.assertRaises(BuildCancelled):
                builder.build(VIDEO_URL, progress_callback=progress, cancel_callback=cancel)
            self.assertTrue(packs_root.is_dir())
            self.assertEqual(list(packs_root.iterdir()), [])

    def test_translation_failure_never_creates_final_pack(self):
        def hallucinate(payload):
            payload["candidates"][0]["surface"] = "not in any cue"
            return payload

        with tempfile.TemporaryDirectory() as temporary:
            packs_root = Path(temporary) / "packs"
            builder = self.make_builder(packs_root, translator=FakeTranslator(hallucinate))
            with self.assertRaises(TranslationContractError):
                builder.build(VIDEO_URL)
            self.assertEqual(list(packs_root.iterdir()), [])

    def test_atomic_commit_happens_once_after_complete_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            packs_root = Path(temporary) / "packs"
            builder = self.make_builder(packs_root)
            real_replace = os.replace
            with patch("video_pack.os.replace", wraps=real_replace) as replace_spy:
                final_path = builder.build(VIDEO_URL)
            self.assertEqual(replace_spy.call_count, 1)
            staging_arg, final_arg = replace_spy.call_args.args
            self.assertIn(".staging-", Path(staging_arg).name)
            self.assertEqual(Path(final_arg), final_path)
            self.assertFalse(Path(staging_arg).exists())
            self.assertTrue(final_path.is_dir())
            self.assertEqual([path for path in packs_root.iterdir() if ".staging-" in path.name], [])
            manifest = verify_pack_directory(final_path)
            self.assertEqual(manifest["status"], "ready")
            audit = json.loads((final_path / "audit.json").read_text(encoding="utf-8"))
            self.assertTrue(audit["passed"])
            self.assertTrue(all(row["passed"] for row in audit["checks"]))

    def test_same_url_reuses_verified_immutable_cache_without_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            packs_root = Path(temporary) / "packs"
            media = FakeMediaTools()
            transcriber = FakeTranscriber()
            translator = FakeTranslator()
            builder = self.make_builder(
                packs_root,
                translator=translator,
                transcriber=transcriber,
                media=media,
            )
            first = builder.build(VIDEO_URL)
            first_counts = copy.deepcopy(media.calls)
            first_transcriber_calls = transcriber.calls
            first_translator_calls = translator.calls
            progress_events = []
            second = builder.build(VIDEO_URL, progress_callback=progress_events.append)
            self.assertEqual(first, second)
            self.assertEqual(media.calls, first_counts)
            self.assertEqual(transcriber.calls, first_transcriber_calls)
            self.assertEqual(translator.calls, first_translator_calls)
            self.assertTrue(progress_events[-1]["cached"])
            self.assertEqual(progress_events[-1]["detail"], "cache_hit")

    def test_pack_schema_hashes_and_video_namespaced_occurrences(self):
        with tempfile.TemporaryDirectory() as temporary:
            final_path = self.make_builder(Path(temporary) / "packs").build(VIDEO_URL)
            self.assertEqual(
                {path.name for path in final_path.iterdir()},
                {
                    "manifest.json",
                    "source.json",
                    "transcript.json",
                    "captions-zh.json",
                    "audit.json",
                    "video.mp4",
                    "phrases",
                },
            )
            documents = {
                name: json.loads((final_path / name).read_text(encoding="utf-8"))
                for name in ["manifest.json", "source.json", "transcript.json", "captions-zh.json", "audit.json"]
            }
            manifest = documents["manifest.json"]
            self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
            self.assertEqual(manifest["builder_version"], BUILDER_VERSION)
            self.assertEqual(manifest["prompt_version"], PROMPT_VERSION)
            self.assertEqual(manifest["video_id"], VIDEO_ID)
            self.assertEqual(len(manifest["candidates"]), 4)
            self.assertTrue(manifest["quality_gates"]["all_passed"])
            self.assertTrue(manifest["unverified_boundaries"])
            self.assertEqual(documents["source.json"]["download_policy"]["max_height"], 360)
            self.assertEqual(documents["source.json"]["download_policy"]["format"], "18")
            self.assertEqual(documents["source.json"]["download_policy"]["player_clients"], ["android", "web_embedded"])
            self.assertTrue(documents["transcript.json"]["word_timestamps"])
            self.assertEqual(len(documents["captions-zh.json"]["segments"]), 4)
            self.assertEqual(documents["captions-zh.json"]["source"], "dsh_cue_translation")

            for candidate in manifest["candidates"]:
                expected_occurrence = f"{VIDEO_ID}:{candidate['cue_id']}:{candidate['surface']}"
                self.assertEqual(candidate["id"], expected_occurrence)
                self.assertEqual(candidate["occurrence_id"], expected_occurrence)
                self.assertIn(candidate["surface"], candidate["phrase_text"])
                self.assertTrue(candidate["phrase_zh"])
                self.assertFalse(candidate["isolated_word_audio_enabled"])
                self.assertLess(candidate["highlight_start_sec"], candidate["highlight_end_sec"])
                self.assertLessEqual(candidate["highlight_end_sec"], candidate["phrase_duration_sec"])

            for relative, expected_hash in manifest["hashes"]["files"].items():
                actual = hashlib.sha256((final_path / relative).read_bytes()).hexdigest()
                self.assertEqual(actual, expected_hash)

    def test_corrupt_cache_is_rejected_instead_of_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            packs_root = Path(temporary) / "packs"
            builder = self.make_builder(packs_root)
            final_path = builder.build(VIDEO_URL)
            (final_path / "video.mp4").write_bytes(b"corrupt")
            with self.assertRaises(CacheIntegrityError):
                builder.build(VIDEO_URL)
            self.assertEqual((final_path / "video.mp4").read_bytes(), b"corrupt")


class SubprocessBoundaryTests(unittest.TestCase):
    def test_transcriber_chunks_long_audio_and_wraps_generator_memory_failure(self):
        calls = {}

        class Model:
            def transcribe(self, _path, **kwargs):
                calls.update(kwargs)

                def failing_segments():
                    raise MemoryError("Unable to allocate feature array")
                    yield None

                info = type("Info", (), {"language": "en", "language_probability": 0.99, "duration": 700.0})()
                return failing_segments(), info

        def factory(_model, **kwargs):
            calls["factory"] = kwargs
            return Model()

        transcriber = FasterWhisperTranscriber("tiny.en", device="cpu", compute_type="int8", cpu_threads=1, model_factory=factory)
        with tempfile.TemporaryDirectory() as temporary:
            wav_path = Path(temporary) / "long.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000)
            with self.assertRaisesRegex(VideoPackError, "transcriber_memory_unavailable"):
                transcriber.transcribe(wav_path)
        self.assertEqual(calls["chunk_length"], 30)
        self.assertFalse(calls["condition_on_previous_text"])
        self.assertEqual(calls["factory"]["cpu_threads"], 1)
        self.assertEqual(calls["factory"]["num_workers"], 1)

    def test_long_wav_is_physically_chunked_and_offsets_return_to_global_time(self):
        calls = []

        class Model:
            def transcribe(self, path, **_kwargs):
                calls.append(path)
                word = type("Word", (), {"word": " hello", "start": 0.2, "end": 0.8, "probability": 0.99})()
                segment = type("Segment", (), {"id": 0, "start": 0.1, "end": 0.9, "text": " hello", "words": [word]})()
                info = type("Info", (), {"language": "en", "language_probability": 0.99, "duration": 45.0})()
                return iter([segment]), info

        with tempfile.TemporaryDirectory() as temporary:
            wav_path = Path(temporary) / "hundred-seconds.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000 * 100)
            transcriber = FasterWhisperTranscriber("fake", model_factory=lambda *_args, **_kwargs: Model())
            result = transcriber.transcribe(wav_path)
            self.assertEqual(len(calls), 3)
            self.assertEqual([row["id"] for row in result["segments"]], ["0:0", "1:0", "2:0"])
            self.assertEqual([round(row["words"][0]["start"], 1) for row in result["segments"]], [0.2, 45.2, 90.2])
            self.assertEqual(result["duration_sec"], 100.0)
            self.assertEqual([path for path in Path(temporary).iterdir() if path.is_dir()], [])

    def test_translation_batches_split_after_bounded_json_failures(self):
        calls = []

        class SplittingTranslator(DshTranslator):
            def __init__(self):
                pass

            def _translate_only_batch(self, cues):
                calls.append(len(cues))
                if len(cues) > 4:
                    raise TranslationContractError("synthetic_large_batch_failure")
                return {str(cue["cue_id"]): f"{cue['cue_id']}号翻译" for cue in cues}

        cues = [{"cue_id": f"cue-{index:04d}", "text": f"Sentence {index}."} for index in range(10)]
        translated = SplittingTranslator().translate_all(cues, batch_size=10, max_workers=1)
        self.assertEqual(len(translated), 10)
        self.assertIn(10, calls)
        self.assertTrue(all(size <= 5 for size in calls[1:]))

    def test_dsh_timeout_retries_once_uses_headless_strict_json_and_windows_flag(self):
        cues = build_natural_cues(sample_transcript())
        payload = valid_payload(cues)
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload, ensure_ascii=False), stderr="")

        translator = DshTranslator(runner=runner, timeout_sec=7.5)
        result = translator.translate(cues)
        self.assertEqual(result, payload)
        self.assertEqual(len(calls), 2)
        for args, kwargs in calls:
            self.assertEqual(args[:3], ["dsh", "--profile", "headless"])
            self.assertIn("--patch", args)
            self.assertTrue(str(args[args.index("--patch") + 1]).endswith("dsh-translation.patch.yml"))
            self.assertEqual(kwargs["timeout"], 7.5)
            self.assertEqual(kwargs["creationflags"], CREATE_NO_WINDOW)
            self.assertFalse(kwargs["check"])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertNotIn("shell", kwargs)
        if os.name == "nt":
            self.assertNotEqual(CREATE_NO_WINDOW, 0)

    def test_dsh_sense_selector_accepts_only_catalog_candidate_ids(self):
        from lexical_sense import sense_candidates

        row = {
            "occurrence_id": "video:cue:bank",
            "surface": "bank",
            "phrase_text": "She called the bank about her account.",
            "gloss_zh": "银行",
        }
        financial = next(candidate for candidate in sense_candidates("bank") if "financial institution" in candidate["definition"])
        calls = []

        def valid_runner(args, **kwargs):
            calls.append(args)
            payload = {"items": [{"occurrence_id": row["occurrence_id"], "candidate_id": financial["candidate_id"], "confidence": 96}]}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

        selected = DshTranslator(runner=valid_runner).resolve_senses([row])
        self.assertEqual(selected[row["occurrence_id"]]["candidate_id"], financial["candidate_id"])
        self.assertIn(financial["candidate_id"], calls[0][-1])

        invalid_calls = []

        def invalid_runner(args, **kwargs):
            invalid_calls.append(args)
            payload = {"items": [{"occurrence_id": row["occurrence_id"], "candidate_id": "invented-sense", "confidence": 100}]}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

        with self.assertRaises(TranslationContractError):
            DshTranslator(runner=invalid_runner).resolve_senses([row])
        self.assertEqual(len(invalid_calls), 2)

    def test_dsh_rejects_non_json_text_after_exactly_one_retry(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout='```json\n{"translations":[],"candidates":[]}\n```', stderr="")

        translator = DshTranslator(runner=runner, timeout_sec=1)
        with self.assertRaises(TranslationContractError):
            translator.translate(build_natural_cues(sample_transcript()))
        self.assertEqual(len(calls), 2)

    def test_media_tools_do_not_assume_a_local_proxy(self):
        captured = []

        def runner(args, **kwargs):
            captured.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(source_metadata()), stderr="")

        cleared = {
            "INFLOW_DOWNLOAD_PROXY": "",
            "HTTPS_PROXY": "",
            "HTTP_PROXY": "",
            "https_proxy": "",
            "http_proxy": "",
        }
        with patch.dict(os.environ, cleared, clear=False):
            SubprocessMediaTools(runner=runner).inspect(VIDEO_URL)
        self.assertNotIn("--proxy", captured[0])

    def test_every_media_subprocess_uses_create_no_window_and_required_flags(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            executable = Path(args[0]).name
            if executable == "yt-dlp" and "--dump-single-json" in args:
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(source_metadata()), stderr="")
            if executable == "yt-dlp":
                output = Path(args[args.index("--output") + 1])
                output.write_bytes(b"mock-video")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if executable == "ffmpeg":
                Path(args[-1]).write_bytes(b"mock-media")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if executable == "ffprobe":
                media_path = Path(args[-1])
                if media_path.suffix == ".mp4":
                    probe = {
                        "format": {"duration": "120.0"},
                        "streams": [
                            {"codec_type": "video", "height": 720},
                            {"codec_type": "audio"},
                        ],
                    }
                else:
                    probe = {"format": {"duration": "2.5"}, "streams": [{"codec_type": "audio"}]}
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(probe), stderr="")
            raise AssertionError(args)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = SubprocessMediaTools(runner=runner, proxy="http://127.0.0.1:7890")
            media.inspect(VIDEO_URL)
            video = root / "video.mp4"
            wav = root / "audio.wav"
            phrase = root / "phrase.mp3"
            media.download(VIDEO_URL, video)
            media.extract_audio(video, wav)
            video_probe = media.probe(video)
            media.clip_phrase(video, 1.0, 3.5, phrase)
            phrase_probe = media.probe(phrase)

        self.assertEqual(video_probe["height"], 720)
        self.assertEqual(phrase_probe["duration_sec"], 2.5)
        self.assertGreaterEqual(len(calls), 6)
        for args, kwargs in calls:
            self.assertEqual(kwargs["creationflags"], CREATE_NO_WINDOW, args)
            self.assertFalse(kwargs["check"], args)
            self.assertTrue(kwargs["capture_output"], args)
            self.assertTrue(kwargs["text"], args)
            self.assertNotIn("shell", kwargs)
        download_args = next(args for args, _ in calls if Path(args[0]).name == "yt-dlp" and "--output" in args)
        self.assertEqual(download_args[download_args.index("--format") + 1], "18")
        self.assertEqual(download_args[download_args.index("--js-runtimes") + 1], "node")
        self.assertEqual(
            download_args[download_args.index("--extractor-args") + 1],
            "youtube:player_client=android,web_embedded,-visionos",
        )
        self.assertEqual(download_args[download_args.index("--proxy") + 1], "http://127.0.0.1:7890")
        self.assertNotIn("--merge-output-format", download_args)
        extract_args = next(args for args, _ in calls if Path(args[0]).name == "ffmpeg" and args[-1].endswith("audio.wav"))
        self.assertIn("16000", extract_args)
        self.assertIn("pcm_s16le", extract_args)


if __name__ == "__main__":
    unittest.main()
