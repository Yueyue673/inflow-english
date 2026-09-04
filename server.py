from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from adaptive_core import (
    INTENSITIES,
    INTENSITY_RANK,
    FAMILIARITY_FEEDBACK,
    LEXICON_STATE_EDITS,
    POLICY_VERSION,
    REDUCER_VERSION,
    apply_encounter,
    apply_interaction,
    apply_lexicon_feedback,
    apply_probe_result,
    atomic_write_json,
    complete_session_adaptation,
    choose_nested_candidates,
    effective_intensity,
    eligible_candidate_count,
    ensure_profile,
    estimate_vocabulary_frontier,
    intensity_allows,
    intervention_budgets,
    item_priority,
    isoformat,
    knowledge_key_for_item,
    mapping_hold_ms,
    normalize_expression,
    new_profile,
    new_session,
    parse_time,
    profile_view,
    rebuild_profile_from_events,
    set_explicit_intensity,
    utc_now,
)
from caption_preview import build_caption_preview, caption_preview_id
from lexical_sense import annotate_lexical_identity, context_fingerprint, warm_wordnet_async
from progressive_video_pack import PROGRESSIVE_SCHEMA, ProgressiveVideoPackBuilder, verify_window_shard
from video_pack import ArgosHeuristicTranslator, BUILDER_VERSION, BuildCancelled, VideoPackBuilder, VideoPackError, validate_source_metadata, validate_youtube_url, verify_pack_directory

ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"
CONTENT_PATH = Path(os.environ.get("INFLOW_ADAPTIVE_CONTENT_PATH", str(ROOT / "content.json"))).resolve()
SOURCE_MEDIA_DIR = ROOT.parent / "mechanism-experiment" / "media"
LOCAL_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "InFlow-English"
LEGACY_DATA_DIR = ROOT / "data" / "live"
DATA_DIR = Path(
    os.environ.get(
        "INFLOW_ADAPTIVE_DATA_DIR",
        str(LEGACY_DATA_DIR if LEGACY_DATA_DIR.exists() else LOCAL_DATA_ROOT / "data"),
    )
).resolve()
SESSIONS_DIR = DATA_DIR / "sessions"
PROFILE_PATH = DATA_DIR / "profile.json"
EVENTS_PATH = DATA_DIR / "events.jsonl"
TRANSACTIONS_DIR = DATA_DIR / "transactions"
LEGACY_PACKS_DIR = ROOT / "data" / "packs"
_DEFAULT_PACKS_DIR = (
    DATA_DIR / "packs"
    if os.environ.get("INFLOW_ADAPTIVE_DATA_DIR")
    else LEGACY_PACKS_DIR
    if LEGACY_DATA_DIR.exists() and LEGACY_PACKS_DIR.exists()
    else LOCAL_DATA_ROOT / "packs"
)
PACKS_DIR = Path(os.environ.get("INFLOW_PACKS_DIR", str(_DEFAULT_PACKS_DIR))).resolve()
IMPORT_JOBS_DIR = DATA_DIR / "import-jobs"
CAPTION_PREVIEWS_DIR = DATA_DIR / "caption-previews"
CAPTION_JOBS_DIR = DATA_DIR / "caption-jobs"
PORT = int(os.environ.get("INFLOW_ADAPTIVE_PORT", "8767"))
QA_ALLOWED = os.environ.get("INFLOW_ADAPTIVE_QA") == "1"
PROGRESSIVE_ENABLED = os.environ.get("INFLOW_ENABLE_PROGRESSIVE") == "1" or QA_ALLOWED
FIXTURE_PACK_ID = "fixture-nasa-lro-v1"
WRITE_LOCK = threading.RLock()
IMPORT_LOCK = threading.RLock()
IMPORT_THREADS: dict[str, threading.Thread] = {}
CAPTION_LOCK = threading.RLock()
CAPTION_THREADS: dict[str, threading.Thread] = {}
HEAVY_QUEUE_LOCK = threading.RLock()
HEAVY_WORK_LOCK = threading.Lock()
HEAVY_RESERVATIONS: set[str] = set()
HEAVY_MAX_PENDING = 4
PACK_CACHE_LOCK = threading.RLock()
PACK_MANIFEST_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], dict[str, Any]]] = {}
SERVER_LOCK_HANDLE = None

CONTENT = json.loads(CONTENT_PATH.read_text(encoding="utf-8")) if CONTENT_PATH.exists() else {"items": []}
ITEMS = {item["id"]: item for item in CONTENT.get("items", [])}
FIXTURE_MEDIA_ALLOWLIST = {
    str(value).replace("\\", "/")
    for value in (
        [CONTENT.get("source_video"), CONTENT.get("captions")]
        + [item.get(field) for item in CONTENT.get("items", []) for field in ("phrase_audio", "followup_audio")]
    )
    if isinstance(value, str) and value.startswith("media/") and ".." not in value.split("/")
}
LEXICON_TRANSLATOR = ArgosHeuristicTranslator(timeout_sec=90.0)
WORDNET_WARMUP = warm_wordnet_async()

app = FastAPI(title="InFlow English Adaptive", docs_url=None, redoc_url=None)

DEFAULT_EXTENSION_ID = "hfkkhkdpakcmpokgbihceoppleeokifd"
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
if QA_ALLOWED:
    ALLOWED_HOSTS.add("testserver")
ALLOWED_ORIGINS = {
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
    f"chrome-extension://{DEFAULT_EXTENSION_ID}",
}
for configured_origin in os.environ.get("INFLOW_ALLOWED_EXTENSION_ORIGINS", "").split(","):
    configured_origin = configured_origin.strip()
    if re.fullmatch(r"chrome-extension://[a-p]{32}", configured_origin):
        ALLOWED_ORIGINS.add(configured_origin)


@app.middleware("http")
async def loopback_request_guard(request: Request, call_next):
    host = str(request.headers.get("host") or "").lower()
    if host not in ALLOWED_HOSTS:
        return JSONResponse(status_code=421, content={"detail": "host_not_allowed"})
    origin = str(request.headers.get("origin") or "")
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse(status_code=403, content={"detail": "origin_not_allowed"})
    fetch_site = str(request.headers.get("sec-fetch-site") or "").lower()
    if fetch_site == "cross-site" and not origin.startswith("chrome-extension://"):
        return JSONResponse(status_code=403, content={"detail": "cross_site_request_not_allowed"})
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
    return response


def pack_path(pack_id: str) -> Path:
    if not pack_id or len(pack_id) > 96 or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in pack_id):
        raise HTTPException(404, "pack_not_found")
    return PACKS_DIR / pack_id


def pack_stat_fingerprint(path: Path) -> tuple[tuple[str, int, int], ...]:
    rows = []
    try:
        for candidate in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
            if not candidate.is_file():
                continue
            stat = candidate.stat()
            rows.append((candidate.relative_to(path).as_posix(), int(stat.st_size), int(stat.st_mtime_ns)))
    except OSError as exc:
        raise HTTPException(409, "pack_failed_integrity_check") from exc
    return tuple(rows)


def load_pack_manifest(pack_id: str) -> dict[str, Any]:
    if pack_id == FIXTURE_PACK_ID:
        return {
            "pack_id": FIXTURE_PACK_ID,
            "status": "ready",
            "title": "NASA 月球样片（回归测试）",
            "duration_sec": 154.5,
            "video_id": "fixture-nasa-lro",
            "source_url": None,
            "candidates": deepcopy(CONTENT.get("items", [])),
            "media": {"video": "media/source.mp4", "captions_zh": "media/captions-zh.json"},
        }
    path = pack_path(pack_id)
    if not path.is_dir():
        raise HTTPException(404, "pack_not_found")
    fingerprint = pack_stat_fingerprint(path)
    with PACK_CACHE_LOCK:
        cached = PACK_MANIFEST_CACHE.get(pack_id)
        if cached and cached[0] == fingerprint:
            return deepcopy(cached[1])
    try:
        manifest = verify_pack_directory(path, expected_pack_id=pack_id, require_audit=True)
    except VideoPackError as exc:
        with PACK_CACHE_LOCK:
            PACK_MANIFEST_CACHE.pop(pack_id, None)
        raise HTTPException(409, "pack_failed_integrity_check") from exc
    verified_fingerprint = pack_stat_fingerprint(path)
    with PACK_CACHE_LOCK:
        PACK_MANIFEST_CACHE[pack_id] = (verified_fingerprint, deepcopy(manifest))
    return deepcopy(manifest)


def find_existing_pack_for_url(source_url: str) -> dict[str, Any] | None:
    if not PACKS_DIR.is_dir():
        return None
    matches = []
    for path in PACKS_DIR.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            manifest = load_pack_manifest(path.name)
        except HTTPException:
            continue
        if manifest.get("source_url") == source_url:
            matches.append(manifest)
    if not matches:
        return None
    matches.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return matches[0]


def pack_effective_speech_sec(pack_id: str, manifest: dict[str, Any]) -> float | None:
    declared = manifest.get("effective_speech_sec")
    if declared is not None:
        try:
            value = float(declared)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return round(value, 3)
    transcript_path = pack_path(pack_id) / str(manifest.get("media", {}).get("transcript") or "transcript.json")
    try:
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    intervals: list[tuple[float, float]] = []
    for word in transcript.get("words", []):
        if not isinstance(word, dict):
            continue
        try:
            start = float(word.get("start_sec"))
            end = float(word.get("end_sec"))
        except (TypeError, ValueError):
            continue
        if start >= 0 and end > start:
            intervals.append((start, end))
    intervals.sort()
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 0.15:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    total = sum(end - start for start, end in merged)
    return round(total, 3) if total > 0 else None


def is_progressive_pack(pack_id: str) -> bool:
    if not re.fullmatch(r"ytp-[A-Za-z0-9_-]{11}-[0-9a-f]{12}", str(pack_id or "")):
        return False
    return (PACKS_DIR / pack_id / "partial-manifest.json").is_file()


def progressive_content_from_delta(pack_id: str, manifest: dict[str, Any], delta: dict[str, Any], *, playhead_sec: float) -> dict[str, Any]:
    items = []
    for candidate in delta.get("candidates", []):
        if float(candidate.get("anchor_sec") or 0.0) <= playhead_sec + 0.5:
            continue
        row = deepcopy(candidate)
        row["sentence_zh"] = row["phrase_zh"]
        row["accepted_zh"] = [row["gloss_zh"]]
        row["probe_eligible"] = False
        row["phrase_audio"] = f"/api/progressive/{pack_id}/files/{row['phrase_audio']}"
        items.append(row)
    return {
        "product_id": pack_id,
        "source_offset_sec": 0.0,
        "video_duration_sec": float(manifest["duration_sec"]),
        "effective_speech_sec": float(delta.get("effective_speech_sec") or 0.0),
        "progressive_windows": deepcopy(delta.get("window_summaries") or []),
        "items": items,
    }


def progressive_pack_content(pack_id: str, *, playhead_sec: float) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    builder = ProgressiveVideoPackBuilder(PACKS_DIR)
    manifest = builder.load(pack_id)
    if not manifest.get("usable"):
        raise HTTPException(409, "progressive_pack_not_usable")
    delta = builder.delta(pack_id, since_revision=-1, playhead_sec=playhead_sec)
    content = progressive_content_from_delta(pack_id, manifest, delta, playhead_sec=playhead_sec)
    video = {
        "source": None,
        "captions": None,
        "transcript": None,
        "native_youtube": True,
        "source_url": manifest["source_url"],
        "source_offset_sec": 0.0,
        "title": manifest["title"],
        "progressive_pack_id": pack_id,
        "progressive_revision": int(delta["revision"]),
    }
    return manifest, content, video


def pack_content(pack_id: str) -> dict[str, Any]:
    if pack_id == FIXTURE_PACK_ID:
        fixture = deepcopy(CONTENT)
        fixture["intervention_budget_override"] = {"low": 1, "medium": 2, "high": 4}
        return fixture
    manifest = load_pack_manifest(pack_id)
    items = []
    for candidate in manifest.get("candidates", []):
        row = deepcopy(candidate)
        row["sentence_zh"] = row["phrase_zh"]
        row["accepted_zh"] = [row["gloss_zh"]]
        row["probe_eligible"] = False
        row["phrase_audio"] = f"/api/packs/{pack_id}/files/{row['phrase_audio']}"
        items.append(row)
    return {
        "product_id": manifest["pack_id"],
        "source_offset_sec": 0.0,
        "video_duration_sec": float(manifest.get("duration_sec") or manifest.get("duration") or 0),
        "effective_speech_sec": pack_effective_speech_sec(pack_id, manifest),
        "items": items,
    }


