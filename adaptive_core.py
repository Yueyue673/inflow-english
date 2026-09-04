from __future__ import annotations

import json
import math
import os
import random
import re
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from lexical_sense import context_fingerprint, provisional_key, stable_key

try:
    from wordfreq import zipf_frequency as _wordfreq_zipf_frequency  # type: ignore
except ImportError:
    _wordfreq_zipf_frequency = None

INTENSITIES = ("low", "medium", "high")
INTENSITY_RANK = {name: index for index, name in enumerate(INTENSITIES)}
POLICY_VERSION = "adaptive-v5"
PROFILE_SCHEMA_VERSION = 3
REDUCER_VERSION = "rules-v5"
PROBE_WINDOW_START_HOURS = 20
PROBE_WINDOW_END_HOURS = 72
FAMILIARITY_FEEDBACK = ("known", "familiar", "unclear")

# Engineering priors, not claims of learning efficacy.  They control only how
# often InFlow may interrupt the source task, measured against English speech
# time rather than raw video length.  Real delayed-learning and Flow Tax data
# must calibrate them later.
INTERVENTION_DENSITY: dict[str, dict[str, float | int]] = {
    "low": {"seconds_per_intervention": 420.0, "cap": 3},
    "medium": {"seconds_per_intervention": 180.0, "cap": 6},
    "high": {"seconds_per_intervention": 90.0, "cap": 10},
}

# Legacy fallback for the fixed NASA regression fixture and old content that
# predates speech-duration metadata.
POLICIES: dict[str, dict[str, int]] = {
    "low": {"max_teaches": 1},
    "medium": {"max_teaches": 2},
    "high": {"max_teaches": 4},
}

TIER_SLOT_ORDER = ("medium", "high", "low", "high")
PROBE_TIER_SLOT_ORDER = ("medium", "high", "high")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def normalize_expression(value: str) -> str:
    text = str(value or "").casefold().replace("’", "'")
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" .,!?:;\"'()[]{}")


