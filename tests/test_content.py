from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ContentIntegrityTests(unittest.TestCase):
    def test_natural_phrase_only_and_alignment_bounds(self):
        content = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))
        self.assertEqual(len(content["items"]), 16)
        qualities = {"word_timestamp_high": 0, "manifest_fallback_wide": 0}
        for item in content["items"]:
            self.assertFalse(item["isolated_word_audio_enabled"])
            self.assertLessEqual(0, item["highlight_start_sec"])
            self.assertLess(item["highlight_start_sec"], item["highlight_end_sec"])
            self.assertLessEqual(item["highlight_end_sec"], item["phrase_duration_sec"])
            self.assertIn(item["surface"].lower(), item["phrase_text"].lower())
            self.assertTrue(item["phrase_zh"].strip())
            self.assertEqual(item["display_unit"], "natural_excerpt")
            qualities[item["alignment_quality"]] += 1
        self.assertEqual(qualities, {"word_timestamp_high": 14, "manifest_fallback_wide": 2})

    def test_phrase_text_translation_and_audio_are_locked_together(self):
        content = {item["id"]: item for item in json.loads((ROOT / "content.json").read_text(encoding="utf-8"))["items"]}
        audit = json.loads((ROOT / "data" / "phrase-material-audit.json").read_text(encoding="utf-8"))
        self.assertEqual(len(audit), 16)
        for row in audit:
            item = content[row["id"]]
            audio_path = ROOT.parent / "mechanism-experiment" / item["phrase_audio"]
            self.assertTrue(row["display_exact_asr"], row["id"])
            self.assertTrue(row["target_sequence_match"], row["id"])
            self.assertLessEqual(abs(row["duration_delta_ms"]), 80)
            self.assertEqual(row["display_text"], item["phrase_text"])
            self.assertEqual(row["display_zh"], item["phrase_zh"])
            self.assertEqual(hashlib.sha256(audio_path.read_bytes()).hexdigest(), row["sha256"])

    def test_pause_anchors_follow_sentence_boundaries(self):
        content = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))
        stimuli = json.loads((ROOT.parent / "mechanism-experiment" / "stimuli.json").read_text(encoding="utf-8"))
        source_offset = float(content["source_offset_sec"])
        expected = {
            item["id"]: round(float(item["sentence_end_source_sec"]) - source_offset + 0.06, 3)
            for item in stimuli["items"]
        }
        for item in content["items"]:
            self.assertAlmostEqual(float(item["anchor_sec"]), expected[item["id"]], places=2)

    def test_followup_probe_material_is_complete_and_audited(self):
        content = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))
        audit_path = ROOT / "data" / "probe-material-audit.json"
        audit = {row["id"]: row for row in json.loads(audit_path.read_text(encoding="utf-8"))}
        variants = set()
        qc_counts = {"weak_pilot": 0, "needs_revision": 0, "excluded_primary_measure": 0}
        for item in content["items"]:
            self.assertGreaterEqual(len(item["accepted_zh"]), 2)
            self.assertEqual(len(item["probe_distractors_zh"]), 2)
            self.assertEqual(len(set(item["probe_distractors_zh"])), 2)
            self.assertNotIn(item["gloss_zh"], item["probe_distractors_zh"])
            self.assertIn(item["surface"].lower(), item["followup_sentence"].lower())
            self.assertTrue(item["followup_audio"].startswith("media/followup/"))
            audio_path = ROOT.parent / "mechanism-experiment" / item["followup_audio"]
            self.assertTrue(audio_path.is_file(), audio_path)
            self.assertGreater(audio_path.stat().st_size, 1000)
            self.assertEqual(hashlib.sha256(audio_path.read_bytes()).hexdigest(), audit[item["id"]]["sha256"])
            self.assertTrue(audit[item["id"]]["target_sequence_match"])
            self.assertGreaterEqual(audit[item["id"]]["duration_sec"], 0.5)
            self.assertLessEqual(audit[item["id"]]["duration_sec"], 12)
            self.assertNotIn(item["probe_variant_id"], variants)
            variants.add(item["probe_variant_id"])
            self.assertFalse(item["eligible_for_primary_measure"])
            qc_counts[item["probe_qc"]] += 1
            self.assertEqual(item["probe_eligible"], item["probe_qc"] == "weak_pilot")
        self.assertEqual(len(variants), 16)
        self.assertEqual(qc_counts, {"weak_pilot": 8, "needs_revision": 5, "excluded_primary_measure": 3})
        thermal = next(item for item in content["items"] if item["id"] == "thermal-signatures")
        self.assertEqual(thermal["phrase_text"], "where the thermal signatures indicate they are unsafe landing sites.")
        self.assertEqual(thermal["phrase_audio"], "media/phrases/thermal-signatures-clause.mp3")
        self.assertEqual(thermal["phrase_duration_sec"], 4.64)

    def test_alignment_audit_found_every_target(self):
        audit = json.loads((ROOT / "data" / "alignment-audit.json").read_text(encoding="utf-8"))
        self.assertEqual(len(audit), 16)
        self.assertTrue(all(row["matched"] for row in audit))
        self.assertEqual(sum(row["safe_for_isolated_cut"] for row in audit), 0)
        self.assertLessEqual(max(abs(row["boundary_delta_start_ms"]) for row in audit), 120)
        self.assertLessEqual(max(abs(row["boundary_delta_end_ms"]) for row in audit), 140)


if __name__ == "__main__":
    unittest.main()
