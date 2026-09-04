from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adaptive_core import atomic_write_json
from lexical_sense import annotate_lexical_identity


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    path = ROOT / "content.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items"] = [annotate_lexical_identity(item) for item in payload.get("items", [])]
    counts = {"resolved": 0, "ambiguous": 0, "provisional": 0}
    for item in payload["items"]:
        status = str(item.get("sense_status") or "provisional")
        counts[status if status in counts else "provisional"] += 1
    output = {"items": len(payload["items"]), "counts": counts, "write_requested": args.write}
    if args.write:
        backup = ROOT / "data" / "backups" / "content-pre-lexical-v1.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
        atomic_write_json(path, payload)
        output.update(written=str(path), backup=str(backup))
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
