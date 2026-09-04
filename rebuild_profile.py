from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path

from adaptive_core import REDUCER_VERSION, atomic_write_json, ensure_profile, rebuild_profile_from_events
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
            "reason": profile["adaptive"].get("reason"),
            "last_changed_at": profile["adaptive"].get("last_changed_at"),
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


def acquire_data_directory_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "server.lock"
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
    except (OSError, IOError) as exc:
        handle.close()
        raise RuntimeError(f"data_directory_in_use:{data_dir}") from exc
    return handle


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "live")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="replace profile.json only when the current projection already matches")
    mode.add_argument("--migrate-reducer", action="store_true", help="backup and replay an older reducer into the current reducer")
    args = parser.parse_args()

    lock_handle = None
    if args.write or args.migrate_reducer:
        try:
            lock_handle = acquire_data_directory_lock(args.data_dir)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc

    content = load_combined_content(args.data_dir)
    seed_path = ROOT / "seed-profile.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8")) if seed_path.exists() else {"known_ids": []}
    events = read_events(args.data_dir / "events.jsonl")
    rebuilt = rebuild_profile_from_events(content, events, known_ids=seed.get("known_ids", []))
    existing_path = args.data_dir / "profile.json"
    existing_raw = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else None
    existing = ensure_profile(existing_raw, content) if existing_raw else None
    if existing and existing.get("legacy_lexicon_v1"):
        rebuilt["legacy_lexicon_v1"] = deepcopy(existing["legacy_lexicon_v1"])
    existing_projection = comparable_projection(existing) if existing else None
    rebuilt_projection = comparable_projection(rebuilt)
    differences = diff_values(existing_projection, rebuilt_projection) if existing else []
    matches = existing is None or not differences
    migration_allowed = False
    stored_reducer = str((existing_raw or {}).get("reducer_version") or "")
    if args.migrate_reducer:
        stored_match = re.fullmatch(r"rules-v(\d+)", stored_reducer)
        current_match = re.fullmatch(r"rules-v(\d+)", REDUCER_VERSION)
        if not existing_raw or not events:
            raise SystemExit("reducer_migration_requires_profile_and_events")
        if not stored_match or not current_match or int(stored_match.group(1)) >= int(current_match.group(1)):
            raise SystemExit(f"unsupported_reducer_migration:{stored_reducer}:{REDUCER_VERSION}")
        missing_items = sorted(set(existing_raw.get("items", {})) - set(rebuilt.get("items", {})))
        if missing_items:
            raise SystemExit(f"reducer_migration_missing_content:{','.join(missing_items[:8])}")
        migration_allowed = True
    output = {
        "ok": matches or migration_allowed,
        "events": len(events),
        "existing": str(existing_path) if existing else None,
        "matches_existing": matches,
        "mismatches": differences[:20],
        "write_requested": bool(args.write or args.migrate_reducer),
        "migration_requested": bool(args.migrate_reducer),
    }
    if differences and not migration_allowed:
        raise SystemExit(json.dumps(output, ensure_ascii=False))
    if args.migrate_reducer:
        digest = hashlib.sha256(json.dumps(existing_raw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        backup_path = args.data_dir / "backups" / f"profile.{stored_reducer}.pre-{REDUCER_VERSION}.{digest}.json"
        if not backup_path.exists():
            atomic_write_json(backup_path, existing_raw)
        atomic_write_json(existing_path, rebuilt)
        output["backup"] = str(backup_path)
        output["written"] = str(existing_path)
        output["migrated_from"] = stored_reducer
        output["migrated_to"] = REDUCER_VERSION
    elif args.write:
        atomic_write_json(existing_path, rebuilt)
        output["written"] = str(existing_path)
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
