from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

DEFAULT_PACKAGE_DIR = Path(os.environ.get("INFLOW_ARGOS_PACKAGE_DIR") or os.environ.get("ARGOS_PACKAGES_DIR") or (Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "InFlow-English" / "argos-packages"))
os.environ.setdefault("ARGOS_PACKAGES_DIR", str(DEFAULT_PACKAGE_DIR))
os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")
os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")

MAX_TEXTS = 256
MAX_TEXT_CHARS = 1500
MAX_TOTAL_CHARS = 80_000


def fail(code: str) -> None:
    print(json.dumps({"ok": False, "error_code": code}), file=sys.stderr)
    raise SystemExit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        fail("argos_input_json_invalid")
    if not isinstance(payload, dict) or set(payload) != {"texts"} or not isinstance(payload["texts"], list):
        fail("argos_input_schema_invalid")
    texts = payload["texts"]
    if not 1 <= len(texts) <= MAX_TEXTS:
        fail("argos_input_count_invalid")
    normalized: list[str] = []
    for value in texts:
        if not isinstance(value, str):
            fail("argos_input_text_invalid")
        text = value.strip()
        if not text or len(text) > MAX_TEXT_CHARS or any(ord(char) < 32 and char not in "\n\t" for char in text):
            fail("argos_input_text_invalid")
        normalized.append(text)
    if sum(map(len, normalized)) > MAX_TOTAL_CHARS:
        fail("argos_input_total_too_large")
    try:
        import argostranslate.package
        import argostranslate.translate

        installed = argostranslate.package.get_installed_packages()
        if not any(package.from_code == "en" and package.to_code == "zh" for package in installed):
            fail("argos_en_zh_model_missing")
        translations = [argostranslate.translate.translate(text, "en", "zh") for text in normalized]
    except SystemExit:
        raise
    except MemoryError:
        fail("argos_memory_unavailable")
    except Exception as exc:
        safe_type = re.sub(r"[^a-z0-9_]", "", type(exc).__name__.casefold())[:48] or "unknown"
        fail(f"argos_runtime_{safe_type}")
    if len(translations) != len(normalized) or any(not isinstance(value, str) or not value.strip() for value in translations):
        fail("argos_translation_output_invalid")
    print(json.dumps({"ok": True, "translations": translations}, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
