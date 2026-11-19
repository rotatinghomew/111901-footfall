"""区间与读数归约的单元测试。"""

import _bootstrap  # noqa: F401
import unittest

from footfall.data import Policy
from footfall.evidence import State, build_evidence
from footfall.interval import Span, outward_round, sum_all

from support import make_dataset


class SpanTest(unittest.TestCase):
    def test_outward_rounding_keeps_integer_feasible_values(self):
        self.assertEqual(outward_round(4.0, lower=False), 4)
        self.assertEqual(outward_round(4.0000000001, lower=False), 4)
        self.assertEqual(outward_round(3.9999999999, lower=True), 4)
        self.assertEqual(outward_round(4.2, lower=True), 5)
        self.assertEqual(outward_round(4.8, lower=False), 4)

    def test_interval_operations_do_not_use_midpoint(self):
        span = Span(3, 9)
        self.assertEqual(span.uncertainty, 6)
        self.assertFalse(hasattr(span, "midpoint"))
        self.assertEqual(str(span), "[3, 9]")

    def test_sum_all_propagates_unknown(self):
        self.assertEqual(sum_all([Span(1, 2), Span(3, 4)]), Span(4, 6))
        self.assertEqual(sum_all([Span(1, 2), Span(3, None)]), Span(4, None))


class CountdownCounterTest(unittest.TestCase):
    """倒计数：读数下降是正常，回绕需 reset 标记。"""

    def dataset(self, readings, **policy):
        return make_dataset(
            readings=readings,
            policy=Policy(counter_mode="countdown", counter_ceiling=60, **policy),
        )

    def test_normal_countdown_step_is_exact(self):
        ds = self.dataset(
            [
                ("d", "lobby", "w0", 50, 0, "in"),
                ("d", "lobby", "w1", 45, 0, "in"),
                ("d", "lobby", "w2", 40, 0, "in"),
            ]
        )
        evidence = build_evidence(ds)
        self.assertEqual(evidence.channel("lobby", "w1", "in").span, Span(5, 5))
        self.assertEqual(evidence.channel("lobby", "w2", "in").span, Span(5, 5))
        self.assertTrue(evidence.channel("lobby", "w2", "in").is_exact)

    def test_wrap_requires_reset_and_is_exact(self):
        ds = self.dataset(
            [
                ("d", "lobby", "w0", 5, 0, "in"),
                ("d", "lobby", "w1", 58, 1, "in"),
            ]
        )
        evidence = build_evidence(ds)
        channel = evidence.channel("lobby", "w1", "in")
        self.assertEqual(channel.span, Span(7, 7))  # 5 + (60-58)
        self.assertEqual(channel.states, (State.RESET,))

    def test_restart_proof_supplies_wrap_without_reset_flag(self):
        from footfall.evidence import RestartProof

        ds = self.dataset(
            [
                ("d", "lobby", "w0", 5, 0, "in"),
                ("d", "lobby", "w1", 58, 0, "in"),
            ]
        )
        without = build_evidence(ds).channel("lobby", "w1", "in")
        self.assertEqual(without.states, (State.ANOMALY,))
        with_proof = build_evidence(
            ds, restart_proofs=(RestartProof("d", "w1"),)
        ).channel("lobby", "w1", "in")
        self.assertEqual(with_proof.span, Span(7, 7))

    def test_unmarked_rise_is_not_guessed(self):
        ds = self.dataset(
            [
                ("d", "lobby", "w0", 50, 0, "in"),
                ("d", "lobby", "w1", 55, 0, "in"),
            ]
        )
        channel = build_evidence(ds).channel("lobby", "w1", "in")
        self.assertEqual(channel.states, (State.ANOMALY,))
        self.assertEqual(channel.span.lo, 0)  # 不静默修正成 55
        self.assertGreater(channel.span.hi, 0)


class CumulativeCounterTest(unittest.TestCase):
    def test_reset_does_not_become_zero(self):
        ds = make_dataset(
            readings=[
                ("d", "lobby", "w0", 10, 0, "in"),
                ("d", "lobby", "w1", 4, 1, "in"),
            ]
        )
        channel = build_evidence(ds).channel("lobby", "w1", "in")
        self.assertEqual(channel.span, Span(0, 4))
        self.assertEqual(channel.states, (State.RESET,))

    def test_missing_window_not_filled_with_zero(self):
        ds = make_dataset(
            readings=[
                ("d", "lobby", "w0", 10, 0, "in"),
                ("d", "lobby", "w2", 16, 0, "in"),
            ]
        )
        evidence = build_evidence(ds)
        gap = evidence.channel("lobby", "w2", "in")
        self.assertEqual(gap.states, (State.RANGE,))
        self.assertEqual(gap.span, Span(0, 6))
        missing = evidence.channel("lobby", "w1", "in")
        self.assertTrue(missing.is_missing)
        self.assertIsNone(missing.span.hi)

    def test_first_window_without_initial_value_is_unknown(self):
        ds = make_dataset(readings=[("d", "lobby", "w0", 10, 0, "in")])
        channel = build_evidence(ds).channel("lobby", "w0", "in")
        self.assertIsNone(channel.span.hi)

    def test_initial_counter_value_settles_first_window(self):
        ds = make_dataset(
            readings=[("d", "lobby", "w0", 10, 0, "in")],
            policy=Policy(initial_counter_value=0),
        )
        channel = build_evidence(ds).channel("lobby", "w0", "in")
        self.assertEqual(channel.span, Span(10, 10))


class DuplicateReportTest(unittest.TestCase):
    def test_duplicate_device_rows_are_not_summed(self):
        ds = make_dataset(
            readings=[
                ("d", "lobby", "w0", 10, 0, "in"),
                ("d", "lobby", "w1", 15, 0, "in"),
                ("d", "lobby", "w1", 15, 0, "in"),
            ]
        )
        evidence = build_evidence(ds)
        self.assertEqual(evidence.channel("lobby", "w1", "in").span, Span(5, 5))
        self.assertEqual(len(evidence.duplicates), 1)

    def test_conflicting_duplicates_keep_wide_envelope_and_warn(self):
        ds = make_dataset(
            readings=[
                ("d", "lobby", "w0", 10, 0, "in"),
                ("d", "lobby", "w1", 15, 0, "in"),
                ("d", "lobby", "w1", 18, 0, "in"),
            ]
        )
        evidence = build_evidence(ds)
        self.assertTrue(evidence.warnings)
        channel = evidence.channel("lobby", "w1", "in")
        # 只保留第一条；另外通过告警说明不一致，而不是把两条相加成 8。
        self.assertEqual(channel.span, Span(5, 5))

    def test_overlap_and_disjoint_aggregation(self):
        readings = [
            ("a", "lobby", "w0", 10, 0, "in"),
            ("b", "lobby", "w0", 10, 0, "in"),
            ("a", "lobby", "w1", 13, 0, "in"),
            ("b", "lobby", "w1", 16, 0, "in"),
        ]
        overlap = build_evidence(
            make_dataset(readings=readings, policy=Policy(evidence_aggregation="overlap"))
        ).channel("lobby", "w1", "in")
        disjoint = build_evidence(
            make_dataset(readings=readings, policy=Policy(evidence_aggregation="disjoint"))
        ).channel("lobby", "w1", "in")
        self.assertEqual(overlap.span, Span(3, 6))
        self.assertEqual(disjoint.span, Span(9, 9))


if __name__ == "__main__":
    unittest.main()
