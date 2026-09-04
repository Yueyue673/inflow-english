from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

TARGET_WINDOW_SEC = 6 * 60.0
BOUNDARY_SEARCH_SEC = 30.0
MIN_WINDOW_SEC = TARGET_WINDOW_SEC - BOUNDARY_SEARCH_SEC
MAX_WINDOW_SEC = TARGET_WINDOW_SEC + BOUNDARY_SEARCH_SEC
ANALYSIS_OVERLAP_SEC = 15.0
MIN_SEMANTIC_PAUSE_SEC = 0.8
MAX_LONG_VIDEO_SEC = 3 * 60 * 60.0
MIN_LONG_VIDEO_SEC = 30 * 60.0


def _finite(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(field) from exc
    if not math.isfinite(number):
        raise ValueError(field)
    return number


def semantic_boundaries(
    duration_sec: float,
    *,
    chapter_starts: Iterable[float] = (),
    pauses: Iterable[Mapping[str, Any]] = (),
    caption_ends: Iterable[float] = (),
) -> list[dict[str, Any]]:
    duration = _finite(duration_sec, "duration_invalid")
    rows: dict[float, dict[str, Any]] = {}

    def add(point: float, kind: str, weight: int) -> None:
        if not 0 < point < duration:
            return
        rounded = round(point, 3)
        existing = rows.get(rounded)
        if existing is None or weight > int(existing["weight"]):
            rows[rounded] = {"at_sec": rounded, "kind": kind, "weight": weight}

    for value in chapter_starts:
        add(_finite(value, "chapter_invalid"), "chapter", 3)
    for pause in pauses:
        start = _finite(pause.get("start_sec"), "pause_invalid")
        end = _finite(pause.get("end_sec"), "pause_invalid")
        if start < 0 or end <= start or end > duration:
            raise ValueError("pause_invalid")
        if end - start >= MIN_SEMANTIC_PAUSE_SEC:
            add((start + end) / 2, "silence", 2)
    for value in caption_ends:
        add(_finite(value, "caption_end_invalid"), "caption_end", 1)
    return sorted(rows.values(), key=lambda row: row["at_sec"])


def plan_long_video_windows(
    duration_sec: float,
    *,
    chapter_starts: Iterable[float] = (),
    pauses: Iterable[Mapping[str, Any]] = (),
    caption_ends: Iterable[float] = (),
    require_long: bool = False,
) -> list[dict[str, Any]]:
    duration = _finite(duration_sec, "duration_invalid")
    minimum = MIN_LONG_VIDEO_SEC if require_long else 0.001
    if duration < minimum or duration > MAX_LONG_VIDEO_SEC:
        raise ValueError("duration_out_of_long_video_range")
    boundaries = semantic_boundaries(
        duration,
        chapter_starts=chapter_starts,
        pauses=pauses,
        caption_ends=caption_ends,
    )
    windows: list[dict[str, Any]] = []
    ownership_start = 0.0
    index = 0
    while ownership_start < duration - 0.001:
        remaining = duration - ownership_start
        if remaining <= MAX_WINDOW_SEC:
            ownership_end = duration
            boundary_kind = "video_end"
            seam_state = "closed"
        else:
            target = ownership_start + TARGET_WINDOW_SEC
            low = ownership_start + MIN_WINDOW_SEC
            high = min(duration, ownership_start + MAX_WINDOW_SEC)
            available = [row for row in boundaries if low <= row["at_sec"] <= high]
            if available:
                bonuses = {"chapter": 20.0, "silence": 10.0, "caption_end": 0.0}
                selected = min(
                    available,
                    key=lambda row: (
                        abs(float(row["at_sec"]) - target) - bonuses.get(str(row["kind"]), 0.0),
                        -int(row["weight"]),
                        float(row["at_sec"]),
                    ),
                )
                ownership_end = float(selected["at_sec"])
                boundary_kind = str(selected["kind"])
                seam_state = "closed" if boundary_kind in {"chapter", "silence"} else "tentative"
            else:
                ownership_end = min(duration, target)
                boundary_kind = "time_fallback"
                seam_state = "open"
        if duration - ownership_end < MIN_WINDOW_SEC / 2:
            ownership_end = duration
            boundary_kind = "video_end"
            seam_state = "closed"
        if ownership_end <= ownership_start:
            raise RuntimeError("window_planner_did_not_advance")
        analysis_start = max(0.0, ownership_start - (ANALYSIS_OVERLAP_SEC if index else 0.0))
        analysis_end = min(duration, ownership_end + (ANALYSIS_OVERLAP_SEC if ownership_end < duration else 0.0))
        windows.append(
            {
                "window_id": f"w{index:04d}",
                "ownership_start_sec": round(ownership_start, 3),
                "ownership_end_sec": round(ownership_end, 3),
                "analysis_start_sec": round(analysis_start, 3),
                "analysis_end_sec": round(analysis_end, 3),
                "boundary_kind": boundary_kind,
                "seam_state": seam_state,
                "status": "pending",
            }
        )
        ownership_start = ownership_end
        index += 1
    return windows


def prioritize_windows(windows: Sequence[Mapping[str, Any]], playhead_sec: float) -> list[str]:
    if not windows:
        return []
    playhead = max(0.0, _finite(playhead_sec, "playhead_invalid"))
    final_end = float(windows[-1]["ownership_end_sec"])

    def key(row: Mapping[str, Any]) -> tuple[int, float, float]:
        start = float(row["ownership_start_sec"])
        end = float(row["ownership_end_sec"])
        contains = start <= playhead < end or (playhead == final_end and end == final_end)
        ahead = start >= playhead
        distance = 0.0 if contains else min(abs(playhead - start), abs(playhead - end))
        return (0 if contains else 1 if ahead else 2, distance, start if ahead else -start)

    return [str(row["window_id"]) for row in sorted(windows, key=key)]


def accept_owned_candidates(window: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    start = float(window["ownership_start_sec"])
    end = float(window["ownership_end_sec"])
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        anchor = _finite(candidate.get("anchor_sec"), "candidate_anchor_invalid")
        if not (start <= anchor < end or (anchor == end and window.get("boundary_kind") == "video_end")):
            continue
        occurrence_id = str(candidate.get("occurrence_id") or candidate.get("id") or "")
        if not occurrence_id:
            occurrence_id = f"fallback:{candidate.get('surface')}:{round(anchor * 1000)}"
        if occurrence_id in seen:
            continue
        seen.add(occurrence_id)
        accepted.append(dict(candidate))
    return sorted(accepted, key=lambda row: (float(row["anchor_sec"]), str(row.get("occurrence_id") or row.get("id") or "")))
