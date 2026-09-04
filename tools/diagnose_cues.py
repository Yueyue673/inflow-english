from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from video_pack import (
    SubprocessMediaTools,
    VideoPackBuilder,
    _normalise_word_timestamps,
    _reliable_transcript_view,
    build_caption_aligned_cues,
    build_natural_cues,
    validate_source_metadata,
    validate_youtube_url,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("output")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    reference = validate_youtube_url(args.url)
    media = SubprocessMediaTools()
    source = validate_source_metadata(reference, media.inspect(reference.canonical_url))
    video = output / "video.mp4"
    wav = output / "source.wav"
    english = output / "english.json3"
    media.download(reference.canonical_url, video)
    english_payload = None
    english_error = None
    try:
        media.download_english_captions(reference.canonical_url, english)
        english_payload = json.loads(english.read_text(encoding="utf-8"))
    except Exception as exc:
        english_error = f"{type(exc).__name__}:{str(exc).split(':', 1)[0]}"
    media.extract_audio(video, wav)
    builder = VideoPackBuilder(output / "unused-packs", media_tools=media)
    transcript = builder.transcriber.transcribe(wav)
    (output / "raw-transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reliable, quality = _reliable_transcript_view(transcript)
    words = _normalise_word_timestamps(reliable)
    segment_rows = []
    reliable_segments = reliable.get("segments", [])
    all_valid = [word for segment in reliable_segments for word in segment.get("words", []) if word.get("start") is not None and word.get("end") is not None and float(word["end"]) > float(word["start"])]
    all_valid.sort(key=lambda word: float(word["start"]))
    for segment in reliable_segments:
        segment_words = [word for word in segment.get("words", []) if word.get("start") is not None and word.get("end") is not None and float(word["end"]) > float(word["start"])]
        if not segment_words:
            continue
        start = float(segment_words[0]["start"])
        end = float(segment_words[-1]["end"])
        previous_end = max((float(word["end"]) for word in all_valid if float(word["end"]) <= start + 0.001 and float(word["start"]) < start), default=start)
        next_start = min((float(word["start"]) for word in all_valid if float(word["start"]) >= end - 0.001 and float(word["end"]) > end), default=float(source["duration_sec"]))
        text = " ".join(str(word.get("word") or "").strip() for word in segment_words).strip()
        segment_rows.append(
            {
                "id": str(segment.get("id")),
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "preceding_gap": round(max(0.0, start - previous_end), 3),
                "following_gap": round(max(0.0, next_start - end), 3),
                "punctuated_end": text.endswith((".", "!", "?", ";", ":")),
                "text": text,
            }
        )
    natural = None
    natural_error = None
    try:
        natural = build_natural_cues(reliable)
    except Exception as exc:
        natural_error = f"{type(exc).__name__}:{exc}"
    aligned = None
    aligned_error = None
    if english_payload is not None:
        try:
            aligned = build_caption_aligned_cues(transcript, english_payload, video_duration_sec=float(source["duration_sec"]))
        except Exception as exc:
            aligned_error = f"{type(exc).__name__}:{exc}"
    result = {
        "source": source,
        "transcriber_chain": builder.transcriber_identity,
        "transcriber_runtime": getattr(builder.transcriber, "runtime_identity", builder.transcriber_identity),
        "quality": quality,
        "word_count": len(words),
        "english_caption_events": len((english_payload or {}).get("events", [])),
        "english_caption_error": english_error,
        "natural_count": len(natural or []),
        "natural_error": natural_error,
        "aligned_count": len(aligned or []),
        "aligned_error": aligned_error,
        "segments_2_to_8_seconds": sum(2 <= row["duration"] <= 8 for row in segment_rows),
        "segments_2_to_8_with_safe_gap": sum(2 <= row["duration"] <= 8 and row["following_gap"] >= 0.2 for row in segment_rows),
        "segments_2_to_8_with_punctuation": sum(2 <= row["duration"] <= 8 and row["punctuated_end"] for row in segment_rows),
        "segments": segment_rows,
    }
    (output / "cue-diagnostic.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "segments"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
