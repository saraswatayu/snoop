#!/usr/bin/env python3
"""Pure-logic tests for snoop. No network. Run: python3 -m unittest -v

Covers the silent-bug surface: the confidence score weighting and the
pattern inference / ranking. SMTP/DNS paths are intentionally not tested
(personal tool -- live network probing is exercised by real use).
"""

import unittest

import verify_email as v


class TestScore(unittest.TestCase):
    def test_verified_is_high_and_clean(self):
        s, ev = v.score_confidence("verified", False, "other", 250)
        self.assertEqual(s, 0.90)
        self.assertIn("RCPT accepted", ev)

    def test_invalid_is_near_zero(self):
        s, _ = v.score_confidence("invalid", False, "other", 550)
        self.assertEqual(s, 0.02)

    def test_no_mx_and_bad_syntax_are_zero(self):
        self.assertEqual(v.score_confidence("no_mx", None, None, None)[0], 0.0)
        self.assertEqual(
            v.score_confidence("bad_syntax", None, None, None)[0], 0.0)

    def test_catch_all_is_mid_and_honest(self):
        s, ev = v.score_confidence("catch_all", True, "other", 250)
        self.assertEqual(s, 0.45)
        self.assertIn("catch-all", ev)

    def test_google_inconclusive_names_the_blocker(self):
        s, ev = v.score_confidence("inconclusive", False, "google", None)
        self.assertEqual(s, 0.35)
        self.assertIn("Google blocks", ev)

    def test_pattern_corroboration_raises_score_and_caps(self):
        base = v.score_confidence("inconclusive", False, "google", None)[0]
        one = v.score_confidence("inconclusive", False, "google", None, 1)[0]
        many = v.score_confidence("inconclusive", False, "google", None, 9)[0]
        self.assertGreater(one, base)
        self.assertAlmostEqual(many, round(0.35 + 0.12, 2))  # +0.04*n cap 0.12
        self.assertIn("matches company pattern", v.score_confidence(
            "inconclusive", False, "google", None, 2)[1])

    def test_pattern_does_not_rescue_invalid(self):
        s, _ = v.score_confidence("invalid", False, "other", 550, 5)
        self.assertEqual(s, 0.02)

    def test_score_clamped_0_1(self):
        s, _ = v.score_confidence("verified", False, "other", 250, 99)
        self.assertLessEqual(s, 1.0)
        self.assertGreaterEqual(s, 0.0)


class TestPatterns(unittest.TestCase):
    def test_infer_flast_and_first_dot_last(self):
        self.assertEqual(v.infer_patterns("jdoe", "john", "doe"), ["flast"])
        self.assertIn("first.last",
                      v.infer_patterns("john.doe", "john", "doe"))

    def test_infer_handles_missing_name(self):
        self.assertEqual(v.infer_patterns("jdoe", "", ""), [])

    def test_predict_roundtrips_with_infer(self):
        lp = v.predict("first.last", "jane", "smith")
        self.assertEqual(lp, "jane.smith")
        self.assertIn("first.last", v.infer_patterns(lp, "jane", "smith"))

    def test_rank_promotes_consensus_pattern(self):
        order, match = v.rank_candidates(
            ["jane.smith@acme.com", "jsmith@acme.com", "janes@acme.com"],
            [("jdoe@acme.com", "John Doe"), ("mroe@acme.com", "Mary Roe")],
            "Jane Smith")
        self.assertEqual(order[0], "jsmith@acme.com")
        self.assertEqual(match["jsmith@acme.com"], 2)

    def test_rank_no_known_is_passthrough(self):
        emails = ["a@acme.com", "b@acme.com"]
        order, match = v.rank_candidates(emails, [], None)
        self.assertEqual(order, emails)
        self.assertEqual(match, {})

    def test_rank_ignores_other_domain_knowns(self):
        order, match = v.rank_candidates(
            ["jane.smith@acme.com", "jsmith@acme.com"],
            [("jdoe@other.com", "John Doe")],  # different domain
            "Jane Smith")
        self.assertEqual(match, {})
        self.assertEqual(order, ["jane.smith@acme.com", "jsmith@acme.com"])

    def test_rank_tolerates_garbage_known(self):
        order, match = v.rank_candidates(
            ["jane.smith@acme.com"],
            [("not-an-email", None), ("", "")],
            "Jane Smith")
        self.assertEqual(order, ["jane.smith@acme.com"])
        self.assertEqual(match, {})


if __name__ == "__main__":
    unittest.main()
