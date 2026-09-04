from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from caption_preview import _english_rows, caption_preview_id
from progressive_video_pack import ProgressiveVideoPackBuilder, verify_window_shard
from video_pack import SubprocessMediaTools, VideoPackBuilder, validate_source_metadata, validate_youtube_url

URL = "https://www.youtube.com/watch?v=c3mPdZA-Fmc"
PLAYHEAD = float(os.environ.get("INFLOW_REAL_PLAYHEAD_SEC", 87 * 60.0))


class RecordingTranscriber:
    def __init__(self, delegate: Any, diagnostics: Path) -> None:
        self.delegate = delegate
        self.diagnostics = diagnostics

    @property
    def identity(self) -> str:
        return str(getattr(self.delegate, "identity", type(self.delegate).__name__))

    @property
    def runtime_identity(self) -> str:
        return str(getattr(self.delegate, "runtime_identity", "not-selected"))

    def transcribe(self, audio_path: Path, *, cancel_callback=None):
        payload = self.delegate.transcribe(audio_path, cancel_callback=cancel_callback)
        self.diagnostics.mkdir(parents=True, exist_ok=True)
        (self.diagnostics / "raw-transcript.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload

    def audit_phrase(self, audio_path: Path):
        return self.delegate.audit_phrase(audio_path)

    def release_model(self) -> None:
        release = getattr(self.delegate, "release_model", None)
        if callable(release):
            release()


class RecordingMedia:
    def __init__(self, delegate: Any, diagnostics: Path) -> None:
        self.delegate = delegate
        self.diagnostics = diagnostics

    @property
    def identity(self) -> str:
        return str(getattr(self.delegate, "identity", type(self.delegate).__name__))

    def download_range(self, canonical_url: str, start_sec: float, end_sec: float, destination: Path) -> None:
        self.delegate.download_range(canonical_url, start_sec, end_sec, destination)
        self.diagnostics.mkdir(parents=True, exist_ok=True)
        probe = self.delegate.probe(destination)
        (self.diagnostics / "range-probe.json").write_text(json.dumps({"start_sec": start_sec, "end_sec": end_sec, "probe": probe}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)


def main() -> None:
    qa_base = Path(os.environ.get("INFLOW_QA_ROOT") or (Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache")) / "InFlow-English" / "qa"))
    qa_root = qa_base / f"progressive-real-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    packs_root = qa_root / "packs"
    scratch = qa_root / "source-index"
    scratch.mkdir(parents=True, exist_ok=False)
    media = SubprocessMediaTools()
    reference = validate_youtube_url(URL)
    started = time.perf_counter()
    metadata = media.inspect(reference.canonical_url)
    source = validate_source_metadata(reference, metadata, max_duration_sec=3 * 60 * 60)
    english_path = scratch / "english.json3"
    media.download_english_captions(reference.canonical_url, english_path)
    payload = json.loads(english_path.read_text(encoding="utf-8"))
    english = _english_rows(payload, float(source["duration_sec"]))
    chapters = [
        float(row.get("start_time"))
        for row in metadata.get("chapters") or []
        if isinstance(row, dict) and row.get("start_time") is not None
    ]
    caption_document = {
        "schema_version": "inflow.caption-preview/1",
        "preview_id": caption_preview_id(URL),
        "video_id": reference.video_id,
        "source_url": reference.canonical_url,
        "title": source["title"],
        "duration_sec": source["duration_sec"],
        "revision": 1,
        "chapter_starts": chapters,
        "segments": english,
    }
    (scratch / "caption-index.json").write_text(json.dumps(caption_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_elapsed = time.perf_counter() - started

    diagnostics = qa_root / "diagnostics"
    base_builder = VideoPackBuilder(packs_root)
    base_builder.transcriber = RecordingTranscriber(base_builder.transcriber, diagnostics)
    base_builder.media_tools = RecordingMedia(base_builder.media_tools, diagnostics)
    builder = ProgressiveVideoPackBuilder(packs_root, base_builder=base_builder)
    manifest = builder.initialize(URL, caption_document, playhead_sec=PLAYHEAD)
    planned = next(row for row in manifest["windows"] if row["ownership_start_sec"] <= PLAYHEAD < row["ownership_end_sec"])
    first_started = time.perf_counter()
    manifest = builder.build_one(manifest["pack_id"])
    first_elapsed = time.perf_counter() - first_started
    finished = next(row for row in manifest["windows"] if row["status"] in {"ready", "ready_no_candidate", "failed"})
    shard = None
    if finished["status"] in {"ready", "ready_no_candidate"}:
        shard = verify_window_shard(packs_root / manifest["pack_id"] / "shards" / finished["window_id"], pack_id=manifest["pack_id"], window_id=finished["window_id"])
    leftovers = [
        str(path.relative_to(qa_root))
        for pattern in ("source-range.mp4", "source-range.wav", "video.mp4")
        for path in qa_root.rglob(pattern)
    ]
    raw_path = diagnostics / "raw-transcript.json"
    raw_summary = None
    if raw_path.is_file():
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        raw_summary = {
            "language": raw.get("language"),
            "language_probability": raw.get("language_probability"),
            "duration_sec": raw.get("duration_sec"),
            "segments": len(raw.get("segments") or []),
            "words": sum(len(segment.get("words") or []) for segment in raw.get("segments") or [] if isinstance(segment, dict)),
            "runtime_transcriber": base_builder.transcriber.runtime_identity,
        }
    result = {
        "ok": finished["status"] in {"ready", "ready_no_candidate"},
        "qa_root": str(qa_root),
        "video_id": reference.video_id,
        "title": source["title"],
        "duration_sec": source["duration_sec"],
        "playhead_sec": PLAYHEAD,
        "caption_segments": len(english),
        "chapters": len(chapters),
        "source_index_elapsed_sec": round(index_elapsed, 2),
        "planned_first_window": planned,
        "finished_window": finished,
        "time_to_first_usable_sec": round(first_elapsed, 2),
        "manifest_status": manifest["status"],
        "manifest_revision": manifest["revision"],
        "coverage_fraction": manifest["coverage_fraction"],
        "candidate_count": len(shard.get("candidates") or []) if shard else 0,
        "cue_count": len(shard.get("transcript_cues") or []) if shard else 0,
        "effective_speech_sec": shard.get("effective_speech_sec") if shard else None,
        "raw_transcript": raw_summary,
        "range_probe_path": str(diagnostics / "range-probe.json") if (diagnostics / "range-probe.json").is_file() else None,
        "range_or_full_media_leftovers": leftovers,
    }
    (qa_root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
