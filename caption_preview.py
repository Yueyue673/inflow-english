from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from video_pack import (
    ArgosHeuristicTranslator,
    SubprocessMediaTools,
    VideoPackError,
    parse_youtube_json3_captions,
    validate_source_metadata,
    validate_youtube_url,
)

CAPTION_PREVIEW_SCHEMA = "inflow.caption-preview/1"
CAPTION_PREVIEW_VERSION = "caption-first/1"
CAPTION_BATCH_SIZE = 24
SOUND_LABEL_TRANSLATIONS = {
    "[applause]": "[掌声]",
    "[music]": "[音乐]",
    "[laughter]": "[笑声]",
    "[cheering]": "[欢呼]",
    "[silence]": "[无声]",
}
SOUND_LABEL_ZH = frozenset(SOUND_LABEL_TRANSLATIONS.values())


def _clean_english_caption(value: str) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"^foreign\s+(?=\[)", "", text, flags=re.IGNORECASE)
    return "" if text.casefold() == "foreign" else text


def _sound_prefix(value: str) -> tuple[str | None, str]:
    text = _clean_english_caption(value)
    match = re.match(r"^(\[[A-Za-z ]+\])(?:\s+|$)(.*)$", text)
    if not match:
        return None, text
    translated = SOUND_LABEL_TRANSLATIONS.get(match.group(1).casefold())
    return translated, match.group(2).strip() if translated else text


def _known_sound_translation(value: str) -> str | None:
    prefix, remainder = _sound_prefix(value)
    return prefix if prefix and not remainder else None


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def caption_preview_id(source_url: str) -> str:
    reference = validate_youtube_url(source_url)
    digest = hashlib.sha256(f"{CAPTION_PREVIEW_VERSION}|{reference.canonical_url}".encode("utf-8")).hexdigest()[:16]
    return f"yt-{reference.video_id}-{digest}"


