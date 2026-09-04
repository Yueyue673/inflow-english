from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from video_pack import VideoPackBuilder, VideoPackError, verify_pack_directory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and audit a sequential InFlow sample corpus.")
    parser.add_argument("--root", required=True, help="Pack output root (use a roomy QA drive).")
    parser.add_argument("--report", required=True, help="JSON report path.")
    parser.add_argument("--corpus", help="JSON corpus file containing samples[].url.")
    parser.add_argument("--url", action="append", default=[], dest="urls")
    args = parser.parse_args()
    if args.corpus:
        corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
        args.urls.extend(str(row["url"]) for row in corpus.get("samples", []) if isinstance(row, dict) and row.get("url"))
    args.urls = list(dict.fromkeys(args.urls))
    if not args.urls:
        parser.error("provide --corpus or at least one --url")
    return args


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    args = parse_args()
    root = Path(args.root).resolve()
    report_path = Path(args.report).resolve()
    root.mkdir(parents=True, exist_ok=True)
    builder = VideoPackBuilder(root)
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    print(json.dumps({"event": "corpus_started", "transcriber": builder.transcriber_identity, "count": len(args.urls)}, ensure_ascii=False), flush=True)

    for ordinal, url in enumerate(args.urls, start=1):
        sample_started = time.perf_counter()

        def progress(event: dict[str, Any]) -> None:
            public = dict(event)
            public.update(sample=ordinal, samples=len(args.urls), elapsed_sec=round(time.perf_counter() - sample_started, 2))
            print(json.dumps(public, ensure_ascii=False), flush=True)

        try:
            pack_path = builder.build(url, progress_callback=progress)
            manifest = verify_pack_directory(pack_path, require_audit=True)
            transcript = json.loads((pack_path / "transcript.json").read_text(encoding="utf-8"))
            captions = json.loads((pack_path / "captions-zh.json").read_text(encoding="utf-8"))
            candidates = manifest.get("candidates", [])
            row = {
                "url": url,
                "status": "ready",
                "pack_id": manifest["pack_id"],
                "title": manifest["title"],
                "duration_sec": manifest["duration_sec"],
                "effective_speech_sec": manifest.get("effective_speech_sec"),
                "candidate_count": len(candidates),
                "cue_count": len(transcript.get("cues", [])),
                "cue_source": transcript.get("cue_source"),
                "transcription_runtime": transcript.get("model"),
                "transcription_chain": transcript.get("model_chain"),
                "translation_runtime": manifest.get("translator"),
                "translation_chain": manifest.get("translator_chain"),
                "caption_source": captions.get("source"),
                "all_phrase_asr_exact": all(candidate.get("phrase_lexical_exact") is True for candidate in candidates),
                "elapsed_sec": round(time.perf_counter() - sample_started, 2),
                "pack_path": str(pack_path),
            }
        except VideoPackError as exc:
            row = {
                "url": url,
                "status": "rejected",
                "error_code": str(exc).split(":", 1)[0],
                "elapsed_sec": round(time.perf_counter() - sample_started, 2),
            }
        except Exception as exc:
            row = {
                "url": url,
                "status": "unexpected_failure",
                "error_type": type(exc).__name__,
                "elapsed_sec": round(time.perf_counter() - sample_started, 2),
            }
        rows.append(row)
        write_report(
            report_path,
            {
                "schema_version": 1,
                "root": str(root),
                "transcriber": builder.transcriber_identity,
                "elapsed_sec": round(time.perf_counter() - started, 2),
                "samples": rows,
            },
        )
        print(json.dumps({"event": "sample_finished", **row}, ensure_ascii=False), flush=True)

    result = {
        "schema_version": 1,
        "root": str(root),
        "transcriber": builder.transcriber_identity,
        "elapsed_sec": round(time.perf_counter() - started, 2),
        "ready": sum(row["status"] == "ready" for row in rows),
        "rejected": sum(row["status"] == "rejected" for row in rows),
        "unexpected_failure": sum(row["status"] == "unexpected_failure" for row in rows),
        "samples": rows,
    }
    write_report(report_path, result)
    print(json.dumps({"event": "corpus_finished", **result}, ensure_ascii=False), flush=True)
    if result["unexpected_failure"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
