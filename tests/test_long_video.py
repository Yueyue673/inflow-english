from __future__ import annotations

import unittest

from long_video import accept_owned_candidates, plan_long_video_windows, prioritize_windows


class LongVideoWindowTests(unittest.TestCase):
    def test_semantic_windows_cover_once_and_analysis_regions_overlap(self):
        windows = plan_long_video_windows(
            2 * 60 * 60,
            chapter_starts=[370, 730, 1090, 1450, 1810, 2170, 2530, 2890, 3250, 3610, 3970, 4330, 4690, 5050, 5410, 5770, 6130, 6490, 6850],
            pauses=[{"start_sec": 358, "end_sec": 360}, {"start_sec": 718, "end_sec": 720}],
            caption_ends=[360, 720, 1080],
            require_long=True,
        )
        self.assertGreater(len(windows), 15)
        self.assertEqual(windows[0]["ownership_start_sec"], 0)
        self.assertEqual(windows[-1]["ownership_end_sec"], 7200)
        for previous, current in zip(windows, windows[1:]):
            self.assertEqual(previous["ownership_end_sec"], current["ownership_start_sec"])
            self.assertLess(current["analysis_start_sec"], current["ownership_start_sec"])
            self.assertGreater(previous["analysis_end_sec"], previous["ownership_end_sec"])
        self.assertIn(windows[0]["boundary_kind"], {"chapter", "silence", "caption_end"})

    def test_semantic_strength_is_bounded_by_distance(self):
        windows = plan_long_video_windows(
            2000,
            chapter_starts=[389],
            pauses=[{"start_sec": 359, "end_sec": 361}],
            caption_ends=[360],
        )
        self.assertEqual(windows[0]["ownership_end_sec"], 360)
        self.assertEqual(windows[0]["boundary_kind"], "silence")
        self.assertEqual(windows[0]["seam_state"], "closed")

    def test_caption_end_is_tentative_not_claimed_semantically_closed(self):
        windows = plan_long_video_windows(1800, caption_ends=[358, 719, 1081, 1440], require_long=True)
        self.assertEqual(windows[0]["ownership_end_sec"], 358)
        self.assertEqual(windows[0]["boundary_kind"], "caption_end")
        self.assertEqual(windows[0]["seam_state"], "tentative")

    def test_fallback_is_open_and_bounded(self):
        windows = plan_long_video_windows(2400, require_long=True)
        self.assertEqual(windows[0]["ownership_end_sec"], 360)
        self.assertEqual(windows[0]["boundary_kind"], "time_fallback")
        self.assertEqual(windows[0]["seam_state"], "open")
        self.assertTrue(all(window["ownership_end_sec"] - window["ownership_start_sec"] <= 390 for window in windows))

    def test_seek_priority_starts_at_playhead_then_moves_forward(self):
        windows = plan_long_video_windows(2400, require_long=True)
        order = prioritize_windows(windows, 1500)
        containing = next(window["window_id"] for window in windows if window["ownership_start_sec"] <= 1500 < window["ownership_end_sec"])
        self.assertEqual(order[0], containing)
        starts = {window["window_id"]: window["ownership_start_sec"] for window in windows}
        future = [window_id for window_id in order[1:] if starts[window_id] >= 1500]
        past = [window_id for window_id in order[1:] if starts[window_id] < 1500]
        self.assertTrue(future)
        self.assertLess(order.index(future[0]), order.index(past[0]))

    def test_overlap_candidate_belongs_to_only_one_ownership_window(self):
        windows = plan_long_video_windows(1800, require_long=True)
        boundary = windows[0]["ownership_end_sec"]
        candidates = [
            {"occurrence_id": "a-1", "knowledge_key": "a", "surface": "alpha", "anchor_sec": boundary - 1},
            {"occurrence_id": "b-1", "knowledge_key": "b", "surface": "beta", "anchor_sec": boundary + 1},
            {"occurrence_id": "a-1", "knowledge_key": "a", "surface": "alpha", "anchor_sec": boundary - 0.96},
        ]
        first = accept_owned_candidates(windows[0], candidates)
        second = accept_owned_candidates(windows[1], candidates)
        self.assertEqual([row["surface"] for row in first], ["alpha"])
        self.assertEqual([row["surface"] for row in second], ["beta"])

    def test_long_mode_accepts_exact_bounds_only(self):
        self.assertTrue(plan_long_video_windows(1800, require_long=True))
        self.assertTrue(plan_long_video_windows(3 * 60 * 60, require_long=True))
        with self.assertRaisesRegex(ValueError, "duration_out_of_long_video_range"):
            plan_long_video_windows(1799.9, require_long=True)
        with self.assertRaisesRegex(ValueError, "duration_out_of_long_video_range"):
            plan_long_video_windows(3 * 60 * 60 + 0.1, require_long=True)


if __name__ == "__main__":
    unittest.main()
