from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from adaptive_core import atomic_write_json
from lexical_sense import resolve_lexical_identity
from long_video import MIN_LONG_VIDEO_SEC, MAX_LONG_VIDEO_SEC, plan_long_video_windows, prioritize_windows
from video_pack import (
    BUILDER_VERSION,
    SCHEMA_VERSION,
    AuditError,
    BuildCancelled,
    CueConstructionError,
    SourceValidationError,
    TranslationContractError,
    VideoPackBuilder,
    VideoPackError,
    _lexical_sequence,
    _normalise_word_timestamps,
    _reliable_transcript_view,
    _sha256,
    _validate_clip_probe,
    _write_json_new,
    build_caption_aligned_cues,
    build_natural_cues,
    build_sparse_natural_cues,
    effective_speech_seconds,
    select_candidate_cue_shortlist,
    validate_candidate_review,
    validate_translation_batch,
    validate_youtube_url,
)

PROGRESSIVE_SCHEMA = "inflow.progressive-pack/1"
WINDOW_SCHEMA = "inflow.window-shard/1"
PROGRESSIVE_BUILDER_VERSION = "progressive-pack-builder/0.2.0"
TERMINAL_WINDOW_STATES = {"ready", "ready_no_candidate", "failed"}
READY_WINDOW_STATES = {"ready", "ready_no_candidate"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_id(value: str, code: str) -> str:
    text = str(value or "")
    if not text or len(text) > 120 or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in text):
        raise VideoPackError(code)
    return text


