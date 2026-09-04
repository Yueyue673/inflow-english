from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import launcher


class LauncherTests(unittest.TestCase):
    def test_launcher_log_resets_after_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "launcher.log"
            path.write_bytes(b"x" * (launcher.MAX_LAUNCHER_LOG_BYTES + 1))
            stream = launcher.open_launcher_log(path)
            stream.write("safe-start\n")
            stream.close()
            content = path.read_text(encoding="utf-8")
            self.assertEqual(content, "[launcher log reset after size limit]\nsafe-start\n")
            self.assertLess(path.stat().st_size, 200)


if __name__ == "__main__":
    unittest.main()
