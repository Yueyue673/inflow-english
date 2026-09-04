from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from video_pack import verify_pack_directory


def canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10]


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def apply_review(source: Path, destination_root: Path, review: dict) -> Path:
    manifest = verify_pack_directory(source, require_audit=True)
    if manifest["pack_id"] != review.get("source_pack_id"):
        raise ValueError("review_source_pack_mismatch")
    directives = review.get("candidates") or {}
    source_candidates = manifest.get("candidates") or []
    source_ids = {str(row.get("occurrence_id") or row.get("id") or "") for row in source_candidates}
    if set(directives) != source_ids:
        raise ValueError("review_candidate_coverage_mismatch")
    review_hash = canonical_hash(review)
    destination_id = f"{manifest['pack_id']}-rv-{review_hash}"
    destination = destination_root / destination_id
    if destination.exists():
        verify_pack_directory(destination, expected_pack_id=destination_id, require_audit=True)
        return destination
    destination_root.mkdir(parents=True, exist_ok=True)
    staging = destination_root / f".{destination_id}.staging-{uuid.uuid4().hex}"
    shutil.copytree(source, staging)
    try:
        kept = []
        hashes = manifest["hashes"]["files"]
        for candidate in source_candidates:
            occurrence_id = str(candidate.get("occurrence_id") or candidate.get("id") or "")
            directive = directives[occurrence_id]
            action = directive.get("action")
            if action == "drop":
                relative = str(candidate["phrase_audio"])
                (staging / relative).unlink()
                hashes.pop(relative, None)
                continue
            if action != "keep" or set(directive) != {"action", "gloss_zh", "phrase_zh"}:
                raise ValueError("review_directive_invalid")
            row = dict(candidate)
            row["gloss_zh"] = str(directive["gloss_zh"]).strip()
            row["phrase_zh"] = str(directive["phrase_zh"]).strip()
            if not row["gloss_zh"] or not row["phrase_zh"]:
                raise ValueError("review_translation_empty")
            kept.append(row)
        if not kept:
            raise ValueError("review_removed_all_candidates")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        manifest["pack_id"] = destination_id
        manifest["created_at"] = now
        manifest["candidates"] = kept
        manifest["review_status"] = "agent_reviewed"
        manifest["review_provenance"] = {
            "review_version": str(review.get("review_version") or ""),
            "review_hash": review_hash,
            "reviewed_at": now,
            "reviewed_from_pack_id": review["source_pack_id"],
            "scope": "candidate_keep_drop_and_chinese_fields_only",
        }
        manifest.setdefault("quality_gates", {})["agent_candidate_review"] = str(review.get("review_version") or "")
        manifest["unverified_boundaries"] = [
            value
            for value in manifest.get("unverified_boundaries", [])
            if value not in {
                "translation_naturalness_requires_human_review",
                "candidate_pedagogical_value_requires_human_review",
                "heuristic_candidate_selection_used_due_to_primary_translator_failure",
            }
        ]
        manifest["unverified_boundaries"].append("agent_review_is_not_user_learning_validation")
        write_json(staging / "manifest.json", manifest)

        audit_path = staging / "audit.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audit["pack_id"] = destination_id
        audit["candidate_count"] = len(kept)
        audit["created_at"] = now
        audit.setdefault("checks", []).append({"name": "agent_candidate_review", "passed": True})
        audit["review_provenance"] = manifest["review_provenance"]
        write_json(audit_path, audit)
        verify_pack_directory(staging, expected_pack_id=destination_id, require_audit=True)
        os.replace(staging, destination)
        verify_pack_directory(destination, expected_pack_id=destination_id, require_audit=True)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--destination-root", required=True)
    parser.add_argument("--review", required=True)
    args = parser.parse_args()
    source_root = Path(args.source_root).resolve()
    destination_root = Path(args.destination_root).resolve()
    document = json.loads(Path(args.review).read_text(encoding="utf-8"))
    outputs = []
    for review in document.get("packs", []):
        source = source_root / str(review["source_pack_id"])
        destination = apply_review(source, destination_root, {**review, "review_version": document.get("review_version")})
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        outputs.append({"pack_id": manifest["pack_id"], "title": manifest["title"], "candidate_count": len(manifest["candidates"]), "path": str(destination)})
    print(json.dumps({"ok": True, "review_version": document.get("review_version"), "packs": outputs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