def normalize_sense(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    return text.strip("。！？，、；：.!,;:")


def knowledge_key_for_item(item: dict[str, Any]) -> str:
    resolved = stable_key(item)
    if resolved is not None:
        return resolved
    return provisional_key(item)


def frequency_for_expression(value: str) -> float | None:
    if _wordfreq_zipf_frequency is None:
        return None
    try:
        score = float(_wordfreq_zipf_frequency(normalize_expression(value), "en"))
    except Exception:
        return None
    return round(score, 3) if math.isfinite(score) and score > 0 else None


def item_frequency(item: dict[str, Any]) -> float | None:
    try:
        explicit = float(item.get("frequency_zipf"))
    except (TypeError, ValueError):
        explicit = 0.0
    return round(explicit, 3) if math.isfinite(explicit) and explicit > 0 else frequency_for_expression(str(item.get("surface") or ""))


def estimate_vocabulary_frontier(profile: dict[str, Any]) -> float | None:
    known = []
    unclear = []
    familiar = []
    for state in profile.get("lexicon", {}).values():
        try:
            frequency = float(state.get("frequency_zipf"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(frequency) or frequency <= 0:
            continue
        status = state.get("status")
        if status == "known":
            known.append(frequency)
        elif status == "unclear":
            unclear.append(frequency)
        elif status == "familiar":
            familiar.append(frequency)
    if len(known) + len(unclear) + len(familiar) < 4:
        return None
    if known and unclear:
        frontier = (min(known) + max(unclear)) / 2
    elif familiar:
        ordered = sorted(familiar)
        frontier = ordered[len(ordered) // 2]
    elif known:
        frontier = min(known)
    else:
        frontier = max(unclear)
    return round(max(1.5, min(6.5, frontier)), 3)


def seed_lexicon_state(
    key: str,
    surface: str,
    gloss_zh: str,
    *,
    known: bool = False,
    frequency_zipf: float | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    return {
        "knowledge_key": key,
        "surface": str(surface),
        "gloss_zh": str(gloss_zh),
        "frequency_zipf": frequency_zipf if frequency_zipf is not None else frequency_for_expression(surface),
        "status": "known" if known else "unseen",
        "explicit_known": bool(known),
        "score": 1.0 if known else 0.0,
        "feedback_counts": {name: 0 for name in FAMILIARITY_FEEDBACK},
        "replay_count": 0,
        "last_feedback_at": None,
        "occurrence_ids": [],
        "context_fingerprints": [context_id] if context_id else [],
        "evidence": [],
    }


def seed_item_state(item_id: str, known_ids: set[str] | None = None, *, knowledge_key: str | None = None) -> dict[str, Any]:
    known = item_id in (known_ids or set())
    return {
        "knowledge_key": knowledge_key,
        "self_report_status": "known" if known else "unseen",
        "explicit_known": bool(known),
        "aural_stage": "self_reported_known" if known else "unknown",
        "confidence": "low",
        "encounter_count": 0,
        "teach_count": 0,
        "replay_count": 0,
        "clean_success_count": 0,
        "clean_failure_count": 0,
        "recognition_success_count": 0,
        "recognition_failure_count": 0,
        "recognition_abstention_count": 0,
        "assisted_probe_count": 0,
        "last_probe_at": None,
        "last_probe_result": None,
        "used_probe_variants": [],
        "last_encounter_at": None,
        "last_taught_at": None,
        "next_window_start": None,
        "next_window_end": None,
        "evidence": [],
    }


def new_profile(
    content: dict[str, Any],
    known_ids: Iterable[str] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    now = isoformat(now or utc_now())
    known = set(known_ids)
    item_states: dict[str, dict[str, Any]] = {}
    lexicon: dict[str, dict[str, Any]] = {}
    for item in content["items"]:
        item_id = str(item["id"])
        key = knowledge_key_for_item(item)
        is_known = item_id in known
        item_states[item_id] = seed_item_state(item_id, known, knowledge_key=key)
        context_id = str(item.get("context_fingerprint") or context_fingerprint(str(item.get("phrase_text") or item.get("sentence") or "")))
        lexical = lexicon.setdefault(
            key,
            seed_lexicon_state(
                key,
                str(item["surface"]),
                str(item["gloss_zh"]),
                known=is_known,
                frequency_zipf=item_frequency(item),
                context_id=context_id,
            ),
        )
        if item_id not in lexical["occurrence_ids"]:
            lexical["occurrence_ids"].append(item_id)
        if context_id and context_id not in lexical["context_fingerprints"]:
            lexical["context_fingerprints"].append(context_id)
        if is_known:
            lexical["status"] = "known"
            lexical["explicit_known"] = True
            lexical["score"] = 1.0
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": "local",
        "policy_version": POLICY_VERSION,
        "reducer_version": REDUCER_VERSION,
        "created_at": now,
        "updated_at": now,
        "explicit_intensity": "medium",
        "adaptive": {
            "reduction": 0,
            "reason": None,
            "preference_epoch": 1,
            "epoch_completed_sessions": 0,
            "epoch_comparable_opportunities": 0,
            "epoch_started_at": now,
            "comparable_opportunities": 0,
            "completed_sessions": 0,
            "session_history": [],
            "last_changed_at": None,
        },
        "reading": {
            "mapping_dwell_ms": 6000,
            "sample_count": 0,
        },
        "items": item_states,
        "lexicon": lexicon,
        "active_session_id": None,
        "active_sessions": {},
    }


def ensure_profile(profile: dict[str, Any], content: dict[str, Any]) -> dict[str, Any]:
    stored_version = int(profile.get("schema_version", 1))
    if stored_version > PROFILE_SCHEMA_VERSION:
        raise ValueError(f"unsupported_profile_schema:{stored_version}")
    profile = deepcopy(profile)
    profile["schema_version"] = PROFILE_SCHEMA_VERSION
    profile["policy_version"] = POLICY_VERSION
    profile["reducer_version"] = REDUCER_VERSION
    profile.setdefault("explicit_intensity", "medium")
    profile.setdefault("adaptive", {})
    adaptive = profile["adaptive"]
    adaptive.setdefault("reduction", 0)
    adaptive.setdefault("reason", None)
    adaptive.setdefault("preference_epoch", 1)
    adaptive.setdefault("epoch_completed_sessions", 0)
    adaptive.setdefault("epoch_comparable_opportunities", 0)
    adaptive.setdefault("epoch_started_at", profile.get("created_at"))
    adaptive.setdefault("comparable_opportunities", 0)
    adaptive.setdefault("completed_sessions", 0)
    adaptive.setdefault("session_history", [])
    adaptive.setdefault("last_changed_at", None)
    profile.setdefault("reading", {"mapping_dwell_ms": 6000, "sample_count": 0})
    profile["reading"].setdefault("mapping_dwell_ms", 6000)
    profile["reading"].setdefault("sample_count", 0)
    profile.setdefault("items", {})
    legacy_lexicon = deepcopy(profile.get("lexicon") or {}) if stored_version < 3 else {}
    if stored_version < 3:
        if legacy_lexicon:
            profile["legacy_lexicon_v1"] = legacy_lexicon
        profile["lexicon"] = {}
    else:
        profile.setdefault("lexicon", {})
    for item in content["items"]:
        item_id = str(item["id"])
        key = knowledge_key_for_item(item)
        existing = profile["items"].get(item_id) or {}
        legacy_key = str(existing.get("knowledge_key") or "")
        legacy_entry = legacy_lexicon.get(legacy_key) if legacy_key else None
        legacy_status = str(existing.get("self_report_status") or (legacy_entry or {}).get("status") or "unseen")
        if legacy_status not in {"unseen", *FAMILIARITY_FEEDBACK}:
            legacy_status = "unseen"
        legacy_known = stored_version < 3 and (existing.get("aural_stage") == "self_reported_known" or bool(existing.get("explicit_known")) or legacy_status == "known")
        defaults = seed_item_state(item_id, knowledge_key=key)
        state = profile["items"].setdefault(item_id, defaults)
        for field, value in defaults.items():
            state.setdefault(field, deepcopy(value))
        state["knowledge_key"] = key
        context_id = str(item.get("context_fingerprint") or context_fingerprint(str(item.get("phrase_text") or item.get("sentence") or "")))
        lexical_defaults = seed_lexicon_state(
            key,
            str(item["surface"]),
            str(item["gloss_zh"]),
            known=legacy_known,
            frequency_zipf=item_frequency(item),
            context_id=context_id,
        )
        lexical = profile["lexicon"].setdefault(key, lexical_defaults)
        for field, value in lexical_defaults.items():
            lexical.setdefault(field, deepcopy(value))
        if item_id not in lexical["occurrence_ids"]:
            lexical["occurrence_ids"].append(item_id)
        if context_id and context_id not in lexical["context_fingerprints"]:
            lexical["context_fingerprints"].append(context_id)
        if legacy_known:
            lexical["status"] = "known"
            lexical["explicit_known"] = bool(
                existing.get("explicit_known")
                or (legacy_entry or {}).get("explicit_known")
                or existing.get("aural_stage") == "self_reported_known"
            )
            lexical["score"] = 1.0
        elif stored_version < 3 and legacy_status in {"familiar", "unclear"}:
            lexical["status"] = legacy_status
            lexical["explicit_known"] = False
            lexical["score"] = {"familiar": 0.35, "unclear": -0.5}[legacy_status]
        state["self_report_status"] = lexical.get("status", "unseen")
        state["explicit_known"] = bool(lexical.get("explicit_known"))
        if state["explicit_known"]:
            state["aural_stage"] = "self_reported_known"
            state["next_window_start"] = None
            state["next_window_end"] = None
    profile.setdefault("active_session_id", None)
    profile.setdefault("active_sessions", {})
    legacy_fixture = profile["active_sessions"].pop("fixture:nasa-lro-v1", None)
    if legacy_fixture and "fixture-nasa-lro-v1" not in profile["active_sessions"]:
        profile["active_sessions"]["fixture-nasa-lro-v1"] = legacy_fixture
    return profile


def effective_intensity(profile: dict[str, Any]) -> str:
    baseline = profile.get("explicit_intensity", "medium")
    baseline_rank = INTENSITY_RANK.get(baseline, 1)
    reduction = min(0, int(profile.get("adaptive", {}).get("reduction", 0)))
    return INTENSITIES[max(0, min(2, baseline_rank + reduction))]


def set_explicit_intensity(profile: dict[str, Any], intensity: str, now: datetime | None = None) -> dict[str, Any]:
    if intensity not in INTENSITIES:
        raise ValueError("invalid_intensity")
    profile = deepcopy(profile)
    event_time = now or utc_now()
    profile["explicit_intensity"] = intensity
    profile["adaptive"]["reduction"] = 0
    profile["adaptive"]["reason"] = "explicit_user_choice"
    profile["adaptive"]["preference_epoch"] = int(profile["adaptive"].get("preference_epoch", 1)) + 1
    profile["adaptive"]["epoch_completed_sessions"] = 0
    profile["adaptive"]["epoch_comparable_opportunities"] = 0
    profile["adaptive"]["epoch_started_at"] = isoformat(event_time)
    profile["adaptive"]["last_changed_at"] = isoformat(event_time)
    profile["updated_at"] = isoformat(event_time)
    return profile


def item_priority(item: dict[str, Any], state: dict[str, Any], now: datetime, *, vocabulary_frontier: float | None = None) -> float:
    stage = state.get("aural_stage", "unknown")
    self_report = state.get("self_report_status", "unseen")
    if bool(state.get("explicit_known")) or self_report == "known" or (stage == "self_reported_known" and not state.get("next_window_start")):
        return -1_000
    due_at = parse_time(state.get("next_window_start"))
    window_end = parse_time(state.get("next_window_end"))
    if due_at and due_at > now:
        return -500
    window_expired = bool(window_end and now > window_end)
    stage_score = {
        "unknown": 100,
        "gist_only": 92,
        "taught": 72,
        "aural_form_linked": 55,
        "generalized": 20,
        "self_reported_known": 15,
    }.get(stage, 70)
    if window_expired:
        stage_score = min(stage_score, 25)
    if self_report == "familiar":
        stage_score = min(stage_score, 38)
    elif self_report == "unclear":
        stage_score += 28
    value_score = float(item.get("value_score", 2)) * 5
    confidence_bonus = 8 if state.get("confidence") == "low" else 0
    replay_bonus = min(10, int(state.get("replay_count", 0)) * 2)
    frontier_bonus = 0.0
    frequency = item_frequency(item)
    if vocabulary_frontier is not None and frequency is not None:
        distance = abs(frequency - vocabulary_frontier)
        frontier_bonus += max(0.0, 18.0 - distance * 10.0)
        if frequency > vocabulary_frontier + 1.0:
            frontier_bonus -= (frequency - vocabulary_frontier - 1.0) * 12.0
        elif frequency < vocabulary_frontier - 1.3:
            frontier_bonus -= (vocabulary_frontier - frequency - 1.3) * 8.0
    repetition_penalty = int(state.get("teach_count", 0)) * 14
    return stage_score + value_score + confidence_bonus + replay_bonus + frontier_bonus - repetition_penalty


def intervention_budgets(
    effective_speech_sec: float | int | None,
    candidate_count: int,
    *,
    override: dict[str, int] | None = None,
) -> dict[str, int]:
    """Return nested interruption ceilings for one video.

    These are frequency budgets, not estimates of learning or mastery.  The
    candidate pool is an independent quality-filtered input; every budget is
    clamped to what the source material can actually support.
    """

    available = max(0, int(candidate_count))
    if available == 0:
        return {intensity: 0 for intensity in INTENSITIES}
    if override is not None:
        result = {
            intensity: min(available, max(0, int(override.get(intensity, 0))))
            for intensity in INTENSITIES
        }
    elif effective_speech_sec is None:
        result = {
            intensity: min(available, int(POLICIES[intensity]["max_teaches"]))
            for intensity in INTENSITIES
        }
    else:
        speech = max(0.0, float(effective_speech_sec))
        result = {}
        for intensity in INTENSITIES:
            policy = INTERVENTION_DENSITY[intensity]
            desired = max(1, math.ceil(speech / float(policy["seconds_per_intervention"])))
            result[intensity] = min(available, int(policy["cap"]), desired)
    result["medium"] = max(result["low"], result["medium"])
    result["high"] = max(result["medium"], result["high"])
    return result


def _spread_pick(
    candidates: list[tuple[dict[str, Any], float]],
    count: int,
) -> list[tuple[dict[str, Any], float]]:
    count = min(max(0, int(count)), len(candidates))
    if count == 0:
        return []
    ordered = sorted(candidates, key=lambda pair: float(pair[0]["anchor_sec"]))
    anchors = [float(pair[0]["anchor_sec"]) for pair in ordered]
    low_anchor, high_anchor = min(anchors), max(anchors)
    targets = [
        low_anchor + (high_anchor - low_anchor) * ((index + 1) / (count + 1))
        for index in range(count)
    ]
    remaining = list(ordered)
    chosen: list[tuple[dict[str, Any], float]] = []
    for target in targets:
        best = max(
            remaining,
            key=lambda pair: pair[1] - abs(float(pair[0]["anchor_sec"]) - target) * 0.65,
        )
        chosen.append(best)
        remaining.remove(best)
    return sorted(chosen, key=lambda pair: float(pair[0]["anchor_sec"]))


def choose_nested_candidates(
    content: dict[str, Any],
    profile: dict[str, Any],
    *,
    seed: int,
    now: datetime | None = None,
    exclude_ids: Iterable[str] = (),
    tier_budgets: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    now = now or utc_now()
    excluded = set(exclude_ids)
    rng = random.Random(seed)
    frontier = estimate_vocabulary_frontier(profile)
    candidates = []
    for item in content["items"]:
        if item["id"] in excluded:
            continue
        state = profile["items"].get(item["id"], seed_item_state(item["id"]))
        score = item_priority(item, state, now, vocabulary_frontier=frontier)
        if score > -100:
            candidates.append((item, score + rng.random() * 0.001))
    if not candidates:
        return []

    if tier_budgets is None:
        configured_override = content.get("intervention_budget_override")
        tier_budgets = intervention_budgets(
            content.get("effective_speech_sec"),
            len(candidates),
            override=configured_override if isinstance(configured_override, dict) else None,
        )
    budgets = {
        intensity: min(len(candidates), max(0, int(tier_budgets.get(intensity, 0))))
        for intensity in INTENSITIES
    }
    budgets["medium"] = max(budgets["low"], budgets["medium"])
    budgets["high"] = max(budgets["medium"], budgets["high"])
    if budgets["high"] == 0:
        return []

    chosen = _spread_pick(candidates, budgets["high"])
    medium = _spread_pick(chosen, budgets["medium"])
    low = _spread_pick(medium, budgets["low"])
    medium_ids = {str(item["id"]) for item, _priority in medium}
    low_ids = {str(item["id"]) for item, _priority in low}

    rows = []
    for item, priority in chosen:
        item_id = str(item["id"])
        tier = "low" if item_id in low_ids else "medium" if item_id in medium_ids else "high"
        row = deepcopy(item)
        row["min_intensity"] = tier
        row["policy_priority"] = round(priority, 3)
        rows.append(row)
    return rows


def delayed_probe_eligible(item: dict[str, Any], state: dict[str, Any], now: datetime) -> bool:
    if item.get("probe_eligible") is not True or item.get("probe_qc") != "weak_pilot":
        return False
    variant_id = str(item.get("probe_variant_id") or "")
    if not variant_id or not item.get("followup_audio") or not item.get("followup_sentence"):
        return False
    if int(state.get("teach_count", 0)) < 1 or not state.get("last_taught_at"):
        return False
    if variant_id in set(state.get("used_probe_variants") or []):
        return False
    taught_at = parse_time(state.get("last_taught_at"))
    if taught_at is None:
        return False
    window_start = parse_time(state.get("next_window_start")) or taught_at + timedelta(hours=PROBE_WINDOW_START_HOURS)
    window_end = parse_time(state.get("next_window_end")) or taught_at + timedelta(hours=PROBE_WINDOW_END_HOURS)
    return window_start <= now <= window_end


def build_delayed_probe(
    content: dict[str, Any],
    profile: dict[str, Any],
    *,
    seed: int,
    now: datetime,
) -> dict[str, Any] | None:
    rng = random.Random(seed ^ 0xA17D10)
    eligible: list[tuple[dict[str, Any], datetime, float]] = []
    for item in content["items"]:
        state = profile["items"].get(item["id"], seed_item_state(item["id"]))
        if not delayed_probe_eligible(item, state, now):
            continue
        deadline = parse_time(state.get("next_window_end")) or now
        eligible.append((item, deadline, rng.random()))
    if not eligible:
        return None
    eligible.sort(key=lambda row: (row[1], -float(row[0].get("value_score", 0)), row[2]))
    target = eligible[0][0]
    distractor_labels = [
        str(label).strip()
        for label in target.get("probe_distractors_zh", [])
        if str(label).strip() and str(label).strip() != str(target.get("gloss_zh", "")).strip()
    ]
    if len(set(distractor_labels)) != 2:
        return None
    choice_labels = [str(target["gloss_zh"]), *distractor_labels]
    rng.shuffle(choice_labels)
    probe_id = uuid.uuid4().hex
    choices = [
        {
            "choice_id": uuid.uuid5(uuid.NAMESPACE_URL, f"{probe_id}:{label}").hex,
            "label": label,
        }
        for label in choice_labels
    ]
    correct_choice_id = next(row["choice_id"] for row in choices if row["label"] == target["gloss_zh"])
    target_state = profile["items"][target["id"]]
    return {
        "probe_id": probe_id,
        "variant_id": target["probe_variant_id"],
        "item_id": target["id"],
        "purpose": "measure",
        "primary_dimension": "auditory_to_current_sense",
        "template_version": "followup-3choice-v1",
        "context_family_id": target["probe_variant_id"],
        "speaker_id": "cosyvoice2-followup-ref-v1",
        "speaker_type": "synthetic",
        "speaker_relation_to_teach": "new_to_item",
        "source_taught_at": target_state.get("last_taught_at"),
        "eligible_at": target_state.get("next_window_start"),
        "followup_audio": target["followup_audio"],
        "followup_sentence": target["followup_sentence"],
        "choices": choices,
        "correct_choice_id": correct_choice_id,
        "feedback_snapshot": {
            "surface": target["surface"],
            "gloss_zh": target["gloss_zh"],
            "followup_sentence": target["followup_sentence"],
        },
        "created_at": isoformat(now),
        "started_at": None,
        "presentation_ids": [],
        "presentation_count": 0,
        "completed_at": None,
        "result": None,
    }


def eligible_candidate_count(
    content: dict[str, Any],
    profile: dict[str, Any],
    *,
    now: datetime | None = None,
    exclude_ids: Iterable[str] = (),
) -> int:
    now = now or utc_now()
    excluded = set(exclude_ids)
    frontier = estimate_vocabulary_frontier(profile)
    return sum(
        1
        for item in content.get("items", [])
        if item["id"] not in excluded
        and item_priority(item, profile["items"].get(item["id"], seed_item_state(item["id"])), now, vocabulary_frontier=frontier) > -100
    )


def new_session(
    content: dict[str, Any],
    profile: dict[str, Any],
    now: datetime | None = None,
    *,
    pack_id: str = "fixture-nasa-lro-v1",
) -> dict[str, Any]:
    now = now or utc_now()
    session_id = uuid.uuid4().hex
    seed = int.from_bytes(uuid.UUID(session_id).bytes[:8], "big")
    probe = build_delayed_probe(content, profile, seed=seed, now=now)
    excluded_ids = {probe["item_id"]} if probe else set()
    configured_override = content.get("intervention_budget_override")
    eligible_teaching_count = eligible_candidate_count(content, profile, now=now, exclude_ids=excluded_ids)
    available_interventions = eligible_teaching_count + (1 if probe else 0)
    total_budgets = intervention_budgets(
        content.get("effective_speech_sec"),
        available_interventions,
        override=configured_override if isinstance(configured_override, dict) else None,
    )
    teaching_budgets = {
        intensity: max(0, budget - (1 if probe else 0))
        for intensity, budget in total_budgets.items()
    }
    items = choose_nested_candidates(
        content,
        profile,
        seed=seed,
        now=now,
        exclude_ids=excluded_ids,
        tier_budgets=teaching_budgets,
    )
    return {
        "schema_version": 1,
        "session_id": session_id,
        "pack_id": pack_id,
        "policy_version": POLICY_VERSION,
        "created_at": isoformat(now),
        "started_at": None,
        "completed_at": None,
        "stage": "probe_ready" if probe else "watch_ready",
        "owner_client_id": None,
        "owner_epoch": 0,
        "explicit_intensity_at_start": profile["explicit_intensity"],
        "effective_intensity_at_start": effective_intensity(profile),
        "preference_epoch_at_start": int(profile.get("adaptive", {}).get("preference_epoch", 1)),
        "probe": probe,
        "candidate_pool_count": len(content.get("items", [])),
        "eligible_candidate_count": eligible_teaching_count,
        "effective_speech_sec": content.get("effective_speech_sec"),
        "intervention_budgets": total_budgets,
        "teaching_budgets": teaching_budgets,
        "intervention_budget_includes_probe": True,
        "items": items,
        "encountered_ids": [],
        "interactions": {},
        "open_interaction": None,
        "last_media_time": 0.0,
        "progress_seq": 0,
        "metrics": None,
    }


def intensity_allows(current: str, minimum: str) -> bool:
    return INTENSITY_RANK[current] >= INTENSITY_RANK[minimum]


def mapping_hold_ms(profile: dict[str, Any], surface: str, gloss: str) -> int:
    learned = int(profile.get("reading", {}).get("mapping_dwell_ms", 6000))
    text_floor = 2400 + min(3600, len(surface) * 90 + len(gloss) * 230)
    return max(4000, min(12_000, max(learned, text_floor)))


def apply_encounter(
    profile: dict[str, Any],
    item_id: str,
    now: datetime | None = None,
    *,
    policy_version: str = POLICY_VERSION,
) -> dict[str, Any]:
    now = now or utc_now()
    profile = deepcopy(profile)
    state = profile["items"][item_id]
    state["encounter_count"] = int(state.get("encounter_count", 0)) + 1
    state["last_encounter_at"] = isoformat(now)
    if state.get("aural_stage") == "unknown":
        state["aural_stage"] = "gist_only"
    state.setdefault("evidence", []).append(
        {
            "at": isoformat(now),
            "kind": "ENCOUNTER",
            "dimension": "auditory_to_current_sense",
            "translation_visible": True,
            "policy_version": policy_version,
        }
    )
    state["evidence"] = state["evidence"][-30:]
    profile["updated_at"] = isoformat(now)
    return profile


def apply_lexicon_feedback(
    profile: dict[str, Any],
    *,
    knowledge_key: str,
    surface: str,
    gloss_zh: str,
    feedback: str,
    replays: int = 0,
    source: str = "mapping",
    count_replays: bool = True,
    context_id: str | None = None,
    now: datetime | None = None,
    policy_version: str = POLICY_VERSION,
) -> dict[str, Any]:
    if feedback not in FAMILIARITY_FEEDBACK:
        raise ValueError("invalid_familiarity_feedback")
    now = now or utc_now()
    profile = deepcopy(profile)
    key = str(knowledge_key or "").strip()
    if not key:
        raise ValueError("knowledge_key_required")
    lexical = profile.setdefault("lexicon", {}).setdefault(
        key,
        seed_lexicon_state(key, surface, gloss_zh, context_id=context_id),
    )
    if context_id:
        contexts = lexical.setdefault("context_fingerprints", [])
        if context_id not in contexts:
            contexts.append(context_id)
    lexical["surface"] = str(surface or lexical.get("surface") or "")
    lexical["gloss_zh"] = str(gloss_zh or lexical.get("gloss_zh") or "")
    counts = lexical.setdefault("feedback_counts", {name: 0 for name in FAMILIARITY_FEEDBACK})
    for name in FAMILIARITY_FEEDBACK:
        counts.setdefault(name, 0)
    counts[feedback] = int(counts.get(feedback, 0)) + 1
    lexical["status"] = feedback
    source_name = str(source or "mapping")
    explicit_source = source_name in {"subtitle", "explicit_no_more_explanations"}
    if feedback == "known":
        lexical["explicit_known"] = bool(lexical.get("explicit_known")) or explicit_source
    else:
        lexical["explicit_known"] = False
    lexical["score"] = {"known": 1.0, "familiar": 0.35, "unclear": -0.5}[feedback]
    if count_replays:
        lexical["replay_count"] = int(lexical.get("replay_count", 0)) + max(0, int(replays))
    lexical["last_feedback_at"] = isoformat(now)
    lexical.setdefault("evidence", []).append(
        {
            "at": isoformat(now),
            "kind": (
                "EXPLICIT_KNOWN_OVERRIDE"
                if source_name == "explicit_no_more_explanations" and feedback == "known"
                else "EXPLICIT_STATE_EDIT"
                if source_name == "subtitle"
                else "SELF_REPORT_AFTER_ANSWER"
            ),
            "feedback": feedback,
            "source": source_name[:40],
            "intent": (
                "no_more_explanations"
                if source_name == "explicit_no_more_explanations"
                else "subtitle_state_edit"
                if source_name == "subtitle"
                else "card_feedback"
            ),
            "known_override": bool(lexical.get("explicit_known")),
            "context_fingerprint": context_id,
            "replays": max(0, int(replays)),
            "answer_visible": True,
            "clean_evidence": False,
            "policy_version": policy_version,
        }
    )
    lexical["evidence"] = lexical["evidence"][-30:]

    for occurrence_id in lexical.get("occurrence_ids", []):
        state = profile.get("items", {}).get(occurrence_id)
        if not state:
            continue
        state["knowledge_key"] = key
        state["self_report_status"] = feedback
        state["explicit_known"] = bool(lexical.get("explicit_known"))
        if count_replays:
            state["replay_count"] = int(state.get("replay_count", 0)) + max(0, int(replays))
        if feedback == "known":
            state["aural_stage"] = "self_reported_known"
            state["confidence"] = "self_report"
            state["next_window_start"] = None
            state["next_window_end"] = None
        elif feedback == "familiar":
            if state.get("aural_stage") == "self_reported_known":
                state["aural_stage"] = "taught" if int(state.get("teach_count", 0)) else "unknown"
            state["confidence"] = "low"
            state["next_window_start"] = isoformat(now + timedelta(days=3))
            state["next_window_end"] = isoformat(now + timedelta(days=10))
        else:
            if state.get("aural_stage") in {"generalized", "stable_for_now"}:
                state["aural_stage"] = "conflicted"
            elif state.get("aural_stage") == "self_reported_known":
                state["aural_stage"] = "taught" if int(state.get("teach_count", 0)) else "unknown"
            state["confidence"] = "low"
            state["next_window_start"] = isoformat(now + timedelta(hours=PROBE_WINDOW_START_HOURS))
            state["next_window_end"] = isoformat(now + timedelta(hours=PROBE_WINDOW_END_HOURS))
    profile["updated_at"] = isoformat(now)
    return profile


def apply_interaction(
    profile: dict[str, Any],
    item_id: str,
    *,
    outcome: str,
    dwell_ms: int,
    phrase_confirmed: bool,
    replays: int,
    familiarity_feedback: str | None = None,
    encounter_already_recorded: bool = False,
    now: datetime | None = None,
    policy_version: str = POLICY_VERSION,
) -> dict[str, Any]:
    now = now or utc_now()
    if familiarity_feedback is not None and familiarity_feedback not in FAMILIARITY_FEEDBACK:
        raise ValueError("invalid_familiarity_feedback")
    profile = deepcopy(profile)
    state = profile["items"][item_id]
    if not encounter_already_recorded:
        state["encounter_count"] = int(state.get("encounter_count", 0)) + 1
        state["last_encounter_at"] = isoformat(now)
    evidence = {
        "at": isoformat(now),
        "kind": "TEACH" if outcome == "completed" else "SKIP" if outcome == "skipped" else "TECHNICAL",
        "dimension": "auditory_to_current_sense",
        "phrase_confirmed": bool(phrase_confirmed),
        "dwell_ms": max(0, int(dwell_ms)),
        "replays": max(0, int(replays)),
        "familiarity_feedback": familiarity_feedback,
        "policy_version": policy_version,
    }
    if familiarity_feedback is not None:
        evidence["self_report_after_answer"] = True
    state.setdefault("evidence", []).append(evidence)
    state["evidence"] = state["evidence"][-30:]
    replay_count = max(0, int(replays))
    if replay_count:
        state["replay_count"] = int(state.get("replay_count", 0)) + replay_count
        replay_key = str(state.get("knowledge_key") or "")
        lexical_replay = profile.get("lexicon", {}).get(replay_key)
        if lexical_replay is not None:
            lexical_replay["replay_count"] = int(lexical_replay.get("replay_count", 0)) + replay_count

    if outcome == "completed" and phrase_confirmed:
        state["aural_stage"] = "taught"
        state["confidence"] = "low"
        state["teach_count"] = int(state.get("teach_count", 0)) + 1
        state["last_taught_at"] = isoformat(now)
        state["next_window_start"] = isoformat(now + timedelta(hours=20))
        state["next_window_end"] = isoformat(now + timedelta(hours=72))
        if 1500 <= dwell_ms <= 30_000:
            reading = profile["reading"]
            old = int(reading.get("mapping_dwell_ms", 6000))
            samples = int(reading.get("sample_count", 0))
            clamped = max(3500, min(12_000, int(dwell_ms)))
            reading["mapping_dwell_ms"] = round(old * 0.75 + clamped * 0.25) if samples else clamped
            reading["sample_count"] = samples + 1
        if familiarity_feedback is not None:
            key = str(state.get("knowledge_key") or "")
            lexical = profile.get("lexicon", {}).get(key) or {}
            profile = apply_lexicon_feedback(
                profile,
                knowledge_key=key,
                surface=str(lexical.get("surface") or item_id),
                gloss_zh=str(lexical.get("gloss_zh") or "当前义项"),
                feedback=familiarity_feedback,
                replays=replays,
                source="mapping",
                count_replays=False,
                now=now,
                policy_version=policy_version,
            )
    profile["updated_at"] = isoformat(now)
    return profile


def apply_probe_result(
    profile: dict[str, Any],
    item_id: str,
    *,
    variant_id: str,
    outcome: str,
    audio_confirmed: bool,
    choice_count: int,
    response_ms: int,
    delay_hours: float = 0.0,
    presentation_count: int = 1,
    probe_id: str = "",
    now: datetime | None = None,
    policy_version: str = POLICY_VERSION,
) -> dict[str, Any]:
    if outcome not in {"correct", "incorrect", "dont_know", "skipped", "technical_failure"}:
        raise ValueError("invalid_probe_outcome")
    scorable = bool(audio_confirmed and outcome in {"correct", "incorrect", "dont_know"})
    if not scorable:
        return deepcopy(profile)
    now = now or utc_now()
    profile = deepcopy(profile)
    state = profile["items"][item_id]
    presentation_count = max(1, int(presentation_count))
    evidence_quality = "ASSISTED_PRACTICE" if presentation_count > 1 else "NOT_EVIDENCE"
    if presentation_count > 1:
        state["assisted_probe_count"] = int(state.get("assisted_probe_count", 0)) + 1
    elif outcome == "correct":
        state["recognition_success_count"] = int(state.get("recognition_success_count", 0)) + 1
        evidence_quality = "WEAK_SUCCESS"
    elif outcome == "dont_know":
        state["recognition_abstention_count"] = int(state.get("recognition_abstention_count", 0)) + 1
        evidence_quality = "ABSTAINED_NONRETRIEVAL"
    else:
        state["recognition_failure_count"] = int(state.get("recognition_failure_count", 0)) + 1
        state["confidence"] = "low"
        evidence_quality = "RECOGNITION_FAILURE"
    if scorable:
        used = list(state.get("used_probe_variants") or [])
        if variant_id not in used:
            used.append(variant_id)
        state["used_probe_variants"] = used[-12:]
        state["last_probe_at"] = isoformat(now)
        state["last_probe_result"] = outcome
        if outcome == "correct":
            state["next_window_start"] = isoformat(now + timedelta(days=6))
            state["next_window_end"] = isoformat(now + timedelta(days=10))
        else:
            state["next_window_start"] = isoformat(now + timedelta(hours=PROBE_WINDOW_START_HOURS))
            state["next_window_end"] = isoformat(now + timedelta(hours=PROBE_WINDOW_END_HOURS))
    state.setdefault("evidence", []).append(
        {
            "at": isoformat(now),
            "kind": "VERIFY",
            "dimension": "auditory_to_current_sense",
            "variant_id": variant_id,
            "probe_id": probe_id or None,
            "outcome": outcome,
            "audio_confirmed": bool(audio_confirmed),
            "first_response": True,
            "presentation_count": presentation_count,
            "first_presentation": presentation_count == 1,
            "answer_revealed_before_response": False,
            "meaning_options_visible_before_response": True,
            "surface_visible_before_response": False,
            "choice_count": max(0, int(choice_count)),
            "response_ms": max(0, int(response_ms)),
            "delay_hours": max(0.0, round(float(delay_hours), 3)),
            "evidence_quality": evidence_quality,
            "policy_version": policy_version,
        }
    )
    state["evidence"] = state["evidence"][-30:]
    profile["updated_at"] = isoformat(now)
    return profile


def complete_session_adaptation(
    profile: dict[str, Any],
    session: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    profile = deepcopy(profile)
    adaptive = profile["adaptive"]
    if session.get("adaptation_applied"):
        return profile
    current_epoch = int(adaptive.get("preference_epoch", 1))
    session_epoch = int(session.get("preference_epoch_at_start", current_epoch))
    comparable = [
        row
        for row in session.get("interactions", {}).values()
        if row.get("outcome") in {"completed", "skipped"}
        and not row.get("technical_failure")
        and int(row.get("preference_epoch", session_epoch)) == current_epoch
    ]
    skipped = sum(row.get("outcome") == "skipped" for row in comparable)
    completed = sum(row.get("outcome") == "completed" for row in comparable)
    skip_rate = skipped / len(comparable) if comparable else 0.0
    adaptive["completed_sessions"] = int(adaptive.get("completed_sessions", 0)) + 1
    adaptive["comparable_opportunities"] = int(adaptive.get("comparable_opportunities", 0)) + len(comparable)
    adaptive["epoch_completed_sessions"] = int(adaptive.get("epoch_completed_sessions", 0)) + 1
    adaptive["epoch_comparable_opportunities"] = int(adaptive.get("epoch_comparable_opportunities", 0)) + len(comparable)
    history = adaptive.setdefault("session_history", [])
    history.append(
        {
            "session_id": session["session_id"],
            "preference_epoch": int(adaptive.get("preference_epoch", 1)),
            "at": isoformat(now),
            "comparable": len(comparable),
            "completed": completed,
            "skipped": skipped,
            "skip_rate": round(skip_rate, 4),
        }
    )
    adaptive["session_history"] = history[-12:]

    stored_history = adaptive["session_history"]
    current_epoch = int(adaptive.get("preference_epoch", 1))
    epoch_history = [
        row
        for row in stored_history
        if int(row.get("preference_epoch", current_epoch)) == current_epoch
    ]
    recent = [row for row in epoch_history[-3:] if int(row.get("comparable", 0)) > 0]
    enough = (
        int(adaptive.get("epoch_completed_sessions", 0)) >= 3
        and int(adaptive.get("epoch_comparable_opportunities", 0)) >= 10
        and len(recent) == 3
    )
    last_changed = parse_time(adaptive.get("last_changed_at"))
    change_cooldown_ok = last_changed is None or now - last_changed >= timedelta(days=7)
    if int(adaptive.get("reduction", 0)) == 0 and enough and change_cooldown_ok:
        comparable_total = sum(int(row["comparable"]) for row in recent)
        skipped_total = sum(int(row["skipped"]) for row in recent)
        recent_skip_rate = skipped_total / comparable_total if comparable_total else 0.0
        negative_sessions = sum(float(row.get("skip_rate", 0)) >= 0.50 for row in recent)
        if recent_skip_rate >= 0.60 and negative_sessions >= 2:
            adaptive["reduction"] = -1
            adaptive["reason"] = "three_session_skip_trend"
            adaptive["last_changed_at"] = isoformat(now)
    elif int(adaptive.get("reduction", 0)) < 0 and last_changed is not None:
        since_change = [
            row
            for row in epoch_history
            if (parse_time(row.get("at")) or now) > last_changed and int(row.get("comparable", 0)) > 0
        ]
        comparable_total = sum(int(row["comparable"]) for row in since_change)
        skipped_total = sum(int(row["skipped"]) for row in since_change)
        recovery_rate = skipped_total / comparable_total if comparable_total else 1.0
        recent_three_stable = len(since_change) >= 3 and all(float(row.get("skip_rate", 1)) <= 0.20 for row in since_change[-3:])
        if len(since_change) >= 5 and comparable_total >= 15 and recovery_rate <= 0.20 and recent_three_stable:
            adaptive["reduction"] = 0
            adaptive["reason"] = "five_session_recovery"
            adaptive["last_changed_at"] = isoformat(now)
    profile["updated_at"] = isoformat(now)
    return profile


def rebuild_profile_from_events(
    content: dict[str, Any],
    events: Iterable[dict[str, Any]],
    *,
    known_ids: Iterable[str] = (),
) -> dict[str, Any]:
    event_rows = list(events)
    profile_created = next((event for event in event_rows if event.get("type") == "profile_created"), None)
    initial_time = parse_time(profile_created.get("at")) if profile_created else None
    profile = new_profile(content, known_ids, now=initial_time)
    interactions_by_session: dict[str, dict[str, dict[str, Any]]] = {}
    encounters_by_session: dict[str, set[str]] = {}
    session_epochs: dict[str, int] = {}
    session_pack_ids: dict[str, str] = {}
    seen_event_ids: set[str] = set()
    resolved_probe_fingerprints: dict[str, str] = {}
    for event in event_rows:
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen_event_ids:
            continue
        if event_id:
            seen_event_ids.add(event_id)
        event_type = event.get("type")
        data = event.get("data") or {}
        event_policy_version = str(event.get("policy_version") or data.get("policy_version") or POLICY_VERSION)
        at = parse_time(event.get("at")) or utc_now()
        session_id = str(event.get("session_id") or "")
        if event_type == "session_created" and session_id:
            pack_id = str(data.get("pack_id") or "fixture-nasa-lro-v1")
            session_pack_ids[session_id] = pack_id
            profile["active_session_id"] = session_id
            profile.setdefault("active_sessions", {})[pack_id] = session_id
            session_epochs[session_id] = int(data.get("preference_epoch", profile["adaptive"].get("preference_epoch", 1)))
        elif event_type == "explicit_intensity_changed" and data.get("intensity") in INTENSITIES:
            profile = set_explicit_intensity(profile, data["intensity"], now=at)
        elif event_type == "encounter_recorded" and data.get("item_id") in profile["items"]:
            profile = apply_encounter(profile, data["item_id"], now=at, policy_version=event_policy_version)
            if session_id:
                encounters_by_session.setdefault(session_id, set()).add(data["item_id"])
        elif event_type == "lexicon_feedback":
            feedback_key = str(data.get("knowledge_key") or "")
            if feedback_key.startswith("v1:"):
                continue
            profile = apply_lexicon_feedback(
                profile,
                knowledge_key=feedback_key,
                surface=str(data.get("surface") or ""),
                gloss_zh=str(data.get("gloss_zh") or ""),
                feedback=str(data.get("familiarity_feedback") or ""),
                replays=int(data.get("replays") or 0),
                source=str(data.get("source") or "subtitle"),
                context_id=str(data.get("context_fingerprint") or "") or None,
                now=at,
                policy_version=event_policy_version,
            )
        elif event_type == "interaction_completed" and data.get("item_id") in profile["items"]:
            profile = apply_interaction(
                profile,
                data["item_id"],
                outcome=data.get("outcome", "technical_failure"),
                dwell_ms=int(data.get("dwell_ms") or 0),
                phrase_confirmed=bool(data.get("phrase_confirmed")),
                replays=int(data.get("replays") or 0),
                familiarity_feedback=data.get("familiarity_feedback"),
                encounter_already_recorded=data["item_id"] in encounters_by_session.get(session_id, set()),
                now=at,
                policy_version=event_policy_version,
            )
            if session_id:
                interactions_by_session.setdefault(session_id, {})[data["item_id"]] = deepcopy(data)
        elif event_type == "probe_completed" and data.get("item_id") in profile["items"]:
            probe_id = str(data.get("probe_id") or "")
            semantic_fingerprint = str(data.get("semantic_fingerprint") or "")
            if not semantic_fingerprint:
                semantic_fingerprint = json.dumps(
                    {
                        "probe_id": probe_id,
                        "item_id": data.get("item_id"),
                        "variant_id": data.get("variant_id"),
                        "outcome": data.get("outcome"),
                        "selected_choice_id": data.get("selected_choice_id"),
                        "audio_confirmed": bool(data.get("audio_confirmed")),
                        "presentation_count": int(data.get("presentation_count") or 1),
                        "choice_count": int(data.get("choice_count") or 0),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            if probe_id in resolved_probe_fingerprints:
                if resolved_probe_fingerprints[probe_id] != semantic_fingerprint:
                    raise ValueError(f"conflicting_probe_result:{probe_id}")
                continue
            if probe_id:
                resolved_probe_fingerprints[probe_id] = semantic_fingerprint
            profile = apply_probe_result(
                profile,
                data["item_id"],
                variant_id=str(data.get("variant_id") or ""),
                outcome=str(data.get("outcome") or "technical_failure"),
                audio_confirmed=bool(data.get("audio_confirmed")),
                choice_count=int(data.get("choice_count") or 0),
                response_ms=int(data.get("response_ms") or 0),
                delay_hours=float(data.get("delay_hours") or 0),
                presentation_count=int(data.get("presentation_count") or 1),
                probe_id=str(data.get("probe_id") or ""),
                now=at,
                policy_version=event_policy_version,
            )
        elif event_type == "session_completed" and session_id:
            profile = complete_session_adaptation(
                profile,
                {
                    "session_id": session_id,
                    "interactions": interactions_by_session.get(session_id, {}),
                    "preference_epoch_at_start": session_epochs.get(session_id, 1),
                },
                now=at,
            )
            pack_id = session_pack_ids.get(session_id, "fixture-nasa-lro-v1")
            if profile.setdefault("active_sessions", {}).get(pack_id) == session_id:
                profile["active_sessions"].pop(pack_id, None)
            if profile.get("active_session_id") == session_id:
                remaining = list(profile["active_sessions"].values())
                profile["active_session_id"] = remaining[-1] if remaining else None
    return profile


def profile_view(profile: dict[str, Any]) -> dict[str, Any]:
    explicit = profile["explicit_intensity"]
    effective = effective_intensity(profile)
    recognition_successes = sum(int(state.get("recognition_success_count", 0)) for state in profile.get("items", {}).values())
    recognition_failures = sum(int(state.get("recognition_failure_count", 0)) for state in profile.get("items", {}).values())
    recognition_abstentions = sum(int(state.get("recognition_abstention_count", 0)) for state in profile.get("items", {}).values())
    assisted_probes = sum(int(state.get("assisted_probe_count", 0)) for state in profile.get("items", {}).values())
    vocabulary_counts = {status: 0 for status in ("unseen", "known", "familiar", "unclear")}
    for lexical in profile.get("lexicon", {}).values():
        status = str(lexical.get("status") or "unseen")
        vocabulary_counts[status if status in vocabulary_counts else "unseen"] += 1
    return {
        "schema_version": profile["schema_version"],
        "policy_version": profile["policy_version"],
        "reducer_version": profile.get("reducer_version", REDUCER_VERSION),
        "control_semantics": "intervention_frequency",
        "explicit_intensity": explicit,
        "effective_intensity": effective,
        "explicit_frequency": explicit,
        "effective_frequency": effective,
        "adaptive_reduced": INTENSITY_RANK[effective] < INTENSITY_RANK[explicit],
        "preference_epoch": int(profile["adaptive"].get("preference_epoch", 1)),
        "epoch_completed_sessions": int(profile["adaptive"].get("epoch_completed_sessions", 0)),
        "epoch_comparable_opportunities": int(profile["adaptive"].get("epoch_comparable_opportunities", 0)),
        "mapping_hold_ms": int(profile["reading"]["mapping_dwell_ms"]),
        "reading_samples": int(profile["reading"]["sample_count"]),
        "completed_sessions": int(profile["adaptive"]["completed_sessions"]),
        "comparable_opportunities": int(profile["adaptive"]["comparable_opportunities"]),
        "recognition_successes": recognition_successes,
        "recognition_failures": recognition_failures,
        "recognition_abstentions": recognition_abstentions,
        "assisted_probes": assisted_probes,
        "vocabulary_counts": vocabulary_counts,
        "vocabulary_frontier_zipf": estimate_vocabulary_frontier(profile),
        "active_session_id": profile.get("active_session_id"),
        "active_sessions": deepcopy(profile.get("active_sessions", {})),
    }
