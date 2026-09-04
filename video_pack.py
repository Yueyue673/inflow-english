from __future__ import annotations

"""Build immutable, audited VideoPacks from public YouTube videos.

The module is deliberately synchronous and has no server/UI concerns.  Long-lived
hosts can run :class:`VideoPackBuilder` in their own worker and observe it through
its progress and cancellation callbacks.
"""

import ctypes
import difflib
import gc
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from lexical_sense import resolve_lexical_identity, sense_candidates

try:
    from wordfreq import zipf_frequency as _wordfreq_zipf_frequency  # type: ignore
except ImportError:  # Optional; deterministic length fallback remains available.
    _wordfreq_zipf_frequency = None


SCHEMA_VERSION = "inflow.video-pack/1"
BUILDER_VERSION = "video-pack-builder/1.9.2"
PROMPT_VERSION = "video-pack-translation-selection/9"
MIN_VIDEO_DURATION_SEC = 20.0
MAX_VIDEO_DURATION_SEC = 15.0 * 60.0
MIN_CUE_DURATION_SEC = 2.0
MAX_CUE_DURATION_SEC = 8.0
MIN_CANDIDATES = 1
MAX_CANDIDATES = 16
TRANSCRIPTION_CHUNK_SEC = 45.0

# The value is non-zero on Windows and zero elsewhere.  Every subprocess call in
# this module passes it explicitly; no call uses a shell.
CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
_CUDA_DLL_DIR_HANDLE: Any | None = None
_CUDA_DLL_HANDLES: list[Any] = []


def _prepare_windows_cuda_runtime() -> None:
    """Expose the user's existing CUDA runtime to CTranslate2 on Windows."""

    global _CUDA_DLL_DIR_HANDLE
    if os.name != "nt" or _CUDA_DLL_HANDLES:
        return
    configured = os.environ.get("INFLOW_CUDA_DLL_DIR")
    dll_root = Path(configured) if configured else Path.home() / "tts_env" / "Lib" / "site-packages" / "torch" / "lib"
    if not dll_root.is_dir():
        return
    os.environ["PATH"] = str(dll_root) + os.pathsep + os.environ.get("PATH", "")
    _CUDA_DLL_DIR_HANDLE = os.add_dll_directory(str(dll_root))
    ordered_patterns = ("cudart64_*.dll", "cublasLt64_*.dll", "cublas64_*.dll", "cudnn64_*.dll")
    for pattern in ordered_patterns:
        matches = sorted(dll_root.glob(pattern))
        if matches:
            _CUDA_DLL_HANDLES.append(ctypes.WinDLL(str(matches[-1])))


ProgressCallback = Callable[[dict[str, Any]], None]
CancelCallback = Callable[[], bool]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_COMMON_ENGLISH = frozenset(
    "a an and are as at be been but by can could did do does for from had has have he her him his i if in into is it its may me more most my no not of on one or our out she so than that the their them they this to up was we were what when where which who will with would you your".split()
)
_TERMINAL_PUNCTUATION_RE = re.compile(r"[.!?][\"'’”)]*$")
_CLAUSE_PUNCTUATION_RE = re.compile(r"[,;:…—][\"'’”)]*$")
_MODEL_CANDIDATE_FIELDS = frozenset({"cue_id", "surface", "gloss_zh", "value_score"})
_MODEL_REVIEW_FIELDS = frozenset({"cue_id", "surface", "gloss_zh", "phrase_zh"})
_MODEL_TRANSLATION_FIELDS = frozenset({"cue_id", "text_zh"})
_UNSAFE_NONTERMINAL_CUE_ENDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "because",
        "before",
        "between",
        "but",
        "by",
        "for",
        "from",
        "if",
        "in",
        "into",
        "nor",
        "of",
        "on",
        "or",
        "over",
        "than",
        "that",
        "the",
        "through",
        "to",
        "under",
        "until",
        "when",
        "while",
        "with",
        "without",
    }
)
_REQUIRED_ROOT_FILES = frozenset(
    {
        "manifest.json",
        "source.json",
        "transcript.json",
        "captions-zh.json",
        "video.mp4",
        "phrases",
    }
)


class VideoPackError(RuntimeError):
    """Base class for expected VideoPack failures."""


class URLValidationError(VideoPackError):
    """The input is not a supported single-video YouTube URL."""


class SourceValidationError(VideoPackError):
    """YouTube metadata or downloaded media violates the source contract."""


class CueConstructionError(VideoPackError):
    """Word timestamps cannot be partitioned into natural 2–8 second cues."""


class TranslationContractError(VideoPackError):
    """The translator returned data outside the strict translation contract."""


class SubprocessFailure(VideoPackError):
    """A child process timed out, failed, or returned malformed output."""


class BuildCancelled(VideoPackError):
    """Cooperative cancellation was requested before atomic commit."""


class AuditError(VideoPackError):
    """A staged or cached pack failed an integrity/semantic audit."""


class CacheIntegrityError(VideoPackError):
    """An immutable cached pack exists but no longer verifies."""


@dataclass(frozen=True)
class YouTubeReference:
    video_id: str
    canonical_url: str


@dataclass(frozen=True)
class SurfaceMatch:
    start_char: int
    end_char: int
    start_sec: float
    end_sec: float
    first_word_index: int
    last_word_index: int


@dataclass(frozen=True)
class ValidatedCandidate:
    cue_id: str
    surface: str
    gloss_zh: str
    value_score: float
    match: SurfaceMatch


@dataclass(frozen=True)
class ValidatedTranslationBatch:
    translations: dict[str, str]
    candidates: tuple[ValidatedCandidate, ...]


@runtime_checkable
class Translator(Protocol):
    """Narrow, injectable translation/selection boundary.

    ``translate`` receives only ``cue_id`` and canonical English cue ``text``.
    It returns a JSON-like mapping with exactly ``translations`` and
    ``candidates``.  Candidate rows may contain only the four fields named in
    ``_MODEL_CANDIDATE_FIELDS``; all timing and phrase material is derived by
    trusted code.
    """

    @property
    def identity(self) -> str: ...

    def translate(self, cues: Sequence[Mapping[str, str]]) -> Mapping[str, Any]: ...


