from __future__ import annotations

import hashlib
from pathlib import Path


def snapshot_live_tree(root: Path) -> dict[str, tuple[int, str]]:
    snapshot: dict[str, tuple[int, str]] = {}
    if not root.is_dir():
        return snapshot
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "server.lock":
            continue
        try:
            payload = path.read_bytes()
        except (OSError, PermissionError):
            continue
        snapshot[path.relative_to(root).as_posix()] = (len(payload), hashlib.sha256(payload).hexdigest())
    return snapshot