def pack_video_view(pack_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    if pack_id == FIXTURE_PACK_ID:
        return {
            "source": "media/source.mp4",
            "captions": "media/captions-zh.json",
            "source_offset_sec": float(CONTENT.get("source_offset_sec", 0)),
            "title": manifest["title"],
        }
    return {
        "source": f"/api/packs/{pack_id}/files/{manifest['media']['video']}",
        "captions": f"/api/packs/{pack_id}/files/{manifest['media']['captions_zh']}",
        "transcript": f"/api/packs/{pack_id}/files/{manifest['media']['transcript']}",
        "source_offset_sec": 0.0,
        "title": manifest["title"],
    }


def acquire_server_lock():
    global SERVER_LOCK_HANDLE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "server.lock"
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        handle.close()
        raise RuntimeError(f"data directory already owned by another InFlow process: {DATA_DIR}")
    SERVER_LOCK_HANDLE = handle
    return handle


def probe_semantic_fingerprint(record: dict[str, Any]) -> str:
    semantic = {
        key: record.get(key)
        for key in (
            "probe_id",
            "item_id",
            "variant_id",
            "outcome",
            "selected_choice_id",
            "audio_confirmed",
            "presentation_count",
            "choice_count",
        )
    }
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_event(
    event_type: str,
    data: dict[str, Any],
    *,
    session_id: str | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": uuid.uuid4().hex,
        "type": event_type,
        "at": isoformat(at or utc_now()),
        "session_id": session_id,
        "policy_version": POLICY_VERSION,
        "data": data,
    }


def existing_event_ids() -> set[str]:
    if not EVENTS_PATH.exists():
        return set()
    ids: set[str] = set()
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event_id = json.loads(line).get("event_id")
        except json.JSONDecodeError:
            continue
        if event_id:
            ids.add(str(event_id))
    return ids


def append_event_payload(payload: dict[str, Any]) -> None:
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    with EVENTS_PATH.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() > 0:
            stream.seek(-1, os.SEEK_END)
            last_byte = stream.read(1)
            stream.seek(0, os.SEEK_END)
            if last_byte != b"\n":
                stream.write(b"\n")
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def apply_transaction(payload: dict[str, Any]) -> None:
    session = payload.get("session")
    profile = payload.get("profile")
    if session is not None:
        atomic_write_json(session_path(session["session_id"]), session)
    if profile is not None:
        atomic_write_json(PROFILE_PATH, profile)
    known_event_ids = existing_event_ids()
    for event in payload.get("events", []):
        if event["event_id"] not in known_event_ids:
            append_event_payload(event)
            known_event_ids.add(event["event_id"])


def recover_transactions() -> None:
    if not TRANSACTIONS_DIR.exists():
        return
    for path in sorted(TRANSACTIONS_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        apply_transaction(payload)
        path.unlink(missing_ok=True)


def commit_state(
    *,
    profile: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> None:
    transaction_id = uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "profile": profile,
        "session": session,
        "events": events or [],
    }
    path = TRANSACTIONS_DIR / f"{transaction_id}.json"
    atomic_write_json(path, payload)
    apply_transaction(payload)
    path.unlink(missing_ok=True)


def load_seed_known_ids() -> list[str]:
    seed_path = ROOT / "seed-profile.json"
    if not seed_path.exists():
        return []
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    return [item_id for item_id in payload.get("known_ids", []) if item_id in ITEMS]


def migrate_profile_reducer(profile: dict[str, Any]) -> dict[str, Any]:
    stored = str(profile.get("reducer_version") or "")
    if not stored or stored == REDUCER_VERSION:
        return ensure_profile(profile, CONTENT)
    version_match = re.fullmatch(r"rules-v(\d+)", stored)
    current_match = re.fullmatch(r"rules-v(\d+)", REDUCER_VERSION)
    if not version_match or not current_match or int(version_match.group(1)) > int(current_match.group(1)):
        raise RuntimeError(f"unsupported_profile_reducer:{stored}")
    if not EVENTS_PATH.exists():
        raise RuntimeError(f"profile_reducer_migration_requires_events:{stored}:{REDUCER_VERSION}")
    from rebuild_profile import load_combined_content, read_events

    complete_content = load_combined_content(DATA_DIR)
    events = read_events(EVENTS_PATH)
    if not events:
        raise RuntimeError(f"profile_reducer_migration_requires_events:{stored}:{REDUCER_VERSION}")
    rebuilt = rebuild_profile_from_events(complete_content, events, known_ids=load_seed_known_ids())
    if profile.get("legacy_lexicon_v1"):
        rebuilt["legacy_lexicon_v1"] = deepcopy(profile["legacy_lexicon_v1"])
    missing_items = sorted(set(profile.get("items", {})) - set(rebuilt.get("items", {})))
    if missing_items:
        raise RuntimeError(f"profile_reducer_migration_missing_content:{','.join(missing_items[:8])}")
    profile_digest = hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    backup_path = DATA_DIR / "backups" / f"profile.{stored}.pre-{REDUCER_VERSION}.{profile_digest}.json"
    if not backup_path.exists():
        atomic_write_json(backup_path, profile)
    commit_state(profile=rebuilt)
    return rebuilt


def load_profile() -> dict[str, Any]:
    recover_transactions()
    if PROFILE_PATH.exists():
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        normalized = migrate_profile_reducer(profile)
        active_id = normalized.get("active_session_id")
        if active_id and active_id not in normalized.get("active_sessions", {}).values():
            path = session_path(active_id)
            if path.exists():
                active_session = json.loads(path.read_text(encoding="utf-8"))
                pack_id = str(active_session.get("pack_id") or "fixture:nasa-lro-v1")
                normalized.setdefault("active_sessions", {})[pack_id] = active_id
        if normalized != profile:
            commit_state(profile=normalized)
        return normalized
    event_time = utc_now()
    profile = new_profile(CONTENT, load_seed_known_ids(), now=event_time)
    commit_state(
        profile=profile,
        events=[
            build_event(
                "profile_created",
                {"seed": "legacy_pretest_read_only", "known_count": len(load_seed_known_ids())},
                at=event_time,
            )
        ],
    )
    return profile


def session_path(session_id: str) -> Path:
    if len(session_id) != 32 or any(character not in "0123456789abcdef" for character in session_id):
        raise HTTPException(404, "session_not_found")
    return SESSIONS_DIR / f"{session_id}.json"


def load_session(session_id: str) -> dict[str, Any]:
    recover_transactions()
    path = session_path(session_id)
    if not path.exists():
        raise HTTPException(404, "session_not_found")
    session = json.loads(path.read_text(encoding="utf-8"))
    if int(session.get("schema_version", 1)) > 1:
        raise HTTPException(409, "unsupported_session_schema")
    original = deepcopy(session)
    if session.get("stage") != "complete" and str(session.get("pack_id") or FIXTURE_PACK_ID) == FIXTURE_PACK_ID:
        current_items = {item["id"]: item for item in CONTENT.get("items", [])}
        migrated_items = []
        for item in session.get("items", []):
            current = current_items.get(item.get("id"))
            if current and item.get("id") not in session.get("interactions", {}):
                policy_fields = {key: item[key] for key in ("min_intensity", "policy_priority") if key in item}
                item = {**deepcopy(current), **policy_fields}
            migrated_items.append(item)
        session["items"] = migrated_items
        session["pack_id"] = FIXTURE_PACK_ID
        session.setdefault(
            "encounter_catalog",
            [{"id": item["id"], "anchor_sec": item["anchor_sec"]} for item in CONTENT.get("items", [])],
        )
        session.setdefault(
            "video",
            {
                "source": "media/source.mp4",
                "captions": "media/captions-zh.json",
                "source_offset_sec": float(CONTENT.get("source_offset_sec", 0)),
            },
        )
        session.setdefault("intervention_budget_includes_probe", True)
    if session != original:
        commit_state(session=session)
    return session


def record_encounter_locked(
    session: dict[str, Any],
    profile: dict[str, Any],
    item_id: str,
    *,
    at: datetime,
) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
    encountered = session.setdefault("encountered_ids", [])
    if item_id in encountered:
        return profile, False, []
    valid_item_ids = {str(row.get("id")) for row in session.get("encounter_catalog", []) if isinstance(row, dict)}
    if item_id not in valid_item_ids:
        raise HTTPException(422, "unknown_item")
    profile = apply_encounter(profile, item_id, now=at)
    encountered.append(item_id)
    event = build_event(
        "encounter_recorded",
        {"item_id": item_id, "translation_visible": True},
        session_id=session["session_id"],
        at=at,
    )
    return profile, True, [event]


def item_view(item: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    state = profile.get("items", {}).get(item["id"], {})
    knowledge_key = str(state.get("knowledge_key") or knowledge_key_for_item(item))
    lexical = profile.get("lexicon", {}).get(knowledge_key, {})
    return {
        "id": item["id"],
        "knowledge_key": knowledge_key,
        "familiarity_status": lexical.get("status", state.get("self_report_status", "unseen")),
        "explicit_known": bool(lexical.get("explicit_known", state.get("explicit_known", False))),
        "surface": item["surface"],
        "gloss_zh": item["gloss_zh"],
        "sentence_zh": item.get("sentence_zh") or item.get("phrase_zh") or item["gloss_zh"],
        "phrase_text": item["phrase_text"],
        "phrase_zh": item.get("phrase_zh") or item.get("sentence_zh") or item["gloss_zh"],
        "display_unit": item.get("display_unit", "natural_excerpt"),
        "phrase_audio": item["phrase_audio"],
        "anchor_sec": item["anchor_sec"],
        "safe_pause_gap_sec": item.get("safe_pause_gap_sec"),
        "pause_delay_sec": item.get("pause_delay_sec"),
        "highlight_start_sec": item["highlight_start_sec"],
        "highlight_end_sec": item["highlight_end_sec"],
        "alignment_quality": item["alignment_quality"],
        "min_intensity": item["min_intensity"],
        "mapping_hold_ms": mapping_hold_ms(profile, item["surface"], item["gloss_zh"]),
    }


def probe_public_view(session: dict[str, Any]) -> dict[str, Any] | None:
    probe = session.get("probe")
    if not probe:
        return None
    public = {
        "probe_id": probe["probe_id"],
        "audio_url": f"/api/sessions/{session['session_id']}/probe/audio/{probe['probe_id']}",
        "choices": [
            {"choice_id": row["choice_id"], "label": row["label"]}
            for row in probe.get("choices", [])
        ],
        "started_at": probe.get("started_at"),
        "presentation_count": int(probe.get("presentation_count", 0)),
        "result": deepcopy(probe.get("result")),
        "feedback": None,
    }
    if probe.get("result") and probe["result"].get("outcome") in {"correct", "incorrect", "dont_know"}:
        snapshot = probe.get("feedback_snapshot") or {}
        public["feedback"] = {
            "surface": snapshot.get("surface"),
            "gloss_zh": snapshot.get("gloss_zh"),
            "followup_sentence": snapshot.get("followup_sentence"),
            "audio_url": public["audio_url"],
        }
    return public


def session_view(session: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    outcomes = {"completed": 0, "skipped": 0, "technical_failure": 0}
    current_epoch = int(profile.get("adaptive", {}).get("preference_epoch", 1))
    current_epoch_skipped = 0
    for record in session.get("interactions", {}).values():
        outcome = record.get("outcome")
        if outcome in outcomes:
            outcomes[outcome] += 1
        if outcome == "skipped" and int(record.get("preference_epoch", session.get("preference_epoch_at_start", 1))) == current_epoch:
            current_epoch_skipped += 1
    return {
        "session_id": session["session_id"],
        "pack_id": str(session.get("pack_id") or FIXTURE_PACK_ID),
        "progressive": bool(session.get("progressive", False)),
        "progressive_revision": session.get("progressive_revision"),
        "progressive_ready_windows": sorted((session.get("progressive_window_speech") or {}).keys()),
        "playhead_at_creation": session.get("playhead_at_creation"),
        "stage": session["stage"],
        "owner_client_id": session.get("owner_client_id"),
        "owner_epoch": int(session.get("owner_epoch", 0)),
        "created_at": session["created_at"],
        "started_at": session.get("started_at"),
        "completed_at": session.get("completed_at"),
        "last_media_time": session.get("last_media_time", 0),
        "progress_seq": session.get("progress_seq", 0),
        "open_interaction": session.get("open_interaction"),
        "probe": probe_public_view(session),
        "candidate_pool_count": int(session.get("candidate_pool_count", len(session.get("items", [])))),
        "eligible_candidate_count": int(session.get("eligible_candidate_count", len(session.get("items", [])))),
        "effective_speech_sec": session.get("effective_speech_sec"),
        "intervention_budgets": deepcopy(session.get("intervention_budgets") or {"low": 1, "medium": 2, "high": 4}),
        "teaching_budgets": deepcopy(session.get("teaching_budgets") or {"low": 1, "medium": 2, "high": 4}),
        "intervention_budget_includes_probe": bool(session.get("intervention_budget_includes_probe", False)),
        "completed_ids": list(session.get("interactions", {})),
        "encountered_ids": list(session.get("encountered_ids", [])),
        "encounter_catalog": deepcopy(
            session.get("encounter_catalog")
            or [{"id": item["id"], "anchor_sec": item["anchor_sec"]} for item in CONTENT.get("items", [])]
        ),
        "interaction_summary": outcomes,
        "current_epoch_skipped": current_epoch_skipped,
        "items": [item_view(item, profile) for item in session.get("items", [])],
        "profile": profile_view(profile),
        "video": deepcopy(
            session.get("video")
            or {
                "source": "media/source.mp4",
                "captions": "media/captions-zh.json",
                "source_offset_sec": float(CONTENT.get("source_offset_sec", 0)),
            }
        ),
    }


def merge_progressive_session(
    session: dict[str, Any],
    profile: dict[str, Any],
    content: dict[str, Any],
    *,
    revision: int,
    playhead_sec: float,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    session = deepcopy(session)
    profile = ensure_profile(profile, content)
    catalog = session.setdefault("encounter_catalog", [])
    catalog_ids = {str(row.get("id")) for row in catalog}
    for item in content.get("items", []):
        if item["id"] not in catalog_ids and float(item.get("anchor_sec") or 0.0) > playhead_sec + 0.5:
            catalog.append({"id": item["id"], "anchor_sec": item["anchor_sec"]})
            catalog_ids.add(item["id"])
    catalog.sort(key=lambda row: (float(row["anchor_sec"]), str(row["id"])))

    existing_items = session.setdefault("items", [])
    existing_ids = {str(item["id"]) for item in existing_items}
    new_pool = [item for item in content.get("items", []) if item["id"] not in existing_ids and float(item.get("anchor_sec") or 0.0) > playhead_sec + 0.5]

    window_speech = session.setdefault("progressive_window_speech", {})
    for row in content.get("progressive_windows", []):
        window_id = str(row.get("window_id") or "")
        if window_id and window_id not in window_speech:
            window_speech[window_id] = float(row.get("effective_speech_sec") or 0.0)
    cumulative_speech = sum(max(0.0, float(value or 0.0)) for value in window_speech.values())

    frontier = estimate_vocabulary_frontier(profile)
    eligible_ids = set(session.get("progressive_eligible_ids") or [])
    priority_time = utc_now()
    for item in content.get("items", []):
        state = profile.get("items", {}).get(item["id"], {})
        if item_priority(item, state, priority_time, vocabulary_frontier=frontier) > -1000:
            eligible_ids.add(item["id"])
    session["progressive_eligible_ids"] = sorted(eligible_ids)
    eligible = len(eligible_ids)
    proposed_budgets = intervention_budgets(cumulative_speech, eligible)
    current_budgets = session.get("intervention_budgets") or {name: 0 for name in INTENSITIES}
    total_budgets = {name: max(int(current_budgets.get(name, 0)), int(proposed_budgets.get(name, 0))) for name in INTENSITIES}

    existing_allowed = {
        name: sum(INTENSITY_RANK.get(str(item.get("min_intensity") or "high"), 2) <= INTENSITY_RANK[name] for item in existing_items)
        for name in INTENSITIES
    }
    remaining = {
        name: max(0, total_budgets[name] - existing_allowed[name])
        for name in INTENSITIES
    }
    remaining["medium"] = max(remaining["low"], remaining["medium"])
    remaining["high"] = max(remaining["medium"], remaining["high"])
    added: list[dict[str, Any]] = []
    if new_pool and remaining["high"] > 0:
        proposal = {**content, "items": new_pool}
        seed_material = f"{session['session_id']}:{revision}".encode("utf-8")
        seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
        added = choose_nested_candidates(proposal, profile, seed=seed, tier_budgets=remaining)
        existing_items.extend(added)

    session["candidate_pool_count"] = max(int(session.get("candidate_pool_count", 0)), len(catalog))
    session["eligible_candidate_count"] = max(int(session.get("eligible_candidate_count", 0)), eligible)
    session["effective_speech_sec"] = max(float(session.get("effective_speech_sec") or 0.0), cumulative_speech)
    session["intervention_budgets"] = total_budgets
    session["teaching_budgets"] = total_budgets
    session["progressive_revision"] = max(int(session.get("progressive_revision") or 0), int(revision))
    session.setdefault("video", {})["progressive_revision"] = session["progressive_revision"]
    return session, profile, [str(item["id"]) for item in added]


async def read_payload(request: Request) -> dict[str, Any]:
    media_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(415, "application_json_required")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as error:
            raise HTTPException(400, "invalid_content_length") from error
        if declared_size < 0:
            raise HTTPException(400, "invalid_content_length")
        if declared_size > 32_768:
            raise HTTPException(413, "json_body_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32_768:
            raise HTTPException(413, "json_body_too_large")
    try:
        payload = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(400, "invalid_json") from error
    if not isinstance(payload, dict):
        raise HTTPException(400, "json_object_required")
    return payload


def client_id_from(payload: dict[str, Any]) -> str:
    client_id = str(payload.get("client_id") or "")
    if len(client_id) != 32 or any(character not in "0123456789abcdef" for character in client_id):
        raise HTTPException(422, "invalid_client_id")
    return client_id


def require_session_owner(session: dict[str, Any], payload: dict[str, Any]) -> str:
    client_id = client_id_from(payload)
    if session.get("owner_client_id") != client_id:
        raise HTTPException(409, "session_owned_by_another_client")
    if int(payload.get("owner_epoch") or 0) != int(session.get("owner_epoch", 0)):
        raise HTTPException(409, "stale_owner_epoch")
    return client_id


def import_job_path(job_id: str) -> Path:
    if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
        raise HTTPException(404, "import_job_not_found")
    return IMPORT_JOBS_DIR / f"{job_id}.json"


def load_import_job(job_id: str) -> dict[str, Any]:
    path = import_job_path(job_id)
    if not path.is_file():
        raise HTTPException(404, "import_job_not_found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(409, "import_job_corrupt") from exc
    if not isinstance(payload, dict):
        raise HTTPException(409, "import_job_corrupt")
    return payload


def save_import_job(job: dict[str, Any]) -> None:
    IMPORT_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(import_job_path(str(job["job_id"])), job)


def caption_job_path(preview_id: str) -> Path:
    if not re.fullmatch(r"yt-[A-Za-z0-9_-]{11}-[0-9a-f]{16}", str(preview_id or "")):
        raise HTTPException(404, "caption_preview_not_found")
    return CAPTION_JOBS_DIR / f"{preview_id}.json"


def caption_content_path(preview_id: str) -> Path:
    caption_job_path(preview_id)
    return CAPTION_PREVIEWS_DIR / f"{preview_id}.json"


def load_caption_job(preview_id: str) -> dict[str, Any]:
    path = caption_job_path(preview_id)
    if not path.is_file():
        raise HTTPException(404, "caption_preview_not_found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(409, "caption_preview_job_corrupt") from exc
    if not isinstance(payload, dict) or payload.get("preview_id") != preview_id:
        raise HTTPException(409, "caption_preview_job_corrupt")
    return payload


def save_caption_job(job: dict[str, Any]) -> None:
    CAPTION_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(caption_job_path(str(job["preview_id"])), job)


def caption_job_public(job: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: deepcopy(job.get(key))
        for key in (
            "preview_id", "video_id", "source_url", "title", "status", "revision", "usable",
            "language", "coverage_fraction", "translated_segments", "total_segments", "error_code",
            "retryable", "created_at", "updated_at", "completed_at",
        )
    }
    result["content_path"] = f"/api/caption-previews/{job['preview_id']}/content" if job.get("usable") else None
    return result


def run_caption_job(preview_id: str) -> None:
    try:
        with CAPTION_LOCK:
            job = load_caption_job(preview_id)
            job.update(status="running", updated_at=isoformat(utc_now()))
            save_caption_job(job)

        def publish(document: dict[str, Any]) -> None:
            with CAPTION_LOCK:
                current = load_caption_job(preview_id)
                has_zh = int(document.get("translated_segments") or 0) > 0
                current.update(
                    title=document.get("title"),
                    status=str(document.get("status") or "english_ready"),
                    revision=int(document.get("revision") or 0),
                    usable=True,
                    language="zh" if has_zh else "en",
                    coverage_fraction=float(document.get("coverage_fraction") or 0.0),
                    translated_segments=int(document.get("translated_segments") or 0),
                    total_segments=int(document.get("total_segments") or 0),
                    error_code=None,
                    updated_at=isoformat(utc_now()),
                )
                save_caption_job(current)

        build_caption_preview(
            job["source_url"],
            caption_content_path(preview_id),
            on_publish=publish,
        )
        with CAPTION_LOCK:
            job = load_caption_job(preview_id)
            now = isoformat(utc_now())
            job.update(status="ready", usable=True, retryable=False, completed_at=now, updated_at=now)
            save_caption_job(job)
    except VideoPackError as exc:
        with CAPTION_LOCK:
            job = load_caption_job(preview_id)
            path = caption_content_path(preview_id)
            usable = path.is_file()
            if usable:
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    usable = False
                    document = {}
            else:
                document = {}
            now = isoformat(utc_now())
            job.update(
                status="degraded" if usable else "failed",
                usable=usable,
                revision=int(document.get("revision") or job.get("revision") or 0),
                language="zh" if int(document.get("translated_segments") or 0) else "en" if usable else None,
                coverage_fraction=float(document.get("coverage_fraction") or 0.0),
                translated_segments=int(document.get("translated_segments") or 0),
                total_segments=int(document.get("total_segments") or 0),
                error_code=str(exc).split(":", 1)[0][:120],
                retryable=True,
                completed_at=now,
                updated_at=now,
            )
            save_caption_job(job)
    except Exception as exc:
        with CAPTION_LOCK:
            job = load_caption_job(preview_id)
            path = caption_content_path(preview_id)
            usable = path.is_file()
            safe_type = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:60] or "unknown"
            now = isoformat(utc_now())
            job.update(
                status="degraded" if usable else "failed",
                usable=usable,
                error_code=f"unexpected_{safe_type}",
                retryable=True,
                completed_at=now,
                updated_at=now,
            )
            save_caption_job(job)
    finally:
        with CAPTION_LOCK:
            CAPTION_THREADS.pop(preview_id, None)


def reserve_heavy_job(token: str) -> bool:
    with HEAVY_QUEUE_LOCK:
        if token in HEAVY_RESERVATIONS:
            return True
        if len(HEAVY_RESERVATIONS) >= HEAVY_MAX_PENDING + 1:
            return False
        HEAVY_RESERVATIONS.add(token)
        return True


class HeavyQueueFull(RuntimeError):
    pass


def run_reserved_heavy_call(token: str, callback):
    if not reserve_heavy_job(token):
        raise HeavyQueueFull("heavy_queue_full")
    try:
        with HEAVY_WORK_LOCK:
            return callback()
    finally:
        with HEAVY_QUEUE_LOCK:
            HEAVY_RESERVATIONS.discard(token)


def run_reserved_heavy_job(
    token: str,
    identifier: str,
    runner,
    registry: dict[str, threading.Thread],
    registry_lock: threading.RLock,
) -> None:
    try:
        with HEAVY_WORK_LOCK:
            runner(identifier)
    finally:
        with registry_lock:
            registry.pop(identifier, None)
        with HEAVY_QUEUE_LOCK:
            HEAVY_RESERVATIONS.discard(token)


def start_caption_thread(preview_id: str) -> bool:
    with CAPTION_LOCK:
        existing = CAPTION_THREADS.get(preview_id)
        if existing and existing.is_alive():
            return True
        token = f"caption:{preview_id}"
        if not reserve_heavy_job(token):
            return False
        thread = threading.Thread(
            target=run_reserved_heavy_job,
            args=(token, preview_id, run_caption_job, CAPTION_THREADS, CAPTION_LOCK),
            name=f"inflow-caption-{preview_id[3:11]}",
            daemon=True,
        )
        CAPTION_THREADS[preview_id] = thread
        thread.start()
        return True


def import_job_public(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(job.get(key))
        for key in (
            "job_id", "source_url", "pack_id", "title", "status", "stage", "fraction", "detail",
            "cancel_requested", "error_code", "retryable", "retry_blocked", "created_at", "updated_at", "started_at",
            "completed_at", "cached", "route", "playhead_sec", "usable", "revision", "ready_ranges",
            "coverage_fraction", "build_complete", "failed_ranges",
        )
    }


def run_import_job(job_id: str) -> None:
    try:
        with IMPORT_LOCK:
            job = load_import_job(job_id)
            if job.get("cancel_requested"):
                now = isoformat(utc_now())
                job.update(status="cancelled", stage="cancelled", completed_at=now, updated_at=now)
                save_import_job(job)
                return
            job.update(status="running", started_at=job.get("started_at") or isoformat(utc_now()), updated_at=isoformat(utc_now()))
            save_import_job(job)

        def cancelled() -> bool:
            with IMPORT_LOCK:
                return bool(load_import_job(job_id).get("cancel_requested"))

        def progress(event: dict[str, Any]) -> None:
            with IMPORT_LOCK:
                current = load_import_job(job_id)
                current.update(
                    status="running" if event.get("stage") != "ready" else "ready",
                    stage=str(event.get("stage") or current.get("stage") or "queued"),
                    fraction=float(event.get("fraction") or 0.0),
                    detail=event.get("detail"),
                    pack_id=event.get("pack_id") or current.get("pack_id"),
                    cached=bool(event.get("cached", False)),
                    updated_at=isoformat(utc_now()),
                )
                save_import_job(current)

        builder = VideoPackBuilder(PACKS_DIR)
        reference = validate_youtube_url(job["source_url"])
        inspected = validate_source_metadata(
            reference,
            builder.media_tools.inspect(reference.canonical_url),
            max_duration_sec=3 * 60 * 60,
        )
        duration = float(inspected["duration_sec"])
        if duration >= 30 * 60:
            if not PROGRESSIVE_ENABLED:
                raise VideoPackError("progressive_learning_not_enabled")
            preview_id = caption_preview_id(reference.canonical_url)
            preview_path = caption_content_path(preview_id)
            if not preview_path.is_file():
                raise VideoPackError("long_video_english_captions_required")
            try:
                caption_document = json.loads(preview_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise VideoPackError("caption_preview_corrupt") from exc
            progressive = ProgressiveVideoPackBuilder(PACKS_DIR, base_builder=builder)
            manifest = progressive.initialize(
                reference.canonical_url,
                caption_document,
                playhead_sec=float(job.get("playhead_sec") or 0.0),
            )
            with IMPORT_LOCK:
                current = load_import_job(job_id)
                current.update(
                    route="progressive",
                    pack_id=manifest["pack_id"],
                    title=manifest["title"],
                    status=manifest["status"],
                    stage="windowing",
                    usable=bool(manifest["usable"]),
                    revision=int(manifest["revision"]),
                    ready_ranges=manifest["ready_ranges"],
                    coverage_fraction=float(manifest["coverage_fraction"]),
                    build_complete=bool(manifest["build_complete"]),
                    failed_ranges=manifest["failed_ranges"],
                    updated_at=isoformat(utc_now()),
                )
                save_import_job(current)

            def publish_revision(latest: dict[str, Any]) -> None:
                with IMPORT_LOCK:
                    current = load_import_job(job_id)
                    complete = bool(latest["build_complete"])
                    current.update(
                        status=latest["status"],
                        stage="complete" if complete else "partial_ready" if latest["usable"] else "windowing",
                        fraction=float(latest["coverage_fraction"]),
                        usable=bool(latest["usable"]),
                        revision=int(latest["revision"]),
                        ready_ranges=latest["ready_ranges"],
                        coverage_fraction=float(latest["coverage_fraction"]),
                        build_complete=complete,
                        failed_ranges=latest["failed_ranges"],
                        error_code=None,
                        retryable=not complete,
                        completed_at=isoformat(utc_now()) if complete else None,
                        updated_at=isoformat(utc_now()),
                    )
                    save_import_job(current)

            manifest = progressive.build_until_horizon(
                manifest["pack_id"],
                prefetch_ahead=1,
                cancelled=cancelled,
                on_revision=publish_revision,
            )
            publish_revision(manifest)
        elif duration <= 15 * 60:
            with IMPORT_LOCK:
                current = load_import_job(job_id)
                current.update(route="atomic", usable=False, build_complete=False, updated_at=isoformat(utc_now()))
                save_import_job(current)
            pack = builder.build(job["source_url"], progress_callback=progress, cancel_callback=cancelled)
            manifest = load_pack_manifest(pack.name)
            with IMPORT_LOCK:
                job = load_import_job(job_id)
                now = isoformat(utc_now())
                job.update(status="ready", stage="ready", fraction=1.0, route="atomic", usable=True, revision=1, ready_ranges=[[0.0, float(manifest.get("duration_sec") or manifest.get("duration"))]], coverage_fraction=1.0, build_complete=True, failed_ranges=[], pack_id=pack.name, title=manifest.get("title"), completed_at=now, updated_at=now, error_code=None, retryable=False)
                save_import_job(job)
        else:
            raise VideoPackError("source_duration_out_of_range")
    except BuildCancelled:
        with IMPORT_LOCK:
            job = load_import_job(job_id)
            now = isoformat(utc_now())
            if job.get("route") == "progressive" and job.get("usable"):
                job.update(status="partial_ready", stage="paused", completed_at=now, updated_at=now, retryable=True)
            else:
                job.update(status="cancelled", stage="cancelled", completed_at=now, updated_at=now, retryable=True)
            save_import_job(job)
    except (VideoPackError, HTTPException) as exc:
        with IMPORT_LOCK:
            job = load_import_job(job_id)
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            safe_code = str(detail).split(":", 1)[0][:120]
            now = isoformat(utc_now())
            if job.get("route") == "progressive" and job.get("usable"):
                job.update(status="partial_ready", stage="degraded", error_code=safe_code, retryable=True, completed_at=now, updated_at=now)
            else:
                job.update(status="failed", stage="failed", error_code=safe_code, retryable=True, completed_at=now, updated_at=now)
            save_import_job(job)
    except Exception as exc:
        with IMPORT_LOCK:
            job = load_import_job(job_id)
            now = isoformat(utc_now())
            safe_type = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:60] or "unknown"
            if job.get("route") == "progressive" and job.get("usable"):
                job.update(status="partial_ready", stage="degraded", error_code=f"unexpected_{safe_type}", retryable=True, completed_at=now, updated_at=now)
            else:
                job.update(status="failed", stage="failed", error_code=f"unexpected_{safe_type}", retryable=True, completed_at=now, updated_at=now)
            save_import_job(job)
    finally:
        with IMPORT_LOCK:
            IMPORT_THREADS.pop(job_id, None)


def start_import_thread(job_id: str) -> bool:
    with IMPORT_LOCK:
        existing = IMPORT_THREADS.get(job_id)
        if existing and existing.is_alive():
            return True
        token = f"import:{job_id}"
        if not reserve_heavy_job(token):
            return False
        thread = threading.Thread(
            target=run_reserved_heavy_job,
            args=(token, job_id, run_import_job, IMPORT_THREADS, IMPORT_LOCK),
            name=f"inflow-import-{job_id[:8]}",
            daemon=True,
        )
        IMPORT_THREADS[job_id] = thread
        thread.start()
        return True


def recover_import_jobs() -> None:
    IMPORT_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for path in IMPORT_JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(job, dict) or job.get("status") not in {"queued", "running"}:
            continue
        pack_id = str(job.get("pack_id") or "")
        ready = False
        if pack_id and (PACKS_DIR / pack_id / "manifest.json").is_file():
            try:
                load_pack_manifest(pack_id)
                ready = True
            except HTTPException:
                ready = False
        now = isoformat(utc_now())
        if ready:
            job.update(status="ready", stage="ready", fraction=1.0, cached=True, retryable=False, completed_at=now, updated_at=now)
        else:
            job.update(status="failed", stage="failed", error_code="interrupted_import", retryable=True, completed_at=now, updated_at=now)
        atomic_write_json(path, job)


recover_import_jobs()


@app.get("/api/health")
def health() -> dict[str, Any]:
    with WRITE_LOCK:
        recover_transactions()
    sessions = len(list(SESSIONS_DIR.glob("*.json"))) if SESSIONS_DIR.exists() else 0
    events = 0
    if EVENTS_PATH.exists():
        events = sum(1 for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines() if line.strip())
    profile_exists = PROFILE_PATH.exists()
    with HEAVY_QUEUE_LOCK:
        heavy_queue_depth = len(HEAVY_RESERVATIONS)
    extension_version = None
    try:
        extension_version = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8")).get("version")
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    return {
        "ok": True,
        "product": "inflow-english",
        "policy_version": POLICY_VERSION,
        "builder_version": BUILDER_VERSION,
        "extension_version": extension_version,
        "wordnet": {"lexicon": "oewn:2024", "warmup": "ready" if WORDNET_WARMUP.done() else "running"},
        "progressive_learning_enabled": PROGRESSIVE_ENABLED,
        "items": len(ITEMS),
        "sessions": sessions,
        "events": events,
        "profile_exists": profile_exists,
        "pending_transactions": len(list(TRANSACTIONS_DIR.glob("*.json"))) if TRANSACTIONS_DIR.exists() else 0,
        "heavy_worker_limit": 1,
        "heavy_pending_limit": HEAVY_MAX_PENDING,
        "heavy_queue_depth": heavy_queue_depth,
        "qa_allowed": QA_ALLOWED,
    }


@app.get("/api/packs")
def list_packs() -> dict[str, Any]:
    with WRITE_LOCK:
        profile = load_profile()
    current_frequency = effective_intensity(profile)
    rows = []
    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    for path in PACKS_DIR.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            manifest = load_pack_manifest(path.name)
        except HTTPException:
            continue
        candidate_count = len(manifest.get("candidates", []))
        speech_sec = pack_effective_speech_sec(manifest["pack_id"], manifest)
        content = pack_content(manifest["pack_id"])
        pack_profile = ensure_profile(profile, content)
        eligible_count = eligible_candidate_count(content, pack_profile)
        budgets = intervention_budgets(speech_sec, eligible_count)
        rows.append(
            {
                "pack_id": manifest["pack_id"],
                "title": manifest.get("title") or manifest["pack_id"],
                "duration_sec": manifest.get("duration_sec") or manifest.get("duration"),
                "effective_speech_sec": speech_sec,
                "source_url": manifest.get("source_url"),
                "captions": f"/api/packs/{manifest['pack_id']}/files/{manifest['media']['captions_zh']}",
                "transcript": f"/api/packs/{manifest['pack_id']}/files/{manifest['media']['transcript']}",
                "candidate_count": candidate_count,
                "eligible_candidate_count": eligible_count,
                "intervention_budgets": budgets,
                "expected_interventions": budgets[current_frequency],
                "effective_frequency": current_frequency,
                "created_at": manifest.get("created_at"),
                "active_session_id": profile.get("active_sessions", {}).get(manifest["pack_id"]),
                "fixture": False,
            }
        )
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    fixture = load_pack_manifest(FIXTURE_PACK_ID)
    fixture_candidate_count = len(fixture["candidates"])
    fixture_content = pack_content(FIXTURE_PACK_ID)
    fixture_profile = ensure_profile(profile, fixture_content)
    fixture_eligible_count = eligible_candidate_count(fixture_content, fixture_profile)
    fixture_budgets = intervention_budgets(None, fixture_eligible_count, override={"low": 1, "medium": 2, "high": 4})
    rows.append(
        {
            "pack_id": FIXTURE_PACK_ID,
            "title": fixture["title"],
            "duration_sec": fixture["duration_sec"],
            "effective_speech_sec": None,
            "source_url": None,
            "candidate_count": fixture_candidate_count,
            "eligible_candidate_count": fixture_eligible_count,
            "intervention_budgets": fixture_budgets,
            "expected_interventions": fixture_budgets[current_frequency],
            "effective_frequency": current_frequency,
            "created_at": None,
            "active_session_id": profile.get("active_sessions", {}).get(FIXTURE_PACK_ID),
            "fixture": True,
        }
    )
    return {
        "frequency_control": "intervention_frequency",
        "effective_frequency": current_frequency,
        "packs": rows,
    }


@app.post("/api/caption-previews")
async def create_caption_preview(request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    source_url = str(payload.get("url") or "")
    try:
        reference = validate_youtube_url(source_url)
        preview_id = caption_preview_id(reference.canonical_url)
    except VideoPackError as exc:
        raise HTTPException(422, str(exc).split(":", 1)[0]) from exc
    with CAPTION_LOCK:
        path = caption_job_path(preview_id)
        if path.is_file():
            job = load_caption_job(preview_id)
            active = CAPTION_THREADS.get(preview_id)
            if job.get("status") in {"queued", "running", "failed"} and job.get("retryable", True) and not (active and active.is_alive()):
                original = deepcopy(job)
                job.update(status="queued", usable=bool(job.get("usable")), error_code=None, retryable=True, completed_at=None, updated_at=isoformat(utc_now()))
                save_caption_job(job)
                if not start_caption_thread(preview_id):
                    save_caption_job(original)
                    raise HTTPException(429, "heavy_queue_full", headers={"Retry-After": "5"})
            return caption_job_public(job)
        now = isoformat(utc_now())
        job = {
            "preview_id": preview_id,
            "video_id": reference.video_id,
            "source_url": reference.canonical_url,
            "title": None,
            "status": "queued",
            "revision": 0,
            "usable": False,
            "language": None,
            "coverage_fraction": 0.0,
            "translated_segments": 0,
            "total_segments": 0,
            "error_code": None,
            "retryable": True,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        save_caption_job(job)
        if not start_caption_thread(preview_id):
            caption_job_path(preview_id).unlink(missing_ok=True)
            raise HTTPException(429, "heavy_queue_full", headers={"Retry-After": "5"})
        return caption_job_public(job)


@app.get("/api/caption-previews/{preview_id}")
def get_caption_preview(preview_id: str) -> dict[str, Any]:
    with CAPTION_LOCK:
        return caption_job_public(load_caption_job(preview_id))


@app.get("/api/caption-previews/{preview_id}/content")
def get_caption_preview_content(preview_id: str) -> dict[str, Any]:
    with CAPTION_LOCK:
        job = load_caption_job(preview_id)
        if not job.get("usable"):
            raise HTTPException(409, "caption_preview_not_usable")
        path = caption_content_path(preview_id)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HTTPException(409, "caption_preview_corrupt") from exc
        if not isinstance(document, dict) or document.get("preview_id") != preview_id or document.get("video_id") != job.get("video_id"):
            raise HTTPException(409, "caption_preview_corrupt")
        return document


@app.post("/api/imports")
async def create_import(request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    source_url = str(payload.get("url") or "")
    force_retry = payload.get("force_retry", False)
    if not isinstance(force_retry, bool):
        raise HTTPException(422, "invalid_force_retry")
    try:
        playhead_sec = float(payload.get("playhead_sec") or 0.0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "invalid_playhead") from exc
    if not math.isfinite(playhead_sec) or playhead_sec < 0 or playhead_sec > 3 * 60 * 60:
        raise HTTPException(422, "invalid_playhead")
    try:
        reference = validate_youtube_url(source_url)
        builder = VideoPackBuilder(PACKS_DIR)
        pack_id = builder.pack_id_for_url(reference.canonical_url)
    except VideoPackError as exc:
        raise HTTPException(422, str(exc).split(":", 1)[0]) from exc
    canonical_url = reference.canonical_url
    with IMPORT_LOCK:
        manifest = None
        if (PACKS_DIR / pack_id / "manifest.json").is_file():
            manifest = load_pack_manifest(pack_id)
        else:
            manifest = find_existing_pack_for_url(canonical_url)
        if manifest is not None:
            pack_id = str(manifest["pack_id"])
            now = isoformat(utc_now())
            job = {
                "job_id": hashlib.sha256(f"cache:{pack_id}".encode("utf-8")).hexdigest()[:32],
                "source_url": canonical_url,
                "pack_id": pack_id,
                "title": manifest.get("title"),
                "status": "ready",
                "stage": "ready",
                "route": "atomic",
                "playhead_sec": playhead_sec,
                "usable": True,
                "revision": 1,
                "ready_ranges": [[0.0, float(manifest.get("duration_sec") or manifest.get("duration"))]],
                "coverage_fraction": 1.0,
                "build_complete": True,
                "failed_ranges": [],
                "fraction": 1.0,
                "detail": "cache_hit",
                "cancel_requested": False,
                "error_code": None,
                "retryable": False,
                "cached": True,
                "created_at": now,
                "updated_at": now,
                "started_at": now,
                "completed_at": now,
            }
            save_import_job(job)
            return import_job_public(job)
        IMPORT_JOBS_DIR.mkdir(parents=True, exist_ok=True)
        matching_jobs = []
        for path in IMPORT_JOBS_DIR.glob("*.json"):
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(existing, dict) and existing.get("source_url") == canonical_url:
                matching_jobs.append(existing)
        matching_jobs.sort(key=lambda row: (str(row.get("updated_at") or ""), str(row.get("created_at") or "")), reverse=True)
        for existing in matching_jobs:
            if existing.get("status") in {"queued", "running", "partial_ready"} or (existing.get("status") == "degraded" and not existing.get("build_complete")):
                existing["playhead_sec"] = playhead_sec
                existing["cancel_requested"] = False
                existing["updated_at"] = isoformat(utc_now())
                save_import_job(existing)
                if not start_import_thread(existing["job_id"]):
                    raise HTTPException(429, "heavy_queue_full", headers={"Retry-After": "5"})
                return import_job_public(existing)
            if existing.get("usable") and existing.get("build_complete"):
                return import_job_public(existing)
        if not force_retry:
            previous_failure = next(
                (
                    row for row in matching_jobs
                    if row.get("status") in {"failed", "cancelled"}
                    or (row.get("status") == "degraded" and not row.get("usable"))
                ),
                None,
            )
            if previous_failure is not None:
                blocked = dict(previous_failure)
                blocked["detail"] = "previous_failure_requires_manual_retry"
                blocked["retryable"] = True
                blocked["retry_blocked"] = True
                return import_job_public(blocked)
        now = isoformat(utc_now())
        job = {
            "job_id": uuid.uuid4().hex,
            "source_url": canonical_url,
            "pack_id": pack_id,
            "title": None,
            "status": "queued",
            "stage": "queued",
            "route": "unknown",
            "playhead_sec": playhead_sec,
            "usable": False,
            "revision": 0,
            "ready_ranges": [],
            "coverage_fraction": 0.0,
            "build_complete": False,
            "failed_ranges": [],
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
        save_import_job(job)
    if not start_import_thread(job["job_id"]):
        with IMPORT_LOCK:
            import_job_path(job["job_id"]).unlink(missing_ok=True)
        raise HTTPException(429, "heavy_queue_full", headers={"Retry-After": "5"})
    return import_job_public(job)


@app.get("/api/imports/{job_id}")
def get_import(job_id: str) -> dict[str, Any]:
    with IMPORT_LOCK:
        return import_job_public(load_import_job(job_id))


@app.post("/api/imports/{job_id}/cancel")
def cancel_import(job_id: str) -> dict[str, Any]:
    with IMPORT_LOCK:
        job = load_import_job(job_id)
        if job.get("status") in {"ready", "complete", "degraded", "failed", "cancelled"}:
            return import_job_public(job)
        job["cancel_requested"] = True
        job["updated_at"] = isoformat(utc_now())
        save_import_job(job)
        return import_job_public(job)


@app.post("/api/progressive/{pack_id}/focus")
async def focus_progressive_pack(pack_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    client_id_from(payload)
    try:
        playhead_sec = float(payload.get("playhead_sec"))
        focus_epoch = int(payload.get("focus_epoch"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "invalid_progressive_focus") from exc
    if not math.isfinite(playhead_sec) or playhead_sec < 0 or playhead_sec > 3 * 60 * 60 or focus_epoch < 1:
        raise HTTPException(422, "invalid_progressive_focus")
    try:
        manifest = ProgressiveVideoPackBuilder(PACKS_DIR).set_focus(pack_id, playhead_sec=playhead_sec, focus_epoch=focus_epoch)
    except VideoPackError as exc:
        raise HTTPException(404, str(exc).split(":", 1)[0]) from exc
    resume_job_id = None
    with IMPORT_LOCK:
        for path in IMPORT_JOBS_DIR.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(job, dict) and job.get("pack_id") == pack_id:
                job["playhead_sec"] = playhead_sec
                job["cancel_requested"] = False
                job["updated_at"] = isoformat(utc_now())
                save_import_job(job)
                if not job.get("build_complete"):
                    resume_job_id = str(job.get("job_id") or "")
                break
    if resume_job_id and not start_import_thread(resume_job_id):
        raise HTTPException(429, "heavy_queue_full", headers={"Retry-After": "5"})
    return {
        "pack_id": pack_id,
        "playhead_sec": manifest["focus"]["playhead_sec"],
        "focus_epoch": manifest["focus"]["focus_epoch"],
        "revision": manifest["revision"],
    }


@app.get("/api/progressive/{pack_id}/delta")
def get_progressive_delta(pack_id: str, since_revision: int = -1, playhead_sec: float = 0.0) -> dict[str, Any]:
    if since_revision < -1 or not math.isfinite(playhead_sec) or playhead_sec < 0 or playhead_sec > 3 * 60 * 60:
        raise HTTPException(422, "invalid_progressive_delta")
    try:
        return ProgressiveVideoPackBuilder(PACKS_DIR).delta(pack_id, since_revision=since_revision, playhead_sec=playhead_sec)
    except VideoPackError as exc:
        raise HTTPException(404, str(exc).split(":", 1)[0]) from exc


@app.get("/api/progressive/{pack_id}/files/{relative_path:path}")
def get_progressive_file(pack_id: str, relative_path: str):
    match = re.fullmatch(r"shards/(w[0-9]{4})/phrases/([A-Za-z0-9._-]+\.mp3)", str(relative_path or ""))
    if not match:
        raise HTTPException(404, "progressive_file_not_found")
    window_id, filename = match.groups()
    try:
        builder = ProgressiveVideoPackBuilder(PACKS_DIR)
        shard_dir = builder.pack_path(pack_id) / "shards" / window_id
        shard = verify_window_shard(shard_dir, pack_id=pack_id, window_id=window_id)
    except VideoPackError as exc:
        raise HTTPException(404, "progressive_file_not_found") from exc
    relative = f"phrases/{filename}"
    if relative not in (shard.get("hashes", {}).get("files") or {}):
        raise HTTPException(404, "progressive_file_not_found")
    path = shard_dir / "phrases" / filename
    return FileResponse(path, media_type="audio/mpeg", filename=None)


@app.get("/api/packs/{pack_id}/files/{relative_path:path}")
def get_pack_file(pack_id: str, relative_path: str):
    manifest = load_pack_manifest(pack_id)
    if pack_id == FIXTURE_PACK_ID:
        raise HTTPException(404, "pack_file_not_found")
    allowed = set((manifest.get("hashes") or {}).get("files") or {})
    if relative_path not in allowed or not relative_path or "\\" in relative_path:
        raise HTTPException(404, "pack_file_not_found")
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(404, "pack_file_not_found")
    path = pack_path(pack_id).joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "pack_file_not_found")
    media_type = {
        ".mp4": "video/mp4",
        ".mp3": "audio/mpeg",
        ".json": "application/json; charset=utf-8",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=None)


@app.get("/api/profile")
def get_profile() -> dict[str, Any]:
    with WRITE_LOCK:
        return profile_view(load_profile())


@app.get("/api/lexicon")
def get_lexicon() -> dict[str, Any]:
    with WRITE_LOCK:
        profile = load_profile()
        rows = [
            {
                "knowledge_key": key,
                "surface": state.get("surface"),
                "gloss_zh": state.get("gloss_zh"),
                "frequency_zipf": state.get("frequency_zipf"),
                "status": state.get("status", "unseen"),
                "explicit_known": bool(state.get("explicit_known")),
                "score": float(state.get("score", 0.0)),
                "feedback_counts": deepcopy(state.get("feedback_counts", {})),
                "replay_count": int(state.get("replay_count", 0)),
                "last_feedback_at": state.get("last_feedback_at"),
                "context_fingerprints": list(state.get("context_fingerprints") or []),
            }
            for key, state in profile.get("lexicon", {}).items()
        ]
    rows.sort(key=lambda row: (str(row.get("surface") or "").casefold(), str(row["knowledge_key"])))
    return {"entries": rows, "counts": profile_view(profile)["vocabulary_counts"]}


@app.post("/api/lexicon/lookup")
async def lookup_lexicon_word(request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    client_id_from(payload)
    surface = str(payload.get("surface") or "").strip()
    sentence = str(payload.get("sentence") or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z'’\-]*(?:\s+[A-Za-z][A-Za-z'’\-]*){0,3}", surface) or len(surface) > 80:
        raise HTTPException(422, "invalid_lexicon_surface")
    if not sentence or len(sentence) > 500 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", sentence):
        raise HTTPException(422, "invalid_lexicon_sentence")
    context_id = context_fingerprint(sentence)
    lookup_item = annotate_lexical_identity({
        "id": f"subtitle:{hashlib.sha256(sentence.encode('utf-8')).hexdigest()[:20]}:{normalize_expression(surface)}",
        "surface": surface,
        "phrase_text": sentence,
        "sentence": sentence,
        "context_fingerprint": context_id,
    })
    derived_key = knowledge_key_for_item(lookup_item)
    with WRITE_LOCK:
        profile = load_profile()
        existing = profile.get("lexicon", {}).get(derived_key)
    if existing is not None:
        state = existing
        return {
            "knowledge_key": state["knowledge_key"],
            "surface": surface,
            "gloss_zh": state["gloss_zh"],
            "sentence_zh": None,
            "status": state.get("status", "unseen"),
            "context_fingerprints": list(state.get("context_fingerprints") or [context_id]),
            "source": "local_lexicon",
        }
    try:
        token = f"translation:{uuid.uuid4().hex}"
        translated = await asyncio.to_thread(
            run_reserved_heavy_call,
            token,
            lambda: LEXICON_TRANSLATOR.translate_all(
                [
                    {"cue_id": "surface", "text": surface},
                    {"cue_id": "sentence", "text": sentence},
                ]
            ),
        )
    except HeavyQueueFull as exc:
        raise HTTPException(429, "heavy_queue_full", headers={"Retry-After": "5"}) from exc
    except VideoPackError as exc:
        raise HTTPException(503, str(exc).split(":", 1)[0]) from exc
    gloss_zh = str(translated["surface"])
    return {
        "knowledge_key": derived_key,
        "surface": surface,
        "gloss_zh": gloss_zh,
        "sentence_zh": translated["sentence"],
        "status": "unseen",
        "context_fingerprints": [context_id],
        "source": "argos_en_zh_offline",
    }


@app.post("/api/lexicon/feedback")
async def set_lexicon_feedback(request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    client_id = client_id_from(payload)
    surface = str(payload.get("surface") or "").strip()
    gloss_zh = str(payload.get("gloss_zh") or "").strip()
    feedback = str(payload.get("familiarity_feedback") or "")
    source = str(payload.get("source") or "subtitle")
    sentence = str(payload.get("sentence") or "").strip()
    if feedback not in LEXICON_STATE_EDITS:
        raise HTTPException(422, "invalid_familiarity_feedback")
    if feedback == "undo" and source != "subtitle":
        raise HTTPException(422, "invalid_lexicon_undo_source")
    if source not in {"subtitle", "mapping", "explicit_no_more_explanations"}:
        raise HTTPException(422, "invalid_lexicon_feedback_source")
    if not re.fullmatch(r"[A-Za-z][A-Za-z'’\-]*(?:\s+[A-Za-z][A-Za-z'’\-]*){0,3}", surface) or len(surface) > 80:
        raise HTTPException(422, "invalid_lexicon_surface")
    if not gloss_zh or len(gloss_zh) > 100 or not re.search(r"[\u3400-\u9fff]", gloss_zh):
        raise HTTPException(422, "invalid_lexicon_gloss")
    requested_key = str(payload.get("knowledge_key") or "").strip()
    context_id = context_fingerprint(sentence) if sentence else None
    with WRITE_LOCK:
        profile_before = load_profile()
        existing = profile_before.get("lexicon", {}).get(requested_key) if requested_key else None
        if existing is not None:
            if normalize_expression(str(existing.get("surface") or "")) != normalize_expression(surface):
                raise HTTPException(422, "knowledge_key_mismatch")
            derived_key = requested_key
        else:
            if not sentence or len(sentence) > 500 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", sentence):
                raise HTTPException(422, "invalid_lexicon_sentence")
            feedback_item = annotate_lexical_identity({
                "id": f"subtitle:{hashlib.sha256(sentence.encode('utf-8')).hexdigest()[:20]}:{normalize_expression(surface)}",
                "surface": surface,
                "phrase_text": sentence,
                "sentence": sentence,
                "context_fingerprint": context_id,
            })
            derived_key = knowledge_key_for_item(feedback_item)
            if requested_key and requested_key != derived_key:
                raise HTTPException(422, "knowledge_key_mismatch")
        if feedback == "undo":
            restore = profile_before.get("lexicon", {}).get(derived_key, {}).get("state_edit_restore")
            if not isinstance(restore, dict):
                raise HTTPException(409, "lexicon_state_undo_unavailable")
        event_time = utc_now()
        profile = apply_lexicon_feedback(
            profile_before,
            knowledge_key=derived_key,
            surface=surface,
            gloss_zh=gloss_zh,
            feedback=feedback,
            source=source,
            context_id=context_id,
            now=event_time,
        )
        event_data = {
            "knowledge_key": derived_key,
            "surface": surface,
            "gloss_zh": gloss_zh,
            "familiarity_feedback": feedback,
            "replays": 0,
            "source": source[:40],
            "intent": (
                "subtitle_state_undo"
                if source == "subtitle" and feedback == "undo"
                else "subtitle_state_edit"
                if source == "subtitle"
                else "no_more_explanations"
                if source == "explicit_no_more_explanations"
                else "card_feedback"
            ),
            "context_fingerprint": context_id,
            "context_hash": hashlib.sha256(sentence.encode("utf-8")).hexdigest()[:24] if sentence else None,
            "client_id": client_id,
        }
        commit_state(profile=profile, events=[build_event("lexicon_feedback", event_data, at=event_time)])
        lexical = profile["lexicon"][derived_key]
        return {
            "knowledge_key": derived_key,
            "surface": lexical["surface"],
            "gloss_zh": lexical["gloss_zh"],
            "status": lexical["status"],
            "explicit_known": lexical["explicit_known"],
            "score": lexical["score"],
            "context_fingerprints": list(lexical.get("context_fingerprints") or []),
        }


@app.put("/api/profile/intensity")
async def update_intensity(request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    intensity = str(payload.get("intensity", ""))
    if intensity not in INTENSITIES:
        raise HTTPException(422, "invalid_intensity")
    with WRITE_LOCK:
        event_time = utc_now()
        profile = set_explicit_intensity(load_profile(), intensity, now=event_time)
        commit_state(
            profile=profile,
            events=[build_event("explicit_intensity_changed", {"intensity": intensity}, at=event_time)],
        )
        return profile_view(profile)


@app.post("/api/sessions")
async def create_session(request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    qa = bool(payload.get("qa"))
    request_client_id = client_id_from(payload) if payload.get("client_id") is not None else None
    pack_id = str(payload.get("pack_id") or FIXTURE_PACK_ID)
    try:
        playhead_sec = float(payload.get("playhead_sec") or 0.0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "invalid_playhead") from exc
    if not math.isfinite(playhead_sec) or playhead_sec < 0 or playhead_sec > 3 * 60 * 60:
        raise HTTPException(422, "invalid_playhead")
    if qa and not QA_ALLOWED:
        raise HTTPException(403, "qa_disabled")
    progressive = is_progressive_pack(pack_id)
    if progressive:
        manifest, content, video_view = progressive_pack_content(pack_id, playhead_sec=playhead_sec)
    else:
        manifest = load_pack_manifest(pack_id)
        content = pack_content(pack_id)
        video_view = pack_video_view(pack_id, manifest)
    with WRITE_LOCK:
        profile = ensure_profile(load_profile(), content)
        active_id = profile.setdefault("active_sessions", {}).get(pack_id)
        if not active_id and profile.get("active_session_id"):
            try:
                candidate = load_session(profile["active_session_id"])
            except HTTPException:
                candidate = None
            if candidate and str(candidate.get("pack_id") or FIXTURE_PACK_ID) == pack_id:
                active_id = candidate["session_id"]
                profile["active_sessions"][pack_id] = active_id
        if active_id:
            try:
                active = load_session(active_id)
            except HTTPException:
                active = None
            if active and active.get("stage") != "complete":
                view = session_view(active, profile)
                active_owner = str(active.get("owner_client_id") or "")
                view["owner_match"] = bool(request_client_id and active_owner == request_client_id)
                view["owner_conflict"] = bool(request_client_id and active_owner and active_owner != request_client_id)
                return view
        event_time = utc_now()
        session = new_session(content, profile, now=event_time, pack_id=pack_id)
        session["qa"] = qa
        session["progressive"] = progressive
        session["progressive_revision"] = int(manifest.get("revision", 0)) if progressive else None
        session["progressive_window_speech"] = {
            str(row["window_id"]): float(row.get("effective_speech_sec") or 0.0)
            for row in content.get("progressive_windows", [])
        } if progressive else {}
        session["progressive_eligible_ids"] = [
            item["id"]
            for item in content.get("items", [])
            if item_priority(
                item,
                profile.get("items", {}).get(item["id"], {}),
                event_time,
                vocabulary_frontier=estimate_vocabulary_frontier(profile),
            ) > -1000
        ] if progressive else []
        session["playhead_at_creation"] = playhead_sec
        session["encounter_catalog"] = [
            {"id": item["id"], "anchor_sec": item["anchor_sec"]}
            for item in content.get("items", [])
        ]
        session["video"] = video_view
        profile["active_session_id"] = session["session_id"]
        profile["active_sessions"][pack_id] = session["session_id"]
        commit_state(
            profile=profile,
            session=session,
            events=[
                build_event(
                    "session_created",
                    {
                        "qa": qa,
                        "pack_id": pack_id,
                        "explicit_intensity": profile["explicit_intensity"],
                        "effective_intensity": effective_intensity(profile),
                        "preference_epoch": int(profile.get("adaptive", {}).get("preference_epoch", 1)),
                        "candidate_count": len(session["items"]),
                        "candidate_pool_count": int(session.get("candidate_pool_count", len(content.get("items", [])))),
                        "effective_speech_sec": session.get("effective_speech_sec"),
                        "intervention_budgets": deepcopy(session.get("intervention_budgets", {})),
                        "probe_scheduled": bool(session.get("probe")),
                    },
                    session_id=session["session_id"],
                    at=event_time,
                )
            ],
        )
        view = session_view(session, profile)
        view["owner_match"] = False
        view["owner_conflict"] = False
        return view


@app.post("/api/sessions/{session_id}/sync-progressive")
async def sync_progressive_session(session_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    try:
        playhead_sec = float(payload.get("playhead_sec") or 0.0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "invalid_playhead") from exc
    if not math.isfinite(playhead_sec) or playhead_sec < 0 or playhead_sec > 3 * 60 * 60:
        raise HTTPException(422, "invalid_playhead")
    with WRITE_LOCK:
        session = load_session(session_id)
        require_session_owner(session, payload)
        if not session.get("progressive") or not is_progressive_pack(str(session.get("pack_id") or "")):
            raise HTTPException(409, "session_not_progressive")
        if session.get("stage") == "complete":
            raise HTTPException(409, "session_already_complete")
        pack_id = str(session["pack_id"])
        builder = ProgressiveVideoPackBuilder(PACKS_DIR)
        manifest = builder.load(pack_id)
        delta = builder.delta(
            pack_id,
            since_revision=int(session.get("progressive_revision") or -1),
            playhead_sec=playhead_sec,
        )
        if not delta.get("changed"):
            return session_view(session, load_profile())
        content = progressive_content_from_delta(pack_id, manifest, delta, playhead_sec=playhead_sec)
        event_time = utc_now()
        session, profile, added_ids = merge_progressive_session(
            session,
            load_profile(),
            content,
            revision=int(delta["revision"]),
            playhead_sec=playhead_sec,
        )
        commit_state(
            profile=profile,
            session=session,
            events=[
                build_event(
                    "progressive_session_synced",
                    {
                        "pack_id": pack_id,
                        "revision": int(delta["revision"]),
                        "playhead_sec": playhead_sec,
                        "added_ids": added_ids,
                    },
                    session_id=session_id,
                    at=event_time,
                )
            ],
        )
        return session_view(session, profile)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    with WRITE_LOCK:
        return session_view(load_session(session_id), load_profile())


@app.post("/api/sessions/{session_id}/start")
async def start_session(session_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    client_id = client_id_from(payload)
    with WRITE_LOCK:
        session = load_session(session_id)
        profile = load_profile()
        if session["stage"] in {"probe_ready", "watch_ready"}:
            event_time = utc_now()
            has_probe = session["stage"] == "probe_ready" and bool(session.get("probe"))
            session["stage"] = "probe" if has_probe else "watch"
            session["started_at"] = session.get("started_at") or isoformat(event_time)
            session["owner_client_id"] = client_id
            session["owner_epoch"] = 1
            if has_probe:
                session["probe"]["started_at"] = session["probe"].get("started_at") or isoformat(event_time)
            event_type = "probe_started" if has_probe else "watch_started"
            event_data = {"client_id": client_id, "owner_epoch": 1}
            if has_probe:
                event_data["probe_id"] = session["probe"]["probe_id"]
                event_data["variant_id"] = session["probe"]["variant_id"]
            commit_state(
                session=session,
                events=[build_event(event_type, event_data, session_id=session_id, at=event_time)],
            )
        elif session["stage"] == "probe_feedback":
            require_session_owner(session, payload)
            event_time = utc_now()
            session["stage"] = "watch"
            commit_state(
                session=session,
                events=[
                    build_event(
                        "watch_started",
                        {"client_id": client_id, "owner_epoch": session["owner_epoch"], "after_probe": True},
                        session_id=session_id,
                        at=event_time,
                    )
                ],
            )
        elif session["stage"] in {"probe", "watch"} and session.get("owner_client_id") != client_id:
            raise HTTPException(409, "session_owned_by_another_client")
        return session_view(session, profile)


def close_open_interaction_as_technical_failure(
    session: dict[str, Any],
    profile: dict[str, Any],
    *,
    event_time: datetime,
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    open_interaction = session.get("open_interaction") or {}
    if not open_interaction:
        return profile, None
    item_id = str(open_interaction.get("item_id") or "")
    item = next((row for row in session.get("items", []) if row.get("id") == item_id), None)
    if not item:
        raise RuntimeError("open_interaction_item_missing")
    record = {
        "interaction_id": str(open_interaction.get("interaction_id") or ""),
        "owner_client_id": open_interaction.get("owner_client_id"),
        "owner_epoch": int(open_interaction.get("owner_epoch", 0)),
        "item_id": item_id,
        "knowledge_key": str(profile.get("items", {}).get(item_id, {}).get("knowledge_key") or knowledge_key_for_item(item)),
        "outcome": "technical_failure",
        "familiarity_feedback": None,
        "dwell_ms": 0,
        "phrase_confirmed": False,
        "replays": 0,
        "technical_failure": True,
        "preference_epoch": int(profile.get("adaptive", {}).get("preference_epoch", 1)),
        "failure_reason": reason[:120],
        "completed_at": isoformat(event_time),
    }
    session.setdefault("interactions", {})[item_id] = record
    session["open_interaction"] = None
    profile = apply_interaction(
        profile,
        item_id,
        outcome="technical_failure",
        dwell_ms=0,
        phrase_confirmed=False,
        replays=0,
        familiarity_feedback=None,
        encounter_already_recorded=item_id in session.get("encountered_ids", []),
        now=event_time,
    )
    return profile, build_event("interaction_completed", record, session_id=session["session_id"], at=event_time)


@app.post("/api/sessions/{session_id}/claim")
async def claim_session(session_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    client_id = client_id_from(payload)
    with WRITE_LOCK:
        session = load_session(session_id)
        profile = load_profile()
        if session["stage"] not in {"probe", "probe_feedback", "watch"}:
            raise HTTPException(409, "session_not_active")
        if session.get("owner_client_id") != client_id:
            event_time = utc_now()
            old_owner_client_id = session.get("owner_client_id")
            old_owner_epoch = int(session.get("owner_epoch", 0))
            events: list[dict[str, Any]] = []
            if session["stage"] == "probe" and session.get("probe") and not session["probe"].get("result"):
                probe = session["probe"]
                probe_record = {
                    "probe_id": probe["probe_id"],
                    "variant_id": probe["variant_id"],
                    "item_id": probe["item_id"],
                    "owner_client_id": old_owner_client_id,
                    "owner_epoch": old_owner_epoch,
                    "resolution": "technical_failure",
                    "outcome": "technical_failure",
                    "selected_choice_id": None,
                    "audio_confirmed": False,
                    "presentation_count": int(probe.get("presentation_count", 0)),
                    "choice_count": len(probe.get("choices", [])),
                    "response_ms": 0,
                    "delay_hours": 0.0,
                    "purpose": probe.get("purpose", "measure"),
                    "primary_dimension": probe.get("primary_dimension", "auditory_to_current_sense"),
                    "template_version": probe.get("template_version", "followup-3choice-v1"),
                    "context_family_id": probe.get("context_family_id"),
                    "speaker_id": probe.get("speaker_id"),
                    "speaker_type": probe.get("speaker_type"),
                    "speaker_relation_to_teach": probe.get("speaker_relation_to_teach"),
                    "first_response": False,
                    "first_presentation": False,
                    "answer_revealed_before_response": False,
                    "meaning_options_visible_before_response": int(probe.get("presentation_count", 0)) > 0,
                    "surface_visible_before_response": False,
                    "evidence_quality": "NOT_EVIDENCE",
                    "scorer_version": "probe-3afc-v1",
                    "preference_eligible": False,
                    "preference_epoch": int(profile.get("adaptive", {}).get("preference_epoch", 1)),
                    "failure_reason": "owner_claimed",
                    "completed_at": isoformat(event_time),
                }
                probe_record["semantic_fingerprint"] = probe_semantic_fingerprint(probe_record)
                probe["result"] = probe_record
                probe["completed_at"] = probe_record["completed_at"]
                session["stage"] = "watch"
                events.append(build_event("probe_completed", probe_record, session_id=session_id, at=event_time))
            profile, orphan_event = close_open_interaction_as_technical_failure(
                session,
                profile,
                event_time=event_time,
                reason="owner_claimed",
            )
            if orphan_event:
                events.append(orphan_event)
            session["owner_client_id"] = client_id
            session["owner_epoch"] = old_owner_epoch + 1
            events.append(
                build_event(
                    "session_claimed",
                    {"client_id": client_id, "owner_epoch": session["owner_epoch"]},
                    session_id=session_id,
                    at=event_time,
                )
            )
            if session["stage"] == "watch" and any(event["type"] == "probe_completed" for event in events):
                events.append(
                    build_event(
                        "watch_started",
                        {"client_id": client_id, "owner_epoch": session["owner_epoch"], "after_probe_claim": True},
                        session_id=session_id,
                        at=event_time,
                    )
                )
            commit_state(profile=profile if orphan_event else None, session=session, events=events)
        return session_view(session, profile)


@app.get("/api/sessions/{session_id}/probe/audio/{probe_id}")
def probe_audio(session_id: str, probe_id: str) -> FileResponse:
    with WRITE_LOCK:
        session = load_session(session_id)
        probe = session.get("probe") or {}
        if probe.get("probe_id") != probe_id:
            raise HTTPException(404, "probe_not_found")
        relative = str(probe.get("followup_audio") or "").replace("\\", "/")
        if not relative.startswith("media/followup/") or relative not in FIXTURE_MEDIA_ALLOWLIST:
            raise HTTPException(404, "probe_audio_not_found")
        path = (SOURCE_MEDIA_DIR / relative.removeprefix("media/")).resolve()
        if not path.is_relative_to(SOURCE_MEDIA_DIR.resolve()) or not path.is_file():
            raise HTTPException(404, "probe_audio_not_found")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.post("/api/sessions/{session_id}/probe/presentation")
async def confirm_probe_presentation(session_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    probe_id = str(payload.get("probe_id") or "")
    presentation_id = str(payload.get("presentation_id") or "")
    if len(presentation_id) != 32 or any(character not in "0123456789abcdef" for character in presentation_id):
        raise HTTPException(422, "invalid_presentation_id")
    with WRITE_LOCK:
        session = load_session(session_id)
        profile = load_profile()
        require_session_owner(session, payload)
        probe = session.get("probe") or {}
        if not probe or probe.get("probe_id") != probe_id:
            raise HTTPException(409, "probe_token_mismatch")
        if probe.get("result") or session.get("stage") != "probe":
            raise HTTPException(409, "probe_not_active")
        presentation_ids = list(probe.get("presentation_ids") or [])
        if presentation_id not in presentation_ids:
            presentation_ids.append(presentation_id)
            probe["presentation_ids"] = presentation_ids[-20:]
            probe["presentation_count"] = int(probe.get("presentation_count", 0)) + 1
            event_time = utc_now()
            commit_state(
                session=session,
                events=[
                    build_event(
                        "probe_audio_confirmed",
                        {
                            "probe_id": probe_id,
                            "presentation_id": presentation_id,
                            "presentation_count": probe["presentation_count"],
                        },
                        session_id=session_id,
                        at=event_time,
                    )
                ],
            )
        return session_view(session, profile)


@app.post("/api/sessions/{session_id}/probe/complete")
async def complete_probe(session_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    requested_outcome = str(payload.get("outcome") or "")
    if requested_outcome not in {"selected", "dont_know", "skipped", "technical_failure"}:
        raise HTTPException(422, "invalid_probe_outcome")
    probe_id = str(payload.get("probe_id") or "")
    selected_choice_id = str(payload.get("selected_choice_id") or "") or None
    requested_audio_confirmed = bool(payload.get("audio_confirmed"))
    response_ms = min(600_000, max(0, int(payload.get("response_ms") or 0)))
    with WRITE_LOCK:
        profile = load_profile()
        session = load_session(session_id)
        require_session_owner(session, payload)
        probe = session.get("probe") or {}
        if not probe or probe.get("probe_id") != probe_id:
            raise HTTPException(409, "probe_token_mismatch")
        presentation_count = int(probe.get("presentation_count", 0))
        audio_confirmed = bool(requested_audio_confirmed and presentation_count >= 1)
        if probe.get("result"):
            existing = probe["result"]
            existing_resolution = existing.get("resolution") or (
                "selected" if existing.get("outcome") in {"correct", "incorrect"} else existing.get("outcome")
            )
            same_semantics = (
                existing_resolution == requested_outcome
                and (requested_outcome != "selected" or existing.get("selected_choice_id") == selected_choice_id)
                and bool(existing.get("audio_confirmed")) == audio_confirmed
            )
            if not same_semantics:
                raise HTTPException(409, "probe_result_conflict")
            return session_view(session, profile)
        if session.get("stage") != "probe":
            raise HTTPException(409, "probe_not_active")
        choices = {row["choice_id"]: row for row in probe.get("choices", [])}
        if requested_outcome == "selected":
            if not audio_confirmed:
                raise HTTPException(422, "probe_audio_must_complete")
            if selected_choice_id not in choices:
                raise HTTPException(422, "invalid_probe_choice")
            outcome = "correct" if selected_choice_id == probe["correct_choice_id"] else "incorrect"
        elif requested_outcome == "dont_know":
            if not audio_confirmed:
                raise HTTPException(422, "probe_audio_must_complete")
            outcome = "dont_know"
        else:
            outcome = requested_outcome
            selected_choice_id = None
        event_time = utc_now()
        state = profile["items"][probe["item_id"]]
        taught_at = parse_time(state.get("last_taught_at"))
        delay_hours = max(0.0, (event_time - taught_at).total_seconds() / 3600) if taught_at else 0.0
        if outcome in {"skipped", "technical_failure"} or not audio_confirmed:
            evidence_quality = "NOT_EVIDENCE"
        elif presentation_count > 1:
            evidence_quality = "ASSISTED_PRACTICE"
        elif outcome == "correct":
            evidence_quality = "WEAK_SUCCESS"
        elif outcome == "dont_know":
            evidence_quality = "ABSTAINED_NONRETRIEVAL"
        else:
            evidence_quality = "RECOGNITION_FAILURE"
        record = {
            "probe_id": probe_id,
            "variant_id": probe["variant_id"],
            "item_id": probe["item_id"],
            "owner_client_id": session.get("owner_client_id"),
            "owner_epoch": int(session.get("owner_epoch", 0)),
            "resolution": requested_outcome,
            "outcome": outcome,
            "selected_choice_id": selected_choice_id,
            "audio_confirmed": audio_confirmed,
            "presentation_count": presentation_count,
            "choice_count": len(choices),
            "response_ms": response_ms,
            "delay_hours": round(delay_hours, 3),
            "purpose": probe.get("purpose", "measure"),
            "primary_dimension": probe.get("primary_dimension", "auditory_to_current_sense"),
            "template_version": probe.get("template_version", "followup-3choice-v1"),
            "context_family_id": probe.get("context_family_id"),
            "speaker_id": probe.get("speaker_id"),
            "speaker_type": probe.get("speaker_type"),
            "speaker_relation_to_teach": probe.get("speaker_relation_to_teach"),
            "first_response": True,
            "first_presentation": presentation_count == 1,
            "answer_revealed_before_response": False,
            "meaning_options_visible_before_response": True,
            "surface_visible_before_response": False,
            "evidence_quality": evidence_quality,
            "scorer_version": "probe-3afc-v1",
            "preference_eligible": False,
            "preference_epoch": int(profile.get("adaptive", {}).get("preference_epoch", 1)),
            "completed_at": isoformat(event_time),
        }
        record["semantic_fingerprint"] = probe_semantic_fingerprint(record)
        if evidence_quality != "NOT_EVIDENCE":
            profile = apply_probe_result(
                profile,
                probe["item_id"],
                variant_id=probe["variant_id"],
                outcome=outcome,
                audio_confirmed=audio_confirmed,
                choice_count=len(choices),
                response_ms=response_ms,
                delay_hours=delay_hours,
                presentation_count=presentation_count,
                probe_id=probe_id,
                now=event_time,
            )
        probe["result"] = record
        probe["completed_at"] = record["completed_at"]
        session["stage"] = "probe_feedback"
        commit_state(
            profile=profile if evidence_quality != "NOT_EVIDENCE" else None,
            session=session,
            events=[build_event("probe_completed", record, session_id=session_id, at=event_time)],
        )
        return session_view(session, profile)


@app.post("/api/sessions/{session_id}/progress")
async def save_progress(session_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    sequence = max(0, int(payload.get("sequence") or 0))
    media_time = max(0.0, float(payload.get("media_time") or 0))
    with WRITE_LOCK:
        session = load_session(session_id)
        require_session_owner(session, payload)
        if sequence > int(session.get("progress_seq", 0)):
            session["progress_seq"] = sequence
            session["last_media_time"] = media_time
            commit_state(session=session)
        return {"ok": True, "sequence": session.get("progress_seq", 0)}


@app.post("/api/sessions/{session_id}/encounters/{item_id}")
async def record_encounter(session_id: str, item_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    with WRITE_LOCK:
        session = load_session(session_id)
        if session["stage"] != "watch":
            raise HTTPException(409, "watch_not_active")
        require_session_owner(session, payload)
        profile = load_profile()
        profile, changed, events = record_encounter_locked(session, profile, item_id, at=utc_now())
        if changed:
            commit_state(profile=profile, session=session, events=events)
        return {"ok": True, "recorded": changed}


@app.post("/api/sessions/{session_id}/interactions/start")
async def start_interaction(session_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    item_id = str(payload.get("item_id", ""))
    with WRITE_LOCK:
        profile = load_profile()
        session = load_session(session_id)
        if session["stage"] != "watch":
            raise HTTPException(409, "watch_not_active")
        client_id = require_session_owner(session, payload)
        item = next((row for row in session["items"] if row["id"] == item_id), None)
        if not item:
            raise HTTPException(422, "item_not_in_session")
        if item_id in session["interactions"]:
            return session_view(session, profile)
        current_intensity = effective_intensity(profile)
        if not intensity_allows(current_intensity, item["min_intensity"]):
            raise HTTPException(409, "intensity_does_not_allow_item")
        open_interaction = session.get("open_interaction")
        if open_interaction and open_interaction.get("item_id") != item_id:
            raise HTTPException(409, "another_interaction_open")
        if not open_interaction:
            event_time = utc_now()
            profile, encounter_changed, events = record_encounter_locked(session, profile, item_id, at=event_time)
            session["open_interaction"] = {
                "interaction_id": uuid.uuid4().hex,
                "item_id": item_id,
                "owner_client_id": client_id,
                "owner_epoch": int(session.get("owner_epoch", 0)),
                "started_at": isoformat(event_time),
            }
            events.append(build_event("interaction_started", {"item_id": item_id}, session_id=session_id, at=event_time))
            commit_state(profile=profile if encounter_changed else None, session=session, events=events)
        return {
            "interaction": session["open_interaction"],
            "item": item_view(item, profile),
            "profile": profile_view(profile),
        }


@app.post("/api/sessions/{session_id}/interactions/{item_id}/complete")
async def complete_interaction(session_id: str, item_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    outcome = str(payload.get("outcome", ""))
    if outcome not in {"completed", "skipped", "technical_failure"}:
        raise HTTPException(422, "invalid_outcome")
    dwell_ms = max(0, int(payload.get("dwell_ms") or 0))
    phrase_confirmed = bool(payload.get("phrase_confirmed"))
    replays = max(0, int(payload.get("replays") or 0))
    familiarity_feedback = payload.get("familiarity_feedback")
    if familiarity_feedback is not None:
        familiarity_feedback = str(familiarity_feedback)
        if familiarity_feedback not in FAMILIARITY_FEEDBACK or outcome != "completed":
            raise HTTPException(422, "invalid_familiarity_feedback")
    interaction_id = str(payload.get("interaction_id") or "")
    with WRITE_LOCK:
        profile = load_profile()
        session = load_session(session_id)
        client_id = require_session_owner(session, payload)
        item = next((row for row in session["items"] if row["id"] == item_id), None)
        if not item:
            raise HTTPException(422, "item_not_in_session")
        existing = session["interactions"].get(item_id)
        if existing:
            if not interaction_id or existing.get("interaction_id") != interaction_id:
                raise HTTPException(409, "interaction_token_mismatch")
            return session_view(session, profile)
        open_interaction = session.get("open_interaction") or {}
        if open_interaction.get("item_id") != item_id:
            raise HTTPException(409, "interaction_lease_required")
        if not interaction_id or open_interaction.get("interaction_id") != interaction_id:
            raise HTTPException(409, "interaction_token_mismatch")
        if open_interaction.get("owner_client_id") != client_id or int(open_interaction.get("owner_epoch", 0)) != int(session.get("owner_epoch", 0)):
            raise HTTPException(409, "interaction_owned_by_another_client")
        event_time = utc_now()
        record = {
            "interaction_id": open_interaction["interaction_id"],
            "owner_client_id": client_id,
            "owner_epoch": int(session.get("owner_epoch", 0)),
            "item_id": item_id,
            "knowledge_key": str(profile.get("items", {}).get(item_id, {}).get("knowledge_key") or knowledge_key_for_item(item)),
            "outcome": outcome,
            "familiarity_feedback": familiarity_feedback,
            "dwell_ms": dwell_ms,
            "phrase_confirmed": phrase_confirmed,
            "replays": replays,
            "technical_failure": outcome == "technical_failure",
            "preference_epoch": int(profile.get("adaptive", {}).get("preference_epoch", 1)),
            "failure_reason": str(payload.get("failure_reason") or "")[:120] or None,
            "completed_at": isoformat(event_time),
        }
        session["interactions"][item_id] = record
        session["open_interaction"] = None
        profile = apply_interaction(
            profile,
            item_id,
            outcome=outcome,
            dwell_ms=dwell_ms,
            phrase_confirmed=phrase_confirmed,
            replays=replays,
            familiarity_feedback=familiarity_feedback,
            encounter_already_recorded=item_id in session.get("encountered_ids", []),
            now=event_time,
        )
        commit_state(
            profile=profile,
            session=session,
            events=[build_event("interaction_completed", record, session_id=session_id, at=event_time)],
        )
        return session_view(session, profile)


@app.post("/api/sessions/{session_id}/complete")
async def complete_session(session_id: str, request: Request) -> dict[str, Any]:
    payload = await read_payload(request)
    with WRITE_LOCK:
        profile = load_profile()
        session = load_session(session_id)
        require_session_owner(session, payload)
        if session["stage"] == "complete":
            return session_view(session, profile)
        if session["stage"] != "watch":
            raise HTTPException(409, "watch_not_active")
        if not bool(payload.get("source_ended")):
            raise HTTPException(422, "source_must_end")
        if session.get("open_interaction"):
            raise HTTPException(409, "interaction_still_open")
        event_time = utc_now()
        session["stage"] = "complete"
        session["completed_at"] = isoformat(event_time)
        session["metrics"] = {
            "source_ended": True,
            "elapsed_ms": max(0, int(payload.get("elapsed_ms") or 0)),
            "manual_seeks": max(0, int(payload.get("manual_seeks") or 0)),
            "technical_failures": max(0, int(payload.get("technical_failures") or 0)),
        }
        if not session.get("adaptation_applied"):
            profile = complete_session_adaptation(profile, session, now=event_time)
            session["adaptation_applied"] = True
        pack_id = str(session.get("pack_id") or FIXTURE_PACK_ID)
        if profile.setdefault("active_sessions", {}).get(pack_id) == session_id:
            profile["active_sessions"].pop(pack_id, None)
        if profile.get("active_session_id") == session_id:
            remaining = list(profile["active_sessions"].values())
            profile["active_session_id"] = remaining[-1] if remaining else None
        completion_event = deepcopy(session["metrics"])
        completion_event["pack_id"] = pack_id
        commit_state(
            profile=profile,
            session=session,
            events=[build_event("session_completed", completion_event, session_id=session_id, at=event_time)],
        )
        return session_view(session, profile)


@app.get("/media/{relative_path:path}")
def fixture_media(relative_path: str) -> FileResponse:
    normalized = f"media/{str(relative_path or '').replace(chr(92), '/')}"
    if normalized not in FIXTURE_MEDIA_ALLOWLIST:
        raise HTTPException(404, "media_not_found")
    path = (SOURCE_MEDIA_DIR / normalized.removeprefix("media/")).resolve()
    root = SOURCE_MEDIA_DIR.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "media_not_found")
    return FileResponse(
        path,
        headers={
            "Cache-Control": "private, no-store",
            "Cross-Origin-Resource-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")


if __name__ == "__main__":
    acquire_server_lock()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
