from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any, Mapping

LEXICON_ID = "oewn:2024"
MAPPING_VERSION = "oewn-2024/rules-v1"
_ALLOWED_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


def normalize_surface(value: str) -> str:
    text = str(value or "").casefold().replace("’", "'")
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" .,!?:;\"'()[]{}")


def context_fingerprint(value: str) -> str:
    normalized = " ".join(str(value or "").split()).casefold()
    digest = 0xCBF29CE484222325
    for byte in normalized.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"ctx1:{digest:016x}"


def provisional_key(item: Mapping[str, Any], *, dimension: str = "aural_form_to_sense") -> str:
    identity = {
        "occurrence_id": str(item.get("occurrence_id") or item.get("id") or ""),
        "video_id": str(item.get("video_id") or ""),
        "cue_id": str(item.get("cue_id") or ""),
        "surface": normalize_surface(str(item.get("surface") or "")),
        "context": " ".join(str(item.get("phrase_text") or item.get("sentence") or "").split()),
        "dimension": dimension,
    }
    if not identity["surface"] or not (identity["occurrence_id"] or identity["context"]):
        raise ValueError("provisional_knowledge_identity_missing")
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
    return f"prov1|{digest}|{dimension}"


def stable_key(item: Mapping[str, Any], *, dimension: str = "aural_form_to_sense") -> str | None:
    if str(item.get("sense_status") or "") != "resolved":
        return None
    lexeme_id = str(item.get("lexeme_id") or "")
    pos = str(item.get("pos") or "")
    sense_id = str(item.get("sense_id") or "")
    if not all(_ALLOWED_ID.fullmatch(value) for value in (lexeme_id, pos, sense_id)):
        return None
    return f"kv1|en|{lexeme_id}|{pos}|{sense_id}|{dimension}"


_WORDNET_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inflow-wordnet")
_WORDNET_INSTANCE: Any = ...


def _query_wordnet(normalized: str, limit: int) -> tuple[str, ...]:
    global _WORDNET_INSTANCE
    if _WORDNET_INSTANCE is ...:
        try:
            import wn  # type: ignore
            from wn.morphy import Morphy  # type: ignore

            wordnet = wn.Wordnet(LEXICON_ID)
            wordnet.lemmatizer = Morphy(wordnet)
            _WORDNET_INSTANCE = wordnet
        except Exception:
            _WORDNET_INSTANCE = None
    wordnet = _WORDNET_INSTANCE
    if wordnet is None or not normalized:
        return ()
    output = []
    seen = set()
    try:
        words = wordnet.words(normalized)
    except Exception:
        return ()
    for word in words:
        try:
            senses = word.senses()
        except Exception:
            continue
        for sense in senses:
            try:
                synset = sense.synset()
                row = {
                    "candidate_id": str(sense.id),
                    "lexeme_id": str(word.id),
                    "lemma": str(word.lemma()),
                    "pos": str(synset.pos),
                    "sense_id": str(sense.id),
                    "synset_id": str(synset.id),
                    "definition": str(synset.definition() or ""),
                    "examples": [str(value) for value in (synset.examples() or [])[:2]],
                }
            except Exception:
                continue
            if row["candidate_id"] in seen or not all(_ALLOWED_ID.fullmatch(row[key]) for key in ("lexeme_id", "pos", "sense_id")):
                continue
            seen.add(row["candidate_id"])
            output.append(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            if len(output) >= limit:
                return tuple(output)
    return tuple(output)


@lru_cache(maxsize=4096)
def _sense_candidate_json(normalized: str, limit: int) -> tuple[str, ...]:
    return _WORDNET_EXECUTOR.submit(_query_wordnet, normalized, limit).result(timeout=30)


def warm_wordnet_async():
    return _WORDNET_EXECUTOR.submit(_query_wordnet, "reconnaissance", 1)


def sense_candidates(surface: str, *, limit: int = 16) -> list[dict[str, Any]]:
    normalized = normalize_surface(surface)
    bounded_limit = max(1, min(64, int(limit)))
    return [json.loads(row) for row in _sense_candidate_json(normalized, bounded_limit)]


def resolve_lexical_identity(item: Mapping[str, Any], *, candidate_id: str | None, confidence: int) -> dict[str, Any]:
    provisional = annotate_lexical_identity(item)
    if provisional.get("sense_status") == "resolved":
        return provisional
    candidates = sense_candidates(str(provisional.get("surface") or ""))
    selected = next((row for row in candidates if row["candidate_id"] == str(candidate_id or "")), None)
    if selected is None or int(confidence) < 90 or int(confidence) > 100:
        return provisional
    provisional.update(
        lexeme_id=selected["lexeme_id"],
        lemma=selected["lemma"],
        pos=selected["pos"],
        sense_id=selected["sense_id"],
        synset_id=selected["synset_id"],
        sense_status="resolved",
        resolution_method="wordnet_constrained_llm",
        sense_confidence=int(confidence),
        sense_candidate_ids=[row["candidate_id"] for row in candidates],
    )
    provisional["knowledge_key"] = stable_key(provisional)
    return provisional


def annotate_lexical_identity(item: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(item)
    context = str(row.get("phrase_text") or row.get("sentence") or "")
    if context:
        row.setdefault("context_fingerprint", context_fingerprint(context))
    if stable_key(row):
        row.setdefault("mapping_version", MAPPING_VERSION)
        row.setdefault("sense_candidate_ids", [row["sense_id"]])
        return row
    candidates = sense_candidates(str(row.get("surface") or ""))
    row["mapping_version"] = MAPPING_VERSION
    row["sense_candidate_ids"] = [candidate["candidate_id"] for candidate in candidates]
    if len(candidates) == 1:
        selected = candidates[0]
        row.update(
            lexeme_id=selected["lexeme_id"],
            lemma=selected["lemma"],
            pos=selected["pos"],
            sense_id=selected["sense_id"],
            synset_id=selected["synset_id"],
            sense_status="resolved",
            resolution_method="dictionary_unique",
            sense_confidence=100,
        )
    else:
        row.update(
            lemma=normalize_surface(str(row.get("surface") or "")),
            pos=None,
            lexeme_id=None,
            sense_id=None,
            synset_id=None,
            sense_status="ambiguous" if candidates else "provisional",
            resolution_method="provisional",
            sense_confidence=0,
        )
    row["knowledge_key"] = stable_key(row) or provisional_key(row)
    return row