def _read_object(path: Path, code: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VideoPackError(code)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoPackError(code) from exc
    if not isinstance(payload, dict):
        raise VideoPackError(code)
    return payload


def _hash_text(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def progressive_pack_id(source_url: str, *, dependency_fingerprint: str = BUILDER_VERSION) -> str:
    reference = validate_youtube_url(source_url)
    digest = hashlib.sha256(
        f"{PROGRESSIVE_SCHEMA}|{PROGRESSIVE_BUILDER_VERSION}|{dependency_fingerprint}|{reference.canonical_url}".encode("utf-8")
    ).hexdigest()[:12]
    return f"ytp-{reference.video_id}-{digest}"


def _caption_document_valid(document: Mapping[str, Any], source_url: str) -> tuple[str, float, list[dict[str, Any]]]:
    reference = validate_youtube_url(source_url)
    if document.get("video_id") != reference.video_id or document.get("source_url") != reference.canonical_url:
        raise SourceValidationError("progressive_caption_identity_mismatch")
    try:
        duration = float(document.get("duration_sec"))
    except (TypeError, ValueError) as exc:
        raise SourceValidationError("progressive_duration_invalid") from exc
    if not math.isfinite(duration) or not MIN_LONG_VIDEO_SEC <= duration <= MAX_LONG_VIDEO_SEC:
        raise SourceValidationError("source_duration_out_of_long_video_range")
    rows = []
    for raw in document.get("segments") or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError):
            continue
        text_en = str(raw.get("text_en") or "").strip()
        text_zh = str(raw.get("text_zh") or "").strip() or None
        if math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration and text_en:
            rows.append({"id": str(raw.get("id") or f"caption-{len(rows) + 1:05d}"), "start": start, "end": end, "text_en": text_en, "text_zh": text_zh})
    rows.sort(key=lambda row: (row["start"], row["end"], row["id"]))
    if not rows:
        raise SourceValidationError("long_video_english_captions_required")
    return reference.video_id, duration, rows


def _window_caption_payload(rows: Sequence[Mapping[str, Any]], analysis_start: float, analysis_end: float) -> dict[str, Any]:
    events = []
    for row in rows:
        start = float(row["start"])
        end = float(row["end"])
        if end <= analysis_start or start >= analysis_end:
            continue
        local_start = max(0.0, start - analysis_start)
        local_end = min(analysis_end - analysis_start, end - analysis_start)
        if local_end <= local_start:
            continue
        events.append({
            "tStartMs": round(local_start * 1000),
            "dDurationMs": round((local_end - local_start) * 1000),
            "segs": [{"utf8": str(row["text_en"])}],
        })
    return {"events": events}


def _offset_cue(cue: Mapping[str, Any], offset: float, window_id: str, index: int) -> dict[str, Any]:
    row = deepcopy(dict(cue))
    row["cue_id"] = f"{window_id}-cue-{index:04d}"
    for field in ("start_sec", "end_sec", "pause_anchor_sec"):
        if row.get(field) is not None:
            row[field] = round(float(row[field]) + offset, 3)
    words = []
    for word in row.get("words") or []:
        copy = dict(word)
        for field in ("start_sec", "end_sec", "start", "end"):
            if copy.get(field) is not None:
                copy[field] = round(float(copy[field]) + offset, 3)
        words.append(copy)
    row["words"] = words
    return row


def _cue_coverage(cues: Sequence[Mapping[str, Any]]) -> float:
    return sum(max(0.0, float(cue.get("end_sec") or 0.0) - float(cue.get("start_sec") or 0.0)) for cue in cues)


def _cue_set_is_sparse(cues: Sequence[Mapping[str, Any]], *, effective_speech_sec: float, window_duration_sec: float) -> bool:
    minimum_count = max(2, math.ceil(max(1.0, window_duration_sec) / 120.0))
    minimum_coverage = min(30.0, max(6.0, effective_speech_sec * 0.12))
    return len(cues) < minimum_count or _cue_coverage(cues) < minimum_coverage


def _caption_alignment_is_better(
    current: Sequence[Mapping[str, Any]],
    aligned: Sequence[Mapping[str, Any]],
    *,
    effective_speech_sec: float,
    window_duration_sec: float,
) -> bool:
    current_coverage = _cue_coverage(current)
    aligned_coverage = _cue_coverage(aligned)
    current_is_sparse = _cue_set_is_sparse(
        current,
        effective_speech_sec=effective_speech_sec,
        window_duration_sec=window_duration_sec,
    )
    materially_better = len(aligned) >= len(current) + 2 or aligned_coverage >= max(6.0, current_coverage * 2.0)
    return bool(current_is_sparse and materially_better)


def _owned_cues(window: Mapping[str, Any], local_cues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    offset = float(window["analysis_start_sec"])
    start = float(window["ownership_start_sec"])
    end = float(window["ownership_end_sec"])
    output = []
    for index, cue in enumerate(local_cues, start=1):
        row = _offset_cue(cue, offset, str(window["window_id"]), index)
        cue_start = float(row["start_sec"])
        cue_end = float(row["end_sec"])
        anchor = float(row.get("pause_anchor_sec", cue_end))
        if cue_start < start or cue_end > end or not start <= anchor < end:
            continue
        output.append(row)
    return output


def _caption_rows_for_owner(rows: Sequence[Mapping[str, Any]], window: Mapping[str, Any]) -> list[dict[str, Any]]:
    start = float(window["ownership_start_sec"])
    end = float(window["ownership_end_sec"])
    return [dict(row) for row in rows if float(row["end"]) > start and float(row["start"]) < end]


def verify_window_shard(path: Path | str, *, pack_id: str | None = None, window_id: str | None = None) -> dict[str, Any]:
    root = Path(path)
    payload = _read_object(root / "window.json", "window_manifest_invalid")
    if payload.get("schema_version") != WINDOW_SCHEMA or payload.get("status") not in READY_WINDOW_STATES:
        raise AuditError("window_manifest_contract_invalid")
    if pack_id is not None and payload.get("pack_id") != pack_id:
        raise AuditError("window_pack_id_mismatch")
    if window_id is not None and payload.get("window_id") != window_id:
        raise AuditError("window_id_mismatch")
    allowed = {"window.json"}
    files = payload.get("hashes", {}).get("files") or {}
    if not isinstance(files, dict):
        raise AuditError("window_hashes_invalid")
    for relative, expected in files.items():
        relative_path = Path(str(relative))
        if relative_path.is_absolute() or ".." in relative_path.parts or "\\" in str(relative):
            raise AuditError("window_asset_path_invalid")
        target = root.joinpath(*relative_path.parts)
        if target.is_symlink() or not target.is_file() or _sha256(target) != expected:
            raise AuditError("window_asset_hash_mismatch")
        allowed.add(relative_path.as_posix())
    actual = set()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise AuditError("window_symlink_not_allowed")
        if item.is_file():
            actual.add(item.relative_to(root).as_posix())
    if actual != allowed:
        raise AuditError("window_layout_invalid")
    return payload


class ProgressiveVideoPackBuilder:
    def __init__(
        self,
        packs_root: Path | str,
        *,
        base_builder: VideoPackBuilder | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.packs_root = Path(packs_root)
        self.base = base_builder or VideoPackBuilder(self.packs_root)
        self.clock = clock
        self.dependency_fingerprint = ":".join(
            [PROGRESSIVE_BUILDER_VERSION, BUILDER_VERSION, self.base.translator_identity, self.base.transcriber_identity, self.base.media_identity]
        )

    def pack_id_for_url(self, source_url: str) -> str:
        return progressive_pack_id(source_url, dependency_fingerprint=self.dependency_fingerprint)

    def pack_path(self, pack_id: str) -> Path:
        return self.packs_root / _safe_id(pack_id, "progressive_pack_id_invalid")

    def manifest_path(self, pack_id: str) -> Path:
        return self.pack_path(pack_id) / "partial-manifest.json"

    def load(self, pack_id: str) -> dict[str, Any]:
        payload = _read_object(self.manifest_path(pack_id), "progressive_manifest_invalid")
        if payload.get("schema_version") != PROGRESSIVE_SCHEMA or payload.get("pack_id") != pack_id:
            raise AuditError("progressive_manifest_contract_invalid")
        return payload

    def initialize(self, source_url: str, caption_document: Mapping[str, Any], *, playhead_sec: float = 0.0) -> dict[str, Any]:
        video_id, duration, caption_rows = _caption_document_valid(caption_document, source_url)
        pack_id = self.pack_id_for_url(source_url)
        root = self.pack_path(pack_id)
        manifest_path = self.manifest_path(pack_id)
        if manifest_path.is_file():
            return self.recover(pack_id)
        root.mkdir(parents=True, exist_ok=True)
        chapter_starts = [float(value) for value in (caption_document.get("chapter_starts") or [])]
        windows = plan_long_video_windows(
            duration,
            chapter_starts=chapter_starts,
            caption_ends=[float(row["end"]) for row in caption_rows],
            require_long=True,
        )
        now = _iso(self.clock())
        focus = max(0.0, min(duration, float(playhead_sec)))
        manifest = {
            "schema_version": PROGRESSIVE_SCHEMA,
            "builder_version": PROGRESSIVE_BUILDER_VERSION,
            "pack_id": pack_id,
            "video_id": video_id,
            "source_url": validate_youtube_url(source_url).canonical_url,
            "title": str(caption_document.get("title") or video_id),
            "duration_sec": duration,
            "status": "building",
            "revision": 0,
            "usable": False,
            "build_complete": False,
            "coverage_fraction": 0.0,
            "ready_ranges": [],
            "failed_ranges": [],
            "focus": {"playhead_sec": focus, "focus_epoch": 1, "updated_at": now},
            "caption_preview_id": caption_document.get("preview_id"),
            "caption_source_revision": int(caption_document.get("revision") or 0),
            "caption_rows": caption_rows,
            "windows": windows,
            "created_at": now,
            "updated_at": now,
            "versions": {
                "progressive": PROGRESSIVE_BUILDER_VERSION,
                "video_pack": BUILDER_VERSION,
                "translator": self.base.translator_identity,
                "transcriber": self.base.transcriber_identity,
                "media": self.base.media_identity,
            },
        }
        atomic_write_json(manifest_path, manifest)
        return manifest

    def set_focus(self, pack_id: str, *, playhead_sec: float, focus_epoch: int) -> dict[str, Any]:
        manifest = self.load(pack_id)
        epoch = int(focus_epoch)
        if epoch <= int(manifest.get("focus", {}).get("focus_epoch", 0)):
            return manifest
        duration = float(manifest["duration_sec"])
        manifest["focus"] = {
            "playhead_sec": max(0.0, min(duration, float(playhead_sec))),
            "focus_epoch": epoch,
            "updated_at": _iso(self.clock()),
        }
        manifest["updated_at"] = manifest["focus"]["updated_at"]
        manifest["revision"] = int(manifest.get("revision", 0)) + 1
        atomic_write_json(self.manifest_path(pack_id), manifest)
        return manifest

    def recover(self, pack_id: str) -> dict[str, Any]:
        manifest = self.load(pack_id)
        changed = False
        root = self.pack_path(pack_id)
        windows = {str(row["window_id"]): row for row in manifest.get("windows", [])}
        shards = root / "shards"
        if shards.is_dir():
            for path in shards.iterdir():
                if not path.is_dir() or path.name.startswith(".") or path.name not in windows:
                    continue
                try:
                    shard = verify_window_shard(path, pack_id=pack_id, window_id=path.name)
                except VideoPackError:
                    continue
                row = windows[path.name]
                if row.get("status") != shard.get("status"):
                    row["status"] = shard["status"]
                    row["candidate_count"] = len(shard.get("candidates") or [])
                    row["error_code"] = None
                    changed = True
        for row in windows.values():
            if row.get("status") == "running":
                row["status"] = "pending"
                changed = True
        for path in root.glob(".staging-*"):
            shutil.rmtree(path, ignore_errors=True)
        if changed:
            manifest["revision"] = int(manifest.get("revision", 0)) + 1
            self._refresh_summary(manifest)
            atomic_write_json(self.manifest_path(pack_id), manifest)
        return manifest

    def _refresh_summary(self, manifest: dict[str, Any]) -> None:
        windows = manifest.get("windows", [])
        ready = [row for row in windows if row.get("status") in READY_WINDOW_STATES]
        failed = [row for row in windows if row.get("status") == "failed"]
        terminal = [row for row in windows if row.get("status") in TERMINAL_WINDOW_STATES]
        manifest["ready_ranges"] = [
            [float(row["ownership_start_sec"]), float(row["ownership_end_sec"])] for row in ready
        ]
        manifest["failed_ranges"] = [
            {"window_id": row["window_id"], "start_sec": row["ownership_start_sec"], "end_sec": row["ownership_end_sec"], "error_code": row.get("error_code")}
            for row in failed
        ]
        manifest["coverage_fraction"] = round(len(ready) / len(windows), 6) if windows else 0.0
        manifest["usable"] = bool(ready)
        manifest["build_complete"] = len(terminal) == len(windows)
        if manifest["build_complete"]:
            manifest["status"] = "degraded" if failed else "complete"
        elif ready:
            manifest["status"] = "partial_ready"
        else:
            manifest["status"] = "building"
        manifest["updated_at"] = _iso(self.clock())

    def _build_window(self, manifest: Mapping[str, Any], window: Mapping[str, Any], *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        if cancelled and cancelled():
            raise BuildCancelled("progressive_build_cancelled")
        pack_id = str(manifest["pack_id"])
        window_id = str(window["window_id"])
        root = self.pack_path(pack_id)
        shards = root / "shards"
        shards.mkdir(parents=True, exist_ok=True)
        final = shards / window_id
        if final.is_dir():
            return verify_window_shard(final, pack_id=pack_id, window_id=window_id)
        staging = root / f".staging-{window_id}-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        committed = False
        try:
            analysis_start = float(window["analysis_start_sec"])
            analysis_end = float(window["analysis_end_sec"])
            expected_duration = analysis_end - analysis_start
            range_path = staging / "source-range.mp4"
            wav_path = staging / "source-range.wav"
            self.base.media_tools.download_range(manifest["source_url"], analysis_start, analysis_end, range_path)
            probe = self.base.media_tools.probe(range_path)
            actual_duration = float(probe.get("duration_sec") or 0.0)
            if probe.get("has_audio") is not True or actual_duration <= 0 or abs(actual_duration - expected_duration) > max(3.0, expected_duration * 0.03):
                raise SourceValidationError("downloaded_range_duration_mismatch")
            self.base.media_tools.extract_audio(range_path, wav_path)
            raw_transcript = self.base.transcriber.transcribe(wav_path, cancel_callback=cancelled)
            language = str(raw_transcript.get("language") or "").lower()
            if not (language == "english" or language.startswith("en")):
                raise SourceValidationError("transcript_primary_language_not_english")
            reliable, timing_quality = _reliable_transcript_view(raw_transcript)
            normalised_words = _normalise_word_timestamps(reliable)
            speech_sec = effective_speech_seconds(normalised_words)
            english_payload = _window_caption_payload(manifest["caption_rows"], analysis_start, analysis_end)
            cue_source = "whisper_natural_partition"
            try:
                local_cues = build_natural_cues(reliable)
            except CueConstructionError:
                try:
                    local_cues = build_sparse_natural_cues(reliable, max_pause_delay_sec=5.0)
                    cue_source = "whisper_sparse_natural_segments"
                except CueConstructionError:
                    local_cues = build_caption_aligned_cues(raw_transcript, english_payload, video_duration_sec=expected_duration)
                    cue_source = "youtube_english_caption_aligned_to_window_asr"
            if english_payload.get("events") and _cue_set_is_sparse(
                local_cues,
                effective_speech_sec=speech_sec,
                window_duration_sec=expected_duration,
            ):
                try:
                    aligned_cues = build_caption_aligned_cues(raw_transcript, english_payload, video_duration_sec=expected_duration)
                    if _caption_alignment_is_better(
                        local_cues,
                        aligned_cues,
                        effective_speech_sec=speech_sec,
                        window_duration_sec=expected_duration,
                    ):
                        local_cues = aligned_cues
                        cue_source = "youtube_english_caption_aligned_to_window_asr"
                except CueConstructionError:
                    pass
            for cue in local_cues:
                if cue.get("following_gap_sec") is None:
                    gap = max(0.0, expected_duration - float(cue["end_sec"]))
                    cue["following_gap_sec"] = round(gap, 3)
                    cue["candidate_allowed"] = gap >= 0.20
            cues = _owned_cues(window, local_cues)
            caption_rows = _caption_rows_for_owner(manifest["caption_rows"], window)
            candidates: list[dict[str, Any]] = []
            if cues:
                try:
                    candidate_cues = select_candidate_cue_shortlist(cues)
                    translated_payload = self.base.translator.translate([
                        {"cue_id": cue["cue_id"], "text": cue["text"], "candidate_allowed": bool(cue.get("candidate_allowed", True))}
                        for cue in candidate_cues
                    ])
                    validated = validate_translation_batch(candidate_cues, translated_payload)
                    cue_by_id = {str(cue["cue_id"]): cue for cue in cues}
                    review_rows = [
                        {
                            "cue_id": selected.cue_id,
                            "surface": selected.surface,
                            "phrase_text": str(cue_by_id[selected.cue_id]["text"]),
                            "gloss_zh": selected.gloss_zh,
                            "phrase_zh": validated.translations[selected.cue_id],
                        }
                        for selected in validated.candidates
                    ]
                    reviewed = {
                        (row["cue_id"], row["surface"]): {"gloss_zh": row["gloss_zh"], "phrase_zh": row["phrase_zh"]}
                        for row in review_rows
                    }
                    reviewer = getattr(self.base.translator, "review_selected", None)
                    if callable(reviewer):
                        reviewed = validate_candidate_review(review_rows, reviewer(review_rows))
                    sense_rows = [
                        {
                            "occurrence_id": f"{manifest['video_id']}:{selected.cue_id}:{selected.surface}",
                            "surface": selected.surface,
                            "phrase_text": str(cue_by_id[selected.cue_id]["text"]),
                            "gloss_zh": reviewed[(selected.cue_id, selected.surface)]["gloss_zh"],
                        }
                        for selected in validated.candidates
                    ]
                    sense_selections: dict[str, dict[str, Any]] = {}
                    sense_resolver = getattr(self.base.translator, "resolve_senses", None)
                    if callable(sense_resolver):
                        try:
                            sense_selections = sense_resolver(sense_rows)
                        except TranslationContractError:
                            sense_selections = {}
                    phrases = staging / "phrases"
                    phrases.mkdir(parents=False, exist_ok=False)
                    phrase_auditor = getattr(self.base.transcriber, "audit_phrase", None)
                    for index, selected in enumerate(validated.candidates, start=1):
                        cue = cue_by_id[selected.cue_id]
                        occurrence_id = f"{manifest['video_id']}:{selected.cue_id}:{selected.surface}"
                        filename = f"{index:02d}-{hashlib.sha256(occurrence_id.encode('utf-8')).hexdigest()[:16]}.mp3"
                        output = phrases / filename
                        local_start = max(0.0, float(cue["start_sec"]) - analysis_start)
                        local_end = min(expected_duration, float(cue["end_sec"]) - analysis_start)
                        preceding_gap = max(0.0, float(cue.get("preceding_gap_sec") or 0.0))
                        following_gap = max(0.0, float(cue.get("following_gap_sec") or 0.0))
                        clip_start = max(0.0, local_start - min(0.10, preceding_gap * 0.45))
                        clip_end = min(expected_duration, local_end + min(0.12, following_gap * 0.45))
                        self.base.media_tools.clip_phrase(range_path, clip_start, clip_end, output)
                        audio_duration = _validate_clip_probe(self.base.media_tools.probe(output), clip_end - clip_start)
                        phrase_audit_text = None
                        if callable(phrase_auditor):
                            phrase_audit_text = str(phrase_auditor(output) or "").strip()
                            if _lexical_sequence(phrase_audit_text) != _lexical_sequence(cue["text"]):
                                output.unlink(missing_ok=True)
                                continue
                        copy = reviewed[(selected.cue_id, selected.surface)]
                        sense_selection = sense_selections.get(occurrence_id, {})
                        candidates.append(resolve_lexical_identity({
                            "id": occurrence_id,
                            "occurrence_id": occurrence_id,
                            "cue_id": selected.cue_id,
                            "surface": selected.surface,
                            "gloss_zh": copy["gloss_zh"],
                            "value_score": selected.value_score,
                            "phrase_text": str(cue["text"]),
                            "phrase_zh": copy["phrase_zh"],
                            "phrase_start_sec": round(float(cue["start_sec"]), 3),
                            "phrase_end_sec": round(float(cue["end_sec"]), 3),
                            "anchor_sec": round(float(cue.get("pause_anchor_sec", cue["end_sec"])), 3),
                            "safe_pause_gap_sec": round(max(0.0, float(cue.get("following_gap_sec") or 0.0)), 3),
                            "pause_delay_sec": round(max(0.0, float(cue.get("pause_anchor_sec", cue["end_sec"])) - float(cue["end_sec"])), 3),
                            "highlight_start_sec": round(selected.match.start_sec - (analysis_start + clip_start), 3),
                            "highlight_end_sec": round(selected.match.end_sec - (analysis_start + clip_start), 3),
                            "phrase_duration_sec": round(clip_end - clip_start, 3),
                            "audio_duration_sec": audio_duration,
                            "phrase_audit_text": phrase_audit_text,
                            "phrase_audio": f"shards/{window_id}/phrases/{filename}",
                            "alignment_quality": "word_timestamp_high",
                            "display_unit": "natural_excerpt",
                            "isolated_word_audio_enabled": False,
                            "window_id": window_id,
                        }, candidate_id=sense_selection.get("candidate_id"), confidence=int(sense_selection.get("confidence") or 0)))
                except (CueConstructionError, TranslationContractError):
                    candidates = []
            range_path.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
            release = getattr(self.base.transcriber, "release_model", None)
            if callable(release):
                release()
            files = {}
            phrases_dir = staging / "phrases"
            if phrases_dir.is_dir():
                for path in sorted(phrases_dir.iterdir()):
                    if path.is_file():
                        files[path.relative_to(staging).as_posix()] = _sha256(path)
            status = "ready" if candidates else "ready_no_candidate"
            shard = {
                "schema_version": WINDOW_SCHEMA,
                "pack_id": pack_id,
                "window_id": window_id,
                "status": status,
                "ownership_start_sec": window["ownership_start_sec"],
                "ownership_end_sec": window["ownership_end_sec"],
                "analysis_start_sec": window["analysis_start_sec"],
                "analysis_end_sec": window["analysis_end_sec"],
                "boundary_kind": window["boundary_kind"],
                "seam_state": window["seam_state"],
                "cue_source": cue_source,
                "effective_speech_sec": speech_sec,
                "timing_quality": timing_quality,
                "captions": caption_rows,
                "transcript_cues": cues,
                "candidates": candidates,
                "hashes": {"algorithm": "sha256", "files": files},
                "created_at": _iso(self.clock()),
            }
            shard["content_hash"] = _hash_text({key: shard[key] for key in shard if key != "created_at"})
            _write_json_new(staging / "window.json", shard)
            verify_window_shard(staging, pack_id=pack_id, window_id=window_id)
            os.replace(staging, final)
            committed = True
            return verify_window_shard(final, pack_id=pack_id, window_id=window_id)
        finally:
            try:
                release = getattr(self.base.transcriber, "release_model", None)
                if callable(release):
                    release()
            except Exception:
                pass
            if not committed:
                shutil.rmtree(staging, ignore_errors=True)

    def build_one(self, pack_id: str, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        manifest = self.recover(pack_id)
        pending = [row for row in manifest["windows"] if row.get("status") == "pending"]
        if not pending:
            self._refresh_summary(manifest)
            atomic_write_json(self.manifest_path(pack_id), manifest)
            return manifest
        order = prioritize_windows(manifest["windows"], float(manifest.get("focus", {}).get("playhead_sec") or 0.0))
        selected_id = next(window_id for window_id in order if any(row["window_id"] == window_id and row.get("status") == "pending" for row in pending))
        selected = next(row for row in manifest["windows"] if row["window_id"] == selected_id)
        selected["status"] = "running"
        selected["started_at"] = _iso(self.clock())
        build_started_at = selected["started_at"]
        manifest["updated_at"] = build_started_at
        atomic_write_json(self.manifest_path(pack_id), manifest)
        result_status = "pending"
        result_candidate_count = 0
        result_error: str | None = None
        try:
            shard = self._build_window(manifest, selected, cancelled=cancelled)
            result_status = str(shard["status"])
            result_candidate_count = len(shard.get("candidates") or [])
        except BuildCancelled:
            latest = self.load(pack_id)
            latest_selected = next(row for row in latest["windows"] if row["window_id"] == selected_id)
            if latest_selected.get("status") == "running" and latest_selected.get("started_at") == build_started_at:
                latest_selected["status"] = "pending"
                latest_selected["error_code"] = None
                atomic_write_json(self.manifest_path(pack_id), latest)
            raise
        except VideoPackError as exc:
            result_status = "failed"
            result_error = str(exc).split(":", 1)[0][:120]
        latest = self.load(pack_id)
        latest_selected = next(row for row in latest["windows"] if row["window_id"] == selected_id)
        # A concurrent seek may have advanced focus/revision while the window was
        # building. Merge only this window's terminal result into the latest
        # manifest; never write the stale focus snapshot back.
        latest_selected["status"] = result_status
        latest_selected["candidate_count"] = result_candidate_count
        latest_selected["error_code"] = result_error
        latest_selected["completed_at"] = _iso(self.clock()) if result_status in TERMINAL_WINDOW_STATES else None
        latest["revision"] = int(latest.get("revision", 0)) + 1
        self._refresh_summary(latest)
        atomic_write_json(self.manifest_path(pack_id), latest)
        return latest

    def build_all(self, pack_id: str, *, cancelled: Callable[[], bool] | None = None, on_revision: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        while True:
            if cancelled and cancelled():
                raise BuildCancelled("progressive_build_cancelled")
            manifest = self.build_one(pack_id, cancelled=cancelled)
            if on_revision:
                on_revision(deepcopy(manifest))
            if manifest.get("build_complete"):
                return manifest

    def build_until_horizon(
        self,
        pack_id: str,
        *,
        prefetch_ahead: int = 1,
        cancelled: Callable[[], bool] | None = None,
        on_revision: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        ahead = max(0, min(1, int(prefetch_ahead)))
        while True:
            manifest = self.recover(pack_id)
            if cancelled and cancelled():
                raise BuildCancelled("progressive_build_cancelled")
            playhead = float(manifest.get("focus", {}).get("playhead_sec") or 0.0)
            windows = manifest["windows"]
            focus_index = next(
                (
                    index for index, window in enumerate(windows)
                    if float(window["ownership_start_sec"]) <= playhead < float(window["ownership_end_sec"])
                ),
                len(windows) - 1,
            )
            target_ids = {
                windows[index]["window_id"]
                for index in range(focus_index, min(len(windows), focus_index + ahead + 1))
            }
            if not any(window["window_id"] in target_ids and window.get("status") == "pending" for window in windows):
                return manifest
            manifest = self.build_one(pack_id, cancelled=cancelled)
            if on_revision:
                on_revision(deepcopy(manifest))

    def delta(self, pack_id: str, *, since_revision: int = -1, playhead_sec: float | None = None) -> dict[str, Any]:
        manifest = self.recover(pack_id)
        revision = int(manifest.get("revision", 0))
        if revision <= int(since_revision):
            return {"pack_id": pack_id, "revision": revision, "changed": False, "status": manifest["status"], "build_complete": manifest["build_complete"]}
        playhead = float(playhead_sec if playhead_sec is not None else manifest.get("focus", {}).get("playhead_sec") or 0.0)
        windows = manifest["windows"]
        focus_index = next(
            (
                index for index, window in enumerate(windows)
                if float(window["ownership_start_sec"]) <= playhead < float(window["ownership_end_sec"])
            ),
            len(windows) - 1,
        )
        horizon_ids = {
            windows[index]["window_id"]
            for index in range(focus_index, min(len(windows), focus_index + 2))
        }
        ready_ids = prioritize_windows(
            [row for row in windows if row["window_id"] in horizon_ids and row.get("status") in READY_WINDOW_STATES],
            playhead,
        )
        captions: list[dict[str, Any]] = []
        cues: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        window_summaries: list[dict[str, Any]] = []
        effective_speech_sec = 0.0
        for window_id in ready_ids:
            shard = verify_window_shard(self.pack_path(pack_id) / "shards" / window_id, pack_id=pack_id, window_id=window_id)
            shard_speech = float(shard.get("effective_speech_sec") or 0.0)
            effective_speech_sec += shard_speech
            window_summaries.append({
                "window_id": window_id,
                "ownership_start_sec": float(shard["ownership_start_sec"]),
                "ownership_end_sec": float(shard["ownership_end_sec"]),
                "effective_speech_sec": round(shard_speech, 3),
            })
            captions.extend(deepcopy(shard.get("captions") or []))
            cues.extend(deepcopy(shard.get("transcript_cues") or []))
            candidates.extend(deepcopy(shard.get("candidates") or []))
        captions.sort(key=lambda row: (float(row["start"]), str(row["id"])))
        cues.sort(key=lambda row: (float(row["start_sec"]), str(row["cue_id"])))
        candidates.sort(key=lambda row: (float(row["anchor_sec"]), str(row["occurrence_id"])))
        return {
            "pack_id": pack_id,
            "video_id": manifest["video_id"],
            "source_url": manifest["source_url"],
            "title": manifest["title"],
            "duration_sec": manifest["duration_sec"],
            "effective_speech_sec": round(effective_speech_sec, 3),
            "window_summaries": window_summaries,
            "revision": revision,
            "changed": True,
            "status": manifest["status"],
            "usable": manifest["usable"],
            "build_complete": manifest["build_complete"],
            "coverage_fraction": manifest["coverage_fraction"],
            "ready_ranges": deepcopy(manifest["ready_ranges"]),
            "failed_ranges": deepcopy(manifest["failed_ranges"]),
            "captions": captions,
            "transcript_cues": cues,
            "candidates": candidates,
        }