def _read_json_object(path: Path, error_code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoPackError(error_code) from exc
    if not isinstance(payload, dict):
        raise VideoPackError(error_code)
    return payload


def _english_rows(payload: Mapping[str, Any], duration_sec: float) -> list[dict[str, Any]]:
    segments = parse_youtube_json3_captions(payload, duration_sec=duration_sec, require_cjk=False)
    rows = []
    for segment in segments:
        text = _clean_english_caption(str(segment["text"]))
        if not text:
            continue
        rows.append(
            {
                "id": f"caption-{len(rows) + 1:05d}",
                "start": float(segment["start"]),
                "end": float(segment["end"]),
                "text_en": text,
                "text_zh": _known_sound_translation(text),
            }
        )
    if not rows:
        raise VideoPackError("english_captions_empty_after_cleanup")
    return rows


def _chinese_rows(payload: Mapping[str, Any], duration_sec: float) -> list[dict[str, Any]]:
    segments = parse_youtube_json3_captions(payload, duration_sec=duration_sec)
    return [
        {
            "id": f"caption-{index:05d}",
            "start": float(row["start"]),
            "end": float(row["end"]),
            "text_en": None,
            "text_zh": str(row["text"]),
        }
        for index, row in enumerate(segments, start=1)
    ]


def _merge_caption_fragments(values: Sequence[str]) -> str | None:
    merged: list[str] = []
    for raw in values:
        text = " ".join(str(raw or "").split())
        if not text:
            continue
        if not merged:
            merged.append(text)
            continue
        previous = merged[-1]
        if text == previous or text in previous:
            continue
        if previous in text:
            merged[-1] = text
        else:
            merged.append(text)
    output = ""
    for text in merged:
        if not output:
            output = text
            continue
        joins_chinese = bool(re.search(r"[\u3400-\u9fff]$", output) and re.match(r"[\u3400-\u9fff]", text))
        output += ("" if joins_chinese else " ") + text
    return re.sub(r"^[。！？!?，,、；;\s]+", "", output).strip() or None


def _substantial_overlap(start: float, end: float, row: Mapping[str, Any]) -> bool:
    row_start = float(row["start"])
    row_end = float(row["end"])
    overlap = min(end, row_end) - max(start, row_start)
    required = max(0.08, min(0.35, min(max(0.001, end - start), max(0.001, row_end - row_start)) * 0.12))
    return overlap >= required


def _merge_tracks(english: Sequence[Mapping[str, Any]], chinese: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not english:
        return [dict(row) for row in chinese]
    if not chinese:
        return [dict(row) for row in english]
    merged = []
    for index, en in enumerate(english, start=1):
        start = float(en["start"])
        end = float(en["end"])
        text_en = str(en.get("text_en") or "") or None
        sound_prefix, spoken = _sound_prefix(text_en or "")
        overlaps = sorted(
            (
                zh for zh in chinese
                if _substantial_overlap(start, end, zh)
                and not (
                    str(zh.get("text_zh") or "").strip() in SOUND_LABEL_ZH
                    and spoken
                    and str(zh.get("text_zh") or "").strip() != sound_prefix
                )
            ),
            key=lambda row: (float(row["start"]), float(row["end"]), str(row.get("id") or "")),
        )
        text_zh = _merge_caption_fragments([str(row.get("text_zh") or "") for row in overlaps])
        if spoken and sound_prefix and text_zh == sound_prefix:
            text_zh = None
        merged.append(
            {
                "id": f"caption-{index:05d}",
                "start": start,
                "end": end,
                "text_en": text_en,
                "text_zh": text_zh,
            }
        )
    return merged


def build_caption_preview(
    source_url: str,
    output_path: Path,
    *,
    media_tools: SubprocessMediaTools | None = None,
    translator: Any | None = None,
    on_publish: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Publish subtitles before any video/ASR/candidate work.

    English is published as soon as the timed track is available. If a Chinese
    timed track exists it is merged and published next. Otherwise Chinese is
    added in small deterministic batches by the local translator. Every
    revision is an atomic, readable file; learning readiness is not represented
    here.
    """

    reference = validate_youtube_url(source_url)
    media = media_tools or SubprocessMediaTools()
    translate = translator or ArgosHeuristicTranslator(timeout_sec=90.0)
    scratch = output_path.parent / f".{output_path.stem}.staging"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=False)

    def check_cancelled() -> None:
        if cancelled and cancelled():
            raise VideoPackError("caption_preview_cancelled")

    def publish(document: dict[str, Any]) -> None:
        _atomic_write_json(output_path, document)
        if on_publish:
            on_publish(document)

    revision = 0
    try:
        check_cancelled()
        metadata = media.inspect(reference.canonical_url)
        source = validate_source_metadata(reference, metadata, max_duration_sec=3 * 60 * 60)
        duration = float(source["duration_sec"])
        title = str(source.get("title") or reference.video_id)

        english: list[dict[str, Any]] = []
        chinese: list[dict[str, Any]] = []
        english_error = None
        chinese_error = None
        document: dict[str, Any] | None = None
        rows: list[dict[str, Any]] = []

        en_path = scratch / "en.json3"
        try:
            media.download_english_captions(reference.canonical_url, en_path)
            english = _english_rows(_read_json_object(en_path, "english_captions_invalid"), duration)
        except VideoPackError as exc:
            english_error = str(exc).split(":", 1)[0]

        if english:
            rows = _merge_tracks(english, [])
            revision += 1
            known_zh = sum(1 for row in rows if row.get("text_zh"))
            document = {
                "schema_version": CAPTION_PREVIEW_SCHEMA,
                "version": CAPTION_PREVIEW_VERSION,
                "preview_id": caption_preview_id(reference.canonical_url),
                "video_id": reference.video_id,
                "source_url": reference.canonical_url,
                "title": title,
                "duration_sec": duration,
                "status": "translating" if known_zh else "english_ready",
                "revision": revision,
                "english_source": "youtube_timed_en",
                "chinese_source": None,
                "english_error": None,
                "chinese_error": None,
                "translated_segments": known_zh,
                "total_segments": len(rows),
                "coverage_fraction": round(known_zh / len(rows), 6),
                "segments": rows,
            }
            publish(document)

        check_cancelled()
        zh_path = scratch / "zh.json3"
        try:
            media.download_chinese_captions(reference.canonical_url, zh_path)
            chinese = _chinese_rows(_read_json_object(zh_path, "chinese_captions_invalid"), duration)
        except VideoPackError as exc:
            chinese_error = str(exc).split(":", 1)[0]

        if not english and not chinese:
            raise VideoPackError("timed_captions_unavailable")

        if chinese:
            rows = _merge_tracks(english, chinese)
            revision += 1
            ready_zh = sum(1 for row in rows if row.get("text_zh"))
            document = {
                "schema_version": CAPTION_PREVIEW_SCHEMA,
                "version": CAPTION_PREVIEW_VERSION,
                "preview_id": caption_preview_id(reference.canonical_url),
                "video_id": reference.video_id,
                "source_url": reference.canonical_url,
                "title": title,
                "duration_sec": duration,
                "status": "ready",
                "revision": revision,
                "english_source": "youtube_timed_en" if english else None,
                "chinese_source": "youtube_timed_zh-Hans",
                "english_error": english_error,
                "chinese_error": None,
                "translated_segments": ready_zh,
                "total_segments": len(rows),
                "coverage_fraction": round(ready_zh / len(rows), 6) if rows else 0.0,
                "segments": rows,
            }
            publish(document)
        elif document is not None:
            document = {**document, "chinese_error": chinese_error}

        if document is None:
            raise VideoPackError("timed_captions_unavailable")
        ready_zh = sum(1 for row in rows if row.get("text_zh"))
        if ready_zh < len(rows) and english:
            for offset in range(0, len(rows), CAPTION_BATCH_SIZE):
                check_cancelled()
                indexes = [index for index in range(offset, min(len(rows), offset + CAPTION_BATCH_SIZE)) if not rows[index].get("text_zh")]
                if not indexes:
                    continue
                payload = [
                    {"cue_id": rows[index]["id"], "text": _sound_prefix(rows[index]["text_en"])[1]}
                    for index in indexes
                ]
                translated = translate.translate_all(payload, batch_size=CAPTION_BATCH_SIZE, max_workers=1)
                for index in indexes:
                    prefix, _spoken = _sound_prefix(rows[index]["text_en"])
                    spoken_zh = str(translated[rows[index]["id"]]).strip()
                    rows[index]["text_zh"] = " ".join(part for part in (prefix, spoken_zh) if part)
                revision += 1
                ready_zh = sum(1 for row in rows if row.get("text_zh"))
                document = {
                    **document,
                    "status": "ready" if ready_zh == len(rows) else "translating",
                    "revision": revision,
                    "chinese_source": "argos_en_zh_offline",
                    "translated_segments": ready_zh,
                    "coverage_fraction": round(ready_zh / len(rows), 6),
                    "segments": rows,
                }
                publish(document)
        return document
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
