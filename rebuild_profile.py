from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path

from adaptive_core import atomic_write_json, ensure_profile, rebuild_profile_from_events
from progressive_video_pack import verify_window_shard
from video_pack import verify_pack_directory

ROOT = Path(__file__).resolve().parent


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def comparable_projection(profile: dict) -> dict:
    return {
        "schema_version": profile["schema_version"],
        "policy_version": profile["policy_version"],
        "reducer_version": profile.get("reducer_version"),
        "explicit_intensity": profile["explicit_intensity"],
        "active_session_id": profile.get("active_session_id"),
        "active_sessions": profile.get("active_sessions", {}),
        "adaptive": {
            "reduction": profile["adaptive"]["reduction"],
            "preference_epoch": profile["adaptive"].get("preference_epoch"),
            "epoch_completed_sessions": profile["adaptive"].get("epoch_completed_sessions"),
            "epoch_comparable_opportunities": profile["adaptive"].get("epoch_comparable_opportunities"),
            "epoch_started_at": profile["adaptive"].get("epoch_started_at"),
            "comparable_opportunities": profile["adaptive"]["comparable_opportunities"],
            "completed_sessions": profile["adaptive"]["completed_sessions"],
            "session_history": profile["adaptive"]["session_history"],
        },
        "reading": profile["reading"],
        "items": {
            item_id: {
                key: state.get(key)
                for key in (
                    "aural_stage",
                    "knowledge_key",
                    "self_report_status",
                    "explicit_known",
                    "replay_count",
                    "confidence",
                    "encounter_count",
                    "teach_count",
                    "clean_success_count",
                    "clean_failure_count",
                    "recognition_success_count",
                    "recognition_failure_count",
                    "recognition_abstention_count",
                    "assisted_probe_count",
                    "last_probe_at",
                    "last_probe_result",
                    "used_probe_variants",
                    "last_encounter_at",
                    "last_taught_at",
                    "next_window_start",
                    "next_window_end",
                    "evidence",
                )
            }
            for item_id, state in profile["items"].items()
        },
        "lexicon": deepcopy(profile.get("lexicon") or {}),
        "legacy_lexicon_v1": deepcopy(profile.get("legacy_lexicon_v1") or {}),
    }


def diff_values(existing, rebuilt, path="") -> list[dict]:
    if isinstance(existing, dict) and isinstance(rebuilt, dict):
        rows = []
        for key in sorted(set(existing) | set(rebuilt)):
            rows.extend(diff_values(existing.get(key), rebuilt.get(key), f"{path}.{key}" if path else key))
        return rows
    if isinstance(existing, list) and isinstance(rebuilt, list):
        rows = []
        for index in range(max(len(existing), len(rebuilt))):
            left = existing[index] if index < len(existing) else "<missing>"
            right = rebuilt[index] if index < len(rebuilt) else "<missing>"
            rows.extend(diff_values(left, right, f"{path}[{index}]"))
        return rows
    if existing != rebuilt:
        return [{"path": path, "existing": existing, "rebuilt": rebuilt}]
    return []


def load_combined_content(data_dir: Path) -> dict:
    content = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))
    merged = dict(content)
    merged["items"] = list(content.get("items", []))
    configured = os.environ.get("INFLOW_PACKS_DIR")
    packs_root = Path(configured) if configured else (ROOT / "data" / "packs" if data_dir.resolve() == (ROOT / "data" / "live").resolve() else data_dir / "packs")
    if not packs_root.is_dir():
        return merged
    known = {item["id"] for item in merged["items"]}
    for manifest_path in sorted(packs_root.glob("*/manifest.json")):
        manifest = verify_pack_directory(manifest_path.parent, require_audit=True)
        for candidate in manifest.get("candidates", []):
            if candidate["id"] in known:
                continue
            item = dict(candidate)
            item["sentence_zh"] = item["phrase_zh"]
            item["accepted_zh"] = [item["gloss_zh"]]
            item["probe_eligible"] = False
            merged["items"].append(item)
            known.add(item["id"])
    for partial_path in sorted(packs_root.glob("*/partial-manifest.json")):
        partial = json.loads(partial_path.read_text(encoding="utf-8"))
        pack_id = str(partial.get("pack_id") or "")
        for window in partial.get("windows", []):
            if window.get("status") not in {"ready", "ready_no_candidate"}:
                continue
            shard = verify_window_shard(
                partial_path.parent / "shards" / str(window["window_id"]),
                pack_id=pack_id,
                window_id=str(window["window_id"]),
            )
            for candidate in shard.get("candidates", []):
                if candidate["id"] in known:
                    continue
                item = dict(candidate)
                item["sentence_zh"] = item["phrase_zh"]
                item["accepted_zh"] = [item["gloss_zh"]]
                item["probe_eligible"] = False
                merged["items"].append(item)
                known.add(item["id"])
    return merged


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "live")
    parser.add_argument("--write", action="store_true", help="replace profile.json only after a successful rebuild")
    args = parser.parse_args()

    content = load_combined_content(args.data_dir)
    seed = json.loads((ROOT / "seed-profile.json").read_text(encoding="utf-8"))
    events = read_events(args.data_dir / "events.jsonl")
    rebuilt = rebuild_profile_from_events(content, events, known_ids=seed.get("known_ids", []))
    existing_path = args.data_dir / "profile.json"
    existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else None
    if existing:
        existing = ensure_profile(existing, content)
        if existing.get("legacy_lexicon_v1"):
            rebuilt["legacy_lexicon_v1"] = deepcopy(existing["legacy_lexicon_v1"])
    existing_projection = comparable_projection(existing) if existing else None
    rebuilt_projection = comparable_projection(rebuilt)
    differences = diff_values(existing_projection, rebuilt_projection) if existing else []
    matches = existing is None or not differences
    output = {
        "ok": matches,
        "events": len(events),
        "existing": str(existing_path) if existing else None,
        "matches_existing": matches,
        "mismatches": differences[:20],
        "write_requested": args.write,
    }
    if not matches:
        raise SystemExit(json.dumps(output, ensure_ascii=False))
    if args.write:
        atomic_write_json(existing_path, rebuilt)
        output["written"] = str(existing_path)
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