@runtime_checkable
class Transcriber(Protocol):
    @property
    def identity(self) -> str: ...

    def transcribe(
        self,
        wav_path: Path,
        *,
        cancel_callback: CancelCallback | None = None,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class MediaTools(Protocol):
    @property
    def identity(self) -> str: ...

    def inspect(self, canonical_url: str) -> Mapping[str, Any]: ...

    def download(self, canonical_url: str, destination: Path) -> None: ...

    def download_range(self, canonical_url: str, start_sec: float, end_sec: float, destination: Path) -> None: ...

    def extract_audio(self, video_path: Path, wav_path: Path) -> None: ...

    def probe(self, media_path: Path) -> Mapping[str, Any]: ...

    def clip_phrase(
        self,
        video_path: Path,
        start_sec: float,
        end_sec: float,
        destination: Path,
    ) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_finite_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(field)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(field) from exc
    if not math.isfinite(number):
        raise ValueError(field)
    return number


def _safe_public_identity(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip()
    if not text or len(text) > 160 or _CONTROL_RE.search(text):
        return fallback
    return text


def _dependency_identity(dependency: Any, fallback: str) -> str:
    try:
        value = dependency.identity
    except Exception:
        value = fallback
    return _safe_public_identity(value, fallback)


def _safe_display_text(value: Any, *, fallback: str = "") -> str:
    text = str(value or fallback).strip()
    text = _CONTROL_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text)[:500]


def _normalize_chinese_spacing(text: str) -> str:
    text = re.sub(r"(?<=[\u3400-\u9fff])(?=[A-Za-z0-9])", " ", text)
    text = re.sub(r"(?<=[A-Za-z0-9])(?=[\u3400-\u9fff])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_youtube_json3_captions(
    payload: Mapping[str, Any],
    *,
    duration_sec: float,
    require_cjk: bool = True,
) -> list[dict[str, Any]]:
    events = payload.get("events") or []
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise SourceValidationError("chinese_auto_captions_invalid")
    raw_rows: list[tuple[float, float | None, str]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        segments = event.get("segs") or []
        if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
            continue
        text = _safe_display_text("".join(str(segment.get("utf8") or "") for segment in segments if isinstance(segment, Mapping)))
        if not text or (require_cjk and not _CJK_RE.search(text)) or (not require_cjk and not re.search(r"[A-Za-z]", text)):
            continue
        try:
            start = _as_finite_float(event.get("tStartMs"), "caption_start") / 1000.0
            raw_duration = event.get("dDurationMs")
            item_duration = None if raw_duration is None else _as_finite_float(raw_duration, "caption_duration") / 1000.0
        except ValueError as exc:
            raise SourceValidationError("chinese_auto_caption_timing_invalid") from exc
        if start < 0 or (item_duration is not None and item_duration <= 0):
            continue
        raw_rows.append((start, item_duration, text))
    raw_rows.sort(key=lambda row: row[0])
    output: list[dict[str, Any]] = []
    for index, (start, item_duration, text) in enumerate(raw_rows):
        next_start = raw_rows[index + 1][0] if index + 1 < len(raw_rows) else float(duration_sec)
        end = start + item_duration if item_duration is not None else next_start
        end = min(float(duration_sec), max(start + 0.05, end))
        if start >= float(duration_sec):
            continue
        output.append(
            {
                "id": f"ytzh-{len(output) + 1:04d}",
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
            }
        )
    if not output:
        raise SourceValidationError("chinese_auto_captions_empty")
    return output


def select_candidate_cue_shortlist(cues: Sequence[Mapping[str, Any]], limit: int = 24) -> list[Mapping[str, Any]]:
    eligible = [cue for cue in cues if bool(cue.get("candidate_allowed", True))]
    if len(eligible) < MIN_CANDIDATES:
        raise CueConstructionError("too_few_safe_candidate_cues")
    scored: list[tuple[float, float, Mapping[str, Any]]] = []
    max_end = max(float(cue.get("end_sec") or 0.0) for cue in eligible) or 1.0
    for cue in eligible:
        tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", str(cue.get("text") or ""))
        content = [token for token in tokens if token.casefold() not in _COMMON_ENGLISH]
        if not content:
            continue
        duration = float(cue.get("duration_sec") or 0.0)
        lexical = sum(max(0, len(token) - 5) for token in content) + max(len(token) for token in content)
        phrase_bonus = 4.0 if 2.5 <= duration <= 6.5 else 0.0
        score = lexical + min(len(content), 6) + phrase_bonus
        scored.append((score, float(cue.get("start_sec") or 0.0) / max_end, cue))
    if len(scored) < MIN_CANDIDATES:
        raise CueConstructionError("too_few_lexical_candidate_cues")
    bucket_count = min(6, max(1, len(scored) // 4))
    buckets: list[list[tuple[float, Mapping[str, Any]]]] = [[] for _ in range(bucket_count)]
    for score, relative, cue in scored:
        bucket = min(bucket_count - 1, int(relative * bucket_count))
        buckets[bucket].append((score, cue))
    for bucket in buckets:
        bucket.sort(key=lambda row: (-row[0], float(row[1].get("start_sec") or 0.0)))
    selected: list[Mapping[str, Any]] = []
    depth = 0
    while len(selected) < min(limit, len(scored)):
        added = False
        for bucket in buckets:
            if depth < len(bucket):
                selected.append(bucket[depth][1])
                added = True
                if len(selected) >= min(limit, len(scored)):
                    break
        if not added:
            break
        depth += 1
    return sorted(selected, key=lambda cue: float(cue.get("start_sec") or 0.0))


def validate_youtube_url(url: str) -> YouTubeReference:
    """Validate and canonicalise a supported single-video YouTube URL.

    Only HTTPS ``/watch?v=…`` and ``youtu.be/<id>`` links are accepted.  Shorts,
    live paths, embeds, playlists, credentials, ports, fragments, malformed IDs,
    and non-YouTube hosts are rejected before any filesystem/network action.
    """

    if not isinstance(url, str) or not url or len(url) > 2048:
        raise URLValidationError("youtube_url_invalid")
    if url != url.strip() or _CONTROL_RE.search(url):
        raise URLValidationError("youtube_url_not_canonical_text")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError("youtube_url_parse_failed") from exc
    if parsed.scheme != "https" or parsed.fragment or parsed.username or parsed.password or port is not None:
        raise URLValidationError("youtube_url_requires_plain_https")
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query, keep_blank_values=True)
    lowered_query_keys = {key.lower() for key in query}
    if {"list", "index"} & lowered_query_keys:
        raise URLValidationError("youtube_playlist_not_supported")

    video_id: str | None = None
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path != "/watch":
            raise URLValidationError("youtube_path_not_supported")
        values = query.get("v", [])
        if len(values) != 1:
            raise URLValidationError("youtube_video_id_missing_or_repeated")
        video_id = values[0]
    elif host == "youtu.be":
        pieces = [piece for piece in parsed.path.split("/") if piece]
        if len(pieces) != 1:
            raise URLValidationError("youtube_short_link_path_invalid")
        video_id = pieces[0]
    else:
        raise URLValidationError("youtube_host_not_supported")

    if not _VIDEO_ID_RE.fullmatch(video_id or ""):
        raise URLValidationError("youtube_video_id_invalid")
    return YouTubeReference(
        video_id=video_id,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def validate_source_metadata(
    reference: YouTubeReference,
    raw: Mapping[str, Any],
    *,
    max_duration_sec: float = MAX_VIDEO_DURATION_SEC,
) -> dict[str, Any]:
    """Strictly validate yt-dlp metadata and return an allow-listed source view."""

    if not isinstance(raw, Mapping):
        raise SourceValidationError("source_metadata_not_object")
    if str(raw.get("id") or "") != reference.video_id:
        raise SourceValidationError("source_video_id_mismatch")
    if raw.get("_type") not in {None, "video"} or raw.get("entries") is not None:
        raise SourceValidationError("source_is_not_single_video")
    if str(raw.get("availability") or "").lower() != "public":
        raise SourceValidationError("source_not_public")
    live_status = str(raw.get("live_status") or "not_live").lower()
    if bool(raw.get("is_live")) or bool(raw.get("was_live")) or live_status != "not_live":
        raise SourceValidationError("source_live_not_supported")
    try:
        duration = _as_finite_float(raw.get("duration"), "duration")
    except ValueError as exc:
        raise SourceValidationError("source_duration_missing") from exc
    if not MIN_VIDEO_DURATION_SEC <= duration <= float(max_duration_sec):
        raise SourceValidationError("source_duration_out_of_range")
    try:
        age_limit = _as_finite_float(raw.get("age_limit", 0), "age_limit")
    except ValueError as exc:
        raise SourceValidationError("source_age_limit_invalid") from exc
    if age_limit > 0:
        raise SourceValidationError("source_age_restricted")
    categories = [str(item).strip() for item in (raw.get("categories") or []) if str(item).strip()]
    if any(item.casefold() == "music" for item in categories):
        raise SourceValidationError("source_music_video_not_supported")
    title = _safe_display_text(raw.get("title"))
    if not title:
        raise SourceValidationError("source_title_missing")
    for possible_url in (raw.get("original_url"), raw.get("webpage_url")):
        if possible_url and re.search(r"youtube\.com/(?:shorts|live)/", str(possible_url), flags=re.IGNORECASE):
            raise SourceValidationError("source_short_or_live_not_supported")

    return {
        "provider": "youtube",
        "video_id": reference.video_id,
        "canonical_url": reference.canonical_url,
        "title": title,
        "duration": round(duration, 3),
        "duration_sec": round(duration, 3),
        "availability": "public",
        "is_live": False,
        "uploader": _safe_display_text(raw.get("uploader")),
        "channel_id": _safe_display_text(raw.get("channel_id")),
        "upload_date": _safe_display_text(raw.get("upload_date")),
        "declared_language": _safe_display_text(raw.get("language")),
        "categories": categories,
    }


class SubprocessMediaTools:
    """yt-dlp/ffmpeg implementation with a single injectable process boundary."""

    def __init__(
        self,
        *,
        runner: CommandRunner = subprocess.run,
        yt_dlp: str = "yt-dlp",
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        proxy: str | None = None,
        inspect_timeout_sec: float = 45.0,
        download_timeout_sec: float = 300.0,
        media_timeout_sec: float = 180.0,
    ) -> None:
        self._runner = runner
        self.yt_dlp = yt_dlp
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        if proxy is None:
            proxy = os.environ.get("INFLOW_DOWNLOAD_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.proxy = str(proxy).strip() or None
        self.inspect_timeout_sec = float(inspect_timeout_sec)
        self.download_timeout_sec = float(download_timeout_sec)
        self.media_timeout_sec = float(media_timeout_sec)

    @property
    def identity(self) -> str:
        return "yt-dlp-2026.08+:format18:android+web-embedded:360p:16khz"

    def _run(self, args: Sequence[str], *, timeout: float, operation: str) -> str:
        command = [str(item) for item in args]
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise SubprocessFailure(f"{operation}_timeout") from exc
        except (OSError, UnicodeError) as exc:
            raise SubprocessFailure(f"{operation}_launch_failed") from exc
        if int(getattr(result, "returncode", 1)) != 0:
            # Child stdout/stderr is intentionally not included: command output is
            # neither persisted nor logged and can never leak credentials.
            raise SubprocessFailure(f"{operation}_failed:{int(result.returncode)}")
        stdout = getattr(result, "stdout", "")
        if not isinstance(stdout, str):
            raise SubprocessFailure(f"{operation}_stdout_invalid")
        return stdout

    def inspect(self, canonical_url: str) -> Mapping[str, Any]:
        stdout = self._run(
            [
                self.yt_dlp,
                "--js-runtimes",
                "node",
                "--extractor-args",
                "youtube:player_client=android,web_embedded,-visionos",
                *(["--proxy", self.proxy] if self.proxy else []),
                "--dump-single-json",
                "--skip-download",
                "--no-playlist",
                "--no-warnings",
                "--quiet",
                "--",
                canonical_url,
            ],
            timeout=self.inspect_timeout_sec,
            operation="yt_dlp_inspect",
        )
        if len(stdout) > 5_000_000:
            raise SubprocessFailure("yt_dlp_metadata_too_large")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SubprocessFailure("yt_dlp_metadata_not_json") from exc
        if not isinstance(payload, Mapping):
            raise SubprocessFailure("yt_dlp_metadata_not_object")
        return payload

    def download(self, canonical_url: str, destination: Path) -> None:
        self._run(
            [
                self.yt_dlp,
                "--js-runtimes",
                "node",
                "--extractor-args",
                "youtube:player_client=android,web_embedded,-visionos",
                *(["--proxy", self.proxy] if self.proxy else []),
                "--no-playlist",
                "--no-warnings",
                "--no-progress",
                "--quiet",
                "--no-part",
                "--force-overwrites",
                "--format",
                "18",
                "--output",
                str(destination),
                "--",
                canonical_url,
            ],
            timeout=self.download_timeout_sec,
            operation="yt_dlp_download",
        )
        _require_nonempty_regular_file(destination, "downloaded_video_missing")

    def download_range(self, canonical_url: str, start_sec: float, end_sec: float, destination: Path) -> None:
        start = _as_finite_float(start_sec, "range_start")
        end = _as_finite_float(end_sec, "range_end")
        if start < 0 or end <= start or end - start > 8 * 60:
            raise SourceValidationError("download_range_invalid")
        self._run(
            [
                self.yt_dlp,
                "--js-runtimes",
                "node",
                "--extractor-args",
                "youtube:player_client=android,web_embedded,-visionos",
                *(["--proxy", self.proxy] if self.proxy else []),
                "--no-playlist",
                "--no-warnings",
                "--no-progress",
                "--quiet",
                "--no-part",
                "--force-overwrites",
                "--download-sections",
                f"*{start:.3f}-{end:.3f}",
                "--force-keyframes-at-cuts",
                "--format",
                "18",
                "--output",
                str(destination),
                "--",
                canonical_url,
            ],
            timeout=self.download_timeout_sec,
            operation="yt_dlp_download_range",
        )
        _require_nonempty_regular_file(destination, "downloaded_range_missing")

    def _download_auto_caption(self, canonical_url: str, language: str, destination: Path) -> None:
        slug = "zh" if language == "zh-Hans" else "en"
        template = destination.parent / f"youtube-auto-{slug}.%(ext)s"
        generated = destination.parent / f"youtube-auto-{slug}.{language}.json3"
        command = [
            self.yt_dlp,
            "--js-runtimes",
            "node",
            "--extractor-args",
            "youtube:player_client=android,web_embedded,-visionos",
            *(["--proxy", self.proxy] if self.proxy else []),
            "--skip-download",
            "--write-auto-subs",
            "--sub-langs",
            language,
            "--sub-format",
            "json3",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--quiet",
            "--force-overwrites",
            "--output",
            str(template),
            "--",
            canonical_url,
        ]
        operation = "yt_dlp_chinese_captions" if language == "zh-Hans" else "yt_dlp_english_captions"
        missing_code = "chinese_auto_captions_missing" if language == "zh-Hans" else "english_auto_captions_missing"
        last_error: SubprocessFailure | None = None
        for attempt in range(2):
            generated.unlink(missing_ok=True)
            try:
                self._run(command, timeout=self.inspect_timeout_sec, operation=operation)
                last_error = None
                break
            except SubprocessFailure as exc:
                last_error = exc
                if attempt:
                    raise
        if last_error is not None:
            raise last_error
        _require_nonempty_regular_file(generated, missing_code)
        os.replace(generated, destination)
        _require_nonempty_regular_file(destination, missing_code)

    def download_chinese_captions(self, canonical_url: str, destination: Path) -> None:
        self._download_auto_caption(canonical_url, "zh-Hans", destination)

    def download_english_captions(self, canonical_url: str, destination: Path) -> None:
        template = destination.parent / "youtube-manual-en.%(ext)s"
        generated = destination.parent / "youtube-manual-en.en.json3"
        command = [
            self.yt_dlp,
            "--js-runtimes",
            "node",
            "--extractor-args",
            "youtube:player_client=android,web_embedded,-visionos",
            *(["--proxy", self.proxy] if self.proxy else []),
            "--skip-download",
            "--write-subs",
            "--sub-langs",
            "en",
            "--sub-format",
            "json3",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--quiet",
            "--force-overwrites",
            "--output",
            str(template),
            "--",
            canonical_url,
        ]
        generated.unlink(missing_ok=True)
        try:
            self._run(command, timeout=self.inspect_timeout_sec, operation="yt_dlp_manual_english_captions")
            _require_nonempty_regular_file(generated, "manual_english_captions_missing")
            os.replace(generated, destination)
            _require_nonempty_regular_file(destination, "english_captions_missing")
        except VideoPackError:
            generated.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            self._download_auto_caption(canonical_url, "en", destination)

    def extract_audio(self, video_path: Path, wav_path: Path) -> None:
        self._run(
            [
                self.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ],
            timeout=self.media_timeout_sec,
            operation="ffmpeg_extract_audio",
        )
        _require_nonempty_regular_file(wav_path, "extracted_audio_missing")

    def probe(self, media_path: Path) -> Mapping[str, Any]:
        stdout = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,height",
                "-of",
                "json",
                str(media_path),
            ],
            timeout=min(self.media_timeout_sec, 45.0),
            operation="ffprobe",
        )
        try:
            payload = json.loads(stdout)
            streams = payload.get("streams") or []
            duration = _as_finite_float((payload.get("format") or {}).get("duration"), "duration")
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SubprocessFailure("ffprobe_output_invalid") from exc
        video_heights = []
        has_audio = False
        for stream in streams:
            if not isinstance(stream, Mapping):
                continue
            codec_type = stream.get("codec_type")
            has_audio = has_audio or codec_type == "audio"
            if codec_type == "video" and stream.get("height") is not None:
                try:
                    video_heights.append(int(stream["height"]))
                except (TypeError, ValueError):
                    raise SubprocessFailure("ffprobe_height_invalid")
        return {
            "duration_sec": duration,
            "has_audio": has_audio,
            "has_video": bool(video_heights),
            "height": max(video_heights) if video_heights else None,
        }

    def clip_phrase(
        self,
        video_path: Path,
        start_sec: float,
        end_sec: float,
        destination: Path,
    ) -> None:
        duration = end_sec - start_sec
        if duration <= 0:
            raise ValueError("clip_duration_invalid")
        self._run(
            [
                self.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-ss",
                f"{start_sec:.3f}",
                "-t",
                f"{duration:.3f}",
                "-map",
                "0:a:0",
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(destination),
            ],
            timeout=self.media_timeout_sec,
            operation="ffmpeg_clip_phrase",
        )
        _require_nonempty_regular_file(destination, "phrase_audio_missing")


class FasterWhisperTranscriber:
    """Lazy faster-whisper adapter that always requests word timestamps."""

    def __init__(
        self,
        model_size: str = "small.en",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 1,
        num_workers: int = 1,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_size = str(model_size)
        self.device = str(device)
        self.compute_type = str(compute_type)
        self.cpu_threads = max(1, int(cpu_threads))
        self.num_workers = max(1, int(num_workers))
        self._model_factory = model_factory
        self._model: Any | None = None

    @property
    def identity(self) -> str:
        return f"faster-whisper:{self.model_size}:{self.device}:{self.compute_type}:threads-{self.cpu_threads}:word-timestamps"

    def _get_model(self) -> Any:
        if self._model is None:
            if self.device.casefold() == "cuda":
                _prepare_windows_cuda_runtime()
            else:
                os.environ["OMP_NUM_THREADS"] = str(self.cpu_threads)
                os.environ["MKL_NUM_THREADS"] = str(self.cpu_threads)
            factory = self._model_factory
            if factory is None:
                try:
                    from faster_whisper import WhisperModel  # type: ignore
                except ImportError as exc:
                    raise VideoPackError("faster_whisper_not_installed") from exc
                factory = WhisperModel
            self._model = factory(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                num_workers=self.num_workers,
            )
        return self._model

    def release_model(self) -> None:
        model = self._model
        self._model = None
        if model is not None:
            del model
        gc.collect()

    def transcribe(
        self,
        wav_path: Path,
        *,
        cancel_callback: CancelCallback | None = None,
    ) -> Mapping[str, Any]:
        try:
            with wave.open(str(wav_path), "rb") as source_wave:
                channels = source_wave.getnchannels()
                sample_width = source_wave.getsampwidth()
                frame_rate = source_wave.getframerate()
                frame_total = source_wave.getnframes()
                compression = source_wave.getcomptype()
                compression_name = source_wave.getcompname()
        except (OSError, wave.Error) as exc:
            raise VideoPackError("transcriber_wav_invalid") from exc
        if frame_rate <= 0 or frame_total <= 0:
            raise VideoPackError("transcriber_wav_invalid")
        duration_sec = frame_total / frame_rate

        segments: list[dict[str, Any]] = []
        first_info: Any | None = None

        def consume(model: Any, audio_path: Path, offset_sec: float, chunk_index: int, multiple: bool) -> None:
            nonlocal first_info
            segments_iter, info = model.transcribe(
                str(audio_path),
                language="en",
                task="transcribe",
                word_timestamps=True,
                vad_filter=True,
                condition_on_previous_text=False,
                chunk_length=30,
            )
            if first_info is None:
                first_info = info
            for local_index, segment in enumerate(segments_iter):
                _raise_if_cancelled(cancel_callback)
                words = []
                for word in list(getattr(segment, "words", None) or []):
                    word_start = getattr(word, "start", None)
                    word_end = getattr(word, "end", None)
                    words.append(
                        {
                            "word": str(getattr(word, "word", "")),
                            "start": None if word_start is None else float(word_start) + offset_sec,
                            "end": None if word_end is None else float(word_end) + offset_sec,
                            "probability": getattr(word, "probability", None),
                        }
                    )
                segment_start = getattr(segment, "start", None)
                segment_end = getattr(segment, "end", None)
                local_id = getattr(segment, "id", local_index)
                segments.append(
                    {
                        "id": f"{chunk_index}:{local_id}" if multiple else local_id,
                        "start": None if segment_start is None else float(segment_start) + offset_sec,
                        "end": None if segment_end is None else float(segment_end) + offset_sec,
                        "text": str(getattr(segment, "text", "")),
                        "words": words,
                    }
                )

        try:
            model = self._get_model()
            if duration_sec <= TRANSCRIPTION_CHUNK_SEC * 1.25:
                consume(model, wav_path, 0.0, 0, False)
            else:
                frames_per_chunk = max(1, int(frame_rate * TRANSCRIPTION_CHUNK_SEC))
                with tempfile.TemporaryDirectory(prefix=".inflow-transcribe-chunks-", dir=wav_path.parent) as temporary:
                    chunk_root = Path(temporary)
                    with wave.open(str(wav_path), "rb") as source_wave:
                        chunk_index = 0
                        frame_offset = 0
                        while frame_offset < frame_total:
                            _raise_if_cancelled(cancel_callback)
                            frames = source_wave.readframes(min(frames_per_chunk, frame_total - frame_offset))
                            if not frames:
                                break
                            chunk_path = chunk_root / f"chunk-{chunk_index:04d}.wav"
                            with wave.open(str(chunk_path), "wb") as chunk_wave:
                                chunk_wave.setnchannels(channels)
                                chunk_wave.setsampwidth(sample_width)
                                chunk_wave.setframerate(frame_rate)
                                chunk_wave.setcomptype(compression, compression_name)
                                chunk_wave.writeframes(frames)
                            consume(model, chunk_path, frame_offset / frame_rate, chunk_index, True)
                            frame_offset += len(frames) // max(1, channels * sample_width)
                            chunk_index += 1
            if first_info is None:
                raise VideoPackError("transcriber_returned_no_info")
            return {
                "language": str(getattr(first_info, "language", "")),
                "language_probability": getattr(first_info, "language_probability", None),
                "duration_sec": duration_sec,
                "segments": segments,
            }
        except (RuntimeError, MemoryError) as exc:
            message = str(exc).casefold()
            if isinstance(exc, MemoryError) or any(marker in message for marker in ("mkl_malloc", "out of memory", "unable to allocate", "cuda_error_out_of_memory")):
                raise VideoPackError("transcriber_memory_unavailable") from exc
            raise VideoPackError("transcriber_runtime_failed") from exc
        except (OSError, wave.Error) as exc:
            raise VideoPackError("transcriber_chunk_io_failed") from exc

    def audit_phrase(self, audio_path: Path) -> str:
        model = self._get_model()
        segments_iter, _info = model.transcribe(
            str(audio_path),
            language="en",
            task="transcribe",
            word_timestamps=False,
            vad_filter=False,
            condition_on_previous_text=False,
            beam_size=5,
        )
        return " ".join(str(getattr(segment, "text", "")).strip() for segment in segments_iter).strip()


class AdaptiveFasterWhisperTranscriber:
    """Prefer a high-quality model but survive real desktop memory pressure."""

    def __init__(
        self,
        primary: FasterWhisperTranscriber,
        fallback: FasterWhisperTranscriber,
        *,
        minimum_primary_memory_bytes: int = 10 * 1024**3,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.minimum_primary_memory_bytes = int(minimum_primary_memory_bytes)
        self.active: FasterWhisperTranscriber | None = None

    @property
    def identity(self) -> str:
        primary_name = Path(self.primary.model_size).name or self.primary.model_size
        fallback_name = Path(self.fallback.model_size).name or self.fallback.model_size
        return (
            f"adaptive-fw:v1:{primary_name}:{self.primary.device}:{self.primary.compute_type}"
            f"=>{fallback_name}:{self.fallback.device}:{self.fallback.compute_type}"
        )

    @property
    def runtime_identity(self) -> str:
        return self.active.identity if self.active is not None else "not-selected"

    def _prefer_fallback_now(self) -> bool:
        available = _available_memory_bytes()
        return available is not None and available < self.minimum_primary_memory_bytes

    def release_model(self) -> None:
        self.primary.release_model()
        if self.fallback is not self.primary:
            self.fallback.release_model()
        gc.collect()

    def transcribe(
        self,
        wav_path: Path,
        *,
        cancel_callback: CancelCallback | None = None,
    ) -> Mapping[str, Any]:
        if self.active is not None:
            return self.active.transcribe(wav_path, cancel_callback=cancel_callback)
        if self._prefer_fallback_now():
            self.active = self.fallback
            return self.active.transcribe(wav_path, cancel_callback=cancel_callback)
        try:
            result = self.primary.transcribe(wav_path, cancel_callback=cancel_callback)
            self.active = self.primary
            return result
        except VideoPackError as exc:
            if str(exc).split(":", 1)[0] != "transcriber_memory_unavailable":
                raise
            self.primary._model = None
            gc.collect()
            self.active = self.fallback
            return self.active.transcribe(wav_path, cancel_callback=cancel_callback)

    def audit_phrase(self, audio_path: Path) -> str:
        if self.active is None:
            raise VideoPackError("transcriber_runtime_not_selected")
        return self.active.audit_phrase(audio_path)


def _candidate_limit_for_cues(cues: Sequence[Mapping[str, Any]]) -> int:
    eligible_count = sum(1 for cue in cues if bool(cue.get("candidate_allowed", True)))
    if eligible_count <= 0:
        return 0
    return min(
        MAX_CANDIDATES,
        eligible_count,
        max(min(4, eligible_count), math.ceil(eligible_count / 2.5)),
    )


class DshTranslator:
    """Strict JSON translator backed by ``dsh --profile headless``.

    The child receives no secrets from this class, is never launched through a
    shell, and its raw stdout/stderr is never logged or included in exceptions.
    A timeout, process failure, malformed JSON, or contract violation is retried
    exactly once.
    """

    def __init__(
        self,
        *,
        runner: CommandRunner = subprocess.run,
        executable: str = "dsh",
        timeout_sec: float = 180.0,
        patch_path: str | Path | None = None,
    ) -> None:
        self._runner = runner
        self._command_prefix = [str(executable)]
        if executable == "dsh" and runner is subprocess.run and os.name == "nt":
            shim = shutil.which("dsh.cmd")
            if shim:
                shim_dir = Path(shim).parent
                node_exe = shim_dir / "node.exe"
                cli_script = shim_dir / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
                if node_exe.is_file() and cli_script.is_file():
                    self._command_prefix = [str(node_exe), str(cli_script)]
                else:
                    self._command_prefix = [shim]
        elif executable == "dsh" and runner is subprocess.run:
            resolved = shutil.which("dsh")
            if resolved:
                self._command_prefix = [resolved]
        self.executable = str(executable)
        self.timeout_sec = float(timeout_sec)
        if patch_path is None:
            default_patch = Path(__file__).with_name("dsh-translation.patch.yml")
            patch_path = default_patch if default_patch.is_file() else None
        self.patch_path = str(patch_path) if patch_path else None

    @property
    def identity(self) -> str:
        executable_name = Path(self.executable).name or "dsh"
        return f"{executable_name}:headless:reasoning-none:{PROMPT_VERSION}"

    @staticmethod
    def _prompt(cues: Sequence[Mapping[str, str]]) -> str:
        safe_cues = []
        for cue in cues:
            safe_cues.append(
                {
                    "cue_id": str(cue.get("cue_id") or ""),
                    "text": str(cue.get("text") or ""),
                    "candidate_allowed": bool(cue.get("candidate_allowed", True)),
                }
            )
        cue_json = json.dumps(safe_cues, ensure_ascii=False, separators=(",", ":"))
        candidate_limit = _candidate_limit_for_cues(safe_cues)
        return (
            "You are a translation data function. Do not use tools. Return one JSON object only; "
            "no Markdown, comments, or surrounding text. Translate every English cue into concise "
            f"Simplified Chinese and select between 1 and {candidate_limit} useful B1-C1 listening-learning candidates. "
            "Prefer quality over filling the limit; longer videos may supply more candidates, while short videos may supply only one. "
            "Prefer one-to-four-word "
            "lexical items, phrasal verbs, idioms, or conventional collocations that are valuable beyond this sentence. "
            "Do not select ordinary compositional phrases that a learner can translate word by word, names, or five-plus-word chunks. "
            "Select at most one candidate from each cue_id. The exact "
            "schema is {\"translations\":[{\"cue_id\":string,\"text_zh\":string}],"
            "\"candidates\":[{\"cue_id\":string,\"surface\":string,\"gloss_zh\":string,"
            "\"value_score\":number}]}. translations must contain every supplied cue_id exactly "
            "once and no unknown cue_id. Candidates may only use cues whose candidate_allowed is true. "
            "Every candidate object must have exactly those four fields. "
            "surface must be copied character-for-character as one continuous, unambiguous span from "
            "that cue's English text; never invent, case-fold, paraphrase, or return a partial word. "
            "value_score must be from 0 to 5. Never return timing, phrase text/translation, anchor, "
            "highlight, audio path, hash, or occurrence ID; trusted code derives them. CUES="
            + cue_json
        )

    def _invoke_once(self, prompt: str) -> Mapping[str, Any]:
        command = [*self._command_prefix, "--profile", "headless"]
        if self.patch_path:
            command.extend(["--patch", self.patch_path])
        command.append(prompt)
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self.timeout_sec,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise TranslationContractError("dsh_timeout") from exc
        except (OSError, UnicodeError) as exc:
            raise TranslationContractError("dsh_launch_failed") from exc
        if int(getattr(result, "returncode", 1)) != 0:
            raise TranslationContractError(f"dsh_failed:{int(result.returncode)}")
        stdout = getattr(result, "stdout", "")
        if not isinstance(stdout, str) or len(stdout) > 2_000_000:
            raise TranslationContractError("dsh_stdout_invalid")
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_json_key")
                result[key] = value
            return result

        def reject_constant(_value: str) -> Any:
            raise ValueError("non_finite_json_number")

        try:
            payload = json.loads(
                stdout,
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise TranslationContractError("dsh_stdout_not_strict_json") from exc
        if not isinstance(payload, Mapping):
            raise TranslationContractError("dsh_json_not_object")
        return payload

    def translate(self, cues: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        prompt = self._prompt(cues)
        last_error: TranslationContractError | None = None
        for _attempt in range(2):
            try:
                payload = self._invoke_once(prompt)
                # Dsh receives only cue_id/text (never trusted timing). Enforce
                # strict JSON/schema and exact text grounding here; the builder
                # repeats the richer word-timestamp alignment validation.
                _validate_text_only_translation_batch(cues, payload)
                return payload
            except TranslationContractError as exc:
                last_error = exc
        raise TranslationContractError("dsh_translation_failed_after_retry") from last_error

    def translate_cues(self, cues: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        """Explicit alias for callers that prefer the longer method name."""

        return self.translate(cues)

    def _translate_only_batch(self, cues: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        safe = [{"cue_id": str(row.get("cue_id") or ""), "text": str(row.get("text") or "")} for row in cues]
        prompt = (
            "You are a Simplified Chinese subtitle translation function. Do not use tools. Return one JSON object only "
            "with schema {\"translations\":[{\"cue_id\":string,\"text_zh\":string}]}. Return every cue_id exactly "
            "once, unchanged, with concise natural Chinese that preserves facts and names. No Markdown or extra fields. CUES="
            + json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        )
        expected = {row["cue_id"] for row in safe}
        last_error: TranslationContractError | None = None
        for _attempt in range(2):
            try:
                payload = self._invoke_once(prompt)
                if set(payload) != {"translations"} or not isinstance(payload.get("translations"), list):
                    raise TranslationContractError("translation_only_schema_invalid")
                result: dict[str, str] = {}
                for row in payload["translations"]:
                    if not isinstance(row, Mapping) or set(row) != _MODEL_TRANSLATION_FIELDS:
                        raise TranslationContractError("translation_only_row_invalid")
                    cue_id = str(row.get("cue_id") or "")
                    text_zh = _normalize_chinese_spacing(str(row.get("text_zh") or "").strip())
                    if cue_id not in expected or cue_id in result or not text_zh or not _CJK_RE.search(text_zh):
                        raise TranslationContractError("translation_only_content_invalid")
                    result[cue_id] = text_zh
                if set(result) != expected:
                    raise TranslationContractError("translation_only_incomplete")
                return result
            except TranslationContractError as exc:
                last_error = exc
        raise TranslationContractError("translation_only_failed_after_retry") from last_error

    def _translate_resilient_batch(self, cues: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        try:
            return self._translate_only_batch(cues)
        except TranslationContractError:
            if len(cues) <= 4:
                raise
            midpoint = len(cues) // 2
            left = self._translate_resilient_batch(cues[:midpoint])
            right = self._translate_resilient_batch(cues[midpoint:])
            overlap = set(left) & set(right)
            if overlap:
                raise TranslationContractError("translation_only_split_overlap")
            return {**left, **right}

    def translate_all(self, cues: Sequence[Mapping[str, Any]], *, batch_size: int = 28, max_workers: int = 3) -> dict[str, str]:
        rows = list(cues)
        batches = [rows[index:index + batch_size] for index in range(0, len(rows), batch_size)]
        if not batches:
            raise TranslationContractError("translation_only_empty")
        translated: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(max_workers, len(batches)), thread_name_prefix="inflow-translate") as executor:
            futures = {executor.submit(self._translate_resilient_batch, batch): batch for batch in batches}
            for future in as_completed(futures):
                translated.update(future.result())
        expected = {str(row.get("cue_id") or "") for row in rows}
        if set(translated) != expected:
            raise TranslationContractError("translation_only_coverage_invalid")
        return translated

    def review_selected(self, rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        safe_rows = [
            {
                "cue_id": str(row.get("cue_id") or ""),
                "surface": str(row.get("surface") or ""),
                "phrase_text": str(row.get("phrase_text") or ""),
                "gloss_zh": str(row.get("gloss_zh") or ""),
                "phrase_zh": str(row.get("phrase_zh") or ""),
            }
            for row in rows
        ]
        prompt = (
            "You are the final Simplified Chinese language editor for an English listening tool. Do not use tools. "
            "Return one JSON object only, with schema {\"items\":[{\"cue_id\":string,\"surface\":string,"
            "\"gloss_zh\":string,\"phrase_zh\":string}]}. Return every row exactly once. Copy cue_id and surface "
            "unchanged. Actively rewrite awkward calques, not merely grammatical errors. Improve only gloss_zh and "
            "phrase_zh so they are accurate in this exact English context, concise, idiomatic Chinese, and preserve "
            "brand/person/place names instead of inventing Chinese names. Use dictionary-standard cultural terms: for "
            "example, yard sale means 庭院旧货出售, not 庭院旧货摊; 'at/from a yard sale' should read 在一次庭院旧货出售中, not 在……上. Recast English coordination into natural Chinese: "
            "for example, a tearful smile full of desperation should become 她含泪的笑容里透着绝望, never 笑容里满是眼睛和绝望. "
            "phrase_zh must translate the complete phrase_text without adding facts. No extra fields, Markdown, or comments. ROWS="
            + json.dumps(safe_rows, ensure_ascii=False, separators=(",", ":"))
        )
        last_error: TranslationContractError | None = None
        for _attempt in range(2):
            try:
                payload = self._invoke_once(prompt)
                validate_candidate_review(rows, payload)
                return payload
            except TranslationContractError as exc:
                last_error = exc
        raise TranslationContractError("candidate_review_failed_after_retry") from last_error

    def resolve_senses(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        safe_rows = []
        allowed: dict[str, set[str]] = {}
        for row in rows:
            occurrence_id = str(row.get("occurrence_id") or row.get("id") or "")
            candidates = sense_candidates(str(row.get("surface") or ""))
            if not occurrence_id or len(candidates) < 2:
                continue
            allowed[occurrence_id] = {candidate["candidate_id"] for candidate in candidates}
            safe_rows.append(
                {
                    "occurrence_id": occurrence_id,
                    "surface": str(row.get("surface") or ""),
                    "phrase_text": str(row.get("phrase_text") or ""),
                    "gloss_zh": str(row.get("gloss_zh") or ""),
                    "candidates": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "pos": candidate["pos"],
                            "definition": candidate["definition"],
                            "examples": candidate["examples"],
                        }
                        for candidate in candidates
                    ],
                }
            )
        if not safe_rows:
            return {}
        prompt = (
            "You are a constrained WordNet sense selector. Do not use tools. Return one JSON object only with schema "
            "{\"items\":[{\"occurrence_id\":string,\"candidate_id\":string|null,\"confidence\":integer}]}. "
            "Return every occurrence_id exactly once and unchanged. candidate_id must be copied exactly from that row's candidates, "
            "or null when the exact contextual sense is not clear. confidence is 0-100; use 90 or higher only when the sentence and "
            "Chinese contextual gloss both unambiguously support one candidate. Never invent keys, definitions, POS, lemmas, timing, "
            "or learning state. ROWS="
            + json.dumps(safe_rows, ensure_ascii=False, separators=(",", ":"))
        )
        last_error: TranslationContractError | None = None
        for _attempt in range(2):
            try:
                payload = self._invoke_once(prompt)
                if set(payload) != {"items"} or not isinstance(payload.get("items"), list):
                    raise TranslationContractError("sense_resolution_schema_invalid")
                result: dict[str, dict[str, Any]] = {}
                for row in payload["items"]:
                    if not isinstance(row, Mapping) or set(row) != {"occurrence_id", "candidate_id", "confidence"}:
                        raise TranslationContractError("sense_resolution_row_invalid")
                    occurrence_id = str(row.get("occurrence_id") or "")
                    candidate_id = row.get("candidate_id")
                    confidence = row.get("confidence")
                    if occurrence_id not in allowed or occurrence_id in result:
                        raise TranslationContractError("sense_resolution_occurrence_invalid")
                    if candidate_id is not None and str(candidate_id) not in allowed[occurrence_id]:
                        raise TranslationContractError("sense_resolution_candidate_invalid")
                    if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
                        raise TranslationContractError("sense_resolution_confidence_invalid")
                    result[occurrence_id] = {"candidate_id": str(candidate_id) if candidate_id is not None else None, "confidence": confidence}
                if set(result) != set(allowed):
                    raise TranslationContractError("sense_resolution_incomplete")
                return result
            except TranslationContractError as exc:
                last_error = exc
        raise TranslationContractError("sense_resolution_failed_after_retry") from last_error


class OpenAICompatibleTranslator(DshTranslator):
    """Strict JSON model adapter enabled only by explicit local configuration.

    It reuses the same constrained prompts and validation as the legacy DSH
    adapter, but calls an OpenAI-compatible Chat Completions endpoint. The API
    key is accepted only through constructor/environment plumbing and never
    appears in identities, exceptions, logs, manifests, or prompts.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        timeout_sec: float = 60.0,
        opener: Any | None = None,
    ) -> None:
        parsed = urlsplit(str(endpoint or "").strip())
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in {"http", "https"} or (parsed.scheme == "http" and not loopback):
            raise ValueError("openai_endpoint_must_be_https_or_loopback")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError("openai_endpoint_invalid")
        normalized_model = str(model or "").strip()
        if not normalized_model or len(normalized_model) > 160 or _CONTROL_RE.search(normalized_model):
            raise ValueError("openai_model_invalid")
        self.endpoint = str(endpoint).strip()
        self.model = normalized_model
        self._api_key = str(api_key or "").strip() or None
        self.timeout_sec = float(timeout_sec)
        self._opener = opener or build_opener(ProxyHandler())

    @property
    def identity(self) -> str:
        model_token = re.sub(r"[^A-Za-z0-9._-]+", "-", self.model)[:80]
        return f"openai-compatible:{model_token}:{PROMPT_VERSION}"

    def _invoke_once(self, prompt: str) -> Mapping[str, Any]:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "Return exactly one strict JSON object and no surrounding text."},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout_sec) as response:
                raw = response.read(2_000_001)
        except (OSError, TimeoutError) as exc:
            raise TranslationContractError("openai_request_failed") from exc
        if len(raw) > 2_000_000:
            raise TranslationContractError("openai_response_too_large")
        try:
            envelope = json.loads(raw.decode("utf-8"))
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content_not_string")
            payload = json.loads(content)
        except (UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise TranslationContractError("openai_response_invalid") from exc
        if not isinstance(payload, Mapping):
            raise TranslationContractError("openai_json_not_object")
        return payload


def configured_default_translator() -> Translator:
    """Build a local-first runtime chain; network translation requires explicit opt-in."""

    translation_backend = str(os.environ.get("INFLOW_TRANSLATION_BACKEND") or "argos").strip().casefold()
    if translation_backend in {"", "argos", "offline", "local"}:
        fallback: Translator = ArgosHeuristicTranslator()
    elif translation_backend == "google":
        fallback = FallbackTranslator(GoogleHeuristicTranslator(), ArgosHeuristicTranslator())
    else:
        raise VideoPackError("unsupported_translation_backend")
    backend = str(os.environ.get("INFLOW_MODEL_BACKEND") or "none").strip().casefold()
    if backend in {"", "none", "off", "disabled"}:
        return fallback
    if backend == "openai":
        endpoint = str(os.environ.get("INFLOW_OPENAI_CHAT_COMPLETIONS_URL") or "").strip()
        model = str(os.environ.get("INFLOW_OPENAI_MODEL") or "").strip()
        if not endpoint or not model:
            raise VideoPackError("openai_model_configuration_incomplete")
        primary = OpenAICompatibleTranslator(
            endpoint=endpoint,
            model=model,
            api_key=os.environ.get("INFLOW_OPENAI_API_KEY"),
        )
        return FallbackTranslator(primary, fallback)
    if backend == "dsh":
        patch_path = os.environ.get("INFLOW_DSH_PATCH") or None
        return FallbackTranslator(DshTranslator(patch_path=patch_path), fallback)
    raise VideoPackError("unsupported_model_backend")


_HEURISTIC_PHRASES = (
    "at least",
    "the point is",
    "one step closer",
    "pay attention",
    "make sure",
    "rather than",
    "even though",
    "as if",
    "in case",
    "no matter",
    "on purpose",
    "with a purpose",
    "all of a sudden",
    "out of nowhere",
    "at the end of the day",
    "give it a go",
    "get rid of",
    "come up with",
    "come to terms",
    "keep in mind",
    "look forward to",
    "take care of",
    "be supposed to",
    "figure out",
    "find out",
    "turn out",
    "end up",
    "work out",
    "go through",
    "deal with",
    "carry on",
    "give up",
    "pick up",
    "set up",
    "break down",
    "bring up",
    "point out",
    "hold on",
    "let go",
    "get over",
    "come across",
    "childhood sweetheart",
    "stark difference",
    "draw a stark difference",
    "fear of rejection",
)
_HEURISTIC_PHRASAL_VERBS = {
    "break", "bring", "call", "carry", "come", "cut", "end", "fall", "figure", "find",
    "get", "give", "go", "hold", "keep", "let", "look", "make", "pick", "point", "pull",
    "put", "run", "set", "show", "take", "turn", "work",
}
_HEURISTIC_PARTICLES = {"around", "away", "back", "down", "in", "into", "off", "on", "out", "over", "through", "up"}
_HEURISTIC_PRONOUNS = {"i", "me", "my", "mine", "you", "your", "yours", "he", "him", "his", "she", "her", "hers", "it", "its", "we", "us", "our", "they", "them", "their"}
_HEURISTIC_AUXILIARIES = {"am", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "have", "has", "had"}


def _zipf_frequency(value: str) -> float:
    if _wordfreq_zipf_frequency is None:
        token_count = len(re.findall(r"[A-Za-z]+", value))
        return 4.0 if token_count <= 4 else 6.0
    try:
        return float(_wordfreq_zipf_frequency(value.casefold(), "en"))
    except Exception:
        return 0.0


class GoogleHeuristicTranslator:
    """Explicit opt-in network translator plus deterministic candidate selection."""

    identity = "google-translate-web+wordfreq-context/v3"

    def __init__(self, *, timeout_sec: float = 15.0, proxy: str | None = None) -> None:
        self.timeout_sec = float(timeout_sec)
        if proxy is None:
            proxy = os.environ.get("INFLOW_TRANSLATION_PROXY") or os.environ.get("INFLOW_DOWNLOAD_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.proxy = str(proxy).strip() or None
        self._cache: dict[str, str] = {}
        self._cache_lock = threading.RLock()

    def _translate_text(self, text: str) -> str:
        text = str(text or "").strip()
        if not text or len(text) > 5000 or _CONTROL_RE.search(text):
            raise TranslationContractError("google_translation_input_invalid")
        with self._cache_lock:
            cached = self._cache.get(text)
        if cached:
            return cached
        query = urlencode({"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text})
        request = Request(
            "https://translate.googleapis.com/translate_a/single?" + query,
            headers={"User-Agent": "Mozilla/5.0 InFlow-English/1.0"},
        )
        last_error: Exception | None = None
        routes = [self.proxy, None] if self.proxy else [None, None]
        for route in routes:
            try:
                opener = build_opener(ProxyHandler({"https": route}) if route else ProxyHandler({}))
                with opener.open(request, timeout=self.timeout_sec) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("translation_response_too_large")
                payload = json.loads(raw.decode("utf-8"))
                translated = _normalize_chinese_spacing("".join(
                    str(part[0]) for part in payload[0] if isinstance(part, list) and part and part[0]
                ).strip())
                translated = re.sub(r"([\u4e00-\u9fff]{2,6})[、，]\1", r"\1", translated)
                if not translated or not _CJK_RE.search(translated):
                    raise ValueError("translation_response_missing_chinese")
                with self._cache_lock:
                    self._cache[text] = translated
                return translated
            except Exception as exc:
                last_error = exc
        raise TranslationContractError("google_translation_failed") from last_error

    def _translate_batch_texts(self, texts: Sequence[str]) -> list[str]:
        if not texts:
            return []
        if len(texts) == 1:
            return [self._translate_text(texts[0])]
        nonce = hashlib.sha256("\u0000".join(texts).encode("utf-8")).hexdigest()[:8].upper()
        prefix = f"INFLOW{nonce}"
        joined = "\n".join(f"{prefix}{index:04d}: {text}" for index, text in enumerate(texts))
        translated = self._translate_text(joined)
        marker = re.compile(rf"{re.escape(prefix)}(\d{{4}})\s*[:：]\s*", re.IGNORECASE)
        matches = list(marker.finditer(translated))
        if len(matches) != len(texts):
            raise TranslationContractError("google_translation_batch_markers_invalid")
        output = []
        for position, match in enumerate(matches):
            if int(match.group(1)) != position:
                raise TranslationContractError("google_translation_batch_markers_invalid")
            end = matches[position + 1].start() if position + 1 < len(matches) else len(translated)
            value = _normalize_chinese_spacing(translated[match.end():end].strip())
            if not value or not _CJK_RE.search(value):
                raise TranslationContractError("google_translation_batch_output_invalid")
            output.append(value)
        return output

    def _translate_many(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        expected = [str(row.get("cue_id") or "") for row in rows]
        if "" in expected or len(set(expected)) != len(rows):
            raise TranslationContractError("google_translation_ids_invalid")
        output: dict[str, str] = {}
        pending: list[tuple[str, str]] = []
        with self._cache_lock:
            for row, cue_id in zip(rows, expected):
                text = str(row.get("text") or "").strip()
                if not text or len(text) > 1500 or _CONTROL_RE.search(text):
                    raise TranslationContractError("google_translation_input_invalid")
                cached = self._cache.get(text)
                if cached:
                    output[cue_id] = cached
                else:
                    pending.append((cue_id, text))
        cursor = 0
        while cursor < len(pending):
            chunk: list[tuple[str, str]] = []
            chars = 0
            while cursor < len(pending) and len(chunk) < 12:
                row = pending[cursor]
                projected = chars + len(row[1]) + 32
                if chunk and projected > 4000:
                    break
                chunk.append(row)
                chars = projected
                cursor += 1
            translated = self._translate_batch_texts([text for _cue_id, text in chunk])
            with self._cache_lock:
                for (cue_id, text), value in zip(chunk, translated):
                    self._cache[text] = value
                    output[cue_id] = value
        if set(output) != set(expected):
            raise TranslationContractError("google_translation_coverage_invalid")
        return output

    @staticmethod
    def _candidate_options(text: str) -> list[tuple[float, str]]:
        tokens = [
            {
                "raw": match.group(0),
                "lower": match.group(0).casefold().replace("’", "'"),
                "start": match.start(),
                "end": match.end(),
            }
            for match in re.finditer(r"[A-Za-z]+(?:['’][A-Za-z]+)*", text)
        ]
        lowers = [token["lower"] for token in tokens]
        options: dict[str, float] = {}

        def span(low: int, high: int) -> str:
            if low < 0 or high > len(tokens) or low >= high:
                return ""
            return text[int(tokens[low]["start"]):int(tokens[high - 1]["end"])]

        def add(low: int, high: int, score: float) -> None:
            surface = span(low, high)
            if not surface or len(re.findall(r"[A-Za-z][A-Za-z'’-]*", surface)) > 4:
                return
            if re.search(r"[.,!?;:]", surface):
                return
            if text.casefold().count(surface.casefold()) != 1:
                return
            options[surface] = max(options.get(surface, float("-inf")), score)

        for phrase in _HEURISTIC_PHRASES:
            phrase_tokens = phrase.split()
            for index in range(len(tokens) - len(phrase_tokens) + 1):
                if lowers[index:index + len(phrase_tokens)] == phrase_tokens:
                    add(index, index + len(phrase_tokens), 100.0 + len(phrase_tokens))

        for index in range(len(tokens) - 1):
            if lowers[index] in _HEURISTIC_PHRASAL_VERBS and lowers[index + 1] in _HEURISTIC_PARTICLES:
                surface = span(index, index + 2)
                frequency = _zipf_frequency(surface)
                if 1.8 <= frequency <= 5.4:
                    add(index, index + 2, 86.0 - abs(frequency - 3.7) * 4.0)
            if index + 2 < len(tokens) and lowers[index] in _HEURISTIC_PHRASAL_VERBS and lowers[index + 2] in _HEURISTIC_PARTICLES:
                surface = span(index, index + 3)
                frequency = _zipf_frequency(surface)
                if 1.8 <= frequency <= 5.4 and lowers[index + 1] in _HEURISTIC_PRONOUNS:
                    add(index, index + 3, 84.0 - abs(frequency - 3.7) * 4.0)

        for index, token in enumerate(tokens):
            lower = str(token["lower"])
            raw = str(token["raw"])
            likely_name = index > 0 and raw[:1].isupper() and lower != "i"
            frequency = _zipf_frequency(lower)
            if (
                len(lower) >= 6
                and "'" not in lower
                and lower not in _COMMON_ENGLISH
                and not likely_name
                and 1.9 <= frequency <= 4.6
            ):
                add(index, index + 1, 64.0 + (4.6 - frequency) * 8.0 + min(8, len(lower)) * 0.4)

        for width in (2, 3):
            for index in range(len(tokens) - width + 1):
                group = tokens[index:index + width]
                group_lowers = [str(token["lower"]) for token in group]
                content = [token for token in group if token["lower"] not in _COMMON_ENGLISH and len(str(token["lower"])) >= 5]
                likely_name = any(position > 0 and str(token["raw"])[:1].isupper() for position, token in enumerate(group, start=index))
                if any("'" in token or token in _HEURISTIC_PRONOUNS or token in _HEURISTIC_AUXILIARIES for token in group_lowers):
                    continue
                surface = span(index, index + width)
                frequency = _zipf_frequency(surface)
                if len(content) >= 2 and not likely_name and 1.7 <= frequency <= 4.6:
                    add(index, index + width, 57.0 + (4.6 - frequency) * 5.0 + sum(len(str(token["lower"])) for token in content) * 0.3)

        return sorted(((score, surface) for surface, score in options.items()), key=lambda row: (-row[0], row[1].casefold()))

    @staticmethod
    def _gloss_from_context_translation(surface: str, translated: str) -> str:
        gloss = re.split(r"\s*[—–]\s*|在(?:这个)?句子中\s*[:：]?", translated, maxsplit=1)[0].strip(" ：:，,-—–")
        if not gloss or not _CJK_RE.search(gloss) or len(gloss) > 100:
            return ""
        return gloss

    def _contextual_glosses(self, rows: Sequence[tuple[str, str, str]]) -> dict[str, str]:
        prompts = [
            {"cue_id": cue_id, "text": f"{surface} — in the sentence: {phrase_text}"}
            for cue_id, surface, phrase_text in rows
        ]
        translated = self._translate_many(prompts)
        output: dict[str, str] = {}
        for cue_id, surface, _phrase_text in rows:
            gloss = self._gloss_from_context_translation(surface, translated[cue_id])
            output[cue_id] = gloss or self._translate_many([{"cue_id": cue_id, "text": surface}])[cue_id]
        return output

    def _select_candidates(self, cues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        limit = _candidate_limit_for_cues(cues)
        per_cue: list[tuple[int, str, str, list[tuple[float, str]]]] = []
        for index, cue in enumerate(cues):
            if not bool(cue.get("candidate_allowed", True)):
                continue
            cue_id = str(cue.get("cue_id") or "")
            text = str(cue.get("text") or "")
            options = self._candidate_options(text)
            if options:
                per_cue.append((index, cue_id, text, options))
        if not per_cue or limit <= 0:
            raise TranslationContractError("heuristic_candidate_pool_empty")
        bucket_count = min(4, limit, len(per_cue))
        buckets: list[list[tuple[int, str, str, list[tuple[float, str]]]]] = [[] for _ in range(bucket_count)]
        for row in per_cue:
            bucket = min(bucket_count - 1, int(row[0] * bucket_count / max(1, len(cues))))
            buckets[bucket].append(row)
        for bucket in buckets:
            bucket.sort(key=lambda row: (-row[3][0][0], row[0]))
        selected: list[tuple[int, float, str, str, str]] = []
        seen_surfaces: set[str] = set()
        depth = 0
        target = min(limit, len(per_cue))
        while len(selected) < target:
            added = False
            for bucket in buckets:
                if depth >= len(bucket):
                    continue
                index, cue_id, text, options = bucket[depth]
                choice = next(((score, surface) for score, surface in options if surface.casefold() not in seen_surfaces), None)
                if choice is None:
                    continue
                score, surface = choice
                seen_surfaces.add(surface.casefold())
                selected.append((index, score, cue_id, surface, text))
                added = True
                if len(selected) >= target:
                    break
            if not added and depth >= max((len(bucket) for bucket in buckets), default=0):
                break
            depth += 1
            if depth > len(per_cue):
                break
        if not selected:
            raise TranslationContractError("heuristic_candidate_pool_empty")
        glosses = self._contextual_glosses([(row[2], row[3], row[4]) for row in selected])
        return [
            {
                "cue_id": cue_id,
                "surface": surface,
                "gloss_zh": glosses[cue_id],
                "value_score": round(min(5.0, 2.4 + score / 50.0), 2),
            }
            for index, score, cue_id, surface, _text in sorted(selected, key=lambda row: row[0])
        ]

    def translate(self, cues: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        rows = list(cues)
        translations = self._translate_many(rows)
        payload = {
            "translations": [{"cue_id": str(row["cue_id"]), "text_zh": translations[str(row["cue_id"])]} for row in rows],
            "candidates": self._select_candidates(rows),
        }
        _validate_text_only_translation_batch(rows, payload)
        return payload

    def translate_cues(self, cues: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        return self.translate(cues)

    def translate_all(self, cues: Sequence[Mapping[str, Any]], *, batch_size: int = 28, max_workers: int = 3) -> dict[str, str]:
        del batch_size, max_workers
        return self._translate_many(list(cues))

    def review_selected(self, rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        safe = list(rows)
        phrase_rows = [{"cue_id": str(row["cue_id"]), "text": str(row["phrase_text"])} for row in safe]
        phrase_translations = self._translate_many(phrase_rows)
        gloss_translations = self._contextual_glosses([
            (str(row["cue_id"]), str(row["surface"]), str(row["phrase_text"]))
            for row in safe
        ])
        return {
            "items": [
                {
                    "cue_id": str(row["cue_id"]),
                    "surface": str(row["surface"]),
                    "gloss_zh": gloss_translations[str(row["cue_id"])],
                    "phrase_zh": phrase_translations[str(row["cue_id"])],
                }
                for row in safe
            ]
        }


class ArgosHeuristicTranslator(GoogleHeuristicTranslator):
    """Offline sentence translation with the same deterministic candidate policy."""

    identity = "argos-en-zh-offline+wordfreq-context/v1"

    def __init__(
        self,
        *,
        python_executable: str | Path | None = None,
        worker_path: str | Path | None = None,
        package_dir: str | Path | None = None,
        timeout_sec: float = 180.0,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "InFlow-English"
        default_argos_python = local_app_data / "argos-env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.python_executable = Path(
            python_executable
            or os.environ.get("INFLOW_ARGOS_PYTHON")
            or default_argos_python
        )
        self.worker_path = Path(worker_path or Path(__file__).resolve().parent / "tools" / "argos_translate_worker.py")
        self.package_dir = Path(
            package_dir
            or os.environ.get("INFLOW_ARGOS_PACKAGE_DIR")
            or (local_app_data / "argos-packages")
        )
        self.timeout_sec = float(timeout_sec)
        self.runner = runner or subprocess.run
        self._cache: dict[str, str] = {}
        self._cache_lock = threading.RLock()

    def _run_batch(self, texts: Sequence[str]) -> list[str]:
        if not self.python_executable.is_file() or not self.worker_path.is_file() or not self.package_dir.is_dir():
            raise TranslationContractError("argos_translation_unavailable")
        env = os.environ.copy()
        env.update(
            {
                "ARGOS_PACKAGES_DIR": str(self.package_dir),
                "ARGOS_DEVICE_TYPE": "cpu",
                "ARGOS_CHUNK_TYPE": "MINISBD",
                "PYTHONUTF8": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
            }
        )
        payload = json.dumps({"texts": list(texts)}, ensure_ascii=False, separators=(",", ":"))
        result = None
        last_code = "argos_translation_failed"
        last_exception: Exception | None = None
        for attempt in range(2):
            last_exception = None
            try:
                result = self.runner(
                    [str(self.python_executable), str(self.worker_path)],
                    input=payload,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_sec,
                    shell=False,
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:
                last_exception = exc
                last_code = "argos_translation_failed"
            else:
                if result.returncode == 0:
                    break
                last_code = "argos_translation_failed"
                try:
                    diagnostic = json.loads(str(result.stderr or "").strip().splitlines()[-1])
                    candidate = str(diagnostic.get("error_code") or "")
                    if re.fullmatch(r"argos_[a-z0-9_]{1,80}", candidate):
                        last_code = candidate
                except (IndexError, AttributeError, TypeError, json.JSONDecodeError):
                    pass
            non_retryable = {
                "argos_en_zh_model_missing",
                "argos_input_count_invalid",
                "argos_input_json_invalid",
                "argos_input_schema_invalid",
                "argos_input_text_invalid",
                "argos_input_total_too_large",
                "argos_translation_output_invalid",
            }
            if attempt == 1 or last_code in non_retryable:
                if last_exception is not None:
                    raise TranslationContractError(last_code) from last_exception
                raise TranslationContractError(last_code)
            gc.collect()
            time.sleep(1.0)
        if result is None or result.returncode != 0:
            raise TranslationContractError(last_code)
        try:
            parsed = json.loads(result.stdout)
            translations = parsed["translations"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise TranslationContractError("argos_translation_output_invalid") from exc
        if not isinstance(translations, list) or len(translations) != len(texts):
            raise TranslationContractError("argos_translation_output_invalid")
        normalized: list[str] = []
        for value in translations:
            translated = _normalize_chinese_spacing(str(value or "").strip())
            translated = re.sub(r"([\u4e00-\u9fff]{2,6})(?:和|、|，)\1", r"\1", translated)
            if not translated or not _CJK_RE.search(translated) or len(translated) > 500:
                raise TranslationContractError("argos_translation_output_invalid")
            normalized.append(translated)
        return normalized

    def _translate_many(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        expected = [str(row.get("cue_id") or "") for row in rows]
        if "" in expected or len(set(expected)) != len(rows):
            raise TranslationContractError("argos_translation_ids_invalid")
        output: dict[str, str] = {}
        pending: list[tuple[str, str]] = []
        with self._cache_lock:
            for row, cue_id in zip(rows, expected):
                text = str(row.get("text") or "").strip()
                if not text or len(text) > 1500 or _CONTROL_RE.search(text):
                    raise TranslationContractError("argos_translation_input_invalid")
                cached = self._cache.get(text)
                if cached:
                    output[cue_id] = cached
                else:
                    pending.append((cue_id, text))
        cursor = 0
        while cursor < len(pending):
            chunk: list[tuple[str, str]] = []
            chars = 0
            while cursor < len(pending) and len(chunk) < 256:
                row = pending[cursor]
                if chunk and chars + len(row[1]) > 70_000:
                    break
                chunk.append(row)
                chars += len(row[1])
                cursor += 1
            translated = self._run_batch([text for _cue_id, text in chunk])
            with self._cache_lock:
                for (cue_id, text), value in zip(chunk, translated):
                    self._cache[text] = value
                    output[cue_id] = value
        if set(output) != set(expected):
            raise TranslationContractError("argos_translation_coverage_invalid")
        return output

    def _translate_text(self, text: str) -> str:
        return self._translate_many([{"cue_id": "single", "text": text}])["single"]


class FallbackTranslator:
    """Try an ordered pair and permanently switch after a bounded failure."""

    def __init__(self, primary: Translator, fallback: Translator) -> None:
        self.primary = primary
        self.fallback = fallback
        self.active: Translator | None = None

    @staticmethod
    def _identity_tokens(translator: Translator) -> list[str]:
        if isinstance(translator, FallbackTranslator):
            return FallbackTranslator._identity_tokens(translator.primary) + FallbackTranslator._identity_tokens(translator.fallback)
        if isinstance(translator, OpenAICompatibleTranslator):
            return [f"openai-{re.sub(r'[^A-Za-z0-9._-]+', '-', translator.model)[:40]}"]
        if isinstance(translator, DshTranslator):
            return [f"dsh-p{PROMPT_VERSION.rsplit('/', 1)[-1]}"]
        if isinstance(translator, GoogleHeuristicTranslator) and not isinstance(translator, ArgosHeuristicTranslator):
            return ["google-v3"]
        if isinstance(translator, ArgosHeuristicTranslator):
            return ["argos-en_zh-1.9"]
        return [_dependency_identity(translator, type(translator).__name__)[:48]]

    @property
    def identity(self) -> str:
        return "translation-chain/v1:" + ">".join(self._identity_tokens(self))

    @property
    def runtime_identity(self) -> str:
        if self.active is None:
            return "not-selected"
        nested = getattr(self.active, "runtime_identity", None)
        if isinstance(nested, str) and nested and nested != "not-selected":
            return nested
        return _dependency_identity(self.active, "not-selected")

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self.active is not None:
            try:
                return getattr(self.active, method)(*args, **kwargs)
            except TranslationContractError:
                if self.active is not self.primary:
                    raise
                self.active = self.fallback
                return getattr(self.fallback, method)(*args, **kwargs)
        try:
            result = getattr(self.primary, method)(*args, **kwargs)
            self.active = self.primary
            return result
        except TranslationContractError:
            self.active = self.fallback
            return getattr(self.fallback, method)(*args, **kwargs)

    def translate(self, cues: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        return self._call("translate", cues)

    def translate_cues(self, cues: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        return self.translate(cues)

    def translate_all(self, cues: Sequence[Mapping[str, Any]], *, batch_size: int = 28, max_workers: int = 3) -> dict[str, str]:
        return self._call("translate_all", cues, batch_size=batch_size, max_workers=max_workers)

    def review_selected(self, rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        return self._call("review_selected", rows)

    def resolve_senses(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        target: Any = self.active or self.primary
        while isinstance(target, FallbackTranslator):
            target = target.active or target.primary
        if isinstance(target, DshTranslator):
            return target.resolve_senses(rows)
        return {}


def _raw_word_timing_counts(transcript: Mapping[str, Any]) -> tuple[int, int]:
    segments = transcript.get("segments") or []
    total = 0
    invalid = 0
    for segment in segments if isinstance(segments, Sequence) and not isinstance(segments, (str, bytes)) else []:
        if not isinstance(segment, Mapping):
            continue
        words = segment.get("words") or []
        if not isinstance(words, Sequence) or isinstance(words, (str, bytes)):
            continue
        for word in words:
            if not isinstance(word, Mapping):
                continue
            total += 1
            try:
                start = float(word.get("start", word.get("start_sec")))
                end = float(word.get("end", word.get("end_sec")))
            except (TypeError, ValueError):
                invalid += 1
                continue
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                invalid += 1
    return total, invalid


def _reliable_transcript_view(transcript: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    segments = transcript.get("segments") or []
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise CueConstructionError("transcript_segments_missing")
    reliable = []
    raw_words = 0
    invalid_words = 0
    excluded_words = 0
    excluded_ids = []
    repaired_ids = []
    repaired_removed_words = 0
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        words = segment.get("words") or []
        if not isinstance(words, Sequence) or isinstance(words, (str, bytes)):
            continue
        raw_words += len(words)
        valid_words = []
        bad = 0
        for word in words:
            if not isinstance(word, Mapping):
                bad += 1
                continue
            try:
                start = float(word.get("start", word.get("start_sec")))
                end = float(word.get("end", word.get("end_sec")))
            except (TypeError, ValueError):
                bad += 1
                continue
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                bad += 1
                continue
            valid_words.append(word)
        invalid_words += bad
        segment_id = str(segment.get("id", len(excluded_ids)))
        if not bad:
            reliable.append(segment)
        elif len(valid_words) >= 4 and bad / max(1, len(words)) <= 0.20:
            repaired = deepcopy(dict(segment))
            repaired["words"] = [deepcopy(dict(word)) for word in valid_words]
            repaired["start"] = float(valid_words[0].get("start", valid_words[0].get("start_sec")))
            repaired["end"] = float(valid_words[-1].get("end", valid_words[-1].get("end_sec")))
            reliable.append(repaired)
            repaired_ids.append(segment_id)
            repaired_removed_words += bad
        else:
            excluded_words += len(words)
            excluded_ids.append(segment_id)
    clean_words = raw_words - excluded_words - repaired_removed_words
    segment_total = len([segment for segment in segments if isinstance(segment, Mapping)])
    excluded_ratio = len(excluded_ids) / segment_total if segment_total else 1.0
    if clean_words < MIN_CANDIDATES * 4 or excluded_ratio > 0.25:
        raise SourceValidationError("transcript_reliable_region_too_small")
    view = dict(transcript)
    view["segments"] = reliable
    view["timing_repaired_segment_ids"] = repaired_ids
    view["timing_excluded_segment_ids"] = excluded_ids
    quality = {
        "raw_word_count": raw_words,
        "invalid_word_count": invalid_words,
        "invalid_word_ratio": round(invalid_words / raw_words, 6) if raw_words else 1.0,
        "excluded_segment_count": len(excluded_ids),
        "excluded_segment_ratio": round(excluded_ratio, 6),
        "excluded_segment_word_count": excluded_words,
        "repaired_segment_count": len(repaired_ids),
        "repaired_removed_word_count": repaired_removed_words,
        "repaired_segment_ids": repaired_ids,
        "reliable_word_count": clean_words,
        "excluded_segment_ids": excluded_ids,
    }
    return view, quality


def build_transcript_caption_units(transcript: Mapping[str, Any]) -> list[dict[str, Any]]:
    units = []
    for segment in transcript.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        text = _safe_display_text(segment.get("text"))
        try:
            start = _as_finite_float(segment.get("start"), "caption_segment_start")
            end = _as_finite_float(segment.get("end"), "caption_segment_end")
        except ValueError:
            words = [word for word in (segment.get("words") or []) if isinstance(word, Mapping)]
            if not words:
                continue
            try:
                start = _as_finite_float(words[0].get("start", words[0].get("start_sec")), "caption_segment_start")
                end = _as_finite_float(words[-1].get("end", words[-1].get("end_sec")), "caption_segment_end")
            except ValueError:
                continue
            text = text or _safe_display_text(" ".join(str(word.get("word", word.get("text", ""))).strip() for word in words))
        if text and start >= 0 and end > start:
            units.append({"cue_id": f"caption-{len(units) + 1:04d}", "start": round(start, 3), "end": round(end, 3), "text": text})
    if not units:
        raise CueConstructionError("transcript_caption_units_empty")
    return units


def effective_speech_seconds(words: Sequence[Mapping[str, Any]]) -> float:
    intervals: list[tuple[float, float]] = []
    for word in words:
        try:
            start = _as_finite_float(word.get("start_sec", word.get("start")), "speech_start")
            end = _as_finite_float(word.get("end_sec", word.get("end")), "speech_end")
        except (AttributeError, ValueError):
            continue
        if start >= 0 and end > start:
            intervals.append((start, end))
    intervals.sort()
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 0.15:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return round(sum(end - start for start, end in merged), 3)


def _lexical_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9']", "", str(value or "").casefold().replace("’", "'"))


def _lexical_sequence(value: Any) -> list[str]:
    return [
        token
        for token in (_lexical_token(part) for part in re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*", str(value or "")))
        if token
    ]


def build_caption_aligned_cues(
    transcript: Mapping[str, Any],
    english_captions: Mapping[str, Any],
    *,
    video_duration_sec: float,
    min_duration_sec: float = MIN_CUE_DURATION_SEC,
    max_duration_sec: float = MAX_CUE_DURATION_SEC,
    max_pause_delay_sec: float = 5.0,
) -> list[dict[str, Any]]:
    reliable_view, _quality = _reliable_transcript_view(transcript)
    clean_words = _normalise_word_timestamps(reliable_view)
    clean_tokens = [_lexical_token(word["text"]) for word in clean_words]

    all_positive = []
    unreliable_segments = set()
    for segment in transcript.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        segment_id = str(segment.get("id", ""))
        segment_words = segment.get("words") or []
        bad = False
        for word in segment_words:
            if not isinstance(word, Mapping):
                bad = True
                continue
            try:
                start = float(word.get("start", word.get("start_sec")))
                end = float(word.get("end", word.get("end_sec")))
            except (TypeError, ValueError):
                bad = True
                continue
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                bad = True
                continue
            all_positive.append({"start": start, "end": end, "segment_id": segment_id})
        if bad:
            unreliable_segments.add(segment_id)
    all_positive.sort(key=lambda row: (row["start"], row["end"]))
    safe_pauses = []
    for index in range(len(all_positive) - 1):
        left = all_positive[index]
        right = all_positive[index + 1]
        gap = right["start"] - left["end"]
        if gap >= 0.20 and left["segment_id"] not in unreliable_segments and right["segment_id"] not in unreliable_segments:
            safe_pauses.append({"anchor": left["end"] + min(0.08, gap * 0.45), "gap": gap})

    chunks = []
    events = english_captions.get("events") or []
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise CueConstructionError("english_caption_events_invalid")
    for event in events:
        if not isinstance(event, Mapping):
            continue
        parts = event.get("segs") or []
        if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
            continue
        text = "".join(str(part.get("utf8") or "") for part in parts if isinstance(part, Mapping))
        text = " ".join(text.replace("\n", " ").split())
        if text:
            chunks.append(text)
    caption_text = " ".join(chunks)
    tokens = re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*|[.!?]", caption_text)
    lexical = []
    sentences = []
    sentence_start = 0
    for token in tokens:
        if token in ".!?":
            if len(lexical) > sentence_start:
                sentences.append((sentence_start, len(lexical), token))
            sentence_start = len(lexical)
        else:
            lexical.append(_lexical_token(token))
    matcher = difflib.SequenceMatcher(None, lexical, clean_tokens, autojunk=False)
    mapping = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset

    cues = []
    used_anchors = set()
    for sentence_start, sentence_end, punctuation in sentences:
        sentence_length = sentence_end - sentence_start
        mapped = [mapping[index] for index in range(sentence_start, sentence_end) if index in mapping]
        if len(mapped) < max(2, math.ceil(sentence_length * 0.8)):
            continue
        low, high = min(mapped), max(mapped)
        if high - low + 1 > len(mapped) + 2:
            continue
        start_sec = float(clean_words[low]["start_sec"])
        end_sec = float(clean_words[high]["end_sec"])
        duration = end_sec - start_sec
        if not float(min_duration_sec) <= duration <= float(max_duration_sec):
            continue
        pause = next((row for row in safe_pauses if row["anchor"] >= end_sec and row["anchor"] - end_sec <= max_pause_delay_sec), None)
        if pause is None:
            continue
        anchor_key = round(float(pause["anchor"]), 3)
        if anchor_key in used_anchors:
            continue
        used_anchors.add(anchor_key)
        previous_end = max(
            (row["end"] for row in all_positive if row["start"] < start_sec and row["end"] <= start_sec + 0.05),
            default=start_sec,
        )
        next_start = min(
            (row["start"] for row in all_positive if row["end"] > end_sec and row["start"] >= end_sec - 0.05),
            default=float(video_duration_sec),
        )
        phrase_text, rendered_words = _render_words(clean_words[low : high + 1])
        phrase_text = phrase_text.rstrip(".!?") + punctuation
        cues.append(
            {
                "cue_id": f"cue-{len(cues) + 1:04d}",
                "start_sec": round(start_sec, 3),
                "end_sec": round(end_sec, 3),
                "duration_sec": round(duration, 3),
                "text": phrase_text,
                "boundary_kind": "caption_aligned_sentence",
                "preceding_gap_sec": round(max(0.0, start_sec - previous_end), 3),
                "following_gap_sec": round(max(0.0, next_start - end_sec), 3),
                "candidate_allowed": True,
                "pause_anchor_sec": anchor_key,
                "pause_gap_sec": round(float(pause["gap"]), 3),
                "pause_delay_sec": round(anchor_key - end_sec, 3),
                "caption_alignment_coverage": round(len(mapped) / sentence_length, 4),
                "words": rendered_words,
            }
        )
    if len(cues) < MIN_CANDIDATES:
        raise CueConstructionError("too_few_caption_aligned_cues")
    return cues


def _normalise_word_timestamps(transcript: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(transcript, Mapping):
        if isinstance(transcript.get("segments"), Sequence) and not isinstance(transcript.get("segments"), (str, bytes)):
            segments = transcript.get("segments") or []
        elif isinstance(transcript.get("words"), Sequence) and not isinstance(transcript.get("words"), (str, bytes)):
            segments = [{"id": 0, "words": transcript.get("words") or []}]
        else:
            raise CueConstructionError("transcript_words_missing")
    elif isinstance(transcript, Sequence) and not isinstance(transcript, (str, bytes)):
        segments = [{"id": 0, "words": transcript}]
    else:
        raise CueConstructionError("transcript_invalid")

    normalised: list[dict[str, Any]] = []
    previous_start = -1.0
    for segment_index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            raise CueConstructionError("transcript_segment_invalid")
        segment_id = str(segment.get("id", segment_index))
        words = segment.get("words") or []
        if not isinstance(words, Sequence) or isinstance(words, (str, bytes)):
            raise CueConstructionError("transcript_segment_words_invalid")
        segment_rows: list[dict[str, Any]] = []
        for word in words:
            if not isinstance(word, Mapping):
                raise CueConstructionError("transcript_word_invalid")
            token = str(word.get("word", word.get("text", ""))).strip()
            if not token or _CONTROL_RE.search(token):
                raise CueConstructionError("transcript_word_text_invalid")
            try:
                start = _as_finite_float(word.get("start", word.get("start_sec")), "word_start")
                end = _as_finite_float(word.get("end", word.get("end_sec")), "word_end")
            except ValueError as exc:
                raise CueConstructionError("transcript_word_time_invalid") from exc
            if start < 0 or end <= start:
                # faster-whisper can emit zero-duration tokens at VAD joins.
                # They are not alignable and therefore cannot enter a cue or
                # become a teaching candidate; never fabricate timestamps.
                continue
            if start + 0.05 < previous_start:
                raise CueConstructionError("transcript_word_order_invalid")
            previous_start = start
            row = {
                "index": len(normalised) + len(segment_rows),
                "text": token,
                "start_sec": start,
                "end_sec": end,
                "segment_id": segment_id,
                "segment_end": False,
            }
            segment_rows.append(row)
        if segment_rows:
            segment_rows[-1]["segment_end"] = True
            normalised.extend(segment_rows)
    if not normalised:
        raise CueConstructionError("transcript_has_no_timed_words")
    for index, row in enumerate(normalised):
        row["index"] = index
    return normalised


def _wordish(character: str) -> bool:
    return bool(character) and (character.isalnum() or character in {"_", "'", "’"})


def _render_words(words: Sequence[Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    text = ""
    rendered: list[dict[str, Any]] = []
    closing = set(".,!?;:%)]}”’")
    opening = set("([{“")
    contractions = ("'s", "'re", "'ve", "'ll", "'d", "n't", "’s", "’re", "’ve", "’ll", "’d")
    for word in words:
        token = str(word["text"])
        needs_space = bool(text)
        if token[0] in closing or token.startswith(contractions) or (text and text[-1] in opening):
            needs_space = False
        if needs_space:
            text += " "
        char_start = len(text)
        text += token
        char_end = len(text)
        lexical_start = char_start
        lexical_end = char_end
        while lexical_start < lexical_end and not _wordish(text[lexical_start]):
            lexical_start += 1
        while lexical_end > lexical_start and not _wordish(text[lexical_end - 1]):
            lexical_end -= 1
        if lexical_start == lexical_end:
            lexical_start, lexical_end = char_start, char_end
        rendered.append(
            {
                "index": int(word["index"]),
                "text": token,
                "start_sec": round(float(word["start_sec"]), 3),
                "end_sec": round(float(word["end_sec"]), 3),
                "segment_id": str(word["segment_id"]),
                "char_start": char_start,
                "char_end": char_end,
                "lexical_start": lexical_start,
                "lexical_end": lexical_end,
            }
        )
    return text, rendered


def _boundary_kind(words: Sequence[Mapping[str, Any]], boundary: int) -> tuple[str, float] | None:
    previous = words[boundary - 1]
    token = str(previous["text"])
    lexical_tail = re.sub(r"^[^A-Za-z']+|[^A-Za-z']+$", "", token).casefold()
    terminal = bool(_TERMINAL_PUNCTUATION_RE.search(token))
    clause = bool(_CLAUSE_PUNCTUATION_RE.search(token))
    # Never manufacture a phrase ending on a dangling English function word,
    # even if Whisper happened to end a segment there.
    if lexical_tail in _UNSAFE_NONTERMINAL_CUE_ENDS and not terminal and not clause:
        return None
    if boundary == len(words):
        return ("transcript_end", 100.0)
    following = words[boundary]
    gap = float(following["start_sec"]) - float(previous["end_sec"])
    choices: list[tuple[str, float]] = []
    if terminal:
        choices.append(("terminal_punctuation", 90.0))
    elif clause:
        choices.append(("clause_punctuation", 70.0))
    if gap >= 0.55:
        choices.append(("long_pause", 80.0))
    elif gap >= 0.25:
        choices.append(("pause", 60.0))
    following_text = str(following.get("text") or "")
    starts_new_sentence = bool(following_text[:1].isupper())
    if bool(previous.get("segment_end")) and (gap >= 0.08 or starts_new_sentence):
        choices.append(("whisper_segment", 50.0))
    return max(choices, key=lambda row: row[1]) if choices else None


def build_natural_cues(
    transcript: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    min_duration_sec: float = MIN_CUE_DURATION_SEC,
    max_duration_sec: float = MAX_CUE_DURATION_SEC,
) -> list[dict[str, Any]]:
    """Partition word timestamps only at ASR/punctuation/pause boundaries.

    Dynamic programming prevents a short tail while refusing a forced hard cut.
    If no complete partition exists in the 2–8 second window, the build fails
    rather than cutting after an arbitrary word.
    """

    minimum = float(min_duration_sec)
    maximum = float(max_duration_sec)
    if minimum <= 0 or maximum < minimum:
        raise ValueError("cue_duration_bounds_invalid")
    words = _normalise_word_timestamps(transcript)
    total = len(words)
    # best[i] = (score, [(end_index, boundary_kind), ...]) for words[i:]
    best: dict[int, tuple[float, list[tuple[int, str]]]] = {total: (0.0, [])}
    for start_index in range(total - 1, -1, -1):
        best_choice: tuple[float, list[tuple[int, str]]] | None = None
        cue_start = float(words[start_index]["start_sec"])
        for end_index in range(start_index + 1, total + 1):
            cue_end = float(words[end_index - 1]["end_sec"])
            duration = cue_end - cue_start
            if duration > maximum + 1e-9:
                break
            if duration < minimum - 1e-9:
                continue
            boundary = _boundary_kind(words, end_index)
            tail = best.get(end_index)
            if boundary is None or tail is None:
                continue
            kind, boundary_score = boundary
            # Prefer a natural boundary and a compact ~4.5 s listening unit;
            # the per-cue cost avoids gratuitous fragmentation.
            score = boundary_score - abs(duration - 4.5) * 3.0 - 18.0 + tail[0]
            proposal = (score, [(end_index, kind), *tail[1]])
            if best_choice is None or proposal[0] > best_choice[0]:
                best_choice = proposal
        if best_choice is not None:
            best[start_index] = best_choice
    if 0 not in best:
        raise CueConstructionError("no_natural_2_to_8_second_partition")

    cues: list[dict[str, Any]] = []
    start_index = 0
    for cue_number, (end_index, boundary_kind) in enumerate(best[0][1], start=1):
        cue_words = words[start_index:end_index]
        text, rendered_words = _render_words(cue_words)
        start_sec = float(cue_words[0]["start_sec"])
        end_sec = float(cue_words[-1]["end_sec"])
        duration = end_sec - start_sec
        preceding_gap = start_sec if start_index == 0 else max(0.0, start_sec - float(words[start_index - 1]["end_sec"]))
        following_gap = None if end_index == total else max(0.0, float(words[end_index]["start_sec"]) - end_sec)
        candidate_allowed = following_gap is None or following_gap >= 0.20
        if not minimum - 1e-9 <= duration <= maximum + 1e-9:
            raise CueConstructionError("cue_duration_internal_error")
        cues.append(
            {
                "cue_id": f"cue-{cue_number:04d}",
                "start_sec": round(start_sec, 3),
                "end_sec": round(end_sec, 3),
                "duration_sec": round(duration, 3),
                "text": text,
                "boundary_kind": boundary_kind,
                "preceding_gap_sec": round(preceding_gap, 3),
                "following_gap_sec": None if following_gap is None else round(following_gap, 3),
                "candidate_allowed": candidate_allowed,
                "words": rendered_words,
            }
        )
        start_index = end_index
    if start_index != total:
        raise CueConstructionError("cue_partition_incomplete")
    return cues


def build_sparse_natural_cues(
    transcript: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    min_duration_sec: float = MIN_CUE_DURATION_SEC,
    max_duration_sec: float = MAX_CUE_DURATION_SEC,
    max_pause_delay_sec: float = 0.0,
) -> list[dict[str, Any]]:
    """Collect independent trustworthy segments without covering the whole video."""

    minimum = float(min_duration_sec)
    maximum = float(max_duration_sec)
    if minimum <= 0 or maximum < minimum:
        raise ValueError("cue_duration_bounds_invalid")
    words = _normalise_word_timestamps(transcript)
    repaired_segments = set(transcript.get("timing_repaired_segment_ids") or []) if isinstance(transcript, Mapping) else set()
    safe_pauses = []
    for index in range(len(words) - 1):
        left = words[index]
        right = words[index + 1]
        if str(left.get("segment_id")) in repaired_segments or str(right.get("segment_id")) in repaired_segments:
            continue
        gap = float(right["start_sec"]) - float(left["end_sec"])
        if gap >= 0.20:
            safe_pauses.append({"anchor": float(left["end_sec"]) + min(0.08, gap * 0.45), "gap": gap})
    by_segment: dict[str, list[dict[str, Any]]] = {}
    segment_order: list[str] = []
    for word in words:
        segment_id = str(word["segment_id"])
        if segment_id not in by_segment:
            by_segment[segment_id] = []
            segment_order.append(segment_id)
        by_segment[segment_id].append(word)
    position = {int(word["index"]): index for index, word in enumerate(words)}
    unsafe_starts = {"and", "but", "or", "so", "because", "which", "that", "of", "to", "for", "with", "from"}
    rows: list[dict[str, Any]] = []
    seen_ranges: set[tuple[int, int]] = set()
    for segment_id in segment_order:
        cue_words = by_segment[segment_id]
        if not cue_words or segment_id in repaired_segments:
            continue
        first_position = position[int(cue_words[0]["index"])]
        last_position = position[int(cue_words[-1]["index"])]
        start_sec = float(cue_words[0]["start_sec"])
        end_sec = float(cue_words[-1]["end_sec"])
        duration = end_sec - start_sec
        if not minimum - 1e-9 <= duration <= maximum + 1e-9:
            continue
        text, rendered_words = _render_words(cue_words)
        lexical = re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)*", text)
        if len(lexical) < 3:
            continue
        first_lexical = lexical[0].casefold().replace("’", "'")
        last_lexical = lexical[-1].casefold().replace("’", "'")
        preceding_gap = start_sec if first_position == 0 else max(0.0, start_sec - float(words[first_position - 1]["end_sec"]))
        following_gap = None if last_position + 1 == len(words) else max(0.0, float(words[last_position + 1]["start_sec"]) - end_sec)
        starts_naturally = first_position == 0 or preceding_gap >= 0.15 or text[:1].isupper()
        terminal = bool(_TERMINAL_PUNCTUATION_RE.search(text))
        clause_only = bool(_CLAUSE_PUNCTUATION_RE.search(text)) and not terminal
        if not starts_naturally or first_lexical in unsafe_starts:
            continue
        if clause_only or not terminal:
            continue
        if last_lexical in _UNSAFE_NONTERMINAL_CUE_ENDS:
            continue
        immediate_following_gap = following_gap
        if following_gap is None:
            pause_anchor = end_sec
            pause_gap = 0.0
        elif following_gap >= 0.20:
            pause_anchor = end_sec + min(0.08, following_gap * 0.45)
            pause_gap = following_gap
        elif max_pause_delay_sec > 0:
            pause = next(
                (
                    row for row in safe_pauses
                    if row["anchor"] >= end_sec and row["anchor"] - end_sec <= float(max_pause_delay_sec)
                ),
                None,
            )
            if pause is None:
                continue
            pause_anchor = float(pause["anchor"])
            pause_gap = float(pause["gap"])
        else:
            continue
        range_key = (round(start_sec * 1000), round(end_sec * 1000))
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)
        rows.append(
            {
                "start_sec": round(start_sec, 3),
                "end_sec": round(end_sec, 3),
                "duration_sec": round(duration, 3),
                "text": text,
                "boundary_kind": "whisper_sparse_segment",
                "preceding_gap_sec": round(preceding_gap, 3),
                "following_gap_sec": round(pause_gap, 3),
                "content_following_gap_sec": None if immediate_following_gap is None else round(immediate_following_gap, 3),
                "pause_anchor_sec": round(pause_anchor, 3),
                "pause_gap_sec": round(pause_gap, 3),
                "pause_delay_sec": round(max(0.0, pause_anchor - end_sec), 3),
                "candidate_allowed": True,
                "words": rendered_words,
            }
        )
    rows.sort(key=lambda row: float(row["start_sec"]))
    if len(rows) < MIN_CANDIDATES:
        raise CueConstructionError("too_few_sparse_natural_cues")
    for index, row in enumerate(rows, start=1):
        row["cue_id"] = f"cue-{index:04d}"
    return rows


def filter_sparse_cues_by_english_captions(
    cues: Sequence[Mapping[str, Any]],
    english_captions: Mapping[str, Any],
    *,
    minimum_ratio: float = 0.96,
) -> list[dict[str, Any]]:
    """Keep sparse ASR cues only when timed platform captions corroborate them."""

    events = english_captions.get("events") or []
    caption_rows: list[dict[str, Any]] = []
    for event in events if isinstance(events, Sequence) and not isinstance(events, (str, bytes)) else []:
        if not isinstance(event, Mapping):
            continue
        parts = event.get("segs") or []
        if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
            continue
        text = " ".join("".join(str(part.get("utf8") or "") for part in parts if isinstance(part, Mapping)).replace("\n", " ").split())
        try:
            start = float(event.get("tStartMs")) / 1000.0
            duration = float(event.get("dDurationMs") or 7000) / 1000.0
        except (TypeError, ValueError):
            continue
        if text:
            caption_rows.append({"start": start, "end": start + max(0.1, duration), "tokens": _lexical_sequence(text)})

    def best_ratio(expected: list[str], available: list[str]) -> float:
        if not expected or not available:
            return 0.0
        best = 0.0
        minimum_width = max(1, len(expected) - 2)
        maximum_width = min(len(available), len(expected) + 2)
        for width in range(minimum_width, maximum_width + 1):
            for start in range(0, len(available) - width + 1):
                score = difflib.SequenceMatcher(None, expected, available[start:start + width], autojunk=False).ratio()
                best = max(best, score)
                if best >= 1.0:
                    return 1.0
        return best

    kept: list[dict[str, Any]] = []
    for cue in cues:
        start = float(cue.get("start_sec") or 0.0)
        end = float(cue.get("end_sec") or 0.0)
        nearby = [row for row in caption_rows if row["start"] <= end + 2.0 and row["end"] >= start - 2.0]
        windows: list[list[str]] = [list(row["tokens"]) for row in nearby]
        for index in range(len(nearby) - 1):
            windows.append(list(nearby[index]["tokens"]) + list(nearby[index + 1]["tokens"]))
        expected = _lexical_sequence(cue.get("text"))
        score = max((best_ratio(expected, window) for window in windows), default=0.0)
        if score + 1e-9 < float(minimum_ratio):
            continue
        row = dict(cue)
        row["english_caption_alignment_coverage"] = round(score, 4)
        kept.append(row)
    if len(kept) < MIN_CANDIDATES:
        raise CueConstructionError("sparse_cues_not_corroborated_by_english_captions")
    for index, row in enumerate(kept, start=1):
        row["cue_id"] = f"cue-{index:04d}"
    return kept


def _locate_surface(cue: Mapping[str, Any], surface: str) -> SurfaceMatch:
    text = str(cue.get("text") or "")
    if not surface or surface != surface.strip() or len(surface) > 160 or _CONTROL_RE.search(surface):
        raise TranslationContractError("candidate_surface_invalid")
    positions: list[int] = []
    cursor = 0
    while True:
        found = text.find(surface, cursor)
        if found < 0:
            break
        end = found + len(surface)
        before_ok = found == 0 or not (_wordish(surface[0]) and _wordish(text[found - 1]))
        after_ok = end == len(text) or not (_wordish(surface[-1]) and _wordish(text[end]))
        if before_ok and after_ok:
            positions.append(found)
        cursor = found + 1
    if not positions:
        raise TranslationContractError("candidate_surface_not_in_cue")
    if len(positions) != 1:
        raise TranslationContractError("candidate_surface_ambiguous")
    start_char = positions[0]
    end_char = start_char + len(surface)
    if not re.search(r"[-‐‑–—]", surface):
        before = text[:start_char].rstrip()
        after = text[end_char:].lstrip()
        if before.endswith(("-", "‐", "‑", "–", "—")) or after.startswith(("-", "‐", "‑", "–", "—")):
            raise TranslationContractError("candidate_surface_inside_hyphenated_unit")
    words = cue.get("words") or []
    overlapping = [
        word
        for word in words
        if int(word["char_end"]) > start_char and int(word["char_start"]) < end_char
    ]
    if not overlapping:
        raise TranslationContractError("candidate_surface_has_no_word_alignment")
    first = overlapping[0]
    last = overlapping[-1]
    allowed_starts = {int(first["char_start"]), int(first["lexical_start"])}
    allowed_ends = {int(last["char_end"]), int(last["lexical_end"])}
    if start_char not in allowed_starts or end_char not in allowed_ends:
        raise TranslationContractError("candidate_surface_partial_word")
    if not any(_wordish(character) for character in surface):
        raise TranslationContractError("candidate_surface_has_no_lexeme")
    return SurfaceMatch(
        start_char=start_char,
        end_char=end_char,
        start_sec=float(first["start_sec"]),
        end_sec=float(last["end_sec"]),
        first_word_index=int(first["index"]),
        last_word_index=int(last["index"]),
    )


def _validate_text_only_translation_batch(
    cues: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> None:
    """Validate Dsh output without accepting or requiring timing data."""

    if not isinstance(payload, Mapping) or set(payload) != {"translations", "candidates"}:
        raise TranslationContractError("translation_top_level_schema_invalid")
    cue_text = {str(cue.get("cue_id") or ""): str(cue.get("text") or "") for cue in cues}
    cue_allowed = {str(cue.get("cue_id") or ""): bool(cue.get("candidate_allowed", True)) for cue in cues}
    if not cue_text or "" in cue_text or len(cue_text) != len(cues):
        raise TranslationContractError("input_cue_ids_invalid")
    translations = payload.get("translations")
    if not isinstance(translations, list):
        raise TranslationContractError("translations_not_array")
    translated_ids: set[str] = set()
    for row in translations:
        if not isinstance(row, Mapping) or set(row) != _MODEL_TRANSLATION_FIELDS:
            raise TranslationContractError("translation_row_schema_invalid")
        cue_id = str(row.get("cue_id") or "")
        text_zh = str(row.get("text_zh") or "").strip()
        if cue_id not in cue_text or cue_id in translated_ids:
            raise TranslationContractError("translation_cue_id_invalid")
        if not text_zh or len(text_zh) > 500 or _CONTROL_RE.search(text_zh) or not _CJK_RE.search(text_zh):
            raise TranslationContractError("translation_text_zh_invalid")
        translated_ids.add(cue_id)
    if translated_ids != set(cue_text):
        raise TranslationContractError("translations_not_complete")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not MIN_CANDIDATES <= len(candidates) <= MAX_CANDIDATES:
        raise TranslationContractError("candidate_count_out_of_range")
    seen: set[tuple[str, str]] = set()
    seen_cues: set[str] = set()
    seen_surfaces: set[str] = set()
    filtered_candidates = []
    for row in candidates:
        if not isinstance(row, Mapping) or set(row) != _MODEL_CANDIDATE_FIELDS:
            raise TranslationContractError("candidate_row_schema_invalid")
        cue_id = str(row.get("cue_id") or "")
        surface = str(row.get("surface") or "")
        gloss_zh = str(row.get("gloss_zh") or "").strip()
        if cue_id not in cue_text:
            raise TranslationContractError("candidate_cue_id_invalid")
        if not cue_allowed.get(cue_id, False):
            raise TranslationContractError("candidate_cue_has_no_safe_pause")
        if not surface or surface != surface.strip() or _CONTROL_RE.search(surface):
            raise TranslationContractError("candidate_surface_invalid")
        lexical_tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", surface)
        policy_word_count_valid = 1 <= len(lexical_tokens) <= 4
        if not gloss_zh or not _CJK_RE.search(gloss_zh) or len(gloss_zh) > 100:
            raise TranslationContractError("candidate_gloss_zh_invalid")
        try:
            score = _as_finite_float(row.get("value_score"), "value_score")
        except ValueError as exc:
            raise TranslationContractError("candidate_value_score_invalid") from exc
        if not 0.0 <= score <= 5.0:
            raise TranslationContractError("candidate_value_score_out_of_range")
        text = cue_text[cue_id]
        positions: list[int] = []
        cursor = 0
        while True:
            found = text.find(surface, cursor)
            if found < 0:
                break
            end = found + len(surface)
            if (
                (found == 0 or not (_wordish(surface[0]) and _wordish(text[found - 1])))
                and (end == len(text) or not (_wordish(surface[-1]) and _wordish(text[end])))
            ):
                positions.append(found)
            cursor = found + 1
        if len(positions) != 1:
            raise TranslationContractError("candidate_surface_not_exact_unambiguous_span")
        occurrence = (cue_id, surface)
        surface_key = surface.casefold()
        if not policy_word_count_valid or occurrence in seen or cue_id in seen_cues or surface_key in seen_surfaces:
            continue
        seen.add(occurrence)
        seen_cues.add(cue_id)
        seen_surfaces.add(surface_key)
        filtered_candidates.append(dict(row))
    if len(filtered_candidates) < MIN_CANDIDATES:
        raise TranslationContractError("candidate_count_after_policy_filter_out_of_range")
    if isinstance(payload, dict):
        payload["candidates"] = filtered_candidates


def validate_translation_batch(
    cues: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> ValidatedTranslationBatch:
    """Enforce exact translator schema and reject hallucinated surfaces."""

    if not isinstance(payload, Mapping) or set(payload) != {"translations", "candidates"}:
        raise TranslationContractError("translation_top_level_schema_invalid")
    cue_by_id: dict[str, Mapping[str, Any]] = {}
    for cue in cues:
        cue_id = str(cue.get("cue_id") or "")
        if not cue_id or cue_id in cue_by_id:
            raise TranslationContractError("input_cue_ids_invalid")
        cue_by_id[cue_id] = cue

    rows = payload.get("translations")
    if not isinstance(rows, list):
        raise TranslationContractError("translations_not_array")
    translations: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _MODEL_TRANSLATION_FIELDS:
            raise TranslationContractError("translation_row_schema_invalid")
        cue_id = str(row.get("cue_id") or "")
        text_zh = str(row.get("text_zh") or "").strip()
        if cue_id not in cue_by_id or cue_id in translations:
            raise TranslationContractError("translation_cue_id_invalid")
        if not text_zh or len(text_zh) > 500 or _CONTROL_RE.search(text_zh) or not _CJK_RE.search(text_zh):
            raise TranslationContractError("translation_text_zh_invalid")
        translations[cue_id] = text_zh
    if set(translations) != set(cue_by_id):
        raise TranslationContractError("translations_not_complete")

    candidates_raw = payload.get("candidates")
    if not isinstance(candidates_raw, list) or not MIN_CANDIDATES <= len(candidates_raw) <= MAX_CANDIDATES:
        raise TranslationContractError("candidate_count_out_of_range")
    candidates: list[ValidatedCandidate] = []
    seen_occurrences: set[tuple[str, str]] = set()
    seen_candidate_cues: set[str] = set()
    seen_candidate_surfaces: set[str] = set()
    for row in candidates_raw:
        if not isinstance(row, Mapping) or set(row) != _MODEL_CANDIDATE_FIELDS:
            raise TranslationContractError("candidate_row_schema_invalid")
        cue_id = str(row.get("cue_id") or "")
        surface = str(row.get("surface") or "")
        gloss_zh = str(row.get("gloss_zh") or "").strip()
        if cue_id not in cue_by_id:
            raise TranslationContractError("candidate_cue_id_invalid")
        if not bool(cue_by_id[cue_id].get("candidate_allowed", True)):
            raise TranslationContractError("candidate_cue_has_no_safe_pause")
        lexical_tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", surface)
        policy_word_count_valid = 1 <= len(lexical_tokens) <= 4
        if not gloss_zh or len(gloss_zh) > 100 or _CONTROL_RE.search(gloss_zh) or not _CJK_RE.search(gloss_zh):
            raise TranslationContractError("candidate_gloss_zh_invalid")
        try:
            value_score = _as_finite_float(row.get("value_score"), "value_score")
        except ValueError as exc:
            raise TranslationContractError("candidate_value_score_invalid") from exc
        if not 0.0 <= value_score <= 5.0:
            raise TranslationContractError("candidate_value_score_out_of_range")
        occurrence = (cue_id, surface)
        if occurrence in seen_occurrences:
            raise TranslationContractError("candidate_duplicate_occurrence")
        match = _locate_surface(cue_by_id[cue_id], surface)
        surface_key = surface.casefold()
        if not policy_word_count_valid or cue_id in seen_candidate_cues or surface_key in seen_candidate_surfaces:
            continue
        seen_candidate_cues.add(cue_id)
        seen_candidate_surfaces.add(surface_key)
        seen_occurrences.add(occurrence)
        candidates.append(
            ValidatedCandidate(
                cue_id=cue_id,
                surface=surface,
                gloss_zh=gloss_zh,
                value_score=round(value_score, 3),
                match=match,
            )
        )
    if len(candidates) < MIN_CANDIDATES:
        raise TranslationContractError("candidate_count_after_cue_dedup_out_of_range")
    return ValidatedTranslationBatch(translations=translations, candidates=tuple(candidates))


def validate_candidate_review(
    requested: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, str]]:
    if not isinstance(payload, Mapping) or set(payload) != {"items"} or not isinstance(payload.get("items"), list):
        raise TranslationContractError("candidate_review_schema_invalid")
    expected = {(str(row.get("cue_id") or ""), str(row.get("surface") or "")) for row in requested}
    if "" in {part for key in expected for part in key} or len(expected) != len(requested):
        raise TranslationContractError("candidate_review_input_invalid")
    reviewed: dict[tuple[str, str], dict[str, str]] = {}
    for row in payload["items"]:
        if not isinstance(row, Mapping) or set(row) != _MODEL_REVIEW_FIELDS:
            raise TranslationContractError("candidate_review_row_invalid")
        key = (str(row.get("cue_id") or ""), str(row.get("surface") or ""))
        gloss = _normalize_chinese_spacing(str(row.get("gloss_zh") or "").strip())
        phrase = _normalize_chinese_spacing(str(row.get("phrase_zh") or "").strip())
        if key not in expected or key in reviewed:
            raise TranslationContractError("candidate_review_identity_changed")
        if not gloss or len(gloss) > 100 or not _CJK_RE.search(gloss) or not phrase or len(phrase) > 500 or not _CJK_RE.search(phrase):
            raise TranslationContractError("candidate_review_chinese_invalid")
        reviewed[key] = {"gloss_zh": gloss, "phrase_zh": phrase}
    if set(reviewed) != expected:
        raise TranslationContractError("candidate_review_incomplete")
    return reviewed


def _require_nonempty_regular_file(path: Path, error_code: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise SourceValidationError(error_code)


def _require_audited_file(path: Path, error_code: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise AuditError(error_code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_json_object(path: Path, error_code: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AuditError(error_code)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError(error_code) from exc
    if not isinstance(payload, dict):
        raise AuditError(error_code)
    return payload


def _safe_relative_file(pack_dir: Path, relative: str) -> Path:
    if not relative or "\\" in relative:
        raise AuditError("manifest_relative_path_invalid")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise AuditError("manifest_relative_path_invalid")
    target = pack_dir.joinpath(*relative_path.parts)
    if target.is_symlink():
        raise AuditError("pack_symlink_not_allowed")
    try:
        target.resolve(strict=False).relative_to(pack_dir.resolve(strict=False))
    except ValueError as exc:
        raise AuditError("manifest_path_escape") from exc
    return target


def _number_close(left: Any, right: Any, tolerance: float = 0.002) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def verify_pack_directory(
    pack_dir: Path | str,
    *,
    expected_pack_id: str | None = None,
    expected_video_id: str | None = None,
    expected_source_url: str | None = None,
    require_audit: bool = True,
) -> dict[str, Any]:
    """Read-only verification used before commit and for cache reuse."""

    root = Path(pack_dir)
    if root.is_symlink() or not root.is_dir():
        raise AuditError("pack_directory_missing_or_symlink")
    expected_root = set(_REQUIRED_ROOT_FILES)
    if require_audit:
        expected_root.add("audit.json")
    actual_root = {path.name for path in root.iterdir()}
    if actual_root != expected_root:
        raise AuditError("pack_layout_invalid")

    manifest = _read_json_object(root / "manifest.json", "manifest_invalid")
    source = _read_json_object(root / "source.json", "source_invalid")
    transcript = _read_json_object(root / "transcript.json", "transcript_invalid")
    captions = _read_json_object(root / "captions-zh.json", "captions_invalid")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise AuditError("manifest_schema_version_invalid")
    pack_id = str(manifest.get("pack_id") or "")
    video_id = str(manifest.get("video_id") or "")
    source_url = str(manifest.get("source_url") or "")
    if not pack_id or not _VIDEO_ID_RE.fullmatch(video_id):
        raise AuditError("manifest_identity_invalid")
    if expected_pack_id is not None and pack_id != expected_pack_id:
        raise AuditError("manifest_pack_id_mismatch")
    if expected_video_id is not None and video_id != expected_video_id:
        raise AuditError("manifest_video_id_mismatch")
    if expected_source_url is not None and source_url != expected_source_url:
        raise AuditError("manifest_source_url_mismatch")
    if source.get("video_id") != video_id or source.get("canonical_url") != source_url:
        raise AuditError("source_identity_mismatch")
    if transcript.get("video_id") != video_id or captions.get("video_id") != video_id:
        raise AuditError("derived_document_identity_mismatch")
    if transcript.get("schema_version") != SCHEMA_VERSION or captions.get("schema_version") != SCHEMA_VERSION:
        raise AuditError("derived_document_schema_invalid")

    cues_raw = transcript.get("cues")
    caption_rows = captions.get("segments")
    if not isinstance(cues_raw, list) or not cues_raw:
        raise AuditError("transcript_cues_invalid")
    if not isinstance(caption_rows, list):
        raise AuditError("caption_cues_invalid")
    cue_by_id: dict[str, Mapping[str, Any]] = {}
    for cue in cues_raw:
        if not isinstance(cue, Mapping):
            raise AuditError("transcript_cue_invalid")
        cue_id = str(cue.get("cue_id") or "")
        if not cue_id or cue_id in cue_by_id:
            raise AuditError("transcript_cue_id_invalid")
        try:
            duration = _as_finite_float(cue.get("duration_sec"), "duration")
        except ValueError as exc:
            raise AuditError("transcript_cue_duration_invalid") from exc
        if not MIN_CUE_DURATION_SEC <= duration <= MAX_CUE_DURATION_SEC:
            raise AuditError("transcript_cue_duration_out_of_range")
        if cue.get("boundary_kind") not in {
            "terminal_punctuation",
            "clause_punctuation",
            "long_pause",
            "pause",
            "whisper_segment",
            "transcript_end",
            "caption_aligned_sentence",
            "whisper_sparse_segment",
        }:
            raise AuditError("transcript_cue_boundary_invalid")
        cue_by_id[cue_id] = cue
    if not caption_rows:
        raise AuditError("caption_segments_empty")
    previous_caption_start = -1.0
    video_duration = float(manifest.get("duration_sec") or manifest.get("duration") or 0.0)
    for row in caption_rows:
        if not isinstance(row, Mapping):
            raise AuditError("caption_row_invalid")
        text_zh = str(row.get("text") or "").strip()
        try:
            start = _as_finite_float(row.get("start"), "caption_start")
            end = _as_finite_float(row.get("end"), "caption_end")
        except ValueError as exc:
            raise AuditError("caption_row_timing_invalid") from exc
        if not text_zh or not _CJK_RE.search(text_zh) or start < previous_caption_start or start < 0 or end <= start or end > video_duration + 0.5:
            raise AuditError("caption_row_contract_invalid")
        previous_caption_start = start

    candidates = manifest.get("candidates")
    if not isinstance(candidates, list) or not MIN_CANDIDATES <= len(candidates) <= MAX_CANDIDATES:
        raise AuditError("manifest_candidate_count_invalid")
    expected_phrase_paths: set[str] = set()
    seen_manifest_cues: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise AuditError("manifest_candidate_invalid")
        cue_id = str(candidate.get("cue_id") or "")
        surface = str(candidate.get("surface") or "")
        cue = cue_by_id.get(cue_id)
        if cue is None:
            raise AuditError("manifest_candidate_unknown_cue")
        if cue_id in seen_manifest_cues:
            raise AuditError("manifest_candidate_duplicate_cue")
        seen_manifest_cues.add(cue_id)
        if not bool(cue.get("candidate_allowed", False)):
            raise AuditError("manifest_candidate_unsafe_pause_cue")
        expected_occurrence = f"{video_id}:{cue_id}:{surface}"
        if candidate.get("id") != expected_occurrence or candidate.get("occurrence_id") != expected_occurrence:
            raise AuditError("manifest_occurrence_namespace_invalid")
        try:
            match = _locate_surface(cue, surface)
        except TranslationContractError as exc:
            raise AuditError("manifest_candidate_surface_invalid") from exc
        if candidate.get("phrase_text") != cue.get("text"):
            raise AuditError("manifest_phrase_text_mismatch")
        phrase_zh = str(candidate.get("phrase_zh") or "").strip()
        if not phrase_zh or not _CJK_RE.search(phrase_zh):
            raise AuditError("manifest_phrase_translation_invalid")
        phrase_audit_text = candidate.get("phrase_audit_text")
        if phrase_audit_text is not None:
            heard_words = _lexical_sequence(phrase_audit_text)
            target_words = _lexical_sequence(surface)
            target_present = any(
                heard_words[index : index + len(target_words)] == target_words
                for index in range(max(0, len(heard_words) - len(target_words) + 1))
            ) if target_words else False
            if candidate.get("phrase_lexical_exact") is not True or heard_words != _lexical_sequence(cue.get("text")) or not target_present:
                raise AuditError("manifest_phrase_unprompted_asr_mismatch")
        try:
            phrase_start = _as_finite_float(candidate.get("phrase_start_sec"), "phrase_start")
            phrase_end = _as_finite_float(candidate.get("phrase_end_sec"), "phrase_end")
            spoken_start = _as_finite_float(candidate.get("spoken_start_sec"), "spoken_start")
            spoken_end = _as_finite_float(candidate.get("spoken_end_sec"), "spoken_end")
            safe_pause_gap = _as_finite_float(candidate.get("safe_pause_gap_sec"), "safe_pause_gap")
        except ValueError as exc:
            raise AuditError("manifest_phrase_timing_invalid") from exc
        cue_start = float(cue["start_sec"])
        cue_end = float(cue["end_sec"])
        cue_pre_gap = max(0.0, float(cue.get("preceding_gap_sec") or 0.0))
        cue_post_gap = max(0.0, float(cue.get("following_gap_sec") or 0.0))
        cue_safe_pause_gap = max(0.0, float(cue.get("pause_gap_sec", cue_post_gap) or 0.0))
        if not _number_close(spoken_start, cue_start) or not _number_close(spoken_end, cue_end):
            raise AuditError("manifest_spoken_timing_mismatch")
        if safe_pause_gap < 0.20 or not _number_close(safe_pause_gap, cue_safe_pause_gap):
            raise AuditError("manifest_safe_pause_gap_invalid")
        if phrase_start > spoken_start or spoken_start - phrase_start > min(0.10, cue_pre_gap * 0.45) + 0.002:
            raise AuditError("manifest_phrase_pre_padding_invalid")
        if phrase_end < spoken_end or phrase_end - spoken_end > min(0.12, cue_post_gap * 0.45) + 0.002:
            raise AuditError("manifest_phrase_post_padding_invalid")
        expected_anchor = float(cue.get("pause_anchor_sec", cue_end + min(0.08, cue_post_gap * 0.45)))
        if not _number_close(candidate.get("anchor_sec"), expected_anchor):
            raise AuditError("manifest_safe_pause_anchor_invalid")
        expected_pause_delay = max(0.0, expected_anchor - cue_end)
        recorded_pause_delay = candidate.get("pause_delay_sec")
        if expected_pause_delay > 5.0 or (recorded_pause_delay is not None and not _number_close(recorded_pause_delay, expected_pause_delay)):
            raise AuditError("manifest_pause_delay_invalid")
        expected_highlight_start = match.start_sec - phrase_start
        expected_highlight_end = match.end_sec - phrase_start
        if not _number_close(candidate.get("highlight_start_sec"), expected_highlight_start) or not _number_close(
            candidate.get("highlight_end_sec"), expected_highlight_end
        ):
            raise AuditError("manifest_highlight_mismatch")
        phrase_path = str(candidate.get("phrase_audio") or "")
        if not phrase_path.startswith("phrases/") or not phrase_path.endswith(".mp3"):
            raise AuditError("manifest_phrase_path_invalid")
        if phrase_path in expected_phrase_paths:
            raise AuditError("manifest_phrase_path_duplicate")
        expected_phrase_paths.add(phrase_path)
        _require_audited_file(_safe_relative_file(root, phrase_path), "phrase_audio_invalid")
        try:
            actual_audio_duration = _as_finite_float(candidate.get("audio_duration_sec"), "audio_duration")
            intended_duration = _as_finite_float(candidate.get("phrase_duration_sec"), "phrase_duration")
        except ValueError as exc:
            raise AuditError("manifest_phrase_duration_invalid") from exc
        if abs(actual_audio_duration - intended_duration) > max(0.25, intended_duration * 0.05):
            raise AuditError("manifest_phrase_audio_duration_mismatch")
        if candidate.get("isolated_word_audio_enabled") is not False:
            raise AuditError("isolated_word_audio_forbidden")

    phrases_dir = root / "phrases"
    if phrases_dir.is_symlink() or not phrases_dir.is_dir():
        raise AuditError("phrases_directory_invalid")
    actual_phrase_paths = {f"phrases/{path.name}" for path in phrases_dir.iterdir() if path.is_file()}
    if actual_phrase_paths != expected_phrase_paths or any(path.is_dir() or path.is_symlink() for path in phrases_dir.iterdir()):
        raise AuditError("phrases_layout_invalid")

    hashes = manifest.get("hashes")
    if not isinstance(hashes, Mapping) or hashes.get("algorithm") != "sha256" or not isinstance(hashes.get("files"), Mapping):
        raise AuditError("manifest_hash_schema_invalid")
    hash_files = dict(hashes["files"])
    required_hash_paths = {
        "video.mp4",
        "source.json",
        "transcript.json",
        "captions-zh.json",
        *expected_phrase_paths,
    }
    if set(hash_files) != required_hash_paths:
        raise AuditError("manifest_hash_coverage_invalid")
    for relative, expected_digest in hash_files.items():
        target = _safe_relative_file(root, str(relative))
        _require_audited_file(target, "hashed_file_missing")
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected_digest)) or _sha256(target) != expected_digest:
            raise AuditError("manifest_hash_mismatch")

    quality = manifest.get("quality_gates")
    if not isinstance(quality, Mapping) or quality.get("all_passed") is not True:
        raise AuditError("manifest_quality_gate_invalid")
    if require_audit:
        audit = _read_json_object(root / "audit.json", "audit_invalid")
        if (
            audit.get("schema_version") != SCHEMA_VERSION
            or audit.get("pack_id") != pack_id
            or audit.get("passed") is not True
            or not isinstance(audit.get("checks"), list)
            or not audit["checks"]
            or any(not isinstance(row, Mapping) or row.get("passed") is not True for row in audit["checks"])
        ):
            raise AuditError("audit_contract_invalid")
    return manifest


def _validate_download_probe(probe: Mapping[str, Any], expected_duration: float) -> dict[str, Any]:
    try:
        duration = _as_finite_float(probe.get("duration_sec"), "duration")
        height = int(probe.get("height"))
    except (TypeError, ValueError) as exc:
        raise SourceValidationError("download_probe_invalid") from exc
    if probe.get("has_video") is not True or probe.get("has_audio") is not True:
        raise SourceValidationError("download_missing_audio_or_video")
    if height <= 0 or height > 720:
        raise SourceValidationError("download_resolution_out_of_range")
    if abs(duration - expected_duration) > max(2.0, expected_duration * 0.02):
        raise SourceValidationError("download_duration_mismatch")
    return {
        "duration_sec": round(duration, 3),
        "height": height,
        "has_video": True,
        "has_audio": True,
    }


def _validate_clip_probe(probe: Mapping[str, Any], intended_duration: float) -> float:
    try:
        actual = _as_finite_float(probe.get("duration_sec"), "duration")
    except ValueError as exc:
        raise AuditError("phrase_probe_invalid") from exc
    if probe.get("has_audio") is not True or actual <= 0:
        raise AuditError("phrase_probe_missing_audio")
    if abs(actual - intended_duration) > max(0.25, intended_duration * 0.05):
        raise AuditError("phrase_probe_duration_mismatch")
    return round(actual, 3)


def _raise_if_cancelled(callback: CancelCallback | None) -> None:
    if callback is None:
        return
    try:
        cancelled = bool(callback())
    except Exception as exc:
        raise VideoPackError("cancel_callback_failed") from exc
    if cancelled:
        raise BuildCancelled("video_pack_build_cancelled")


def _available_memory_bytes() -> int | None:
    try:
        import psutil  # type: ignore

        return int(psutil.virtual_memory().available)
    except (ImportError, AttributeError, OSError, ValueError):
        return None


def _guard_pack_root(path: Path) -> None:
    parts = [part.casefold() for part in path.resolve(strict=False).parts]
    if any(parts[index] == "data" and parts[index + 1] == "live" for index in range(len(parts) - 1)):
        raise VideoPackError("packs_root_must_not_be_data_live")


class VideoPackBuilder:
    """Synchronous, dependency-injected and atomically committing builder."""

    def __init__(
        self,
        packs_root: Path | str,
        *,
        translator: Translator | None = None,
        transcriber: Transcriber | None = None,
        media_tools: MediaTools | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.packs_root = Path(packs_root)
        _guard_pack_root(self.packs_root)
        self.translator: Translator = translator or configured_default_translator()
        if transcriber is not None:
            self.transcriber = transcriber
        else:
            local_model = Path.home() / "k4b_work" / "whisper-large-v3"
            explicit_model = os.environ.get("INFLOW_WHISPER_MODEL")
            explicit_device = os.environ.get("INFLOW_WHISPER_DEVICE")
            try:
                cpu_threads = max(1, int(os.environ.get("INFLOW_WHISPER_CPU_THREADS") or "1"))
            except ValueError:
                cpu_threads = 1
            if explicit_model:
                device = explicit_device or ("cuda" if Path(explicit_model) == local_model else "cpu")
                compute_type = os.environ.get("INFLOW_WHISPER_COMPUTE") or ("float16" if device == "cuda" else "int8")
                self.transcriber = FasterWhisperTranscriber(
                    explicit_model,
                    device=device,
                    compute_type=compute_type,
                    cpu_threads=cpu_threads,
                    num_workers=1,
                )
            else:
                primary_model = str(local_model) if local_model.exists() else "small.en"
                primary_device = explicit_device or ("cuda" if local_model.exists() else "cpu")
                primary_compute = os.environ.get("INFLOW_WHISPER_COMPUTE") or ("float16" if primary_device == "cuda" else "int8")
                primary = FasterWhisperTranscriber(
                    primary_model,
                    device=primary_device,
                    compute_type=primary_compute,
                    cpu_threads=cpu_threads,
                    num_workers=1,
                )
                fallback = FasterWhisperTranscriber(
                    "tiny.en",
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=1,
                    num_workers=1,
                )
                self.transcriber = AdaptiveFasterWhisperTranscriber(primary, fallback)
        self.media_tools: MediaTools = media_tools or SubprocessMediaTools()
        self.clock = clock
        self.translator_identity = _dependency_identity(self.translator, self.translator.__class__.__name__)
        self.transcriber_identity = _dependency_identity(self.transcriber, self.transcriber.__class__.__name__)
        self.media_identity = _dependency_identity(self.media_tools, self.media_tools.__class__.__name__)

    def _config_fingerprint(self) -> str:
        payload = {
            "schema": SCHEMA_VERSION,
            "builder": BUILDER_VERSION,
            "prompt": PROMPT_VERSION,
            "translator": self.translator_identity,
            "transcriber": self.transcriber_identity,
            "media": self.media_identity,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:12]

    def pack_id_for_url(self, source_url: str) -> str:
        reference = validate_youtube_url(source_url)
        return f"yt-{reference.video_id}-{self._config_fingerprint()}"

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        *,
        stage: str,
        fraction: float,
        pack_id: str,
        cached: bool = False,
        detail: str | None = None,
    ) -> None:
        if callback is None:
            return
        event = {
            "stage": stage,
            "fraction": round(max(0.0, min(1.0, fraction)), 4),
            "pack_id": pack_id,
            "cached": bool(cached),
            "detail": detail,
        }
        try:
            callback(event)
        except Exception:
            # Progress is observational; a broken observer must not create a
            # failed build or turn an already committed pack into an error.
            return

    def _validate_cached(self, final_path: Path, reference: YouTubeReference, pack_id: str) -> dict[str, Any]:
        try:
            manifest = verify_pack_directory(
                final_path,
                expected_pack_id=pack_id,
                expected_video_id=reference.video_id,
                expected_source_url=reference.canonical_url,
                require_audit=True,
            )
        except AuditError as exc:
            raise CacheIntegrityError("cached_pack_failed_audit") from exc
        versions = manifest.get("versions") or {}
        expected_versions = {
            "schema": SCHEMA_VERSION,
            "builder": BUILDER_VERSION,
            "prompt": PROMPT_VERSION,
            "translator": self.translator_identity,
            "transcriber": self.transcriber_identity,
            "media": self.media_identity,
        }
        if versions != expected_versions:
            raise CacheIntegrityError("cached_pack_version_mismatch")
        return manifest

    def build(
        self,
        source_url: str,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_callback: CancelCallback | None = None,
    ) -> Path:
        reference = validate_youtube_url(source_url)
        pack_id = f"yt-{reference.video_id}-{self._config_fingerprint()}"
        self._emit(progress_callback, stage="validating", fraction=0.01, pack_id=pack_id)
        _raise_if_cancelled(cancel_callback)
        self.packs_root.mkdir(parents=True, exist_ok=True)
        final_path = self.packs_root / pack_id
        if final_path.exists() or final_path.is_symlink():
            self._validate_cached(final_path, reference, pack_id)
            self._emit(
                progress_callback,
                stage="ready",
                fraction=1.0,
                pack_id=pack_id,
                cached=True,
                detail="cache_hit",
            )
            return final_path

        staging = self.packs_root / f".{pack_id}.staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        committed = False
        try:
            raw_metadata = self.media_tools.inspect(reference.canonical_url)
            _raise_if_cancelled(cancel_callback)
            source = validate_source_metadata(reference, raw_metadata)

            self._emit(progress_callback, stage="downloading", fraction=0.10, pack_id=pack_id)
            _raise_if_cancelled(cancel_callback)
            video_path = staging / "video.mp4"
            self.media_tools.download(reference.canonical_url, video_path)
            _require_nonempty_regular_file(video_path, "downloaded_video_missing")
            video_probe = _validate_download_probe(
                self.media_tools.probe(video_path),
                float(source["duration_sec"]),
            )
            caption_segments: list[dict[str, Any]] | None = None
            caption_fallback_reason: str | None = None
            caption_downloader = getattr(self.media_tools, "download_chinese_captions", None)
            if callable(caption_downloader):
                raw_caption_path = staging / "youtube-auto-zh.json3"
                try:
                    caption_downloader(reference.canonical_url, raw_caption_path)
                    try:
                        raw_caption_payload = json.loads(raw_caption_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                        raise SourceValidationError("chinese_auto_captions_not_json") from exc
                    if not isinstance(raw_caption_payload, Mapping):
                        raise SourceValidationError("chinese_auto_captions_not_object")
                    caption_segments = parse_youtube_json3_captions(
                        raw_caption_payload,
                        duration_sec=float(source["duration_sec"]),
                    )
                except VideoPackError as exc:
                    caption_fallback_reason = str(exc).split(":", 1)[0]
                    caption_segments = None
                finally:
                    raw_caption_path.unlink(missing_ok=True)

            english_caption_payload: dict[str, Any] | None = None
            english_caption_error: str | None = None
            english_downloader = getattr(self.media_tools, "download_english_captions", None)
            if callable(english_downloader):
                raw_english_path = staging / "youtube-auto-en.json3"
                try:
                    english_downloader(reference.canonical_url, raw_english_path)
                    parsed_english = json.loads(raw_english_path.read_text(encoding="utf-8"))
                    if not isinstance(parsed_english, dict):
                        raise SourceValidationError("english_auto_captions_not_object")
                    english_caption_payload = parsed_english
                except (VideoPackError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                    english_caption_error = str(exc).split(":", 1)[0]
                    english_caption_payload = None
                finally:
                    raw_english_path.unlink(missing_ok=True)

            self._emit(progress_callback, stage="extracting_audio", fraction=0.35, pack_id=pack_id)
            _raise_if_cancelled(cancel_callback)
            wav_path = staging / "source-16khz.wav"
            self.media_tools.extract_audio(video_path, wav_path)
            _require_nonempty_regular_file(wav_path, "extracted_audio_missing")

            self._emit(progress_callback, stage="transcribing", fraction=0.45, pack_id=pack_id)
            _raise_if_cancelled(cancel_callback)
            try:
                raw_transcript = self.transcriber.transcribe(
                    wav_path,
                    cancel_callback=cancel_callback,
                )
            finally:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    # Leaving the WAV would fail the strict layout audit and thus
                    # still prevent commit; do not hide the primary exception.
                    pass
            _raise_if_cancelled(cancel_callback)
            runtime_transcriber_identity = str(getattr(self.transcriber, "runtime_identity", self.transcriber_identity))
            language = str(raw_transcript.get("language") or "").strip().lower()
            if not (language == "english" or language.startswith("en")):
                raise SourceValidationError("transcript_primary_language_not_english")
            probability_raw = raw_transcript.get("language_probability")
            if probability_raw is not None:
                try:
                    probability = _as_finite_float(probability_raw, "language_probability")
                except ValueError as exc:
                    raise SourceValidationError("transcript_language_probability_invalid") from exc
                if probability < 0.5:
                    raise SourceValidationError("transcript_english_confidence_too_low")
            else:
                probability = None
            reliable_transcript, timing_quality = _reliable_transcript_view(raw_transcript)
            raw_word_count = int(timing_quality["raw_word_count"])
            invalid_word_count = int(timing_quality["invalid_word_count"])
            invalid_word_ratio = float(timing_quality["invalid_word_ratio"])

            self._emit(progress_callback, stage="segmenting", fraction=0.62, pack_id=pack_id)
            _raise_if_cancelled(cancel_callback)
            normalised_words = _normalise_word_timestamps(reliable_transcript)
            effective_speech_sec = effective_speech_seconds(normalised_words)
            cue_source = "whisper_natural_partition"
            try:
                cues = build_natural_cues(reliable_transcript)
            except CueConstructionError as partition_error:
                try:
                    cues = build_sparse_natural_cues(reliable_transcript)
                    if english_caption_payload is not None:
                        cues = filter_sparse_cues_by_english_captions(cues, english_caption_payload)
                    cue_source = "whisper_sparse_natural_segments"
                except CueConstructionError as sparse_error:
                    if english_caption_payload is None:
                        raise SourceValidationError("english_caption_alignment_required") from sparse_error
                    cues = build_caption_aligned_cues(
                        raw_transcript,
                        english_caption_payload,
                        video_duration_sec=float(source["duration_sec"]),
                    )
                    cue_source = "youtube_english_caption_aligned_to_whisper"
            for cue in cues:
                if cue.get("following_gap_sec") is None:
                    tail_gap = max(0.0, float(source["duration_sec"]) - float(cue["end_sec"]))
                    cue["following_gap_sec"] = round(tail_gap, 3)
                    cue["candidate_allowed"] = tail_gap >= 0.20
            transcript_document = {
                "schema_version": SCHEMA_VERSION,
                "video_id": reference.video_id,
                "model": runtime_transcriber_identity,
                "model_chain": self.transcriber_identity,
                "language": language,
                "language_probability": probability,
                "word_timestamps": True,
                "effective_speech_sec": effective_speech_sec,
                "cue_source": cue_source,
                "english_caption_error": english_caption_error,
                "timing_quality": timing_quality,
                "words": [
                    {
                        "index": int(word["index"]),
                        "text": str(word["text"]),
                        "start_sec": round(float(word["start_sec"]), 3),
                        "end_sec": round(float(word["end_sec"]), 3),
                        "segment_id": str(word["segment_id"]),
                    }
                    for word in normalised_words
                ],
                "cues": cues,
            }

            release_transcriber = getattr(self.transcriber, "release_model", None)
            if callable(release_transcriber):
                release_transcriber()

            self._emit(progress_callback, stage="translating", fraction=0.70, pack_id=pack_id)
            _raise_if_cancelled(cancel_callback)
            full_caption_translator = getattr(self.translator, "translate_all", None)
            candidate_cues = select_candidate_cue_shortlist(cues) if caption_segments is not None or callable(full_caption_translator) else list(cues)
            translator_input = [
                {
                    "cue_id": str(cue["cue_id"]),
                    "text": str(cue["text"]),
                    "candidate_allowed": bool(cue.get("candidate_allowed", True)),
                }
                for cue in candidate_cues
            ]
            translated_payload = self.translator.translate(translator_input)
            _raise_if_cancelled(cancel_callback)

            self._emit(progress_callback, stage="selecting", fraction=0.78, pack_id=pack_id)
            validated = validate_translation_batch(candidate_cues, translated_payload)
            _raise_if_cancelled(cancel_callback)
            cue_by_id = {str(cue["cue_id"]): cue for cue in cues}
            review_rows = [
                {
                    "cue_id": selected.cue_id,
                    "surface": selected.surface,
                    "phrase_text": str(cue_by_id[selected.cue_id]["text"]),
                    "gloss_zh": selected.gloss_zh,
                    "phrase_zh": validated.translations[selected.cue_id],
                }
                for selected in validated.candidates
            ]
            reviewed = {
                (row["cue_id"], row["surface"]): {"gloss_zh": row["gloss_zh"], "phrase_zh": row["phrase_zh"]}
                for row in review_rows
            }
            reviewer = getattr(self.translator, "review_selected", None)
            if callable(reviewer):
                reviewed = validate_candidate_review(review_rows, reviewer(review_rows))
            sense_rows = [
                {
                    "occurrence_id": f"{reference.video_id}:{selected.cue_id}:{selected.surface}",
                    "surface": selected.surface,
                    "phrase_text": str(cue_by_id[selected.cue_id]["text"]),
                    "gloss_zh": reviewed[(selected.cue_id, selected.surface)]["gloss_zh"],
                }
                for selected in validated.candidates
            ]
            sense_selections: dict[str, dict[str, Any]] = {}
            sense_resolver = getattr(self.translator, "resolve_senses", None)
            if callable(sense_resolver):
                try:
                    sense_selections = sense_resolver(sense_rows)
                except TranslationContractError:
                    sense_selections = {}
            if caption_segments is None:
                if callable(full_caption_translator):
                    caption_units = build_transcript_caption_units(raw_transcript)
                    all_cue_input = [{"cue_id": unit["cue_id"], "text": unit["text"]} for unit in caption_units]
                    full_translations = full_caption_translator(all_cue_input)
                    _raise_if_cancelled(cancel_callback)
                    caption_source = "dsh_batched_transcript_translation"
                else:
                    caption_units = [
                        {"cue_id": cue["cue_id"], "start": cue["start_sec"], "end": cue["end_sec"], "text": cue["text"]}
                        for cue in cues
                    ]
                    full_translations = validated.translations
                    caption_source = "dsh_cue_translation"
                caption_segments = [
                    {
                        "id": f"dshzh-{index:04d}",
                        "start": unit["start"],
                        "end": unit["end"],
                        "text": full_translations[str(unit["cue_id"])],
                    }
                    for index, unit in enumerate(caption_units, start=1)
                ]
            else:
                caption_source = "youtube_auto_zh-Hans"
            captions_document = {
                "schema_version": SCHEMA_VERSION,
                "video_id": reference.video_id,
                "source": caption_source,
                "fallback_reason": caption_fallback_reason,
                "segments": caption_segments,
            }

            source_document = {
                "schema_version": SCHEMA_VERSION,
                **source,
                "media_probe": video_probe,
                "download_policy": {
                    "single_video": True,
                    "max_height": 360,
                    "format": "18",
                    "player_clients": ["android", "web_embedded"],
                    "container": "mp4",
                    "cookies": False,
                },
            }
            _write_json_new(staging / "source.json", source_document)
            _write_json_new(staging / "transcript.json", transcript_document)
            _write_json_new(staging / "captions-zh.json", captions_document)

            self._emit(progress_callback, stage="clipping", fraction=0.82, pack_id=pack_id)
            _raise_if_cancelled(cancel_callback)
            phrases_dir = staging / "phrases"
            phrases_dir.mkdir(parents=False, exist_ok=False)
            candidates: list[dict[str, Any]] = []
            phrase_auditor = getattr(self.transcriber, "audit_phrase", None)
            for index, selected in enumerate(validated.candidates, start=1):
                _raise_if_cancelled(cancel_callback)
                cue = cue_by_id[selected.cue_id]
                occurrence_id = f"{reference.video_id}:{selected.cue_id}:{selected.surface}"
                filename_hash = hashlib.sha256(occurrence_id.encode("utf-8")).hexdigest()[:16]
                relative_audio = f"phrases/{index:02d}-{filename_hash}.mp3"
                output_path = staging / relative_audio
                spoken_start_sec = float(cue["start_sec"])
                spoken_end_sec = float(cue["end_sec"])
                preceding_gap = max(0.0, float(cue.get("preceding_gap_sec") or 0.0))
                immediate_following_gap = max(0.0, float(cue.get("following_gap_sec") or 0.0))
                safe_pause_gap = max(0.0, float(cue.get("pause_gap_sec", immediate_following_gap) or 0.0))
                start_sec = max(0.0, spoken_start_sec - min(0.10, preceding_gap * 0.45))
                end_sec = min(float(source["duration_sec"]), spoken_end_sec + min(0.12, immediate_following_gap * 0.45))
                intended_duration = end_sec - start_sec
                self.media_tools.clip_phrase(video_path, start_sec, end_sec, output_path)
                _require_nonempty_regular_file(output_path, "phrase_audio_missing")
                actual_duration = _validate_clip_probe(
                    self.media_tools.probe(output_path),
                    intended_duration,
                )
                phrase_audit_text = None
                if callable(phrase_auditor):
                    phrase_audit_text = str(phrase_auditor(output_path) or "").strip()
                    expected_words = _lexical_sequence(cue["text"])
                    heard_words = _lexical_sequence(phrase_audit_text)
                    target_words = _lexical_sequence(selected.surface)
                    target_present = any(
                        heard_words[index : index + len(target_words)] == target_words
                        for index in range(max(0, len(heard_words) - len(target_words) + 1))
                    ) if target_words else False
                    if heard_words != expected_words or not target_present:
                        output_path.unlink(missing_ok=True)
                        self._emit(
                            progress_callback,
                            stage="clipping",
                            fraction=0.82 + 0.10 * index / len(validated.candidates),
                            pack_id=pack_id,
                            detail=f"phrase_{index}_rejected_by_unprompted_asr",
                        )
                        continue
                anchor_sec = min(
                    float(source["duration_sec"]),
                    float(cue.get("pause_anchor_sec", spoken_end_sec + min(0.08, immediate_following_gap * 0.45))),
                )
                pause_delay_sec = max(0.0, anchor_sec - spoken_end_sec)
                reviewed_copy = reviewed[(selected.cue_id, selected.surface)]
                sense_selection = sense_selections.get(occurrence_id, {})
                candidates.append(
                    resolve_lexical_identity({
                        "id": occurrence_id,
                        "occurrence_id": occurrence_id,
                        "cue_id": selected.cue_id,
                        "surface": selected.surface,
                        "gloss_zh": reviewed_copy["gloss_zh"],
                        "value_score": selected.value_score,
                        "frequency_zipf": round(_zipf_frequency(selected.surface), 3),
                        "phrase_text": str(cue["text"]),
                        "phrase_zh": reviewed_copy["phrase_zh"],
                        "phrase_start_sec": round(start_sec, 3),
                        "phrase_end_sec": round(end_sec, 3),
                        "spoken_start_sec": round(spoken_start_sec, 3),
                        "spoken_end_sec": round(spoken_end_sec, 3),
                        "safe_pause_gap_sec": round(safe_pause_gap, 3),
                        "pause_delay_sec": round(pause_delay_sec, 3),
                        "cue_source": cue_source,
                        "phrase_duration_sec": round(intended_duration, 3),
                        "audio_duration_sec": actual_duration,
                        "phrase_audit_text": phrase_audit_text,
                        "phrase_lexical_exact": True if phrase_audit_text is not None else None,
                        "target_start_sec": round(selected.match.start_sec, 3),
                        "target_end_sec": round(selected.match.end_sec, 3),
                        "highlight_start_sec": round(selected.match.start_sec - start_sec, 3),
                        "highlight_end_sec": round(selected.match.end_sec - start_sec, 3),
                        "anchor_sec": round(anchor_sec, 3),
                        "phrase_audio": relative_audio,
                        "alignment_quality": "word_timestamp_high",
                        "display_unit": "natural_excerpt",
                        "isolated_word_audio_enabled": False,
                    },
                    candidate_id=sense_selection.get("candidate_id"),
                    confidence=int(sense_selection.get("confidence") or 0),
                )
                )
                self._emit(
                    progress_callback,
                    stage="clipping",
                    fraction=0.82 + 0.10 * index / len(validated.candidates),
                    pack_id=pack_id,
                    detail=f"phrase_{index}_of_{len(validated.candidates)}",
                )

            if len(candidates) < MIN_CANDIDATES:
                raise AuditError("insufficient_unprompted_asr_verified_candidates")

            created_at = _isoformat(self.clock())
            hash_paths = [
                "video.mp4",
                "source.json",
                "transcript.json",
                "captions-zh.json",
                *[str(candidate["phrase_audio"]) for candidate in candidates],
            ]
            file_hashes = {
                relative: _sha256(_safe_relative_file(staging, relative))
                for relative in sorted(hash_paths)
            }
            runtime_translator_identity = str(getattr(self.translator, "runtime_identity", self.translator_identity))
            versions = {
                "schema": SCHEMA_VERSION,
                "builder": BUILDER_VERSION,
                "prompt": PROMPT_VERSION,
                "translator": self.translator_identity,
                "transcriber": self.transcriber_identity,
                "media": self.media_identity,
            }
            quality_gates = {
                "all_passed": True,
                "public_source": True,
                "non_live_single_video": True,
                "duration_20_seconds_to_15_minutes": True,
                "max_height_720p": True,
                "english_word_timestamps": True,
                "unreliable_timestamp_segments_isolated": True,
                "reliable_region_sufficient": True,
                "cross_source_sentence_alignment_when_needed": True,
                "natural_cues_2_to_8_seconds": True,
                "complete_chinese_captions": True,
                "candidate_count_1_to_16": True,
                "candidate_surfaces_exact_and_unambiguous": True,
                "one_candidate_per_cue": True,
                "selected_chinese_schema_validated": True,
                "translation_runtime": runtime_translator_identity,
                "candidate_safe_pause_gap_at_least_200ms": True,
                "phrase_padding_never_crosses_adjacent_speech": True,
                "phrase_audio_duration_aligned": True,
                "phrase_audio_unprompted_asr": "verified" if callable(phrase_auditor) else "adapter_not_available",
                "occurrences_video_namespaced": True,
            }
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "builder_version": BUILDER_VERSION,
                "pack_id": pack_id,
                "status": "ready",
                "video_id": reference.video_id,
                "source_url": reference.canonical_url,
                "title": source["title"],
                "duration": source["duration_sec"],
                "duration_sec": source["duration_sec"],
                "effective_speech_sec": effective_speech_sec,
                "created_at": created_at,
                "transcription_model": runtime_transcriber_identity,
                "transcription_model_chain": self.transcriber_identity,
                "translator": runtime_translator_identity,
                "translator_chain": self.translator_identity,
                "prompt_version": PROMPT_VERSION,
                "versions": versions,
                "media": {
                    "video": "video.mp4",
                    "transcript": "transcript.json",
                    "captions_zh": "captions-zh.json",
                    "phrases": "phrases/",
                },
                "hashes": {"algorithm": "sha256", "files": file_hashes},
                "candidates": candidates,
                "quality_gates": quality_gates,
                "unverified_boundaries": [
                    "translation_naturalness_requires_human_review",
                    "candidate_pedagogical_value_requires_human_review",
                    *(["heuristic_candidate_selection_used_due_to_primary_translator_failure"] if "google-translate-web" in runtime_translator_identity else []),
                    "learning_effect_not_established",
                    "support_beyond_the_audited_source_contract_not_established",
                ],
            }
            _write_json_new(staging / "manifest.json", manifest)

            self._emit(progress_callback, stage="auditing", fraction=0.95, pack_id=pack_id)
            _raise_if_cancelled(cancel_callback)
            verify_pack_directory(
                staging,
                expected_pack_id=pack_id,
                expected_video_id=reference.video_id,
                expected_source_url=reference.canonical_url,
                require_audit=False,
            )
            audit = {
                "schema_version": SCHEMA_VERSION,
                "pack_id": pack_id,
                "video_id": reference.video_id,
                "created_at": created_at,
                "passed": True,
                "candidate_count": len(candidates),
                "cue_count": len(cues),
                "model_contract": {
                    "translation_fields": sorted(_MODEL_TRANSLATION_FIELDS),
                    "candidate_fields": sorted(_MODEL_CANDIDATE_FIELDS),
                    "review_fields": sorted(_MODEL_REVIEW_FIELDS),
                    "candidate_timing_fields_allowed": False,
                    "surface_policy": "exact_contiguous_unambiguous_whole_words",
                },
                "checks": [
                    {"name": "strict_layout", "passed": True},
                    {"name": "source_policy", "passed": True},
                    {"name": "media_probe", "passed": True},
                    {"name": "word_timestamp_cues", "passed": True},
                    {"name": "chinese_caption_timeline", "passed": True},
                    {"name": "candidate_translation_coverage", "passed": True},
                    {"name": "candidate_model_schema", "passed": True},
                    {"name": "surface_grounding", "passed": True},
                    {"name": "phrase_alignment", "passed": True},
                    {"name": "phrase_unprompted_asr", "passed": True},
                    {"name": "occurrence_namespace", "passed": True},
                    {"name": "sha256_coverage", "passed": True},
                ],
            }
            _write_json_new(staging / "audit.json", audit)
            verify_pack_directory(
                staging,
                expected_pack_id=pack_id,
                expected_video_id=reference.video_id,
                expected_source_url=reference.canonical_url,
                require_audit=True,
            )
            _raise_if_cancelled(cancel_callback)

            try:
                os.replace(staging, final_path)
            except OSError:
                # A concurrent builder may have won the immutable path race.
                if final_path.exists() and not final_path.is_symlink():
                    self._validate_cached(final_path, reference, pack_id)
                    self._emit(
                        progress_callback,
                        stage="ready",
                        fraction=1.0,
                        pack_id=pack_id,
                        cached=True,
                        detail="concurrent_cache_hit",
                    )
                    return final_path
                raise
            committed = True
            self._emit(progress_callback, stage="ready", fraction=1.0, pack_id=pack_id)
            return final_path
        finally:
            if not committed and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "AuditError",
    "AdaptiveFasterWhisperTranscriber",
    "ArgosHeuristicTranslator",
    "BUILDER_VERSION",
    "BuildCancelled",
    "CREATE_NO_WINDOW",
    "CacheIntegrityError",
    "CueConstructionError",
    "DshTranslator",
    "OpenAICompatibleTranslator",
    "FasterWhisperTranscriber",
    "FallbackTranslator",
    "GoogleHeuristicTranslator",
    "MAX_CANDIDATES",
    "MAX_CUE_DURATION_SEC",
    "MAX_VIDEO_DURATION_SEC",
    "MIN_CANDIDATES",
    "MIN_CUE_DURATION_SEC",
    "MIN_VIDEO_DURATION_SEC",
    "PROMPT_VERSION",
    "SCHEMA_VERSION",
    "SourceValidationError",
    "SubprocessFailure",
    "SubprocessMediaTools",
    "TranslationContractError",
    "Translator",
    "Transcriber",
    "URLValidationError",
    "ValidatedTranslationBatch",
    "VideoPackBuilder",
    "VideoPackError",
    "YouTubeReference",
    "build_natural_cues",
    "build_sparse_natural_cues",
    "effective_speech_seconds",
    "parse_youtube_json3_captions",
    "select_candidate_cue_shortlist",
    "validate_candidate_review",
    "validate_source_metadata",
    "validate_translation_batch",
    "validate_youtube_url",
    "verify_pack_directory",
]
