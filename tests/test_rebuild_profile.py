from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

from adaptive_core import REDUCER_VERSION, isoformat, rebuild_profile_from_events, utc_now

ROOT = Path(__file__).resolve().parents[1]
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LOCK_HOLDER = r"""
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
handle = path.open('a+b')
if handle.seek(0, os.SEEK_END) == 0:
    handle.write(b'0')
    handle.flush()
handle.seek(0)
if os.name == 'nt':
    import msvcrt
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print('ready', flush=True)
sys.stdin.read(1)
"""


class RebuildProfileLockTests(unittest.TestCase):
    def test_explicit_reducer_migration_backs_up_and_replays_before_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary)
            content = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))
            seed_path = ROOT / "seed-profile.json"
            seed = json.loads(seed_path.read_text(encoding="utf-8")) if seed_path.exists() else {"known_ids": []}
            item_id = content["items"][0]["id"]
            now = utc_now()
            events = [
                {"schema_version": 1, "event_id": "1" * 32, "type": "profile_created", "at": isoformat(now - timedelta(hours=1)), "session_id": None, "policy_version": "adaptive-v5", "data": {}},
                {"schema_version": 1, "event_id": "2" * 32, "type": "interaction_completed", "at": isoformat(now), "session_id": "a" * 32, "policy_version": "adaptive-v5", "data": {"item_id": item_id, "outcome": "completed", "dwell_ms": 6000, "phrase_confirmed": True, "replays": 0, "familiarity_feedback": None}},
            ]
            rebuilt = rebuild_profile_from_events(content, events, known_ids=seed.get("known_ids", []))
            stale = deepcopy(rebuilt)
            stale["reducer_version"] = "rules-v5"
            stale["items"][item_id]["next_window_start"] = "2099-01-01T00:00:00Z"
            (data_root / "events.jsonl").write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")
            event_bytes = (data_root / "events.jsonl").read_bytes()
            (data_root / "profile.json").write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(ROOT / "rebuild_profile.py"), "--data-dir", str(data_root), "--migrate-reducer"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["migrated_from"], "rules-v5")
            self.assertEqual(output["migrated_to"], REDUCER_VERSION)
            migrated = json.loads((data_root / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(migrated["reducer_version"], REDUCER_VERSION)
            self.assertEqual(migrated["items"][item_id]["next_window_start"], rebuilt["items"][item_id]["next_window_start"])
            self.assertEqual((data_root / "events.jsonl").read_bytes(), event_bytes)
            backups = list((data_root / "backups").glob("profile.rules-v5.pre-rules-v6.*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["items"][item_id]["next_window_start"], "2099-01-01T00:00:00Z")

    def test_write_refuses_while_server_data_lock_is_held(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "server.lock"
            holder = subprocess.Popen(
                [sys.executable, "-c", LOCK_HOLDER, str(lock_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                creationflags=CREATE_NO_WINDOW,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                result = subprocess.run(
                    [sys.executable, str(ROOT / "rebuild_profile.py"), "--data-dir", temporary, "--write"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=15,
                    creationflags=CREATE_NO_WINDOW,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("data_directory_in_use", result.stderr + result.stdout)
                self.assertFalse((Path(temporary) / "profile.json").exists())
                self.assertFalse((Path(temporary) / "events.jsonl").exists())
            finally:
                if holder.stdin:
                    holder.stdin.write("x")
                    holder.stdin.flush()
                    holder.stdin.close()
                holder.wait(timeout=5)
                if holder.stdout:
                    holder.stdout.close()
                if holder.stderr:
                    holder.stderr.close()


if __name__ == "__main__":
    unittest.main()
