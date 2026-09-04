from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

from adaptive_core import (
    apply_interaction,
    apply_lexicon_feedback,
    apply_probe_result,
    build_delayed_probe,
    choose_nested_candidates,
    complete_session_adaptation,
    effective_intensity,
    eligible_candidate_count,
    ensure_profile,
    estimate_vocabulary_frontier,
    intensity_allows,
    intervention_budgets,
    item_priority,
    knowledge_key_for_item,
    mapping_hold_ms,
    new_profile,
    new_session,
    parse_time,
    rebuild_profile_from_events,
    set_explicit_intensity,
    utc_now,
)

ROOT = Path(__file__).resolve().parents[1]
CONTENT = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))


class AdaptiveCoreTests(unittest.TestCase):
    def test_low_medium_high_are_nested_budgets(self):
        profile = new_profile(CONTENT, {"detailed"})
        rows = choose_nested_candidates(CONTENT, profile, seed=42)
        self.assertEqual(len(rows), 4)
        counts = {
            intensity: sum(intensity_allows(intensity, row["min_intensity"]) for row in rows)
            for intensity in ("low", "medium", "high")
        }
        self.assertEqual(counts, {"low": 1, "medium": 2, "high": 4})
        self.assertNotIn("detailed", {row["id"] for row in rows})

    def test_frequency_budgets_scale_with_effective_speech_and_candidate_pool(self):
        self.assertEqual(intervention_budgets(50, 16), {"low": 1, "medium": 1, "high": 1})
        self.assertEqual(intervention_budgets(274.8, 16), {"low": 1, "medium": 2, "high": 4})
        self.assertEqual(intervention_budgets(583.8, 16), {"low": 2, "medium": 4, "high": 7})
        self.assertEqual(intervention_budgets(900, 16), {"low": 3, "medium": 5, "high": 10})
        self.assertEqual(intervention_budgets(900, 4), {"low": 3, "medium": 4, "high": 4})
        self.assertEqual(intervention_budgets(900, 0), {"low": 0, "medium": 0, "high": 0})

    def test_dynamic_frequency_selection_stays_nested(self):
        content = deepcopy(CONTENT)
        content["effective_speech_sec"] = 583.8
        profile = new_profile(content, {"detailed"})
        rows = choose_nested_candidates(content, profile, seed=42)
        counts = {
            frequency: sum(intensity_allows(frequency, row["min_intensity"]) for row in rows)
            for frequency in ("low", "medium", "high")
        }
        self.assertEqual(counts, {"low": 2, "medium": 4, "high": 7})
        low_ids = {row["id"] for row in rows if intensity_allows("low", row["min_intensity"])}
        medium_ids = {row["id"] for row in rows if intensity_allows("medium", row["min_intensity"])}
        high_ids = {row["id"] for row in rows}
        self.assertLessEqual(low_ids, medium_ids)
        self.assertLessEqual(medium_ids, high_ids)

    def test_resolved_sense_key_is_stable_and_provisional_key_is_occurrence_scoped(self):
        resolved = {
            "surface": "reconnaissance",
            "gloss_zh": "侦察",
            "sense_status": "resolved",
            "lexeme_id": "oewn-reconnaissance-w1",
            "pos": "n",
            "sense_id": "oewn-reconnaissance__1.04.01..",
        }
        same_sense = {**resolved, "surface": "Reconnaissance", "gloss_zh": "勘察"}
        other_sense = {**resolved, "sense_id": "oewn-reconnaissance__1.04.99.."}
        self.assertEqual(knowledge_key_for_item(resolved), knowledge_key_for_item(same_sense))
        self.assertNotEqual(knowledge_key_for_item(resolved), knowledge_key_for_item(other_sense))
        first_occurrence = {"id": "video-a:cue-1", "surface": "bank", "phrase_text": "They sat by the bank."}
        second_occurrence = {"id": "video-b:cue-8", "surface": "bank", "phrase_text": "She called the bank."}
        self.assertNotEqual(knowledge_key_for_item(first_occurrence), knowledge_key_for_item(second_occurrence))

    def test_mapping_known_suppresses_stable_sense_without_creating_override(self):
        first = deepcopy(CONTENT["items"][0])
        second = deepcopy(first)
        first["id"] = "same-a"
        second["id"] = "same-b"
        for item in (first, second):
            item.update(
                sense_status="resolved",
                lexeme_id="oewn-reconnaissance-w1",
                pos="n",
                sense_id="oewn-reconnaissance__1.04.01..",
            )
        content = {"items": [first, second]}
        profile = new_profile(content)
        key = knowledge_key_for_item(first)
        result = apply_lexicon_feedback(
            profile,
            knowledge_key=key,
            surface=first["surface"],
            gloss_zh=first["gloss_zh"],
            feedback="known",
            source="mapping",
        )
        self.assertFalse(result["lexicon"][key]["explicit_known"])
        self.assertEqual(result["lexicon"][key]["evidence"][-1]["kind"], "SELF_REPORT_AFTER_ANSWER")
        for item in content["items"]:
            state = result["items"][item["id"]]
            self.assertEqual(state["self_report_status"], "known")
            self.assertEqual(item_priority(item, state, utc_now()), -1000)
        self.assertEqual(eligible_candidate_count(content, result), 0)
        overridden = apply_lexicon_feedback(
            result,
            knowledge_key=key,
            surface=first["surface"],
            gloss_zh=first["gloss_zh"],
            feedback="known",
            source="subtitle",
        )
        self.assertTrue(overridden["lexicon"][key]["explicit_known"])
        self.assertEqual(overridden["lexicon"][key]["evidence"][-1]["kind"], "EXPLICIT_STATE_EDIT")
        opted_out = apply_lexicon_feedback(
            result,
            knowledge_key=key,
            surface=first["surface"],
            gloss_zh=first["gloss_zh"],
            feedback="known",
            source="explicit_no_more_explanations",
        )
        self.assertTrue(opted_out["lexicon"][key]["explicit_known"])
        self.assertEqual(opted_out["lexicon"][key]["evidence"][-1]["kind"], "EXPLICIT_KNOWN_OVERRIDE")
        self.assertEqual(opted_out["lexicon"][key]["evidence"][-1]["intent"], "no_more_explanations")
        reopened = apply_lexicon_feedback(
            overridden,
            knowledge_key=key,
            surface=first["surface"],
            gloss_zh=first["gloss_zh"],
            feedback="familiar",
            source="subtitle",
        )
        self.assertFalse(reopened["lexicon"][key]["explicit_known"])
        self.assertEqual(reopened["lexicon"][key]["status"], "familiar")
        due = parse_time(reopened["items"][first["id"]]["next_window_start"]) + timedelta(seconds=1)
        self.assertGreater(item_priority(first, reopened["items"][first["id"]], due), -1000)

    def test_schema_two_lexicon_is_archived_and_occurrence_status_is_preserved(self):
        item = deepcopy(CONTENT["items"][0])
        content = {"items": [item]}
        profile = new_profile(content)
        old_key = f"v1:{item['surface'].casefold()}::{item['gloss_zh']}"
        profile["schema_version"] = 2
        profile["policy_version"] = "adaptive-v4"
        profile["reducer_version"] = "rules-v3"
        profile["lexicon"] = {
            old_key: {
                "knowledge_key": old_key,
                "surface": item["surface"],
                "gloss_zh": item["gloss_zh"],
                "status": "familiar",
                "explicit_known": False,
                "score": 0.35,
                "occurrence_ids": [item["id"]],
            }
        }
        profile["items"][item["id"]]["knowledge_key"] = old_key
        profile["items"][item["id"]]["self_report_status"] = "familiar"
        migrated = ensure_profile(profile, content)
        new_key = knowledge_key_for_item(item)
        self.assertEqual(migrated["schema_version"], 3)
        self.assertIn(old_key, migrated["legacy_lexicon_v1"])
        self.assertNotIn(old_key, migrated["lexicon"])
        self.assertEqual(migrated["lexicon"][new_key]["status"], "familiar")
        self.assertEqual(migrated["items"][item["id"]]["knowledge_key"], new_key)

    def test_vocabulary_frontier_starts_only_after_four_explicit_signals(self):
        profile = new_profile({"items": []})
        profile["lexicon"] = {
            "known-a": {"status": "known", "frequency_zipf": 4.8},
            "known-b": {"status": "known", "frequency_zipf": 4.2},
            "unclear-a": {"status": "unclear", "frequency_zipf": 3.4},
        }
        self.assertIsNone(estimate_vocabulary_frontier(profile))
        profile["lexicon"]["unclear-b"] = {"status": "unclear", "frequency_zipf": 3.8}
        self.assertEqual(estimate_vocabulary_frontier(profile), 4.0)
        state = {"aural_stage": "unknown", "self_report_status": "unseen", "confidence": "low", "teach_count": 0, "replay_count": 0}
        near = {"surface": "candidate", "frequency_zipf": 4.0, "value_score": 3, "anchor_sec": 1}
        too_common = {"surface": "understand", "frequency_zipf": 5.6, "value_score": 3, "anchor_sec": 1}
        self.assertGreater(item_priority(near, state, utc_now(), vocabulary_frontier=4.0), item_priority(too_common, state, utc_now(), vocabulary_frontier=4.0))

    def test_familiar_and_unclear_use_different_windows_without_claiming_clean_evidence(self):
        item = deepcopy(CONTENT["items"][0])
        content = {"items": [item]}
        now = utc_now()
        profile = new_profile(content, now=now)
        key = knowledge_key_for_item(item)
        familiar = apply_lexicon_feedback(profile, knowledge_key=key, surface=item["surface"], gloss_zh=item["gloss_zh"], feedback="familiar", now=now)
        unclear = apply_lexicon_feedback(profile, knowledge_key=key, surface=item["surface"], gloss_zh=item["gloss_zh"], feedback="unclear", now=now)
        familiar_start = parse_time(familiar["items"][item["id"]]["next_window_start"])
        unclear_start = parse_time(unclear["items"][item["id"]]["next_window_start"])
        self.assertGreater(familiar_start, unclear_start)
        self.assertEqual(familiar["lexicon"][key]["status"], "familiar")
        self.assertEqual(unclear["lexicon"][key]["status"], "unclear")
        self.assertFalse(familiar["lexicon"][key]["evidence"][-1]["clean_evidence"])
        self.assertTrue(familiar["lexicon"][key]["evidence"][-1]["answer_visible"])

    def test_replay_without_familiarity_feedback_does_not_choose_a_status(self):
        profile = new_profile(CONTENT)
        result = apply_interaction(profile, "refine", outcome="completed", dwell_ms=3000, phrase_confirmed=True, replays=3)
        key = result["items"]["refine"]["knowledge_key"]
        self.assertEqual(result["lexicon"][key]["status"], "unseen")
        self.assertEqual(result["lexicon"][key]["replay_count"], 3)
        self.assertEqual(result["items"]["refine"]["replay_count"], 3)
        self.assertEqual(result["items"]["refine"]["aural_stage"], "taught")

    def test_mapping_feedback_updates_status_but_remains_self_report(self):
        profile = new_profile(CONTENT)
        result = apply_interaction(profile, "refine", outcome="completed", dwell_ms=3000, phrase_confirmed=True, replays=1, familiarity_feedback="known")
        key = result["items"]["refine"]["knowledge_key"]
        self.assertEqual(result["lexicon"][key]["status"], "known")
        self.assertEqual(result["lexicon"][key]["replay_count"], 1)
        self.assertEqual(result["lexicon"][key]["evidence"][-1]["replays"], 1)
        self.assertEqual(result["items"]["refine"]["replay_count"], 1)
        self.assertEqual(result["items"]["refine"]["confidence"], "self_report")
        self.assertEqual(result["items"]["refine"]["evidence"][-1]["kind"], "TEACH")
        self.assertTrue(result["items"]["refine"]["evidence"][-1]["self_report_after_answer"])

    def test_mapping_hold_has_readable_floor_and_personal_memory(self):
        profile = new_profile(CONTENT)
        self.assertGreaterEqual(mapping_hold_ms(profile, "refine", "细化；改进"), 4000)
        profile["reading"]["mapping_dwell_ms"] = 9000
        self.assertEqual(mapping_hold_ms(profile, "refine", "细化；改进"), 9000)

    def test_completed_teach_updates_item_and_reading_pace(self):
        profile = new_profile(CONTENT)
        now = utc_now()
        result = apply_interaction(
            profile,
            "refine",
            outcome="completed",
            dwell_ms=7800,
            phrase_confirmed=True,
            replays=1,
            now=now,
        )
        state = result["items"]["refine"]
        self.assertEqual(state["aural_stage"], "taught")
        self.assertEqual(state["teach_count"], 1)
        self.assertEqual(result["reading"]["mapping_dwell_ms"], 7800)
        self.assertGreater(state["next_window_start"], state["last_taught_at"])

    def test_delayed_probe_only_exists_inside_window_and_is_not_reused(self):
        taught_at = utc_now()
        profile = apply_interaction(
            new_profile(CONTENT),
            "refine",
            outcome="completed",
            dwell_ms=6000,
            phrase_confirmed=True,
            replays=0,
            now=taught_at,
        )
        self.assertIsNone(build_delayed_probe(CONTENT, profile, seed=7, now=taught_at + timedelta(hours=19)))
        probe = build_delayed_probe(CONTENT, profile, seed=7, now=taught_at + timedelta(hours=21))
        self.assertIsNotNone(probe)
        self.assertEqual(probe["item_id"], "refine")
        self.assertEqual(len(probe["choices"]), 3)
        self.assertEqual(len({row["label"] for row in probe["choices"]}), 3)
        self.assertIsNone(build_delayed_probe(CONTENT, profile, seed=7, now=taught_at + timedelta(hours=73)))
        used = apply_probe_result(
            profile,
            "refine",
            variant_id=probe["variant_id"],
            outcome="correct",
            audio_confirmed=True,
            choice_count=3,
            response_ms=2400,
            delay_hours=21,
            now=taught_at + timedelta(hours=21),
        )
        self.assertIsNone(build_delayed_probe(CONTENT, used, seed=7, now=taught_at + timedelta(hours=22)))

    def test_probe_qc_gate_blocks_unfit_material_even_when_due(self):
        now = utc_now()
        profile = apply_interaction(
            new_profile(CONTENT),
            "hazardous",
            outcome="completed",
            dwell_ms=6000,
            phrase_confirmed=True,
            replays=0,
            now=now - timedelta(hours=24),
        )
        self.assertIsNone(build_delayed_probe(CONTENT, profile, seed=7, now=now))

    def test_probe_correct_is_weak_evidence_not_mastery(self):
        now = utc_now()
        profile = apply_interaction(
            new_profile(CONTENT),
            "refine",
            outcome="completed",
            dwell_ms=6000,
            phrase_confirmed=True,
            replays=0,
            now=now - timedelta(hours=24),
        )
        result = apply_probe_result(
            profile,
            "refine",
            variant_id="refine:followup-sentence-v1",
            outcome="correct",
            audio_confirmed=True,
            choice_count=3,
            response_ms=1800,
            delay_hours=24,
            now=now,
        )
        state = result["items"]["refine"]
        self.assertEqual(state["aural_stage"], "taught")
        self.assertEqual(state["recognition_success_count"], 1)
        self.assertEqual(state["clean_success_count"], 0)
        self.assertEqual(state["evidence"][-1]["evidence_quality"], "WEAK_SUCCESS")

    def test_replayed_probe_audio_is_assisted_practice_not_recognition_success(self):
        now = utc_now()
        profile = apply_interaction(
            new_profile(CONTENT),
            "refine",
            outcome="completed",
            dwell_ms=6000,
            phrase_confirmed=True,
            replays=0,
            now=now - timedelta(hours=24),
        )
        result = apply_probe_result(
            profile,
            "refine",
            variant_id="refine:followup-sentence-v1",
            outcome="correct",
            audio_confirmed=True,
            choice_count=3,
            response_ms=1800,
            delay_hours=24,
            presentation_count=2,
            now=now,
        )
        state = result["items"]["refine"]
        self.assertEqual(state["recognition_success_count"], 0)
        self.assertEqual(state["recognition_failure_count"], 0)
        self.assertEqual(state["assisted_probe_count"], 1)
        self.assertEqual(state["evidence"][-1]["evidence_quality"], "ASSISTED_PRACTICE")
        self.assertFalse(state["evidence"][-1]["first_presentation"])

    def test_probe_skip_and_technical_failure_leave_profile_unchanged(self):
        profile = new_profile(CONTENT)
        original = deepcopy(profile)
        for outcome in ("skipped", "technical_failure"):
            profile = apply_probe_result(
                profile,
                "refine",
                variant_id="refine:followup-sentence-v1",
                outcome=outcome,
                audio_confirmed=False,
                choice_count=3,
                response_ms=0,
            )
        self.assertEqual(profile, original)

    def test_new_session_schedules_one_probe_and_excludes_it_from_teaching(self):
        now = utc_now()
        profile = apply_interaction(
            new_profile(CONTENT),
            "refine",
            outcome="completed",
            dwell_ms=6000,
            phrase_confirmed=True,
            replays=0,
            now=now - timedelta(hours=24),
        )
        session = new_session(CONTENT, profile, now=now)
        self.assertEqual(session["stage"], "probe_ready")
        self.assertEqual(session["probe"]["item_id"], "refine")
        self.assertNotIn("refine", {row["id"] for row in session["items"]})
        teaching_counts = {
            intensity: sum(intensity_allows(intensity, row["min_intensity"]) for row in session["items"])
            for intensity in ("low", "medium", "high")
        }
        self.assertEqual(teaching_counts, {"low": 0, "medium": 1, "high": 3})
        total_interventions = {intensity: count + 1 for intensity, count in teaching_counts.items()}
        self.assertEqual(total_interventions, {"low": 1, "medium": 2, "high": 4})

    def test_single_skip_never_changes_long_term_intensity(self):
        profile = new_profile(CONTENT)
        session = {
            "session_id": "a" * 32,
            "interactions": {
                "refine": {"outcome": "skipped", "technical_failure": False},
            },
        }
        result = complete_session_adaptation(profile, session)
        self.assertEqual(result["adaptive"]["reduction"], 0)
        self.assertEqual(effective_intensity(result), "medium")

    def test_session_adaptation_uses_only_current_preference_epoch(self):
        profile = new_profile(CONTENT)
        old_epoch = profile["adaptive"]["preference_epoch"]
        profile = set_explicit_intensity(profile, "high")
        current_epoch = profile["adaptive"]["preference_epoch"]
        session = {
            "session_id": "d" * 32,
            "preference_epoch_at_start": old_epoch,
            "interactions": {
                "old-skip": {"outcome": "skipped", "technical_failure": False, "preference_epoch": old_epoch},
                "new-complete": {"outcome": "completed", "technical_failure": False, "preference_epoch": current_epoch},
            },
        }
        profile = complete_session_adaptation(profile, session)
        self.assertEqual(profile["adaptive"]["epoch_comparable_opportunities"], 1)
        self.assertEqual(profile["adaptive"]["session_history"][-1]["skipped"], 0)
        self.assertEqual(profile["adaptive"]["session_history"][-1]["completed"], 1)

    def test_old_sessions_cannot_downshift_a_new_explicit_epoch(self):
        now = utc_now()
        profile = new_profile(CONTENT)
        for index in range(2):
            session = {
                "session_id": f"{index + 100:032x}",
                "interactions": {
                    f"old-{index}-{item}": {"outcome": "skipped", "technical_failure": False}
                    for item in range(4)
                },
            }
            profile = complete_session_adaptation(profile, session, now=now - timedelta(days=12 - index))
        profile = set_explicit_intensity(profile, "high", now=now - timedelta(days=8))
        new_session = {
            "session_id": "e" * 32,
            "interactions": {
                f"new-{item}": {"outcome": "skipped", "technical_failure": False}
                for item in range(4)
            },
        }
        profile = complete_session_adaptation(profile, new_session, now=now)
        self.assertEqual(profile["adaptive"]["completed_sessions"], 3)
        self.assertEqual(profile["adaptive"]["comparable_opportunities"], 12)
        self.assertEqual(profile["adaptive"]["epoch_completed_sessions"], 1)
        self.assertEqual(profile["adaptive"]["epoch_comparable_opportunities"], 4)
        self.assertEqual(profile["adaptive"]["reduction"], 0)
        self.assertEqual(effective_intensity(profile), "high")

    def test_three_session_trend_can_only_reduce_one_level(self):
        now = utc_now()
        profile = set_explicit_intensity(new_profile(CONTENT), "high", now=now - timedelta(days=8))
        for index in range(3):
            session = {
                "session_id": f"{index + 1:032x}",
                "interactions": {
                    f"item-{index}-{item}": {
                        "outcome": "skipped" if item < 3 else "completed",
                        "technical_failure": False,
                    }
                    for item in range(4)
                },
            }
            profile = complete_session_adaptation(profile, session, now=now + timedelta(minutes=index))
        self.assertEqual(profile["adaptive"]["completed_sessions"], 3)
        self.assertEqual(profile["adaptive"]["comparable_opportunities"], 12)
        self.assertEqual(profile["adaptive"]["reduction"], -1)
        self.assertEqual(effective_intensity(profile), "medium")

    def test_technical_failures_do_not_train_preference(self):
        profile = new_profile(CONTENT)
        for index in range(4):
            session = {
                "session_id": f"{index + 20:032x}",
                "interactions": {
                    f"technical-{index}-{item}": {
                        "outcome": "technical_failure",
                        "technical_failure": True,
                    }
                    for item in range(6)
                },
            }
            profile = complete_session_adaptation(profile, session)
        self.assertEqual(profile["adaptive"]["comparable_opportunities"], 0)
        self.assertEqual(profile["adaptive"]["reduction"], 0)

    def test_recent_teach_is_silent_until_natural_window_opens(self):
        now = utc_now()
        profile = apply_interaction(
            new_profile(CONTENT),
            "refine",
            outcome="completed",
            dwell_ms=6000,
            phrase_confirmed=True,
            replays=0,
            now=now,
        )
        item = next(row for row in CONTENT["items"] if row["id"] == "refine")
        state = profile["items"]["refine"]
        self.assertLess(item_priority(item, state, now + timedelta(hours=1)), -100)
        inside_window = item_priority(item, state, now + timedelta(hours=21))
        after_window = item_priority(item, state, now + timedelta(days=30))
        self.assertGreater(inside_window, 0)
        self.assertLess(after_window, inside_window)

    def test_event_ledger_rebuilds_persistent_profile(self):
        now = utc_now()
        session_id = "f" * 32
        events = [
            {
                "event_id": "explicit-1",
                "type": "explicit_intensity_changed",
                "at": (now - timedelta(days=1)).isoformat(),
                "session_id": None,
                "data": {"intensity": "high"},
            },
            {
                "event_id": "session-create-1",
                "type": "session_created",
                "at": (now - timedelta(hours=1)).isoformat(),
                "session_id": session_id,
                "data": {"preference_epoch": 2},
            },
            {
                "event_id": "interaction-1",
                "type": "interaction_completed",
                "at": now.isoformat(),
                "session_id": session_id,
                "data": {
                    "item_id": "refine",
                    "outcome": "completed",
                    "dwell_ms": 6800,
                    "phrase_confirmed": True,
                    "replays": 1,
                    "preference_epoch": 2,
                },
            },
            {
                "event_id": "session-complete-1",
                "type": "session_completed",
                "at": (now + timedelta(minutes=1)).isoformat(),
                "session_id": session_id,
                "data": {},
            },
        ]
        events.extend([deepcopy(events[2]), deepcopy(events[3])])
        rebuilt = rebuild_profile_from_events(CONTENT, events, known_ids={"detailed"})
        self.assertEqual(rebuilt["explicit_intensity"], "high")
        self.assertEqual(rebuilt["items"]["refine"]["teach_count"], 1)
        self.assertEqual(rebuilt["reading"]["mapping_dwell_ms"], 6800)
        self.assertEqual(rebuilt["adaptive"]["completed_sessions"], 1)
        self.assertEqual(rebuilt["adaptive"]["comparable_opportunities"], 1)
        self.assertIsNone(rebuilt["active_session_id"])

    def test_rebuild_preserves_historical_event_policy_version(self):
        now = utc_now()
        rebuilt = rebuild_profile_from_events(
            CONTENT,
            [
                {
                    "event_id": "old-encounter",
                    "type": "encounter_recorded",
                    "policy_version": "adaptive-v1",
                    "at": (now - timedelta(minutes=2)).isoformat(),
                    "session_id": "9" * 32,
                    "data": {"item_id": "refine"},
                },
                {
                    "event_id": "old-teach",
                    "type": "interaction_completed",
                    "policy_version": "adaptive-v1",
                    "at": (now - timedelta(minutes=1)).isoformat(),
                    "session_id": "9" * 32,
                    "data": {
                        "item_id": "refine",
                        "outcome": "completed",
                        "dwell_ms": 6000,
                        "phrase_confirmed": True,
                        "replays": 0,
                    },
                },
            ],
        )
        evidence = rebuilt["items"]["refine"]["evidence"]
        self.assertEqual([row["policy_version"] for row in evidence], ["adaptive-v1", "adaptive-v1"])
        self.assertEqual(rebuilt["policy_version"], "adaptive-v5")

    def test_probe_event_replay_is_idempotent(self):
        now = utc_now()
        teach_event = {
            "event_id": "teach-probe-target",
            "type": "interaction_completed",
            "at": (now - timedelta(hours=24)).isoformat(),
            "session_id": "b" * 32,
            "data": {
                "item_id": "refine",
                "outcome": "completed",
                "dwell_ms": 6000,
                "phrase_confirmed": True,
                "replays": 0,
                "preference_epoch": 1,
            },
        }
        probe_event = {
            "event_id": "probe-result-one",
            "type": "probe_completed",
            "at": now.isoformat(),
            "session_id": "c" * 32,
            "data": {
                "probe_id": "probe-stable-id",
                "item_id": "refine",
                "variant_id": "refine:followup-sentence-v1",
                "outcome": "correct",
                "audio_confirmed": True,
                "choice_count": 3,
                "response_ms": 2200,
                "delay_hours": 24,
            },
        }
        same_probe_new_event = deepcopy(probe_event)
        same_probe_new_event["event_id"] = "probe-result-two"
        rebuilt = rebuild_profile_from_events(
            CONTENT,
            [teach_event, probe_event, deepcopy(probe_event), same_probe_new_event],
        )
        state = rebuilt["items"]["refine"]
        self.assertEqual(state["teach_count"], 1)
        self.assertEqual(state["recognition_success_count"], 1)
        self.assertEqual(state["used_probe_variants"], ["refine:followup-sentence-v1"])
        self.assertEqual(state["aural_stage"], "taught")
        conflicting = deepcopy(probe_event)
        conflicting["event_id"] = "probe-result-conflict"
        conflicting["data"]["outcome"] = "incorrect"
        with self.assertRaisesRegex(ValueError, "conflicting_probe_result"):
            rebuild_profile_from_events(CONTENT, [teach_event, probe_event, conflicting])

    def test_rebuild_preserves_incomplete_active_session(self):
        now = utc_now()
        session_id = "a" * 32
        rebuilt = rebuild_profile_from_events(
            CONTENT,
            [
                {"event_id": "profile", "type": "profile_created", "at": now.isoformat(), "session_id": None, "data": {}},
                {
                    "event_id": "session",
                    "type": "session_created",
                    "at": now.isoformat(),
                    "session_id": session_id,
                    "data": {"preference_epoch": 1},
                },
            ],
            known_ids={"detailed"},
        )
        self.assertEqual(rebuilt["active_session_id"], session_id)
        self.assertEqual(rebuilt["active_sessions"], {"fixture-nasa-lro-v1": session_id})

    def test_future_profile_schema_is_rejected_not_guessed(self):
        profile = new_profile(CONTENT)
        profile["schema_version"] = 99
        with self.assertRaisesRegex(ValueError, "unsupported_profile_schema:99"):
            ensure_profile(profile, CONTENT)

    def test_explicit_choice_overrides_adaptive_reduction(self):
        profile = new_profile(CONTENT)
        profile["adaptive"]["reduction"] = -1
        changed = set_explicit_intensity(profile, "high")
        self.assertEqual(changed["adaptive"]["reduction"], 0)
        self.assertEqual(effective_intensity(changed), "high")


if __name__ == "__main__":
    unittest.main()
