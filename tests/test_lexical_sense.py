from __future__ import annotations

import threading
import unittest

import lexical_sense
from lexical_sense import annotate_lexical_identity, provisional_key, resolve_lexical_identity, sense_candidates, stable_key


class LexicalSenseTests(unittest.TestCase):
    def test_wordnet_lookup_is_safe_after_main_thread_connection_exists(self):
        self.assertEqual(len(sense_candidates("reconnaissance")), 1)
        lexical_sense._sense_candidate_json.cache_clear()
        result = []
        errors = []

        def lookup():
            try:
                result.extend(sense_candidates("collectively"))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=lookup)
        thread.start()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pos"], "r")

    def test_unique_dictionary_sense_becomes_stable_key(self):
        item = annotate_lexical_identity({
            "id": "video-a:cue-1:reconnaissance",
            "surface": "reconnaissance",
            "phrase_text": "They completed a reconnaissance mission.",
        })
        self.assertEqual(item["sense_status"], "resolved")
        self.assertEqual(item["resolution_method"], "dictionary_unique")
        self.assertTrue(stable_key(item).startswith("kv1|en|"))

    def test_ambiguous_word_remains_occurrence_scoped(self):
        first = annotate_lexical_identity({
            "id": "video-a:cue-1:bank",
            "surface": "bank",
            "phrase_text": "They sat by the bank.",
        })
        second = annotate_lexical_identity({
            "id": "video-b:cue-9:bank",
            "surface": "bank",
            "phrase_text": "She called the bank.",
        })
        self.assertEqual(first["sense_status"], "ambiguous")
        self.assertIsNone(stable_key(first))
        self.assertNotEqual(provisional_key(first), provisional_key(second))
        self.assertNotEqual(first["knowledge_key"], second["knowledge_key"])

    def test_constrained_resolution_requires_known_candidate_and_high_confidence(self):
        item = {"id": "video-a:cue-1:bank", "surface": "bank", "phrase_text": "She called the bank about her account."}
        financial = next(row for row in sense_candidates("bank") if "financial institution" in row["definition"])
        low = resolve_lexical_identity(item, candidate_id=financial["candidate_id"], confidence=89)
        unknown = resolve_lexical_identity(item, candidate_id="invented-sense", confidence=100)
        resolved = resolve_lexical_identity(item, candidate_id=financial["candidate_id"], confidence=95)
        self.assertEqual(low["sense_status"], "ambiguous")
        self.assertEqual(unknown["sense_status"], "ambiguous")
        self.assertEqual(resolved["sense_status"], "resolved")
        self.assertEqual(resolved["resolution_method"], "wordnet_constrained_llm")
        self.assertEqual(resolved["sense_id"], financial["sense_id"])
        self.assertTrue(resolved["knowledge_key"].startswith("kv1|en|"))

    def test_gloss_wording_does_not_define_identity(self):
        item = {"id": "video-a:cue-1", "surface": "bank", "phrase_text": "They sat by the bank."}
        translated_differently = {**item, "gloss_zh": "河岸"}
        self.assertEqual(provisional_key(item), provisional_key(translated_differently))


if __name__ == "__main__":
    unittest.main()
