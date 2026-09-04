from __future__ import annotations

import shutil
from pathlib import Path


def copy_extension_for_browser(source: Path, destination: Path, *, open_shadow_for_test: bool = False) -> None:
    shutil.copytree(source, destination)
    if not open_shadow_for_test:
        return
    script = destination / "content-script.js"
    content = script.read_text(encoding="utf-8")
    production = 'attachShadow({ mode: "closed" })'
    if content.count(production) != 1:
        raise RuntimeError("production_closed_shadow_marker_missing")
    script.write_text(content.replace(production, 'attachShadow({ mode: "open" })'), encoding="utf-8")
