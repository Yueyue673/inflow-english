from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
ALLOWED = {
    "manifest.json",
    "content-script.js",
    "page-hook.js",
    "page-bridge.js",
    "service-worker.js",
    "popup.html",
    "popup.js",
    "popup.css",
    "icons/icon-16.png",
    "icons/icon-32.png",
    "icons/icon-48.png",
    "icons/icon-128.png",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(output_dir: Path, *, store_first_upload: bool = False) -> tuple[Path, str]:
    actual = {
        path.relative_to(EXTENSION).as_posix()
        for path in EXTENSION.rglob("*")
        if path.is_file() and path.name != "README.md"
    }
    unexpected = sorted(actual - ALLOWED)
    missing = sorted(ALLOWED - actual)
    if unexpected or missing:
        raise SystemExit(json.dumps({"unexpected": unexpected, "missing": missing}))
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    version = str(manifest.get("version") or "")
    if not version or not all(part.isdigit() for part in version.split(".")):
        raise SystemExit("invalid_manifest_version")
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-CWS-first-upload" if store_first_upload else ""
    archive = output_dir / f"InFlow-English-Chrome-{version}{suffix}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for relative in sorted(ALLOWED):
            if relative == "manifest.json" and store_first_upload:
                store_manifest = dict(manifest)
                store_manifest.pop("key", None)
                data = (json.dumps(store_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            else:
                data = (EXTENSION / relative).read_bytes()
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            target.writestr(info, data)
    with zipfile.ZipFile(archive) as source:
        names = set(source.namelist())
        packaged_manifest = json.loads(source.read("manifest.json"))
        key_ok = "key" not in packaged_manifest if store_first_upload else packaged_manifest.get("key") == manifest.get("key")
        if names != ALLOWED or source.testzip() is not None or not key_ok:
            raise SystemExit("archive_verification_failed")
    digest = sha256(archive)
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return archive, digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the allowlisted InFlow Chrome extension archive.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--store-first-upload", action="store_true", help="Strip manifest.key for a brand-new Chrome Web Store item.")
    args = parser.parse_args()
    archive, digest = build(args.output_dir.resolve(), store_first_upload=args.store_first_upload)
    print(json.dumps({"archive": str(archive), "sha256": digest, "files": len(ALLOWED), "store_first_upload": args.store_first_upload}, indent=2))


if __name__ == "__main__":
    main()
